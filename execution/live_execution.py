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
    OPERATION_CLOSE,
    OPERATION_OPEN,
    OPERATION_REDUCE,
    IntentJournal,
    IntentJournalError,
    OrderIntent,
    SUBMIT_PROTOCOL_VERSION,
    recover_pending_cancels,
    recover_pending_intents,
)
from execution.order import (
    PURPOSE_CLOSE,
    PURPOSE_OPEN,
    PURPOSE_REDUCE,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    validate_quantity_lattice,
)
from execution.order_manager import OrderManager, OrderStateError

from brokers.models import (
    BrokerConnectionError,
    BrokerError,
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
        allow_restricted_hedging: bool = False,
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
        # ATLAS-MT5-HEDGING-01: HEDGING SOLO con opt-in explicito restringido
        # (una posicion propiedad de Atlas por simbolo). Por defecto False.
        self._allow_restricted_hedging = bool(allow_restricted_hedging)

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
            # 02A/02B: el id de una CANCELACION no es una orden del broker;
            # OPEN/CLOSE/REDUCE SI son ordenes de broker con cid<->ticket propio.
            if intent.operation == OPERATION_CANCEL:
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

    # --------------------------------------------------------- fallos ORDER-07B

    def _reject_order(self, order: Order, reason: str) -> None:
        """Rechazo terminal de una orden que aun no tiene intent persistido."""
        try:
            self._order_manager.reject(order.order_id, reason)
        except OrderStateError:
            pass

    def _mark_pre_send_rejected(self, order: Order, reason: str) -> None:
        try:
            self._order_manager.reject(order.order_id, reason)
        except OrderStateError:
            pass
        try:
            self._journal.mark_state(order.client_order_id, "REJECTED")
        except IntentJournalError:
            pass

    def _mark_pre_send_failed(self, order: Order, reason: str) -> None:
        try:
            self._order_manager.mark_failed(order.order_id, reason)
        except OrderStateError:
            pass
        try:
            self._journal.mark_state(order.client_order_id, "FAILED")
        except IntentJournalError:
            pass

    def _mark_ambiguous(self, order: Order, reason: str) -> None:
        try:
            self._order_manager.mark_unknown(order.order_id)
        except OrderStateError:
            pass
        try:
            self._journal.mark_state(order.client_order_id, "UNKNOWN")
        except IntentJournalError:
            pass

    def _sync_order_from_broker(self, order: Order, broker_order) -> None:
        """Refleja el estado agregado del broker en OrderManager (best-effort).

        El journal ya es autoridad persistente; este sync evita que OrderManager
        quede atrasado. No inventa fills: usa cantidades/precios observados.
        """
        if broker_order is None:
            return
        try:
            raw_state = getattr(broker_order, "state", None)
            value = getattr(raw_state, "value", raw_state)
            state = OrderStatus(str(value))
        except (TypeError, ValueError):
            return
        fills = tuple(getattr(broker_order, "fills", ()) or ())
        commission = None
        if fills:
            try:
                commission = sum(
                    float(getattr(f, "commission", 0.0) or 0.0)
                    for f in fills
                )
            except (TypeError, ValueError):
                commission = None
        try:
            self._order_manager.sync_broker_state(
                order.order_id,
                state=state,
                filled_quantity=getattr(broker_order, "filled_quantity", None),
                average_fill_price=getattr(broker_order, "avg_fill_price", None),
                commission=commission,
            )
        except (KeyError, OrderStateError, TypeError, ValueError):
            pass

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
        if mode == ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
            return
        if mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            # ATLAS-MT5-HEDGING-01: HEDGING admitido SOLO bajo politica explicita
            # restringida (opt-in). NO se acepta por el simple margin_mode; el
            # cierre/reduccion dirigido y la verificacion de propiedad se aplican
            # en `_observe_target_position` y en el adapter.
            if self._allow_restricted_hedging:
                return
            raise UnsupportedAccountModeError(
                "cuenta HEDGING sin opt-in restringido "
                "(allow_restricted_hedging=False): ninguna operacion permitida"
            )
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

    # --------------------------------------------- gates de reduccion (02B)

    def _assert_session_reduce(self, now: datetime) -> None:
        """Sesion para CLOSE/REDUCE: bloquea cerrada/desconocida/HALTED."""
        decision = self._sessions.get_session(
            self._instrument, int(now.timestamp())
        )
        state = decision.state.value
        if state in ("CLOSED", "UNKNOWN", "HALTED"):
            raise ExecutionBlockedError(
                "session", f"estado {state}: no se opera automaticamente"
            )
        if not decision.can_hold_positions:
            raise ExecutionBlockedError(
                "session", f"estado {state}: no permite gestionar posiciones"
            )

    def _assert_kill_switch_reduce(self) -> None:
        """Kill switch para CLOSE/REDUCE: ALLOW/BLOCK_NEW_ORDERS permiten una
        reduccion ESTRICTA; HALT/EMERGENCY la bloquean (sin bypass implicito)."""
        status = self._kill_switch.status()
        state = str(getattr(getattr(status, "state", None), "value", status.state))
        if state in ("HALT", "EMERGENCY"):
            raise ExecutionBlockedError(
                "kill_switch",
                f"estado {state}: reduccion automatica prohibida",
            )

    def _observe_net_position(self):
        """Posicion neta FIABLE del broker para el instrumento (fail-closed)."""
        try:
            positions = list(self._broker.get_positions())
        except BrokerConnectionError as exc:
            raise ExecutionBlockedError(
                "position_evidence", "posiciones del broker no determinables"
            ) from exc
        matching = [p for p in positions if p.instrument == self._instrument]
        if not matching:
            raise ExecutionBlockedError(
                "position_evidence", "sin posicion abierta del instrumento"
            )
        if len(matching) > 1:
            raise ExecutionBlockedError(
                "position_evidence", "identidad de posicion ambigua (varias posiciones)"
            )
        pos = matching[0]
        if abs(float(pos.quantity)) <= 1e-12:
            raise ExecutionBlockedError("position_evidence", "posicion neta nula")
        return pos

    # ------------------------------------ HEDGING restringido (MT5-HEDGING-01)

    def _broker_positions_for_instrument(self) -> list:
        try:
            positions = list(self._broker.get_positions())
        except BrokerError as exc:
            # El adaptador falla cerrado (p.ej. HEDGING con multiples posiciones
            # por simbolo) => se propaga como bloqueo del coordinador.
            raise ExecutionBlockedError(
                "position_evidence",
                f"posiciones del broker no determinables ({type(exc).__name__})",
            ) from exc
        return [p for p in positions if p.instrument == self._instrument]

    def _position_is_owned(self, position_id) -> bool:
        """True si el POSITION_IDENTIFIER esta vinculado a un intent de Atlas."""
        if position_id is None:
            return False
        return bool(
            self._journal.find_by_position_id(str(position_id))
        )

    def _assert_no_open_position_for_entry(self) -> None:
        """Politica restringida: una sola posicion por simbolo.

        Con HEDGING restringido, una APERTURA exige que NO exista ya ninguna
        posicion del instrumento (propia o externa) => sin LONG+SHORT
        simultaneos ni multiples posiciones.
        """
        if not self._allow_restricted_hedging:
            return
        existing = self._broker_positions_for_instrument()
        if existing:
            raise ExecutionBlockedError(
                "position_evidence",
                "ya existe una posicion para el instrumento (politica HEDGING "
                "restringida: una sola posicion por simbolo)",
            )

    def _observe_target_position(self, target_position_id: str | None):
        """Posicion objetivo para CLOSE/REDUCE.

        NETTING: la posicion neta unica del instrumento (sin target explicito).
        HEDGING restringido: exige `target_position_id` (POSITION_IDENTIFIER),
        verificando PROPIEDAD (vinculo durable en el journal) y unicidad. Nunca
        selecciona por indice ni por ticket minimo.
        """
        if not self._allow_restricted_hedging:
            return self._observe_net_position()
        if not target_position_id or not str(target_position_id).strip():
            raise ExecutionBlockedError(
                "position_target",
                "HEDGING restringido exige target_position_id explicito "
                "(POSITION_IDENTIFIER); no se selecciona posicion por indice",
            )
        target = str(target_position_id)
        if not self._position_is_owned(target):
            raise ExecutionBlockedError(
                "position_ownership",
                "POSITION_IDENTIFIER objetivo no vinculado a Atlas "
                "(propiedad desconocida/externa)",
            )
        matching = [
            p
            for p in self._broker_positions_for_instrument()
            if p.position_id is not None and str(p.position_id) == target
        ]
        if len(matching) != 1:
            raise ExecutionBlockedError(
                "position_evidence",
                f"identidad de posicion ambigua/ausente para el objetivo "
                f"(coincidencias={len(matching)})",
            )
        pos = matching[0]
        if abs(float(pos.quantity)) <= 1e-12:
            raise ExecutionBlockedError("position_evidence", "posicion objetivo nula")
        return pos

    # ------------------- identidad de operacion CLOSE/REDUCE (H-2)

    def _base_operation_id(self, operation: str, requested_q) -> str:
        """Identificador BASE determinista (compatibilidad NETTING)."""
        if operation == OPERATION_CLOSE:
            return f"CLOSE:{self._instrument}"
        return f"REDUCE:{self._instrument}:{requested_q}"

    def _next_free_operation_id(self, base_id: str) -> str:
        """Identificador UNICO derivado del base para una operacion NUEVA.

        El primer sufijo libre evita reutilizar la identidad de una operacion ya
        resuelta sobre el mismo instrumento/cantidad (p.ej. tras cerrar y volver
        a abrir, o una segunda reduccion del mismo tamano).
        """
        if self._journal.get(base_id) is None:
            return base_id
        n = 2
        while self._journal.get(f"{base_id}#{n}") is not None:
            n += 1
        return f"{base_id}#{n}"

    def _target_present(self, target_position_id: str | None) -> bool:
        """True si el OBJETIVO de la operacion sigue vivo en el broker.

        NETTING: una posicion neta del instrumento. HEDGING restringido: la
        posicion con el `POSITION_IDENTIFIER` indicado. No lanza por ausencia.
        """
        positions = self._broker_positions_for_instrument()
        if self._allow_restricted_hedging:
            if not target_position_id or not str(target_position_id).strip():
                return False
            target = str(target_position_id)
            return any(
                p.position_id is not None and str(p.position_id) == target
                for p in positions
            )
        return len(positions) == 1

    def _assert_no_unresolved_reduce(self, target_position_id: str | None) -> None:
        """Bloquea una operacion nueva que eludiria un cierre/reduccion pendiente.

        Si existe un intent CLOSE/REDUCE SIN RESOLVER sobre el MISMO objetivo
        (mismo instrumento y, en HEDGING, misma identidad de posicion), ninguna
        solicitud nueva sobre ese objetivo puede enviarse --ni con otro
        `operation_id`-- hasta resolverlo por lookup.
        """
        for intent in self._journal.unresolved():
            if intent.operation not in (OPERATION_CLOSE, OPERATION_REDUCE):
                continue
            if intent.instrument != self._instrument:
                continue
            if (
                self._allow_restricted_hedging
                and target_position_id is not None
                and intent.target_position_id is not None
                and str(intent.target_position_id) != str(target_position_id)
            ):
                continue
            raise ExecutionBlockedError(
                "reduce_pending",
                "existe un cierre/reduccion sin resolver sobre el objetivo; "
                "resolver por lookup antes de enviar otra orden",
            )

    def _resolve_reduce_identity(
        self,
        operation: str,
        operation_id: str | None,
        requested_q,
        target_position_id: str | None,
    ):
        """Resuelve el `operation_id` de la operacion.

        Devuelve `(op_id, None)` para enviar una operacion (nueva o reintento con
        id explicito) o `(None, intent_terminal)` para una repeticion ya resuelta
        sin nada que enviar (idempotencia). Lanza en conflicto/pendiente.
        """
        instrument = self._instrument
        if operation_id is not None:
            op_id = str(operation_id)
            existing = self._journal.get(op_id)
            if existing is None:
                return op_id, None
            if existing.operation != operation or existing.instrument != instrument:
                raise ExecutionBlockedError(
                    "identity", "operation_id reutilizado con identidad distinta"
                )
            if (
                operation == OPERATION_REDUCE
                and abs(float(existing.quantity) - requested_q) > 1e-9
            ):
                raise ExecutionBlockedError(
                    "identity", "operation_id reutilizado con cantidad distinta"
                )
            if existing.terminal:
                return None, existing
            raise ExecutionBlockedError(
                "reduce_pending",
                "operacion previa sin resolver: resolver por lookup (no repetir)",
            )
        base_id = self._base_operation_id(operation, requested_q)
        existing = self._journal.get(base_id)
        if existing is None:
            return base_id, None
        if existing.operation != operation or existing.instrument != instrument:
            return self._next_free_operation_id(base_id), None
        if not existing.terminal:
            raise ExecutionBlockedError(
                "reduce_pending",
                "operacion previa sin resolver: resolver por lookup (no repetir)",
            )
        # Base TERMINAL: si queda objetivo vivo es una operacion NUEVA (p.ej. se
        # cerro y se volvio a abrir, o segunda reduccion); si no, no-op.
        if self._target_present(target_position_id):
            return self._next_free_operation_id(base_id), None
        return None, existing

    # Tolerancia de volumen para comparar ejecutado vs residual de posicion.
    _RESIDUAL_EPS = 1e-6

    def _observe_position_after_send(
        self, operation: str, target_position_id: str | None
    ):
        """Re-consulta la POSICION OBJETIVO tras el envio (evidencia de posicion).

        Devuelve `(position_or_None, reason_or_None)`:
          * `(None, None)` -> ausencia observada de la posicion objetivo.
          * `(pos, None)`  -> posicion viva, resuelta por POSITION_IDENTIFIER
                              (HEDGING; nunca por indice/primera fila).
          * `(None, motivo)` -> consulta NO concluyente (error/None/ambiguedad):
                              no autoriza a declarar exito.
        """
        try:
            positions = self._broker_positions_for_instrument()
        except ExecutionBlockedError as exc:
            return None, f"posiciones no consultables ({exc.reason})"
        except Exception as exc:  # noqa: BLE001 - consulta incierta => sin resolver
            return None, f"posiciones no consultables ({type(exc).__name__})"
        if self._allow_restricted_hedging:
            if not target_position_id or not str(target_position_id).strip():
                return None, "objetivo HEDGING sin POSITION_IDENTIFIER"
            target = str(target_position_id)
            matching = [
                p
                for p in positions
                if p.position_id is not None and str(p.position_id) == target
            ]
            if len(matching) > 1:
                return None, "identidad de posicion ambigua tras el envio"
            if not matching:
                return None, None
            pos = matching[0]
            if pos.ticket is None:
                return None, "posicion objetivo sin POSITION_TICKET actual"
            return pos, None
        # NETTING: una unica posicion neta del instrumento.
        if len(positions) > 1:
            return None, "multiples posiciones del instrumento tras el envio"
        return (positions[0] if positions else None), None

    def _confirm_close_reduce(self, operation: str, op_id: str) -> str | None:
        """Confirma con evidencia AUTORITATIVA de orden + fills + POSICION ACTUAL.

        Combina el estado/fills de la propia orden (`lookup_order_history`) con la
        re-consulta del estado ACTUAL de la posicion objetivo, y compara volumen
        inicial, ejecutado y residual. Devuelve:
          * "full"     -> orden ejecutada por el volumen esperado Y residual
                          observado coherente (cierre: posicion ausente;
                          reduccion: residual == disponible - ejecutado).
          * "partial"  -> ejecucion parcial con residual coherente.
          * "rejected" -> el broker no ejecuto la operacion.
          * None       -> sin evidencia suficiente o discrepancia: SIN RESOLVER.
        La ausencia AISLADA de la posicion NO demuestra que esta orden la cerro.
        """
        intent = self._journal.get(op_id)
        if intent is None:
            return None
        lookup = getattr(self._broker, "lookup_order_history", None)
        if not callable(lookup):
            return None
        try:
            result = lookup(op_id)
        except Exception:  # noqa: BLE001 - consulta incierta => sin resolver
            return None
        if not bool(getattr(result, "complete", False)):
            return None
        order = getattr(result, "order", None)
        if order is None:
            return None
        state = getattr(order, "state", None)
        if state in (
            BrokerOrderState.REJECTED,
            BrokerOrderState.EXPIRED,
            BrokerOrderState.CANCELLED,
        ):
            return "rejected"

        requested = float(intent.quantity)
        available_before = float(
            intent.available_quantity
            if intent.available_quantity is not None
            else requested
        )
        filled = float(getattr(order, "filled_quantity", 0.0) or 0.0)

        # 2) Estado ACTUAL de la posicion objetivo (no sustituible por existe/no existe).
        position, reason = self._observe_position_after_send(
            operation, intent.target_position_id
        )
        if reason is not None:
            return None  # consulta no concluyente => sin resolver
        observed_residual = 0.0 if position is None else abs(float(position.quantity))
        expected_residual = max(0.0, available_before - filled)
        eps = self._RESIDUAL_EPS

        # Sin fills atribuibles: la ausencia de posicion NO prueba exito.
        if filled <= eps:
            return None

        if filled + eps < requested:
            # PARCIAL: el residual observado debe ser coherente con lo ejecutado.
            if abs(observed_residual - expected_residual) <= eps:
                return "partial"
            return None

        # Ejecutado por el volumen solicitado: residual debe ser el esperado.
        if abs(observed_residual - expected_residual) <= eps:
            return "full"
        return None

    def _reflect_close_reduce(
        self, operation: str, op_id: str, submission: OrderSubmission, order: Order
    ) -> str:
        """Fija el estado del intent segun la evidencia autoritativa y devuelve
        el `stage`. NUNCA declara exito sin confirmacion completa."""
        prefix = "close" if operation == OPERATION_CLOSE else "reduce"
        broker_id = (
            None
            if submission.broker_order is None
            else (submission.broker_order.broker_order_id or None)
        )
        if submission.status is SubmitStatus.UNKNOWN:
            try:
                self._order_manager.mark_unknown(order.order_id)
            except OrderStateError:
                pass
            self._journal.mark_state(op_id, "UNKNOWN")
            return f"{prefix}_unknown"
        if submission.status is SubmitStatus.REJECTED:
            try:
                self._order_manager.reject(order.order_id, submission.detail or "rechazada")
            except OrderStateError:
                pass
            self._journal.mark_state(op_id, "REJECTED")
            raise ExecutionBlockedError(
                f"{prefix}_rejected", submission.detail or "rechazada por el broker"
            )
        if submission.broker_order is not None:
            self._record_broker_identity(op_id, submission.broker_order)
            self._sync_order_from_broker(order, submission.broker_order)
        verdict = self._confirm_close_reduce(operation, op_id)
        if verdict == "full":
            try:
                self._order_manager.sync_broker_state(
                    order.order_id,
                    state=OrderStatus.FILLED,
                    filled_quantity=order.quantity,
                )
            except (KeyError, OrderStateError):
                pass
            self._journal.mark_state(op_id, "FILLED", broker_order_id=broker_id)
            return "closed" if operation == OPERATION_CLOSE else "reduced"
        if verdict == "partial":
            try:
                self._order_manager.sync_broker_state(
                    order.order_id,
                    state=OrderStatus.PARTIALLY_FILLED,
                    filled_quantity=getattr(
                        submission.broker_order, "filled_quantity", None
                    ),
                )
            except (KeyError, OrderStateError):
                pass
            self._journal.mark_state(
                op_id, "PARTIALLY_FILLED", broker_order_id=broker_id
            )
            return f"{prefix}_partial"
        if verdict == "rejected":
            try:
                self._order_manager.reject(
                    order.order_id, "el broker no ejecuto la operacion"
                )
            except OrderStateError:
                pass
            self._journal.mark_state(op_id, "REJECTED", broker_order_id=broker_id)
            raise ExecutionBlockedError(
                f"{prefix}_rejected", "el broker no ejecuto la operacion"
            )
        # Sin evidencia autoritativa suficiente: SIN RESOLVER (no falso exito).
        try:
            self._order_manager.mark_unknown(order.order_id)
        except OrderStateError:
            pass
        self._journal.mark_state(op_id, "UNKNOWN", broker_order_id=broker_id)
        return f"{prefix}_unknown"

    def _validate_reduce_quantity(self, quantity) -> float:
        spec = self._instrument_spec
        try:
            return validate_quantity_lattice(
                quantity,
                minimum=spec.quantity_min,
                maximum=spec.quantity_max,
                step=spec.quantity_step,
            )
        except ValueError as exc:
            raise ExecutionBlockedError("quantity", str(exc)) from exc

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
        spec = self._instrument_spec
        try:
            quantity = validate_quantity_lattice(
                quantity,
                minimum=spec.quantity_min,
                maximum=spec.quantity_max,
                step=spec.quantity_step,
            )
        except ValueError as exc:
            raise ExecutionBlockedError("quantity", str(exc)) from exc

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
        # HEDGING restringido: una sola posicion por simbolo; una APERTURA exige
        # que no exista ya posicion del instrumento.
        if is_entry:
            self._assert_no_open_position_for_entry()

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
        # ORDER-07B: si el gate rechaza, la orden NO puede quedar CREATED
        # huerfana en OrderManager (ni bloquear una identidad posterior).
        if self._projected_gate is not None:
            try:
                self._projected_gate(order)
            except ExecutionBlockedError as exc:
                self._reject_order(order, str(exc))
                raise
            except Exception as exc:  # noqa: BLE001 - gate ajeno => fail-closed
                reason = f"{type(exc).__name__}: {exc}"
                self._reject_order(order, reason)
                raise ExecutionBlockedError(
                    "projected_exposure", reason
                ) from exc

        # Intent journal WRITE-AHEAD (obligatorio). Fallo => NO submit.
        try:
            intent = self._journal.begin(
                OrderIntent.from_order(
                    order,
                    run_id=self._run_id,
                    protocol_version=SUBMIT_PROTOCOL_VERSION,
                )
            )
        except IntentJournalError as exc:
            try:
                self._order_manager.reject(
                    order.order_id, f"intent persist failed: {exc}"
                )
            except OrderStateError:
                pass
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # ORDER-07B: preparacion pre-send. Cualquier fallo aqui es un fallo
        # operativo DEMOSTRADO sin envio => FAILED (no UNKNOWN, no CREATED).
        try:
            self._order_manager.approve(order.order_id)
            self._order_manager.mark_submitting(order.order_id)
        except OrderStateError as exc:
            self._mark_pre_send_failed(order, f"order_manager: {exc}")
            raise ExecutionBlockedError("order_manager", str(exc)) from exc
        try:
            self._journal.mark_state(client_order_id, "SUBMITTING")
        except IntentJournalError as exc:
            self._mark_pre_send_failed(order, f"journal submitting: {exc}")
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # Frontera durable ya escrita: a partir de aqui el envio pudo ocurrir.
        try:
            self._gate.approve(client_order_id, token=self._token)
            submission = self._broker.submit_order(order)
        except ExecutionNotAuthorizedError as exc:
            self._mark_pre_send_failed(order, str(exc))
            raise
        except OrderRejectedError as exc:
            self._mark_pre_send_rejected(order, str(exc))
            raise
        except BrokerError as exc:
            self._mark_pre_send_failed(order, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - fase no demostrable => ambiguo
            self._mark_ambiguous(order, f"{type(exc).__name__}: {exc}")
            raise
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
            self._sync_order_from_broker(order, submission.broker_order)
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
        """Adopta el estado AUTORITATIVO del broker para el objetivo."""
        intent = self._journal.get(target_cid)
        state = getattr(getattr(broker_order, "state", None), "value", None)
        if intent is None or not state or intent.terminal:
            return
        try:
            self._journal.mark_state(target_cid, state)
        except IntentJournalError:
            pass
        order = self._order_by_client_id(target_cid)
        if order is not None:
            self._sync_order_from_broker(order, broker_order)

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
            protocol_version=SUBMIT_PROTOCOL_VERSION,
        )
        try:
            intent = self._journal.begin(cancel_intent)
        except IntentJournalError as exc:
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc
        try:
            self._journal.mark_state(op_id, "SUBMITTING")
        except IntentJournalError as exc:
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # 6) Autorizacion de capacidad `CANCEL` + llamada al broker.
        try:
            self._gate.approve_operation(
                OPERATION_CANCEL, op_id=op_id, target=target_cid, token=self._token
            )
            broker_order = self._broker.cancel_order(
                target_cid, operation_id=op_id
            )
        except OrderRejectedError as exc:
            self._journal.mark_state(op_id, "REJECTED")
            raise ExecutionBlockedError("cancel_rejected", str(exc)) from exc
        except ExecutionNotAuthorizedError as exc:
            self._journal.mark_state(op_id, "FAILED")
            raise ExecutionBlockedError("cancel_not_authorized", str(exc)) from exc
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
        except BrokerError as exc:
            # Post-send no interpretable (p.ej. fallo de mapeo del broker):
            # ambiguo, nunca "no enviado".
            self._journal.mark_state(op_id, "UNKNOWN")
            raise ExecutionBlockedError(
                "cancel_unknown", "resultado no interpretable: resolver por lookup"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - fase no demostrable => ambiguo
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
        if state in (BrokerOrderState.REJECTED, BrokerOrderState.EXPIRED):
            self._journal.mark_state(
                op_id, "REJECTED",
                broker_order_id=getattr(broker_order, "broker_order_id", None),
            )
            self._adopt_target_state(target_cid, broker_order)
            raise ExecutionBlockedError(
                "cancel_target_terminal",
                f"orden en estado {state.value}: cancelacion no aplicable",
            )
        if state in (
            BrokerOrderState.SUBMITTED,
            BrokerOrderState.PARTIALLY_FILLED,
        ):
            # El broker sigue mostrando la orden activa: la cancelacion NO se
            # confirma y el objetivo conserva su estado real.
            self._journal.mark_state(op_id, "UNKNOWN")
            self._adopt_target_state(target_cid, broker_order)
            raise ExecutionBlockedError(
                "cancel_target_active",
                f"orden sigue {state.value}: cancelacion sin confirmar",
            )
        # UNKNOWN u otro estado no concluyente: ambiguo, sin cancelacion.
        self._journal.mark_state(op_id, "UNKNOWN")
        self._adopt_target_state(target_cid, broker_order)
        raise ExecutionBlockedError(
            "cancel_unknown",
            f"estado tras cancelar {getattr(state, 'value', state)}: sin resolver",
        )

    # ------------------------------------------------ cierre/reduccion (02B)

    def close_position(
        self, *, operation_id: str | None = None, target_position_id: str | None = None
    ) -> ExecutionOutcome:
        """Cierra el REMANENTE de una posicion NETTING (02B).

        Cantidad objetivo = exposicion neta OBSERVADA en el broker. Nunca se
        inventa la cantidad. Exige modo NETTING, identidad de posicion
        inequivoca, no-inversion, broker sano, sesion operable, kill switch
        ALLOW/BLOCK_NEW_ORDERS, reconciliacion coherente y LIVE-03 valida.
        """
        return self._close_or_reduce(
            operation=OPERATION_CLOSE,
            requested=None,
            operation_id=operation_id,
            target_position_id=target_position_id,
        )

    def reduce_position(
        self,
        *,
        quantity: float,
        operation_id: str | None = None,
        target_position_id: str | None = None,
    ) -> ExecutionOutcome:
        """Reduce una cantidad EXPLICITA de una posicion NETTING (02B).

        La cantidad debe ser > 0 y <= exposicion neta observada en el broker, y
        compatible con el lattice (min/step/max). No redondea hacia arriba.
        """
        return self._close_or_reduce(
            operation=OPERATION_REDUCE,
            requested=quantity,
            operation_id=operation_id,
            target_position_id=target_position_id,
        )

    def _close_or_reduce(
        self,
        *,
        operation: str,
        requested,
        operation_id: str | None,
        target_position_id: str | None,
    ) -> ExecutionOutcome:
        instrument = self._instrument
        kind = OPERATION_CLOSE if operation == OPERATION_CLOSE else OPERATION_REDUCE
        purpose = PURPOSE_CLOSE if operation == OPERATION_CLOSE else PURPOSE_REDUCE
        requested_q = None
        if operation == OPERATION_REDUCE:
            requested_q = self._validate_reduce_quantity(requested)

        # 1) Identidad de la operacion (H-2): explicita => reintento idempotente;
        #    derivada => NUEVA operacion con identificador UNICO (no colisiona
        #    tras cerrar/abrir de nuevo ni entre reducciones del mismo tamano).
        op_id, resolved = self._resolve_reduce_identity(
            operation, operation_id, requested_q, target_position_id
        )
        if op_id is None:
            return ExecutionOutcome(
                allowed=True,
                stage="reduce_idempotent",
                reason="operacion ya resuelta; sin segunda llamada al broker",
                intent=resolved,
            )

        # 1b) Ninguna operacion nueva puede eludir un cierre/reduccion PENDIENTE
        #     sobre el MISMO objetivo (ni con otro identificador).
        self._assert_no_unresolved_reduce(target_position_id)

        # 2) Gates obligatorios (NO se pasa por el limite de NUEVAS posiciones).
        now = self._clock()
        self._assert_account_mode(is_entry=False)  # NETTING verificado
        self._assert_session_reduce(now)
        self._assert_kill_switch_reduce()
        self._assert_broker_health()
        self._assert_reconciliation()
        self._assert_market_data(now)
        self._assert_currency()

        # 3) Identidad/exposicion efectiva: posicion del BROKER (NETTING: neta;
        #    HEDGING restringido: dirigida por POSITION_IDENTIFIER + propiedad).
        pos = self._observe_target_position(target_position_id)
        available = abs(float(pos.quantity))
        net = float(pos.quantity)
        side = OrderSide.SELL if net > 0 else OrderSide.BUY  # lado reductor
        if operation == OPERATION_CLOSE:
            try:
                qty = validate_quantity_lattice(
                    available,
                    minimum=self._instrument_spec.quantity_min,
                    maximum=self._instrument_spec.quantity_max,
                    step=self._instrument_spec.quantity_step,
                )
            except ValueError as exc:
                raise ExecutionBlockedError("quantity", str(exc)) from exc
        else:
            if requested_q > available + 1e-9:
                raise ExecutionBlockedError(
                    "quantity",
                    f"cantidad {requested_q} > exposicion disponible {available}",
                )
            qty = requested_q

        # 4) Idempotencia en el broker: si ya cerrada, no hay nada que hacer.
        if available <= 1e-12:
            raise ExecutionBlockedError(
                "position_evidence", "sin exposicion disponible que reducir"
            )

        # 5) Orden de broker EXPLICITA con proposito tipado y reduce_only. La
        #    no-inversion la verifican el coordinador (side/qty) y el adapter
        #    justo antes de enviar.
        try:
            order = self._order_manager.create_order(
                strategy_id="coordinated",
                strategy_version="1.0.0",
                account_id=self._account_id,
                instrument=instrument,
                side=side,
                quantity=qty,
                order_type=OrderType.MARKET,
                client_order_id=op_id,
                signal_id=op_id,
                reduce_only=True,
                purpose=purpose,
                target_position_id=target_position_id,
            )
        except OrderStateError as exc:
            raise ExecutionBlockedError("order_manager", str(exc)) from exc

        # 6) Write-ahead del intent (v3) ANTES de tocar el broker.
        if operation == OPERATION_CLOSE:
            candidate = OrderIntent.for_close(
                operation_id=op_id,
                instrument=instrument,
                side=side.value,
                available_quantity=available,
                target_position_id=target_position_id,
                run_id=self._run_id,
                protocol_version=SUBMIT_PROTOCOL_VERSION,
            )
        else:
            candidate = OrderIntent.for_reduce(
                operation_id=op_id,
                instrument=instrument,
                side=side.value,
                quantity=qty,
                available_quantity=available,
                target_position_id=target_position_id,
                run_id=self._run_id,
                protocol_version=SUBMIT_PROTOCOL_VERSION,
            )
        try:
            intent = self._journal.begin(candidate)
        except IntentJournalError as exc:
            try:
                self._order_manager.reject(
                    order.order_id, f"intent persist failed: {exc}"
                )
            except OrderStateError:
                pass
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        # 7) Frontera durable SUBMITTING + gate + envio.
        try:
            self._order_manager.approve(order.order_id)
            self._order_manager.mark_submitting(order.order_id)
        except OrderStateError as exc:
            self._mark_pre_send_failed(order, f"order_manager: {exc}")
            raise ExecutionBlockedError("order_manager", str(exc)) from exc
        try:
            self._journal.mark_state(op_id, "SUBMITTING")
        except IntentJournalError as exc:
            self._mark_pre_send_failed(order, f"journal submitting: {exc}")
            raise ExecutionBlockedError("journal_persist", str(exc)) from exc

        try:
            self._gate.approve_operation(
                kind, op_id=op_id, target=instrument, token=self._token
            )
            submission = self._broker.submit_order(order)
        except ExecutionNotAuthorizedError as exc:
            self._mark_pre_send_failed(order, str(exc))
            raise
        except OrderRejectedError as exc:
            self._mark_pre_send_rejected(order, str(exc))
            raise
        except BrokerError as exc:
            self._mark_pre_send_failed(order, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - fase no demostrable => ambiguo
            self._mark_ambiguous(order, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._gate.consume_operation(
                kind, op_id=op_id, target=instrument, token=self._token
            )

        # 8) Resultado basado en evidencia AUTORITATIVA (H-1): un UNKNOWN o una
        #    ejecucion parcial NUNCA se declara como cierre/reduccion confirmada.
        stage = self._reflect_close_reduce(operation, op_id, submission, order)
        return ExecutionOutcome(
            allowed=True,
            stage=stage,
            reason="operacion reductora enviada",
            order=order,
            submission=submission,
            intent=intent,
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
