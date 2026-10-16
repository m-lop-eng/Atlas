"""LiveExecutionRuntime (MT5-DEMO-LIVE-READINESS-03A): runtime coordinado.

Compone, por INYECCION EXPLICITA, las dependencias reales
(`LiveExecutionCoordinator` + `AccountLedger` + `IntentJournal` +
`LedgerApplier` + reconciliacion + kill switch) y fuerza una secuencia de
arranque OBLIGATORIA antes de admitir la primera operacion:

    DEMO/mapping/moneda/NETTING
      -> carga + migracion no destructiva del journal
      -> prime de identidades canonicas
      -> resolucion lookup-only de intents pendientes (OPEN/CANCEL/CLOSE/REDUCE)
      -> observacion del broker (posiciones/ordenes/cuenta)
      -> reconstruccion/reconciliacion del ledger esperado vs observado
      -> salud + reconciliacion + kill switch
      -> runtime PREPARADO (solo entonces se habilitan envios)

No conecta ningun broker por construirse: en tests se inyecta `FakeMT5`. NO es
un entrypoint live ni incluye conexion automatica. Si queda cualquier
ambiguedad bloqueante (intent pendiente, journal corrupto, discrepancia
contable, identidad ambigua) el runtime NO se marca preparado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from execution.intent_journal import OPERATION_CANCEL, IntentJournal
from execution.ledger_applier import LedgerApplier, CommissionPolicy
from execution.ledger_store import LedgerStore
from execution.live_execution import (
    ExecutionOutcome,
    LiveExecutionCoordinator,
    LiveExecutionError,
    MarketDataReference,
)
from execution.order_manager import OrderManager

from brokers.models import BrokerOrderState
from killswitches.models import KillSource, KillSwitchState
from reconciliation.models import (
    AccountSnapshot,
    InternalSnapshot,
    OrderSnapshot,
    ReconciliationReport,
    ReconciliationStatus,
)
from reconciliation.orchestrator import reconcile_with_broker

_UTC = timezone.utc

# Operaciones que representan una ORDEN de broker (no una cancelacion).
_ORDER_OPERATIONS = ("OPEN", "CLOSE", "REDUCE")


class RuntimeNotReadyError(LiveExecutionError):
    """Se intento operar antes de superar la secuencia de arranque."""


@dataclass(slots=True)
class ReadinessReport:
    """Resultado de la secuencia de arranque."""

    ready: bool
    stage: str
    reason: str
    primed: int = 0
    resolved: tuple = ()
    unresolved: int = 0
    reconciliation: ReconciliationReport | None = None


@dataclass(slots=True)
class LiveExecutionRuntime:
    """Runtime inyectable de ejecucion coordinada (DEMO/NETTING, FakeMT5 en tests)."""

    broker: Any
    order_manager: OrderManager
    journal: IntentJournal
    ledger: Any
    ledger_store: LedgerStore
    sessions: Any
    kill_switch: Any
    instrument: str
    instrument_spec: Any
    account_id: str
    account_currency: str
    max_open_positions: int
    risk_state_provider: Callable[[], Any]
    market_data_provider: Callable[[], MarketDataReference | None]
    data_max_age_s: float
    account_mode_provider: Callable[[], int | None]
    marks_provider: Callable[[], dict[str, float]]
    projected_exposure_gate: Callable[[Any], None] | None = None
    commission_policy: CommissionPolicy = field(default_factory=CommissionPolicy)
    reconciliation_engine: Any | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(_UTC)
    run_id: str = "LIVE-RUNTIME"
    # ATLAS-MT5-HEDGING-01: opt-in explicito restringido (una posicion Atlas por
    # simbolo). Por defecto False: HEDGING no se habilita solo por margin_mode.
    allow_restricted_hedging: bool = False
    # ATLAS-MT5-ACCOUNTING-05G: autoridad de admision contable MT5 (fail-closed).
    # Requerida para un broker que marque `requires_accounting_admission` (el
    # adaptador MT5 lo hace). NO es un flag para desactivar la contabilidad: si el
    # adaptador la exige y falta el componente, el arranque se BLOQUEA.
    mt5_accounting: Any | None = None

    _coordinator: LiveExecutionCoordinator = field(init=False, default=None)
    _applier: LedgerApplier = field(init=False, default=None)
    _ready: bool = field(init=False, default=False)
    _last_report: ReconciliationReport | None = field(init=False, default=None)
    _commission_unverified: float = field(init=False, default=0.0)
    _history_incomplete: bool = field(init=False, default=False)
    _snapshot_present: bool = field(init=False, default=False)
    _ownership_unverified: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # La economia del instrumento queda registrada en el ledger (autoridad de
        # metadata economica); sin ella `apply_fill` usaria el path legacy cs=1.
        self.ledger.register_instrument(self.instrument_spec)
        # El coordinador exige un broker gate-bindable y un journal valido.
        self._coordinator = LiveExecutionCoordinator(
            broker=self.broker,
            order_manager=self.order_manager,
            journal=self.journal,
            sessions=self.sessions,
            kill_switch=self.kill_switch,
            instrument=self.instrument,
            instrument_spec=self.instrument_spec,
            account_id=self.account_id,
            account_currency=self.account_currency,
            max_open_positions=self.max_open_positions,
            risk_state_provider=self.risk_state_provider,
            market_data_provider=self.market_data_provider,
            data_max_age_s=self.data_max_age_s,
            account_mode_provider=self.account_mode_provider,
            projected_exposure_gate=self.projected_exposure_gate,
            reconciliation=self.reconciliation_engine,
            clock=self.clock,
            run_id=self.run_id,
            allow_restricted_hedging=self.allow_restricted_hedging,
        )
        self._applier = LedgerApplier(
            ledger=self.ledger,
            account_currency=self.account_currency,
            commission_policy=self.commission_policy,
            clock=self.clock,
        )

    # ------------------------------------------------------------ propiedades
    @property
    def coordinator(self) -> LiveExecutionCoordinator:
        return self._coordinator

    @property
    def applier(self) -> LedgerApplier:
        return self._applier

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def last_reconciliation(self) -> ReconciliationReport | None:
        return self._last_report

    # --------------------------------------------------------------- arranque

    def _demo_environment_ok(self) -> bool:
        env = getattr(self.broker, "environment", None)
        value = getattr(env, "value", env)
        return str(value).upper() == "DEMO"

    def _mt5_accounting_required(self) -> bool:
        """True si el broker EXIGE admision contable MT5 (adaptador MT5)."""
        return bool(getattr(self.broker, "requires_accounting_admission", False))

    def initialize(self) -> ReadinessReport:
        """Secuencia de arranque fail-closed. Devuelve el informe de preparacion."""
        # 1) config DEMO / mapping / moneda / NETTING
        if not self._demo_environment_ok():
            return self._not_ready("config", "entorno no DEMO (produccion prohibida)")
        symbol_map = getattr(getattr(self.broker, "config", None), "symbol_map", None)
        if not symbol_map or self.instrument not in symbol_map:
            return self._not_ready("config", "mapping de simbolos explicito ausente")
        if not self.account_currency or not str(self.account_currency).strip():
            return self._not_ready("config", "moneda de cuenta ausente")
        mode = self.account_mode_provider()
        if mode is None:
            return self._not_ready("account_mode", "margin_mode no verificable")
        mode_int = int(mode)
        if mode_int == 2:  # HEDGING
            if not self.allow_restricted_hedging:
                return self._not_ready(
                    "account_mode",
                    "cuenta HEDGING sin opt-in restringido "
                    "(allow_restricted_hedging=False)",
                )
        elif mode_int != 0:  # no NETTING ni HEDGING restringido
            return self._not_ready(
                "account_mode", f"margin_mode no soportado ({mode_int})"
            )

        # 2) journal: validar/migrar de forma no destructiva
        try:
            self.journal.migrate_if_needed()
        except Exception as exc:  # noqa: BLE001 - journal corrupto => fail-closed
            return self._not_ready("journal", f"journal no migrable: {exc}")

        # 3) identidades canonicas
        try:
            primed = self._coordinator.prime_identity_from_journal()
        except Exception as exc:  # noqa: BLE001
            return self._not_ready("identity", f"prime fallo: {exc}")

        # 4) resolucion lookup-only de intents pendientes (sin reenvio)
        resolved: list = []
        try:
            resolved.extend(self._coordinator.resolve_pending())
            resolved.extend(self._coordinator.resolve_pending_cancels())
        except Exception as exc:  # noqa: BLE001
            return self._not_ready("recovery", f"lookup fallo: {exc}")
        # Un intent ACTIVO conocido (SUBMITTED/PARTIALLY_FILLED/ACKNOWLEDGED) es
        # gestionable (cancelar/cerrar) aunque bloquee NUEVAS aperturas. Lo que
        # impide marcar el runtime preparado es una AMBIGUEDAD no resuelta
        # (CREATED/SUBMITTING/UNKNOWN) o un lookup incierto/NOT_FOUND.
        uncertain = {
            "NOT_FOUND", "LOOKUP_UNCERTAIN", "BROKER_UNKNOWN",
        }
        blocking = [
            i for i in self.journal.unresolved()
            if i.state in ("CREATED", "SUBMITTING", "UNKNOWN")
        ]
        if any(getattr(r, "result", "") in uncertain for r in resolved):
            blocking = list(self.journal.unresolved())
        if blocking:
            return self._not_ready(
                "recovery", f"{len(blocking)} intent(s) ambiguo(s) sin resolver",
                primed, tuple(resolved), len(blocking),
            )

        # 5) observacion del broker (posiciones/ordenes/cuenta) + salud
        if not bool(self.broker.health()):
            self.kill_switch.set_source(KillSource.BROKER, KillSwitchState.HALT, "broker no sano")
            return self._not_ready("broker", "broker no sano", primed, tuple(resolved))

        # 5b) HEDGING restringido: verificacion de PROPIEDAD de posiciones.
        #     Una sola posicion por simbolo; toda posicion debe estar vinculada a
        #     un intent de Atlas (journal). Externas/multiples/ambiguas => block.
        if self.allow_restricted_hedging:
            ownership_reason = self._ownership_ready()
            if ownership_reason is not None:
                return self._not_ready(
                    "ownership", ownership_reason, primed, tuple(resolved)
                )

        # 6) reconstruir/reconciliar ledger esperado vs observado
        try:
            self._snapshot_present = self.ledger_store.load(self.ledger)
        except Exception as exc:  # noqa: BLE001 - ledger corrupto => fail-closed
            return self._not_ready("ledger", f"ledger no cargable: {exc}", primed, tuple(resolved))
        # 6b) El BROKER es la verdad de los fills: reconstruye/contabiliza los
        #     fills observados de cada orden conocida (idempotente), con estado
        #     EXPLICITO de historial. Cubre el fallo entre aplicar un fill y
        #     persistir el ledger.
        try:
            self._apply_known_fills()
            self.ledger_store.persist(self.ledger)
        except Exception as exc:  # noqa: BLE001
            return self._not_ready("fills", f"no se pudieron contabilizar fills: {exc}", primed, tuple(resolved))
        # Un historial INCOMPLETO/ambiguo impide demostrar que el estado
        # contable (snapshot y/o reconstruccion desde el broker) cubre TODOS los
        # fills; un snapshot estructuralmente valido NO demuestra cobertura de
        # los fills posteriores. No se inventa watermark: fail-closed siempre,
        # con o sin snapshot.
        if self._history_incomplete:
            return self._not_ready(
                "history",
                "historial del broker incompleto/ambiguo: cobertura de fills no "
                f"demostrable (snapshot_present={self._snapshot_present}); "
                "reconstruccion no verificable",
                primed, tuple(resolved),
            )
        report = self._reconcile()
        if not report.trading_allowed:
            self.kill_switch.update_from_reconciliation(report.status)
            return self._not_ready(
                "reconciliation", f"reconciliacion {report.status.value}",
                primed, tuple(resolved), 0, report,
            )

        # 7) salud/reconciliacion/kill switch coherentes
        self.kill_switch.update_from_reconciliation(report.status)
        ks_state = self.kill_switch.status().state
        if ks_state in (KillSwitchState.HALT, KillSwitchState.EMERGENCY):
            return self._not_ready(
                "kill_switch", f"kill switch {ks_state.value}", primed, tuple(resolved), 0, report
            )

        # 7b) admision contable MT5 (fail-closed; requerida por el adaptador MT5).
        #     Se evalua DESPUES de la reconciliacion SPOT para que un exito previo
        #     NO compense un fallo del ledger MT5.
        if self._mt5_accounting_required() and self.mt5_accounting is None:
            return self._not_ready(
                "mt5_accounting",
                "adaptador MT5 sin componente de admision contable (fail-closed)",
                primed, tuple(resolved), 0, report,
            )
        if self.mt5_accounting is not None:
            try:
                admission = self.mt5_accounting.initialize(self.broker)
            except Exception as exc:  # noqa: BLE001 - fail-closed
                return self._not_ready(
                    "mt5_accounting",
                    f"admision contable MT5 fallo ({type(exc).__name__})",
                    primed, tuple(resolved), 0, report,
                )
            if not bool(getattr(admission, "admitted", False)):
                return self._not_ready(
                    "mt5_accounting",
                    "admision contable MT5: "
                    f"{getattr(admission, 'stage', '?')}: "
                    f"{getattr(admission, 'reason', '?')}",
                    primed, tuple(resolved), 0, report,
                )

        # 8) preparado
        self._ready = True
        self._last_report = report
        return ReadinessReport(
            ready=True, stage="ready", reason="runtime preparado",
            primed=primed, resolved=tuple(resolved), unresolved=0, reconciliation=report,
        )

    def _not_ready(
        self,
        stage: str,
        reason: str,
        primed: int = 0,
        resolved: tuple = (),
        unresolved: int = 0,
        report: ReconciliationReport | None = None,
    ) -> ReadinessReport:
        self._ready = False
        return ReadinessReport(
            ready=False, stage=stage, reason=reason,
            primed=primed, resolved=resolved, unresolved=unresolved, reconciliation=report,
        )

    # ---------------------------------------------------------- reconciliacion

    def _broker_snapshot(self):
        from reconciliation.orchestrator import broker_state_from_adapter

        known = tuple(
            i.client_order_id
            for i in self.journal.all()
            if i.operation in _ORDER_OPERATIONS
        )
        return broker_state_from_adapter(self.broker, known_client_order_ids=known)

    def _internal_snapshot(self) -> InternalSnapshot:
        fills = self._applier.internal_fills()
        fill_stats = {f.client_order_id: f for f in fills}
        # El JOURNAL es la autoridad de ciclo de vida (el coordinador actualiza
        # su estado con la verdad del broker); el OrderManager solo aporta
        # ordenes no registradas en el journal.
        orders: dict[str, OrderSnapshot] = {}
        for intent in self.journal.all():
            if intent.operation not in _ORDER_OPERATIONS:
                continue
            fs = fill_stats.get(intent.client_order_id)
            orders[intent.client_order_id] = OrderSnapshot(
                client_order_id=intent.client_order_id,
                instrument=intent.instrument,
                quantity=intent.quantity,
                state=intent.state,
                filled_quantity=(fs.quantity if fs is not None else 0.0),
                avg_fill_price=(fs.avg_price if fs is not None else None),
                broker_order_id=intent.broker_order_id,
            )
        for o in self.order_manager.orders.values():
            if o.client_order_id in orders:
                continue
            orders[o.client_order_id] = OrderSnapshot(
                client_order_id=o.client_order_id,
                instrument=o.instrument,
                quantity=o.quantity,
                state=o.status.value,
                filled_quantity=o.fill_quantity,
                avg_fill_price=o.average_fill_price,
                broker_order_id=o.broker_order_id,
            )
        val = self.ledger.valuation(self.marks_provider())
        account = (
            None
            if val is None
            else AccountSnapshot(
                self.account_id, self.ledger.cash, val.equity,
                currency=self.account_currency,
            )
        )
        return InternalSnapshot(
            orders=tuple(orders.values()),
            fills=fills,
            positions=self._applier.position_snapshots(),
            account=account,
        )

    def _ownership_ready(self) -> str | None:
        """HEDGING restringido: comprueba PROPIEDAD de todas las posiciones.

        Devuelve un motivo de bloqueo (str) o None si es coherente:
          * >1 posicion por simbolo -> block (politica restringida);
          * posicion sin POSITION_IDENTIFIER -> block (identidad no verificable);
          * posicion no vinculada a Atlas -> block (externa/sin propietario);
          * posicion vinculada a un intent NO terminal pero ausente en broker ->
            block (divergencia journal/broker).
        """
        try:
            positions = list(self.broker.get_positions())
        except Exception as exc:  # noqa: BLE001
            return f"posiciones no determinables ({type(exc).__name__})"
        by_symbol: dict[str, int] = {}
        for p in positions:
            by_symbol[p.instrument] = by_symbol.get(p.instrument, 0) + 1
        if any(count > 1 for count in by_symbol.values()):
            return "multiples posiciones por simbolo (politica restringida)"
        broker_ids: set[str] = set()
        for p in positions:
            if p.position_id is None:
                return "posicion sin POSITION_IDENTIFIER (identidad no verificable)"
            pid = str(p.position_id)
            broker_ids.add(pid)
            if not self.journal.find_by_position_id(pid):
                return f"posicion externa/sin propietario ({p.instrument})"
        for intent in self.journal.all():
            if intent.position_id is not None and intent.position_id not in broker_ids:
                if not intent.terminal:
                    return "posicion vinculada ausente en broker (intent no terminal)"
        return None

    def _link_open_position(self, out: ExecutionOutcome) -> None:
        """Vincula durablemente la posicion creada por una APERTURA (HEDGING)."""
        if not self.allow_restricted_hedging:
            return
        order = getattr(out, "order", None)
        if order is None or str(getattr(order, "purpose", "OPEN")) != "OPEN":
            return
        sub = getattr(out, "submission", None)
        if sub is None or sub.broker_order is None:
            return
        cid = getattr(order, "client_order_id", None)
        if not cid:
            return
        current = self.journal.get(cid)
        if current is not None and current.position_id is not None:
            return  # ya vinculada (idempotente)
        try:
            matching = [
                p for p in self.broker.get_positions()
                if p.instrument == self.instrument
            ]
        except Exception:  # noqa: BLE001
            self._ownership_unverified = True
            return
        if len(matching) != 1 or matching[0].position_id is None:
            # No se puede probar la propiedad de forma inequivoca: fail-closed.
            self._ownership_unverified = True
            return
        self.journal.link_position(cid, matching[0].position_id)

    def _apply_known_fills(self) -> None:
        """Aplica (idempotente) los fills que el broker reporta para cada orden
        conocida por el journal (OPEN/CLOSE/REDUCE), con estado EXPLICITO de
        historial (`lookup_order_history` si el adapter lo ofrece).

        NO inventa fills: solo contabiliza los que el broker revela con historial
        COMPLETO. Un `INCOMPLETE` marca el runtime (fail-closed si no hay
        snapshot); un `EMPTY_CONFIRMED` no aporta fills (su estado ya lo fijo la
        recuperacion lookup-only).
        """
        from brokers.models import OrderNotFoundError

        lookup = getattr(self.broker, "lookup_order_history", None)
        for intent in self.journal.all():
            if intent.operation not in _ORDER_OPERATIONS:
                continue
            cid = intent.client_order_id
            if callable(lookup):
                result = lookup(cid)
                status = getattr(result, "status", None)
                if status == "INCOMPLETE":
                    self._history_incomplete = True
                    continue
                if status == "EMPTY_CONFIRMED":
                    continue
                bro = getattr(result, "order", None)
            else:
                # Fallback generico: solo cuando el adapter no expone el estado
                # explicito. Un BrokerConnectionError se propaga (no se silencia).
                try:
                    bro = self.broker.get_order(cid)
                except OrderNotFoundError:
                    continue
            if bro is None or not getattr(bro, "fills", ()):
                continue
            self._applier.apply_broker_order(cid, bro)

    def _reconcile(self) -> ReconciliationReport:
        report = reconcile_with_broker(
            self._coordinator._recon_engine, self._internal_snapshot(), self.broker
        )
        self._last_report = report
        return report

    def reconcile_now(self) -> ReconciliationReport:
        report = self._reconcile()
        self.kill_switch.update_from_reconciliation(report.status)
        return report

    # -------------------------------------------------------------- operacion

    def _ensure_operable(self) -> None:
        if not self._ready:
            raise RuntimeNotReadyError("runtime no preparado: ejecute initialize()")
        if self._commission_unverified > 0.0:
            raise RuntimeNotReadyError(
                "comision no valorable pendiente: runtime bloqueado (fail-safe)"
            )
        if self._ownership_unverified:
            raise RuntimeNotReadyError(
                "propiedad de posicion no verificable: runtime bloqueado (fail-safe)"
            )
        if self._last_report is not None and not self._last_report.trading_allowed:
            raise RuntimeNotReadyError(
                f"reconciliacion {self._last_report.status.value}: operacion bloqueada"
            )
        # Gate contable MT5: no existe camino alternativo (submit/cancel/close/reduce
        # pasan todos por aqui). Si el adaptador lo exige y falta el componente, o
        # este no esta admitido, se bloquea.
        if self._mt5_accounting_required() and self.mt5_accounting is None:
            raise RuntimeNotReadyError(
                "admisión contable MT5 ausente: runtime bloqueado (fail-closed)"
            )
        if self.mt5_accounting is not None:
            try:
                self.mt5_accounting.ensure_admitted()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeNotReadyError(
                    f"admisión contable MT5 no satisfecha: {exc}"
                ) from exc

    def _post_operation(self, out: ExecutionOutcome) -> ExecutionOutcome:
        submission = getattr(out, "submission", None)
        if submission is not None and submission.broker_order is not None:
            cid = getattr(out.order, "client_order_id", None) or submission.client_order_id
            outcome = self._applier.apply_broker_order(cid, submission.broker_order)
            self._commission_unverified += outcome.unverified_commission
            self.ledger_store.persist(self.ledger)
        # HEDGING restringido: vincula durablemente cid -> POSITION_IDENTIFIER.
        self._link_open_position(out)
        report = self.reconcile_now()
        # Admision contable MT5: re-valida tras la actividad del broker. Si falla,
        # el estado de admision queda BLOQUEADO y `_ensure_operable` impide la
        # siguiente operacion (no basta con que el ledger SPOT haya actualizado).
        if self.mt5_accounting is not None:
            refresh = getattr(self.mt5_accounting, "refresh", None)
            if callable(refresh):
                refresh(self.broker)
        return out

    def submit(self, **kwargs) -> ExecutionOutcome:
        self._ensure_operable()
        out = self._coordinator.submit(**kwargs)
        return self._post_operation(out)

    def cancel_pending(self, *, client_order_id: str, operation_id: str | None = None) -> ExecutionOutcome:
        self._ensure_operable()
        # Preserva/contabiliza los fills YA ejecutados del objetivo (idempotente)
        # antes de cancelar el remanente.
        self._apply_known_fills()
        out = self._coordinator.cancel_pending(
            client_order_id=client_order_id, operation_id=operation_id
        )
        return self._post_operation(out)

    def close_position(self, *, operation_id: str | None = None, target_position_id: str | None = None) -> ExecutionOutcome:
        self._ensure_operable()
        out = self._coordinator.close_position(
            operation_id=operation_id, target_position_id=target_position_id
        )
        return self._post_operation(out)

    def reduce_position(self, *, quantity: float, operation_id: str | None = None, target_position_id: str | None = None) -> ExecutionOutcome:
        self._ensure_operable()
        out = self._coordinator.reduce_position(
            quantity=quantity, operation_id=operation_id, target_position_id=target_position_id
        )
        return self._post_operation(out)


__all__ = [
    "LiveExecutionRuntime",
    "ReadinessReport",
    "RuntimeNotReadyError",
]
