"""MultiAssetPaperOrchestrator (MULTI-ASSET-04): orquestacion multi-instrumento.

Arquitectura elegida: **C — orquestador global + N workers por instrumento**,
reutilizando `PaperTradingEngine` como worker SIN reescribir su pipeline.

Servicios GLOBALES (una sola instancia logica por cuenta/run):
  * `AccountLedger` (MULTI-ASSET-03: cash/equity/posiciones globales);
  * `OrderManager` (identidad unica `client_order_id`, multi-instrumento);
  * `RiskEngine` + limites de cuenta (gross/net/leverage/concentration/
    max_open_positions GLOBALES);
  * `KillSwitchCoordinator` + `MonitoringEngine` (safety global);
  * `ReconciliationEngine` (cuenta global);
  * `IntentJournal` (multi-instrumento sin colision);
  * `PaperBrokerAdapter` (un unico broker/cuenta).

Estado POR INSTRUMENTO (cada worker `PaperTradingEngine`):
  * estrategia (instancia propia), `_bars`, `_last_open_time`, `_pending`
    (una entrada por instrumento), `_position`, `_pending_resolution`,
    session, `InstrumentSpec`.

Hooks inyectados para compartir sin romper el engine:
  * `price_registry`: marks/precios por instrumento COMPARTIDOS; evita que un
    instrumento pise el `price_source` del broker compartido.
  * `order_fills`: registro de fills por cid COMPARTIDO (reconciliacion global).
  * `risk_state_provider`: estado de riesgo GLOBAL (equity/open_positions/daily/
    drawdown) — el daily risk NO se duplica por instrumento.

NO implementa en este bloque: market-data multi-stream real, kill switch
instrument-scoped, replay multi-activo, FX real, margin/correlacion, MT5 real.
NO se activa por defecto: es opt-in explicito.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from brokers.paper import PaperBrokerAdapter
from execution.intent_journal import IntentJournal
from execution.instrument import InstrumentSpec
from execution.order_manager import OrderManager
from killswitches.coordinator import KillSwitchCoordinator
from live.events import ClosedBarEvent
from monitoring.engine import MonitoringEngine, apply_to_kill_switch
from monitoring.wiring import (
    collect,
    observe_broker,
    observe_data_feed,
    observe_execution,
    observe_reconciliation,
    observe_reconciliation_report,
    observe_session,
    observe_system,
)
from papertrading.engine import PaperTradingEngine
from papertrading.ledger import AccountLedger
from papertrading.models import PaperEngineConfig
from reconciliation.engine import ReconciliationEngine
from risk.engine import RiskEngine
from risk.types import AccountRiskState, RiskConfig
from sessions.manager import MarketSessionManager
from strategies.base.strategy import BaseStrategy

_UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(_UTC)


class MultiAssetPaperOrchestrator:
    """Orquestador global + workers por instrumento (paper, single-account)."""

    def __init__(
        self,
        *,
        run_id: str = "PAPER-MA",
        account_id: str = "PAPER-MA-01",
        account_currency: str = "USD",
        initial_cash: float,
        risk_config: RiskConfig | None = None,
        clock: Callable[[], datetime] = _utc_now,
        intent_journal: IntentJournal | None = None,
        global_data_feed=None,
        global_session=None,
    ) -> None:
        self.run_id = run_id
        self.account_id = account_id
        self.account_currency = account_currency
        self._clock = clock
        # Observaciones GLOBALES de infraestructura (opcionales):
        #  * `global_data_feed(now) -> DataFeedObs`: salud de la infraestructura
        #    de market data COMPARTIDA. Si no se provee, NO se simula HEALTHY:
        #    DATA_FEED global queda UNKNOWN (fail-closed) y bloquea (no se puede
        #    afirmar salud global sin evidencia). La freshness por instrumento la
        #    protege LIVE-03 localmente.
        #  * `global_session(now) -> SessionObs`: veto de sesion realmente GLOBAL
        #    (cuenta/run). Si no se provee, no hay veto global de sesion; las
        #    sesiones por instrumento se gestionan LOCALMENTE en cada worker.
        self._global_data_feed = global_data_feed
        self._global_session = global_session

        # --- servicios GLOBALES (exactamente una instancia) ---
        self.ledger = AccountLedger(
            initial_cash=initial_cash, account_currency=account_currency
        )
        self.order_manager = OrderManager()
        self.risk_engine = RiskEngine(risk_config or RiskConfig())
        self.kill_switch = KillSwitchCoordinator(clock=clock)
        self.monitoring = MonitoringEngine(clock=clock)
        self.reconciliation = ReconciliationEngine()
        self.journal = intent_journal
        self.sessions = MarketSessionManager()
        self.broker = PaperBrokerAdapter(
            account_id=account_id, initial_cash=initial_cash, clock=clock
        )
        self.broker.connect()

        # --- estado compartido de precios y fills ---
        self._prices: dict[str, float] = {}
        self._order_fills: dict[str, tuple] = {}
        # El broker compartido consulta el registro por instrumento.
        self.broker.price_source = lambda instrument: self._prices[instrument]

        # --- estado de cuenta GLOBAL (daily/peak) ---
        self._peak_equity = 0.0
        self._day_key = None
        self._day_start_equity: float | None = None
        self._day_pnl = 0.0
        self._last_close_dt: datetime | None = None
        # Autoridad GLOBAL de monitoring: ultimo exito de broker observado a
        # nivel de CUENTA (no por instrumento).
        self._last_global_broker_success: datetime | None = None
        self._last_global_monitoring = None

        self.workers: dict[str, PaperTradingEngine] = {}
        self._specs: dict[str, InstrumentSpec] = {}

    # ------------------------------------------------------------- registro

    def add_instrument(
        self,
        *,
        instrument: str,
        strategy: BaseStrategy,
        instrument_spec: InstrumentSpec,
        calendar,
    ) -> PaperTradingEngine:
        """Registra un instrumento: strategy + bars + pending + position propios."""
        if instrument in self.workers:
            raise ValueError(f"instrumento ya registrado: {instrument!r}")
        if instrument_spec.atlas_instrument_id != instrument:
            raise ValueError(
                f"instrument_spec id {instrument_spec.atlas_instrument_id!r} != "
                f"instrument {instrument!r}"
            )
        self.ledger.register_instrument(instrument_spec)
        self._specs[instrument] = instrument_spec
        self.sessions.register(instrument, calendar)
        config = PaperEngineConfig(
            account_id=self.account_id,
            instrument=instrument,
            currency=self.account_currency,
            instrument_spec=instrument_spec,
        )
        worker = PaperTradingEngine(
            strategy,
            self.order_manager,
            self.risk_engine,
            self.broker,
            self.sessions,
            self.reconciliation,
            self.kill_switch,
            self.monitoring,
            clock=self._clock,
            run_id=self.run_id,
            config=config,
            ledger=self.ledger,
            intent_journal=self.journal,
            price_registry=self._prices,
            order_fills=self._order_fills,
            risk_state_provider=self._global_risk_state,
            enable_monitoring=False,  # autoridad global unica = orquestador
        )
        self.workers[instrument] = worker
        return worker

    # ------------------------------------------------------------- routing

    @property
    def marks(self) -> dict[str, float]:
        return dict(self._prices)

    def on_closed_bar(
        self,
        closed: ClosedBarEvent,
        *,
        data_state: str = "CONNECTED",
        data_quality: str = "HEALTHY",
    ):
        """Enruta una barra cerrada al worker de su instrumento.

        Un instrumento NO registrado se ignora (no contamina a otros).
        """
        worker = self.workers.get(closed.symbol)
        if worker is None:
            return None
        # MULTI-ASSET-05 (fix): prevalidar SIN efectos secundarios ANTES de tocar
        # el estado compartido. `worker.bar_rejection_reason` cubre LIVE-03
        # (timestamp futuro/invalido) Y la idempotencia del worker
        # (`open_time <= _last_open_time` -> "restart_skip"), por lo que un
        # duplicado o evento antiguo tampoco muta `_prices`/marks ni el estado de
        # valoracion/riesgo. No se duplican reglas y `worker.on_closed_bar` vuelve
        # a validar (defensa en profundidad) conservando su auditoria.
        if worker.bar_rejection_reason(closed) is None:
            # Mark/compartido antes de procesar (valoracion global consistente).
            self._prices[closed.symbol] = float(closed.close)
            self._last_close_dt = datetime.fromtimestamp(closed.close_time, tz=_UTC)
        worker.note_data_feed(data_state, data_quality)
        record = worker.on_closed_bar(closed)
        # Autoridad GLOBAL de monitoring: solo observaciones GLOBALES al kill
        # switch compartido (nunca las locales del worker).
        self._apply_global_monitoring(worker.last_report)
        return record

    def apply_bar_group(self, emissions) -> None:
        """Procesa un GRUPO temporal (mismo `close_time`) con barrera de mercado.

        MULTI-ASSET-06. `emissions`: iterable de objetos con `closed`
        (`ClosedBarEvent` con `symbol == atlas_id`), `data_state`, `data_quality`
        (p. ej. `IngressEmission`). Secuencia:

          1. Prevalidar (no mutante) cada evento (`worker.bar_rejection_reason`).
          2. Actualizar los marks de TODOS los admisibles del grupo.
          3. Evaluar los workers en orden estable por `atlas_id`, aplicando el
             veto global DESPUES DE CADA worker (antes del siguiente).

        Los eventos rechazados NO mutan marks; aun asi se les pasa al worker para
        conservar su auditoria (BAR_SKIPPED/RESTART_SKIP). Unico escritor de
        fuentes globales: el orquestador.
        """
        resolved: list[tuple] = []
        for emission in emissions:
            closed = emission.closed
            worker = self.workers.get(closed.symbol)
            if worker is None:
                continue
            resolved.append((worker, emission))

        # 1-2. Prevalidar y fijar marks de los admisibles ANTES de evaluar.
        max_close_time: int | None = None
        for worker, emission in resolved:
            if worker.bar_rejection_reason(emission.closed) is None:
                self._prices[emission.closed.symbol] = float(emission.closed.close)
                ct = emission.closed.close_time
                max_close_time = ct if max_close_time is None else max(max_close_time, ct)
        if max_close_time is not None:
            self._last_close_dt = datetime.fromtimestamp(max_close_time, tz=_UTC)

        # 3. Evaluar en orden estable por atlas_id; veto global tras cada worker.
        for worker, emission in sorted(
            resolved, key=lambda we: we[1].closed.symbol
        ):
            worker.note_data_feed(emission.data_state, emission.data_quality)
            worker.on_closed_bar(emission.closed)
            self._apply_global_monitoring(worker.last_report)

    def set_global_data_feed(self, provider) -> None:
        """Fija (o limpia con `None`) la observacion GLOBAL de infra de datos.

        MULTI-ASSET-06: permite al driver de replay publicar salud global SOLO
        tras validar los streams; `None` => DATA_FEED UNKNOWN (fail-closed).
        """
        self._global_data_feed = provider

    def _apply_global_monitoring(self, report) -> None:
        """Aplica al kill switch SOLO observaciones GLOBALES de cuenta.

        DATA_FEED global: SOLO desde una observacion explicita de infraestructura
        compartida (`global_data_feed`). Si no se provee, NO se hardcodea HEALTHY:
        queda UNKNOWN (fail-closed) — la llegada de un evento de un instrumento no
        demuestra que toda la infraestructura este sana. La freshness local la
        protege LIVE-03 por instrumento.

        SESSION: la sesion de un calendario es LOCAL (la gestiona cada worker en
        `can_open_positions`). NO se propaga al kill global. Un veto de sesion
        realmente GLOBAL solo alimenta SESSION si se inyecta (`global_session`).

        BROKER/RECONCILIATION/SYSTEM/EXECUTION son de cuenta. Unico escritor.
        """
        now = self._clock()
        if self.broker.health():
            self._last_global_broker_success = now
        if self._global_data_feed is not None:
            data_obs = self._global_data_feed(now)
        else:
            # Sin observacion explicita de infraestructura => UNKNOWN (bloquea).
            data_obs = observe_data_feed(None, None, None)
        broker_obs = observe_broker(
            self.broker,
            last_success=self._last_global_broker_success,
            consecutive_errors=0,
        )
        recon_obs = (
            observe_reconciliation_report(report, self._last_close_dt)
            if report is not None
            else observe_reconciliation(None, None)
        )
        if self._global_session is not None:
            ses_obs = self._global_session(now)
        else:
            # No existe estado global de sesion: no se propaga la sesion local de
            # ningun calendario al kill global (no veto global de sesion).
            ses_obs = observe_session("OPEN", now)
        sys_obs = observe_system(
            now, heartbeat_at=now, persistence_ok=True, unhandled_errors=0
        )
        ex_obs = observe_execution(self.order_manager.orders.values())
        snapshot = self.monitoring.evaluate(
            collect(
                data_feed=data_obs,
                broker=broker_obs,
                reconciliation=recon_obs,
                session=ses_obs,
                system=sys_obs,
                execution=ex_obs,
            )
        )
        apply_to_kill_switch(self.kill_switch, snapshot)
        self._last_global_monitoring = snapshot

    # ---------------------------------------------------- estado global cuenta

    def _global_risk_state(self) -> AccountRiskState:
        """Estado de riesgo GLOBAL (una sola autoridad; no por instrumento)."""
        val = self.ledger.valuation(self._prices)
        equity = val.equity if val is not None else self.ledger.cash
        self._peak_equity = max(self._peak_equity, equity)
        if self._last_close_dt is not None:
            day = self._last_close_dt.date()
            if self._day_key != day:
                self._day_key = day
                self._day_start_equity = equity
        start = self._day_start_equity if self._day_start_equity is not None else equity
        self._day_pnl = equity - start
        peak = self._peak_equity or 1.0
        dd = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        return AccountRiskState(
            equity=equity,
            balance=self.ledger.cash,
            open_positions=len(self.ledger.positions()),
            current_daily_pnl=self._day_pnl,
            current_drawdown=dd,
            kill_switch=False,
            account_active=True,
            strategy_active=True,
        )

    def spec(self, instrument: str) -> InstrumentSpec | None:
        return self._specs.get(instrument)

    @property
    def positions(self):
        return self.ledger.positions()


__all__ = ["MultiAssetPaperOrchestrator"]
