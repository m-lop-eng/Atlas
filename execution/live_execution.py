"""LiveExecutionCoordinator (MT5-DEMO-EXECUTION-FOUNDATION-01).

UNICA ruta segura de ejecucion hacia `MT5BrokerAdapter.submit_order`. Compone,
en orden y de forma fail-closed, los controles que la auditoria demostro que hoy
NO viven en el adapter:

    sesion/instrumento -> kill switch -> salud broker -> reconciliacion
    -> LIVE-03 freshness -> divisas/semantica de cuenta -> riesgo global +
    exposicion proyectada + limite de posiciones -> OrderManager (identidad)
    -> IntentJournal write-ahead (OBLIGATORIO) -> autorizacion de capacidad
    (ExecutionGate) -> MT5BrokerAdapter.submit_order -> resolucion/registro.

Garantias clave (no cambian LIVE-02..LIVE-09; son capas ADITIVAS):

  * El adapter MT5 solo acepta un submit si el `ExecutionGate` que le fue
    enlazado conoce (y autoriza) ese `client_order_id`. El token del gate lo
    posee EXCLUSIVAMENTE el coordinador: un caller con el adapter (pero sin el
    token) NO puede enviar -> `ExecutionNotAuthorizedError`.
  * El `IntentJournal` es OBLIGATORIO y debe estar inicializado de forma
    duradera; el intent se persiste ANTES del submit. Un fallo de persistencia
    NO envia (fail-closed).
  * Un intent previo sin resolver bloquea nuevos envios.
  * No se reconstruye `client_order_id` desde comentarios MT5 truncados: la
    identidad recuperada proviene del journal y se PRIMEA en el adapter
    (`remember_identity`), nunca del texto del comentario.
  * No hay reenvio automatico ante UNKNOWN/timeout/desconexion/NOT_FOUND.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from execution.instrument import InstrumentSpec
from execution.intent_journal import (
    IntentJournal,
    IntentJournalError,
    OrderIntent,
    recover_pending_intents,
)
from execution.order import Order, OrderSide, OrderType
from execution.order_manager import OrderManager, OrderStateError

from brokers.models import (
    BrokerOrder,
    BrokerOrderState,
    OrderSubmission,
    SubmitStatus,
)
from reconciliation.engine import ReconciliationEngine
from reconciliation.models import InternalSnapshot, ReconciliationReport
from reconciliation.orchestrator import reconcile_with_broker

_UTC = timezone.utc

# Semantica de cuenta MT5 (mismos valores oficiales que `brokers.mt5`).
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


class LiveExecutionError(Exception):
    """Error base de la capa de ejecucion coordinada."""


class ExecutionNotAuthorizedError(LiveExecutionError):
    """Submit rechazado por el gate de capacidad (sin autorizacion del coordinador)."""


class ExecutionBlockedError(LiveExecutionError):
    """Un control obligatorio bloqueo el envio (fail-closed).

    `stage` identifica el control; `reason` el motivo (sin secretos).
    """

    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f"{stage}: {reason}")


class UnresolvedIntentsError(ExecutionBlockedError):
    """Existe al menos un intent previo sin resolucion terminal."""

    def __init__(self, count: int) -> None:
        super().__init__(
            "unresolved_intents",
            f"{count} intent(s) previo(s) sin resolver: no se envia una orden nueva",
        )


class UnsupportedAccountModeError(ExecutionBlockedError):
    """Semantica de cuenta no verificada o sin procedimiento seguro de cierre."""

    def __init__(self, detail: str) -> None:
        super().__init__("account_mode", detail)


@dataclass(frozen=True, slots=True)
class MarketDataReference:
    """Referencia minima de market data para LIVE-03 (ultima vela cerrada)."""

    instrument: str
    interval_seconds: int
    open_time: int
    close_time: int


def check_market_data_reference(
    ref: MarketDataReference | None,
    *,
    instrument: str,
    interval_seconds: int | None,
    now: datetime,
    max_age_s: float,
) -> str | None:
    """Validez estructural/temporal + freshness LIVE-03. `None` si es valida.

    Espeja la semantica de LIVE-03 (identidad instrumento/intervalo, timestamp
    alineado, no futuro, freshness vs `max_age_s`) sin fabricar cierres.
    """
    if ref is None:
        return "no_market_data"
    if ref.instrument != instrument:
        return "wrong_instrument"
    if interval_seconds is not None and ref.interval_seconds != interval_seconds:
        return "wrong_interval"
    if (
        ref.interval_seconds <= 0
        or ref.open_time <= 0
        or ref.open_time % ref.interval_seconds != 0
    ):
        return "invalid_timestamp"
    if ref.close_time > int(now.timestamp()):
        return "future_timestamp"
    age = now.timestamp() - ref.close_time
    if age > max_age_s:
        return "stale_market_data"
    return None


class ExecutionGate:
    """Gate de capacidad que enlaza el adapter con el coordinador.

    Solo quien posee el token puede `approve`/`consume`. El adapter, que recibe
    el gate pero NO el token, unicamente puede `check`. Esto impide que un
    caller con el adapter en la mano envie una orden sin pasar por el
    coordinador (que es el unico que aprueba `client_order_id`).
    """

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("ExecutionGate requiere un token no vacio")
        self.__token = token
        self._approved: set[str] = set()

    def _require_token(self, token: str) -> None:
        if not isinstance(token, str) or token != self.__token:
            raise ExecutionNotAuthorizedError("token de autorizacion invalido")

    def approve(self, client_order_id: str, *, token: str) -> None:
        self._require_token(token)
        self._approved.add(client_order_id)

    def consume(self, client_order_id: str, *, token: str) -> None:
        self._require_token(token)
        self._approved.discard(client_order_id)

    def check(self, client_order_id: str) -> None:
        """Invocado por el adapter en `submit_order`. No requiere token."""
        if client_order_id not in self._approved:
            raise ExecutionNotAuthorizedError(
                f"submit sin autorizacion explicita del coordinador: "
                f"{client_order_id!r}"
            )

    @property
    def approved_count(self) -> int:
        return len(self._approved)


@dataclass(slots=True)
class ExecutionOutcome:
    """Resultado de un intento de envio coordinado."""

    allowed: bool
    stage: str
    reason: str
    order: Order | None = None
    submission: OrderSubmission | None = None
    intent: OrderIntent | None = None


class LiveExecutionCoordinator:
    """Coordinador de ejecucion con todos los gates obligatorios.

    Args:
        broker: adapter gate-bindable (`bind_execution_gate`). Se le enlaza el
            gate; NO se puede enviar sin la autorizacion del coordinador.
        order_manager: autoridad de ciclo de vida de ordenes.
        journal: `IntentJournal` YA inicializado (obligatorio). `None` => error.
        sessions: `MarketSessionManager` (guard de sesion).
        kill_switch: `KillSwitchCoordinator` (guard de kill switch).
        instrument: `atlas_instrument_id` unico para esta fase.
        instrument_spec: economics del instrumento (divisa de cotizacion).
        account_currency: divisa de la cuenta (verificada).
        max_open_positions: limite global de posiciones (riesgo).
        risk_state_provider: `Callable[[], AccountRiskState]` (estado global).
        market_data_provider: `Callable[[], MarketDataReference | None]`.
        data_max_age_s: freshness maxima (LIVE-03) en segundos.
        account_mode_provider: `Callable[[], int | None]` (margin_mode MT5).
        projected_exposure_gate: `Callable[[Order], None]` opcional (F-03); si
            se provee, se ejecuta pre-submit y puede bloquear levantando.
        reconciliation: motor de reconciliacion (por defecto uno nuevo).
        clock: reloj UTC-aware inyectable.
        run_id: trazabilidad del intent.
    """

    def __init__(
        self,
        *,
        broker: Any,
        order_manager: OrderManager,
        journal: IntentJournal,
        sessions: Any,
        kill_switch: Any,
        instrument: str,
        instrument_spec: InstrumentSpec,
        account_id: str,
        account_currency: str,
        max_open_positions: int,
        risk_state_provider: Callable[[], Any],
        market_data_provider: Callable[[], MarketDataReference | None],
        data_max_age_s: float,
        account_mode_provider: Callable[[], int | None],
        projected_exposure_gate: Callable[[Order], None] | None = None,
        reconciliation: ReconciliationEngine | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(_UTC),
        run_id: str = "LIVE-EXEC",
    ) -> None:
        if not isinstance(journal, IntentJournal):
            raise TypeError(
                "LiveExecutionCoordinator exige un IntentJournal (obligatorio); "
                "None o un objeto distinto NO es aceptable (fail-closed)"
            )
        if not hasattr(broker, "bind_execution_gate"):
            raise TypeError(
                "broker no admite gate de ejecucion (bind_execution_gate): "
                "no es un adapter de ejecucion habilitable"
            )
        if not isinstance(instrument_spec, InstrumentSpec):
            raise TypeError("instrument_spec debe ser InstrumentSpec")
        if instrument_spec.atlas_instrument_id != instrument:
            raise ValueError(
                f"instrument_spec id {instrument_spec.atlas_instrument_id!r} != "
                f"instrument {instrument!r}"
            )
        if not account_currency or not str(account_currency).strip():
            raise ValueError("account_currency obligatoria")
        if not account_id or not str(account_id).strip():
            raise ValueError("account_id obligatorio")

        self._broker = broker
        self._order_manager = order_manager
        self._journal = journal
        self._sessions = sessions
        self._kill_switch = kill_switch
        self._instrument = instrument
        self._instrument_spec = instrument_spec
        self._account_id = str(account_id)
        self._account_currency = account_currency
        self._max_open_positions = int(max_open_positions)
        self._risk_state_provider = risk_state_provider
        self._market_data_provider = market_data_provider
        self._data_max_age_s = float(data_max_age_s)
        self._account_mode_provider = account_mode_provider
        self._projected_gate = projected_exposure_gate
        self._clock = clock
        self._run_id = run_id

        self._recon_engine = reconciliation or ReconciliationEngine()
        self._reconciliation_blocked = False

        self._token = secrets.token_hex(32)
        self._gate = ExecutionGate(self._token)
        broker.bind_execution_gate(self._gate)

    # --------------------------------------------------------------- consulta
    @property
    def journal(self) -> IntentJournal:
        return self._journal

    @property
    def reconciliation_blocked(self) -> bool:
        return self._reconciliation_blocked

    # --------------------------------------------------------------- recovery

    def prime_identity_from_journal(self) -> int:
        """Reconstruye la correlacion cid<->ticket desde el journal (no del comentario).

        Devuelve cuantos intents con `broker_order_id` se primearon. NO inventa
        tickets ni reconstruye `client_order_id` desde comentarios truncados.
        """
        primed = 0
        for intent in self._journal.all():
            broker_id = intent.broker_order_id
            if not broker_id:
                continue
            try:
                ticket = int(broker_id)
            except (TypeError, ValueError):
                continue
            self._broker.remember_identity(intent.client_order_id, ticket)
            primed += 1
        return primed

    def resolve_pending(self):
        """Resuelve (SOLO lookup) los intents pendientes. NUNCA reenvia."""
        return recover_pending_intents(self._journal, self._broker, clock=self._clock)

    # ---------------------------------------------------------- reconciliacion

    def reconcile(self, internal: InternalSnapshot) -> ReconciliationReport:
        report = reconcile_with_broker(self._recon_engine, internal, self._broker)
        self.update_reconciliation(report.status)
        return report

    def update_reconciliation(self, status) -> None:
        self._reconciliation_blocked = (
            status is not None
            and str(getattr(status, "value", status)) != "RECONCILIATION_OK"
        )

    # ------------------------------------------------------------------ gates

    def _assert_no_unresolved(self) -> None:
        pending = self._journal.unresolved()
        if pending:
            raise UnresolvedIntentsError(len(pending))

    def _assert_session(self, now: datetime, *, is_entry: bool) -> None:
        decision = self._sessions.get_session(
            self._instrument, int(now.timestamp())
        )
        allowed = (
            decision.can_open_positions if is_entry else decision.can_hold_positions
        )
        if not allowed:
            raise ExecutionBlockedError(
                "session", f"estado {decision.state.value}: no operable ({decision.reason})"
            )

    def _assert_kill_switch(self) -> None:
        status = self._kill_switch.status()
        if not status.can_trade:
            raise ExecutionBlockedError(
                "kill_switch", f"estado {status.state.value}: can_trade=False"
            )

    def _assert_broker_health(self) -> None:
        if not bool(self._broker.health()):
            raise ExecutionBlockedError("broker_health", "broker no READY")

    def _assert_reconciliation(self) -> None:
        if self._reconciliation_blocked:
            raise ExecutionBlockedError(
                "reconciliation", "reconciliacion bloqueada/indeterminada"
            )

    def _assert_market_data(self, now: datetime) -> None:
        ref = self._market_data_provider()
        reason = check_market_data_reference(
            ref,
            instrument=self._instrument,
            interval_seconds=getattr(ref, "interval_seconds", None),
            now=now,
            max_age_s=self._data_max_age_s,
        )
        if reason is not None:
            raise ExecutionBlockedError("market_data", reason)

    def _assert_currency(self) -> None:
        quote = str(self._instrument_spec.quote_currency)
        if quote != self._account_currency:
            raise ExecutionBlockedError(
                "currency",
                f"quote {quote!r} != cuenta {self._account_currency!r} "
                "(sin FX: no representable de forma inequivoca)",
            )

    def _assert_account_mode(self, *, is_entry: bool) -> None:
        mode = self._account_mode_provider()
        if mode is None:
            raise UnsupportedAccountModeError(
                "semantica de cuenta NO verificada (margin_mode ausente)"
            )
        if mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise UnsupportedAccountModeError(
                "cuenta HEDGING sin procedimiento seguro de cierre: "
                "ninguna apertura permitida"
            )
        if mode != ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
            raise UnsupportedAccountModeError(
                f"margin_mode no soportado/verificado: {mode}"
            )

    def _assert_position_limit(self) -> None:
        state = self._risk_state_provider()
        open_positions = int(getattr(state, "open_positions", 0))
        if open_positions >= self._max_open_positions:
            raise ExecutionBlockedError(
                "position_limit",
                f"open_positions {open_positions} >= max {self._max_open_positions}",
            )

    # --------------------------------------------------------------- submit

    def submit(
        self,
        *,
        side: OrderSide,
        quantity: float,
        strategy_id: str,
        strategy_version: str,
        client_order_id: str,
        order_type: OrderType = OrderType.MARKET,
        is_entry: bool = True,
        reduce_only: bool = False,
        stop_price: float | None = None,
        limit_price: float | None = None,
        signal_id: str | None = None,
    ) -> ExecutionOutcome:
        """Ejecuta la ruta segura completa. Levanta `ExecutionBlockedError` en veto."""
        if not client_order_id or not str(client_order_id).strip():
            raise ExecutionBlockedError("identity", "client_order_id obligatorio")

        now = self._clock()
        # 1-8: gates obligatorios, en orden, TODOS antes de tocar el broker.
        self._assert_no_unresolved()
        self._assert_session(now, is_entry=is_entry)
        self._assert_kill_switch()
        self._assert_broker_health()
        self._assert_reconciliation()
        self._assert_market_data(now)
        self._assert_currency()
        self._assert_account_mode(is_entry=is_entry)
        self._assert_position_limit()

        # OrderManager: identidad estable + idempotencia.
        try:
            order = self._order_manager.create_order(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                account_id=self._account_id,
                instrument=self._instrument,
                side=side,
                quantity=quantity,
                order_type=order_type,
                client_order_id=client_order_id,
                signal_id=signal_id or client_order_id,
                stop_price=stop_price,
                limit_price=limit_price,
                reduce_only=reduce_only,
            )
        except OrderStateError as exc:
            raise ExecutionBlockedError("order_manager", str(exc)) from exc

        # Exposicion proyectada (F-03), justo antes de persistir/enviar.
        if self._projected_gate is not None:
            try:
                self._projected_gate(order)
            except ExecutionBlockedError:
                raise
            except Exception as exc:  # noqa: BLE001 - gate ajeno => fail-closed
                raise ExecutionBlockedError(
                    "projected_exposure", f"{type(exc).__name__}: {exc}"
                ) from exc

        # Intent journal WRITE-AHEAD (obligatorio). Fallo => NO submit.
        try:
            intent = self._journal.begin(
                OrderIntent.from_order(order, run_id=self._run_id)
            )
        except IntentJournalError as exc:
            try:
                self._order_manager.reject(
                    order.order_id, f"intent persist failed: {exc}"
                )
            except OrderStateError:
                pass
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # Autorizacion de capacidad + envio.
        self._order_manager.approve(order.order_id)
        self._order_manager.submit(order.order_id)
        self._gate.approve(client_order_id, token=self._token)
        try:
            submission = self._broker.submit_order(order)
        finally:
            self._gate.consume(client_order_id, token=self._token)

        self._reflect_submission(order, intent, submission)
        return ExecutionOutcome(
            allowed=True,
            stage="submitted",
            reason="submitted",
            order=order,
            submission=submission,
            intent=intent,
        )

    def _reflect_submission(
        self, order: Order, intent: OrderIntent, submission: OrderSubmission
    ) -> None:
        cid = order.client_order_id
        if submission.status is SubmitStatus.CONFIRMED and submission.broker_order is not None:
            self._record_broker_identity(cid, submission.broker_order)
            self._journal.mark_state(
                cid,
                submission.broker_order.state.value,
                broker_order_id=submission.broker_order.broker_order_id,
            )
            return
        if submission.status is SubmitStatus.REJECTED:
            try:
                self._order_manager.reject(order.order_id, submission.detail or "rechazada")
            except OrderStateError:
                pass
            self._journal.mark_state(cid, "REJECTED")
            return
        # UNKNOWN: transitorio; NO reenvio. Resolucion por lookup posterior.
        try:
            self._order_manager.mark_unknown(order.order_id)
        except OrderStateError:
            pass
        self._journal.mark_state(cid, "UNKNOWN")

    def _record_broker_identity(self, cid: str, broker_order: BrokerOrder) -> None:
        broker_id = getattr(broker_order, "broker_order_id", None)
        if not broker_id:
            return
        try:
            self._broker.remember_identity(cid, int(broker_id))
        except (TypeError, ValueError):
            return


__all__ = [
    "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING",
    "ACCOUNT_MARGIN_MODE_RETAIL_NETTING",
    "ExecutionBlockedError",
    "ExecutionGate",
    "ExecutionNotAuthorizedError",
    "ExecutionOutcome",
    "LiveExecutionCoordinator",
    "LiveExecutionError",
    "MarketDataReference",
    "UnresolvedIntentsError",
    "UnsupportedAccountModeError",
    "check_market_data_reference",
]
