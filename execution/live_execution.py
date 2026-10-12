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
    OPERATION_CANCEL,
    OPERATION_OPEN,
    IntentJournal,
    IntentJournalError,
    OrderIntent,
    recover_pending_cancels,
    recover_pending_intents,
)
from execution.order import Order, OrderSide, OrderType
from execution.order_manager import OrderManager, OrderStateError

from brokers.models import (
    BrokerConnectionError,
    BrokerOrder,
    BrokerOrderState,
    OrderNotFoundError,
    OrderRejectedError,
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

    Solo quien posee el token puede `approve*`/`consume*`. El adapter, que recibe
    el gate pero NO el token, unicamente puede `check*`. Esto impide que un
    caller con el adapter en la mano envie/cancele sin pasar por el coordinador.

    MT5-DEMO-EXECUTION-CONTROLS-02A: cada permiso queda ligado a la TUPLA
    `(kind, op_id, target)`:

      * `kind` distingue el TIPO de operacion (`OPEN` vs `CANCEL`): un permiso
        de apertura NUNCA autoriza una cancelacion (y viceversa);
      * `op_id` es la identidad de la operacion;
      * `target` es el objetivo exacto (`client_order_id` afectado).

    Los metodos historicos `approve`/`consume`/`check` son el caso `OPEN` con
    `op_id == target == client_order_id` (compatibilidad LIVE-09 intacta).
    """

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("ExecutionGate requiere un token no vacio")
        self.__token = token
        self._approved: set[tuple[str, str, str]] = set()

    def _require_token(self, token: str) -> None:
        if not isinstance(token, str) or token != self.__token:
            raise ExecutionNotAuthorizedError("token de autorizacion invalido")

    # -------------------------------------------------- permisos por operacion

    def approve_operation(
        self, kind: str, *, op_id: str, target: str, token: str
    ) -> None:
        self._require_token(token)
        self._approved.add((str(kind), str(op_id), str(target)))

    def consume_operation(
        self, kind: str, *, op_id: str, target: str, token: str
    ) -> None:
        self._require_token(token)
        self._approved.discard((str(kind), str(op_id), str(target)))

    def check_operation(self, kind: str, *, op_id: str, target: str) -> None:
        """Invocado por el adapter (submit/cancel). No requiere token."""
        if (str(kind), str(op_id), str(target)) not in self._approved:
            raise ExecutionNotAuthorizedError(
                f"operacion {kind!r} sin autorizacion explicita del coordinador: "
                f"op_id={op_id!r} target={target!r}"
            )

    # -------------------------------------------------- compatibilidad OPEN

    def approve(self, client_order_id: str, *, token: str) -> None:
        self.approve_operation(
            OPERATION_OPEN, op_id=client_order_id, target=client_order_id, token=token
        )

    def consume(self, client_order_id: str, *, token: str) -> None:
        self.consume_operation(
            OPERATION_OPEN, op_id=client_order_id, target=client_order_id, token=token
        )

    def check(self, client_order_id: str) -> None:
        """Invocado por el adapter en `submit_order`. No requiere token."""
        self.check_operation(
            OPERATION_OPEN, op_id=client_order_id, target=client_order_id
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
            # 02A: la identidad cid<->ticket es de una orden de TRADING (OPEN);
            # el id de una operacion de cancelacion NO es una orden del broker.
            if intent.operation != OPERATION_OPEN:
                continue
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
        """Resuelve (SOLO lookup) los intents de APERTURA pendientes. NUNCA reenvia."""
        return recover_pending_intents(self._journal, self._broker, clock=self._clock)

    def resolve_pending_cancels(self):
        """Resuelve (SOLO lookup) las cancelaciones pendientes. NUNCA repite.

        Mira el estado autoritativo del OBJETIVO; jamas reenvia ni cancela a
        ciegas. Un target `SUBMITTED`/`PARTIALLY_FILLED` deja la cancelacion sin
        resolver (no se repite automaticamente).
        """
        return recover_pending_cancels(self._journal, self._broker, clock=self._clock)

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

    # ---------------------------------------------- cancelacion (02A)

    def _order_by_client_id(self, client_order_id: str) -> Order | None:
        for order in self._order_manager.orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    def _adopt_target_cancelled(self, target_cid: str) -> None:
        """Refleja en el estado interno que el OBJETIVO quedo CANCELLED."""
        intent = self._journal.get(target_cid)
        if intent is not None and not intent.terminal:
            self._journal.mark_state(target_cid, "CANCELLED")
        order = self._order_by_client_id(target_cid)
        if order is not None and not order.status.terminal:
            try:
                self._order_manager.cancel(order.order_id)
            except OrderStateError:
                pass

    def _adopt_target_state(self, target_cid: str, broker_order) -> None:
        """Adopta el estado AUTORITATIVO del broker para el objetivo (journal)."""
        intent = self._journal.get(target_cid)
        state = getattr(getattr(broker_order, "state", None), "value", None)
        if intent is None or not state or intent.terminal:
            return
        try:
            self._journal.mark_state(target_cid, state)
        except IntentJournalError:
            pass

    def cancel_pending(
        self, *, client_order_id: str, operation_id: str | None = None
    ) -> ExecutionOutcome:
        """Cancelacion COORDINADA de una orden de APERTURA pendiente (02A).

        Alcance: SOLO ordenes originales de apertura identificadas de forma
        inequivoca. NO cierra/reduce posiciones ni cancela ordenes de reduccion
        (fuera de alcance). Una cancelacion REDUCE exposicion POTENCIAL, por lo
        que NO se bloquea por el veto de nuevas entradas; pero exige broker sano,
        evidencia ACTUAL del broker sobre la orden exacta, write-ahead del intent
        y autorizacion de capacidad `CANCEL`. Un resultado ambiguo NO se reenvia:
        queda `UNKNOWN` y se resuelve solo por lookup. La repeticion de una
        solicitud ya resuelta es idempotente (no vuelve a llamar al broker).
        """
        target_cid = str(client_order_id or "")
        if not target_cid:
            raise ExecutionBlockedError("identity", "client_order_id obligatorio")
        op_id = str(operation_id) if operation_id else f"CANCEL:{target_cid}"

        # 1) Idempotencia: resuelta => no-op; sin resolver => NO repetir a ciegas.
        existing = self._journal.get(op_id)
        if existing is not None:
            if existing.terminal:
                return ExecutionOutcome(
                    allowed=True,
                    stage="cancel_idempotent",
                    reason="cancelacion ya resuelta; sin segunda llamada al broker",
                    intent=existing,
                )
            raise ExecutionBlockedError(
                "cancel_pending",
                "cancelacion previa sin resolver: resolver por lookup (no repetir)",
            )

        # 2) Objetivo identificado inequivocamente: intent OPEN de apertura.
        target_intent = self._journal.get(target_cid)
        if target_intent is None:
            raise ExecutionBlockedError(
                "cancel_target", "objetivo sin intent de apertura identificable"
            )
        if target_intent.operation != OPERATION_OPEN:
            raise ExecutionBlockedError(
                "cancel_target", "el objetivo no es una apertura (fuera de alcance 02A)"
            )
        if target_intent.reduce_only:
            raise ExecutionBlockedError(
                "cancel_target", "orden de reduccion: fuera de alcance 02A"
            )

        # 3) Broker sano: necesario para obtener evidencia y para cancelar.
        self._assert_broker_health()

        # 4) Evidencia ACTUAL del broker sobre la orden EXACTA (aunque la
        #    reconciliacion este bloqueada: no se usa ninguna suposicion).
        try:
            current = self._broker.get_order(target_cid)
        except OrderNotFoundError:
            raise ExecutionBlockedError(
                "cancel_evidence", "el broker no conoce la orden objetivo"
            )
        except BrokerConnectionError:
            raise ExecutionBlockedError(
                "cancel_evidence", "estado del broker incierto"
            )
        if current is None:
            raise ExecutionBlockedError(
                "cancel_evidence", "el broker no conoce la orden objetivo"
            )
        if current.state is BrokerOrderState.UNKNOWN:
            raise ExecutionBlockedError(
                "cancel_evidence", "estado de la orden no determinable"
            )
        if current.state is BrokerOrderState.CANCELLED:
            self._adopt_target_cancelled(target_cid)
            return ExecutionOutcome(
                allowed=True,
                stage="cancel_already_done",
                reason="la orden ya estaba cancelada en el broker",
            )
        if current.state is BrokerOrderState.FILLED:
            self._adopt_target_state(target_cid, current)
            raise ExecutionBlockedError(
                "cancel_target_filled",
                "orden ya ejecutada: no se cancela; se preservan los fills",
            )
        if current.state in (BrokerOrderState.REJECTED, BrokerOrderState.EXPIRED):
            self._adopt_target_state(target_cid, current)
            raise ExecutionBlockedError(
                "cancel_target_terminal",
                f"orden en estado {current.state.value}: nada pendiente que cancelar",
            )
        # SUBMITTED / PARTIALLY_FILLED => cancelar SOLO el remanente pendiente.
        if float(current.remaining) <= 0.0:
            self._adopt_target_state(target_cid, current)
            raise ExecutionBlockedError(
                "cancel_target_filled", "sin remanente pendiente que cancelar"
            )

        # 5) Write-ahead del intent de cancelacion ANTES de tocar el broker.
        cancel_intent = OrderIntent.for_cancel(
            operation_id=op_id,
            target_client_order_id=target_cid,
            instrument=target_intent.instrument,
            side=target_intent.side,
            order_type=target_intent.order_type,
            quantity=target_intent.quantity,
            target_broker_order_id=(
                current.broker_order_id or target_intent.broker_order_id
            ),
            run_id=self._run_id,
        )
        try:
            intent = self._journal.begin(cancel_intent)
        except IntentJournalError as exc:
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # 6) Autorizacion de capacidad `CANCEL` + llamada al broker.
        self._gate.approve_operation(
            OPERATION_CANCEL, op_id=op_id, target=target_cid, token=self._token
        )
        try:
            broker_order = self._broker.cancel_order(
                target_cid, operation_id=op_id
            )
        except OrderRejectedError as exc:
            self._journal.mark_state(op_id, "REJECTED")
            raise ExecutionBlockedError("cancel_rejected", str(exc)) from exc
        except OrderNotFoundError as exc:
            # NOT_FOUND no prueba nada: permanece sin resolver (sin suposicion).
            raise ExecutionBlockedError(
                "cancel_not_found", "el broker no conoce entonces la orden"
            ) from exc
        except BrokerConnectionError as exc:
            self._journal.mark_state(op_id, "UNKNOWN")
            raise ExecutionBlockedError(
                "cancel_unknown", "resultado incierto: resolver por lookup"
            ) from exc
        finally:
            self._gate.consume_operation(
                OPERATION_CANCEL, op_id=op_id, target=target_cid, token=self._token
            )

        # 7) Reflexion del resultado (verdad del broker).
        state = getattr(broker_order, "state", None)
        if state is BrokerOrderState.CANCELLED:
            self._journal.mark_state(
                op_id, "CANCELLED",
                broker_order_id=getattr(broker_order, "broker_order_id", None),
            )
            self._adopt_target_cancelled(target_cid)
            return ExecutionOutcome(
                allowed=True,
                stage="cancel_confirmed",
                reason="cancelacion confirmada; remanente pendiente cancelado",
                intent=intent,
            )
        if state is BrokerOrderState.FILLED:
            self._journal.mark_state(
                op_id, "REJECTED",
                broker_order_id=getattr(broker_order, "broker_order_id", None),
            )
            self._adopt_target_state(target_cid, broker_order)
            raise ExecutionBlockedError(
                "cancel_target_filled",
                "la orden se ejecuto: no cancelada; se preservan los fills",
            )
        # Aun SUBMITTED/PARTIALLY_FILLED tras cancelar: sin resolver, sin reenvio.
        self._journal.mark_state(op_id, "UNKNOWN")
        raise ExecutionBlockedError(
            "cancel_unknown",
            f"estado tras cancelar {getattr(state, 'value', state)}: sin resolver",
        )


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
