"""Tests de Paper Trading (B7): plataforma paper del sistema completo.

Batería acordada (B7):
  1. Flujo completo barra -> señal -> riesgo -> orden -> fill -> posición -> salida
     con audit trail reconstruible de extremo a extremo.
  2. Sesión CLOSED => nunca llega una orden al broker.
  3. RiskEngine rechaza => el broker nunca recibe la orden.
  4. Kill switch activo => el broker nunca recibe la orden.
  5. Reconciliación UNKNOWN => nuevas órdenes bloqueadas (fail-safe).
  6. Submission UNKNOWN del broker => resolución posterior SIN duplicación.
  7. Fill parcial => posición correcta.
  8. Múltiples fills => precio medio (VWAP) correcto.
  9. Restart => se recupera sin duplicar exposición ni reprocesar barras viejas.
 10. Duplicados / out-of-order / gap (B1) => la estrategia no procesa barra
     inválida.
 11. Stale data => bloqueo vía Monitoring -> Kill Switch.
 12. CLOSING/CLOSED no liquidan posiciones automáticamente.
 13. Reconciliación tras cada ejecución (report + diffs vacíos).
 14. Monitoring detecta fallo => B5 bloquea; health recuperado => B5 lachado;
     recovery explícito => volver a operar.
 15. P&L / equity deterministas (la vuelta completa cierra en el valor exacto).
 16. Mismo input => mismo resultado (two runs, mismas curvas/audit/P&L).
 17. Sin lookahead: la entrada se ejecuta en el OPEN de t+1, nunca antes.
 18. Ninguna llamada a broker real: el engine exige PaperBrokerAdapter.
 19. FINAL_OOS (perímetro congelado) no se toca.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest

from brokers.models import (
    BrokerFill,
    BrokerOrderState,
    OrderSubmission,
    SubmitStatus,
)
from brokers.paper import PaperBrokerAdapter
from execution.order import Fill, Order, OrderSide, OrderStatus
from execution.order_manager import OrderManager
from killswitches.coordinator import KillSwitchCoordinator
from killswitches.models import KillSource, KillSwitchState
from live.adapter import MarketDataAdapter
from live.engine import EngineState, LiveDataEngine
from live.events import ClosedBarEvent, MarketDataEvent
from monitoring.engine import MonitoringEngine
from monitoring.models import HealthStatus, MonitorComponent, MonitorConfig
from papertrading.engine import PaperTradingEngine
from papertrading.models import PaperEngineConfig, PaperEvent, PaperPosition
from papertrading.replay import ReplayMarketDataAdapter
from reconciliation.engine import ReconciliationEngine
from reconciliation.models import (
    DiscrepancyClass,
    ReconciliationStatus,
)
from risk.engine import RiskEngine
from risk.types import RiskConfig
from sessions.calendar import SessionDefinition, StaticMarketCalendar
from sessions.manager import MarketSessionManager, SessionState
from sessions.markets import btc_24_7_calendar
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

UTC = timezone.utc
EPOCH = 1_704_067_200  # 2024-01-01T00:00Z
START = EPOCH + 15 * 3600  # 15:00Z (alineado a intervalos de 3600s)
T0 = datetime.fromtimestamp(START, tz=UTC)
INSTRUMENT = "BTCUSDT"

# Happy path: decisión LONG en B2 (close >= nivel), entrada al open de B3,
# salida por STOP en B6. Sin lookahead en ningún momento.
HAPPY = [
    (50000, 50100, 49900, 50050),  # B1 close 50050  (< 50100 -> NO_TRADE)
    (50050, 50200, 50000, 50150),  # B2 close 50150  (>= 50100 -> LONG)
    (50150, 50300, 50100, 50200),  # B3 open 50150   -> entrada
    (50200, 50400, 50150, 50300),  # B4 mantiene (stop 50050)
    (50300, 50500, 50200, 50400),  # B5 mantiene
    (50050, 50150, 49950, 50000),  # B6 low 49950 <= stop 50050 -> STOP
]

LEVEL = 50_100.0
STOP_DISTANCE = 100.0


class Clock:
    """Reloj mutable y determinista compartido por todo el stack."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class _LevelLong(BaseStrategy):
    """Señala LONG cada barra cuyo close >= `level` (stop a `stop_distance`)."""

    def __init__(
        self,
        level: float = LEVEL,
        stop_distance: float = STOP_DISTANCE,
        stop: bool = True,
    ) -> None:
        self.metadata = StrategyMetadata(
            strategy_id="TEST-LONG",
            strategy_name="level-long",
            strategy_family="test",
            market="crypto",
            instrument=INSTRUMENT,
            timeframe="1h",
            version="1.0.0",
            parameter_set_version="p1",
            data_version="d1",
            status="IDEA",
        )
        self.level = level
        self.stop_distance = stop_distance
        self.with_stop = stop
        self.calls = 0

    def generate_signal(self, data) -> Signal:
        self.calls += 1
        signal = Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.NO_TRADE,
            reference_price=0.0,
        )
        if not data:
            return signal
        last = data[-1]
        close = float(last["close"])
        base = Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=close,
            target_quantity=None,
            stop_price=(
                close - self.stop_distance if self.with_stop else None
            ),
        )
        return base if close >= self.level else signal


def _calendar(
    open_minute: int = 10 * 60,
    close_minute: int = 12 * 60,
    closing_minutes: int = 0,
) -> StaticMarketCalendar:
    """Calendario de día en UTC (configurable para CLOSED/CLOSING)."""
    return StaticMarketCalendar(
        calendar_id="day-utc",
        version="1.0.0",
        timezone="UTC",
        definitions=(
            SessionDefinition(
                name="core",
                open_minute=open_minute,
                close_minute=close_minute,
                weekdays=frozenset(),
                pre_open_minutes=0,
                closing_minutes=closing_minutes,
            ),
        ),
    )


def _closed_bar(
    open_time: int,
    ohlc: tuple[float, float, float, float],
    *,
    interval: int = 3600,
    seq: int = 1,
) -> ClosedBarEvent:
    o, h, l, c = ohlc
    return ClosedBarEvent(
        symbol=INSTRUMENT,
        open_time=open_time,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1000.0,
        interval_seconds=interval,
        emission_sequence=seq,
    )


def _stack(
    *,
    strategy: BaseStrategy | None = None,
    clock: Clock | None = None,
    calendar: StaticMarketCalendar | None = None,
    initial_cash: float = 1_000_000.0,
    monitor_config: MonitorConfig | None = None,
) -> SimpleNamespace:
    clock = clock or Clock(T0)
    broker = PaperBrokerAdapter(initial_cash=initial_cash, clock=clock)
    sessions = MarketSessionManager()
    sessions.register(INSTRUMENT, calendar or btc_24_7_calendar())
    order_manager = OrderManager()
    risk = RiskEngine(
        RiskConfig(risk_per_trade=0.00025)  # 1M*0.00025/100pts = 2.5 qty
    )
    reconciliation = ReconciliationEngine()
    kill_switch = KillSwitchCoordinator(clock=clock)
    monitoring = MonitoringEngine(
        clock=clock, config=monitor_config or MonitorConfig()
    )
    engine = PaperTradingEngine(
        strategy or _LevelLong(),
        order_manager,
        risk,
        broker,
        sessions,
        reconciliation,
        kill_switch,
        monitoring,
        clock=clock,
        config=PaperEngineConfig(instrument=INSTRUMENT, account_id="PAPER-01"),
    )
    return SimpleNamespace(
        engine=engine,
        broker=broker,
        order_manager=order_manager,
        risk=risk,
        reconciliation=reconciliation,
        kill_switch=kill_switch,
        monitoring=monitoring,
        sessions=sessions,
        clock=clock,
        strategy=strategy or engine.strategy,
    )


def _drive(
    env: SimpleNamespace,
    ohlc: list[tuple[float, float, float, float]],
    *,
    start: int = START,
    interval: int = 3600,
) -> list[ClosedBarEvent]:
    bars = []
    for i, row in enumerate(ohlc):
        open_time = start + i * interval
        bar = _closed_bar(open_time, row, interval=interval, seq=i + 1)
        env.clock.set(datetime.fromtimestamp(open_time + interval, tz=UTC))
        env.engine.on_closed_bar(bar)
        bars.append(bar)
    return bars


def _audit_events(env: SimpleNamespace) -> list[str]:
    return [e.event for e in env.engine.audit_log]


class TestFullFlow:
    def test_full_flow_position_pnl_and_audit_trail(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        bars = _drive(env, HAPPY)

        assert env.engine.position is None
        assert len(env.engine.trades) == 1
        trade = env.engine.trades[0]
        assert trade.side == "LONG"
        assert trade.exit_reason == "STOP"
        assert trade.entry_price == pytest.approx(HAPPY[2][0])  # open B3
        # stop = close de la barra de decisión (B2) - distancia.
        assert trade.exit_price == pytest.approx(HAPPY[1][3] - STOP_DISTANCE)
        assert trade.quantity == pytest.approx(2.5)
        assert trade.pnl == pytest.approx(-250.0)
        assert env.engine.equity_curve[-1][1] == pytest.approx(999_750.0)

        # audit trail: la cadena completa está presente con IDs correlacionados.
        events = _audit_events(env)
        for expected in (
            PaperEvent.BAR_CLOSED.value,
            PaperEvent.SIGNAL.value,
            PaperEvent.RISK_DECISION.value,
            PaperEvent.ENTRY_STAGED.value,
            PaperEvent.ENTRY_SUBMITTED.value,
            PaperEvent.ENTRY_FILLED.value,
            PaperEvent.POSITION_OPENED.value,
            PaperEvent.EXIT_STOP.value,
            PaperEvent.EXIT_SUBMITTED.value,
            PaperEvent.EXIT_FILLED.value,
            PaperEvent.POSITION_CLOSED.value,
            PaperEvent.RECONCILIATION.value,
            PaperEvent.MONITORING.value,
        ):
            assert expected in events

        cid = f"TEST-LONG:{INSTRUMENT}:{bars[1].open_time}"
        trail = env.engine.trail("signal_id", cid)
        assert len(trail) >= 6
        trail_events = [t.event for t in trail]
        assert PaperEvent.ENTRY_SUBMITTED.value in trail_events
        assert PaperEvent.EXIT_FILLED.value in trail_events

        # Sin duplicación en el broker: exactamente una entrada + una salida.
        assert set(env.broker._orders) == {cid, f"EXIT:{cid}"}

    def test_no_duplicate_orders_at_broker(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        assert len(env.broker._orders) == 2  # solo ENTRY + EXIT

    def test_reconciliation_after_each_execution(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        bars = _drive(env, HAPPY)
        assert env.engine.last_report is not None
        assert env.engine.last_report.status == ReconciliationStatus.RECONCILIATION_OK
        n_recon = sum(1 for e in env.engine.audit_log
                      if e.event == PaperEvent.RECONCILIATION.value)
        assert n_recon == len(bars)
        # Sin discrepancia material: solo clasificaciones MATCH.
        assert env.engine.last_report.status == ReconciliationStatus.RECONCILIATION_OK
        assert all(
            d.classification == DiscrepancyClass.MATCH
            for d in env.engine.last_report.diffs
        )


class TestNoLookahead:
    def test_entry_price_is_open_of_next_bar(self):
        strategy = _LevelLong(level=-1.0)  # LONG a partir de la primera barra
        env = _stack(strategy=strategy)
        # B1 close 50090 decide LONG; debe ejecutarse al OPEN de B2 (50200),
        # nunca al close de B1 (50090).
        bars = [
            (50000, 50100, 49900, 50090),
            (50200, 50300, 50100, 50250),
        ]
        _drive(env, bars)
        pos = env.engine.position
        assert pos is not None
        assert pos.avg_entry_price == pytest.approx(bars[1][0])
        assert strategy.calls == 2  # una decisión por barra cerrada
        assert env.engine.trades == ()


class TestGates:
    def test_session_closed_never_reaches_broker(self):
        env = _stack(
            strategy=_LevelLong(level=-1.0),
            calendar=_calendar(),  # 10:00-12:00 UTC; barras a las 15:00
        )
        bars = [
            (50000, 50100, 49900, 50050),
            (50100, 50200, 50050, 50150),
            (50200, 50300, 50150, 50250),
        ]
        start = EPOCH + 15 * 3600  # 15:00Z
        _drive(env, bars, start=start)
        assert env.broker._orders == {}
        assert env.engine.position is None
        events = _audit_events(env)
        assert any(
            e == PaperEvent.ENTRY_SKIPPED_SESSION.value for e in events
        )
        assert PaperEvent.POSITION_OPENED.value not in events

    def test_risk_rejection_never_reaches_broker(self):
        # Sin stop => riesgo no cuantificable => REJECTED.
        env = _stack(strategy=_LevelLong(level=LEVEL, stop=False))
        _drive(env, HAPPY[:3])
        assert env.broker._orders == {}
        events = _audit_events(env)
        assert PaperEvent.RISK_REJECTED.value in events
        assert PaperEvent.ENTRY_SUBMITTED.value not in events

    def test_active_kill_switch_never_reaches_broker(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.kill_switch.set_source(
            KillSource.MANUAL, KillSwitchState.HALT, detail="test"
        )
        _drive(env, HAPPY[:3])
        assert env.broker._orders == {}
        assert not env.kill_switch.status().can_trade
        events = _audit_events(env)
        assert any(
            e == PaperEvent.ENTRY_SKIPPED_KILL_SWITCH.value for e in events
        )
        assert PaperEvent.ENTRY_SUBMITTED.value not in events


class TestUnknownSubmission:
    def test_UNKNOWN_submission_resolved_later_without_duplication(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        # La decisión LONG ocurre en B2 -> cid determinista para la entrada.
        cid = f"TEST-LONG:{INSTRUMENT}:{START + 3600}"
        env.broker.simulate_response_loss(cid)
        bars = _drive(env, HAPPY[:3])

        # La decisión (B2) se envió y el broker la llenó, pero la respuesta se
        # perdió -> UNKNOWN. La posición NO se fabrica sin confirmar.
        assert PaperEvent.SUBMISSION_UNKNOWN.value in _audit_events(env)

        # B3: resolución explícita -> la posición adopta el fill del broker.
        pos = env.engine.position
        assert pos is not None
        assert pos.avg_entry_price == pytest.approx(HAPPY[2][0])  # open B3
        assert pos.quantity == pytest.approx(2.5)
        assert len(env.broker._orders) == 1  # una sola orden, sin reenvío
        events = _audit_events(env)
        assert PaperEvent.SUBMISSION_RESOLVED.value in events
        # No se encadenó una segunda exposición.
        assert events.count(PaperEvent.ENTRY_SUBMITTED.value) == 1

    def test_UNKNOWN_reconciliation_blocks_new_orders(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])
        env.broker.disconnect()
        report = env.engine.reconcile()
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN
        env.engine.run_monitoring()
        assert not env.kill_switch.status().can_trade
        assert env.kill_switch.status().state in (
            KillSwitchState.HALT,
            KillSwitchState.BLOCK_NEW_ORDERS,
        )


class TestFills:
    def _multi_fill_broker(self, fills: list[tuple[float, float]], clock: Clock):
        class _FillSetBroker(PaperBrokerAdapter):
            def __init__(self):  # noqa: D501
                super().__init__(initial_cash=1_000_000.0, clock=clock)
                self._prev = None

            def submit_order(self, order):
                if order.client_order_id in self._orders:
                    return OrderSubmission(
                        client_order_id=order.client_order_id,
                        status=SubmitStatus.CONFIRMED,
                        broker_order=self._orders[order.client_order_id],
                        detail="idempotente",
                    )
                now = self.clock()
                bid = f"BRK-{self._broker_seq_num():06d}"
                fs = tuple(BrokerFill(bid, q, p, now) for q, p in fills)
                total = sum(q for q, _ in fills)
                cost = sum(q * p for q, p in fills)
                if order.side.value == "BUY":
                    self._cash -= cost
                else:
                    self._cash += cost
                avg = cost / total
                state = (
                    BrokerOrderState.FILLED
                    if total >= order.quantity - 1e-9
                    else BrokerOrderState.PARTIALLY_FILLED
                )
                bo = self._build(order, bid, now, state, total, avg, fs)
                self._orders[order.client_order_id] = bo
                self._prev = bo
                return OrderSubmission(
                    client_order_id=order.client_order_id,
                    status=SubmitStatus.CONFIRMED,
                    broker_order=bo,
                    detail="OK",
                )

            def _broker_seq_num(self):  # pragma: no cover - helper
                if self._prev is None:
                    self._seq_i = 0
                self._seq_i += 1
                return self._seq_i

            def _build(self, order, bid, now, state, total, avg, fs):
                return self._new_order(
                    order, bid, now, state,
                    filled_quantity=total, avg_fill_price=avg, fills=fs,
                )

        return _FillSetBroker()

    def _engine_with(self, fills):
        clock = Clock(T0)
        broker = self._multi_fill_broker(fills, clock)
        sessions = MarketSessionManager()
        sessions.register(INSTRUMENT, btc_24_7_calendar())
        engine = PaperTradingEngine(
            _LevelLong(level=LEVEL),
            OrderManager(),
            RiskEngine(RiskConfig(risk_per_trade=0.00025)),
            broker,
            sessions,
            ReconciliationEngine(),
            KillSwitchCoordinator(clock=clock),
            MonitoringEngine(clock=clock),
            clock=clock,
            config=PaperEngineConfig(instrument=INSTRUMENT),
        )
        return SimpleNamespace(
            engine=engine, broker=broker, clock=clock, sessions=sessions
        )

    def test_partial_fill_correct_position(self):
        env = self._engine_with([(1.0, 50_100.0)])
        _drive(env, HAPPY[:4])
        pos = env.engine.position
        assert pos is not None
        assert pos.quantity == pytest.approx(1.0)
        assert pos.avg_entry_price == pytest.approx(50_100.0)
        # La posición parcial queda a mercado y reconciliada.
        assert env.engine.last_report.status == (
            ReconciliationStatus.RECONCILIATION_OK
        )

    def test_multiple_fills_vwap_price(self):
        env = self._engine_with([(1.0, 50_100.0), (1.5, 50_130.0)])
        _drive(env, HAPPY[:4])
        pos = env.engine.position
        assert pos is not None
        expected_vwap = (1.0 * 50_100.0 + 1.5 * 50_130.0) / 2.5
        assert pos.quantity == pytest.approx(2.5)
        assert pos.avg_entry_price == pytest.approx(expected_vwap)


class TestRestart:
    def test_restart_recovers_without_duplicate_exposure(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        assert snap["position"] is None
        assert len(snap["signed_signals"]) == 2

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        # Reprocesar exactamente las mismas barras: todas se ignoran.
        _drive(env2, HAPPY)
        assert env2.broker._orders == {}          # sin órdenes nuevas
        assert env2.engine.trades == env1.engine.trades
        assert PaperEvent.RESTART_SKIP.value in _audit_events(env2)

        # Barras FUTURAS: la primera decide, la segunda ejecuta UNA orden
        # nueva y NO duplica la exposición histórica (y no se cierra: el
        # stop nuevo queda por debajo de los mínimos de las barras).
        future = [
            (50700, 50900, 50600, 50800),
            (50800, 51000, 50750, 50900),
        ]
        start = START + 6 * 3600
        _drive(env2, future, start=start, interval=3600)
        new_cid = f"TEST-LONG:{INSTRUMENT}:{start}"
        assert set(env2.broker._orders) == {new_cid}
        assert env2.engine.position is not None
        assert len(env2.engine.trades) == 1  # solo la operación histórica

    def test_restart_with_open_position_preserves_it(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY[:3])  # deja la posición abierta
        assert env1.engine.position is not None
        snap = env1.engine.snapshot()

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        assert env2.engine.position is not None
        assert env2.engine.position.quantity == (
            env1.engine.position.quantity
        )
        # Una barra nueva mientras se mantiene la posición: sin nueva entrada.
        _drive(env2, HAPPY[3:4], start=START + 3 * 3600)
        assert env2.broker._orders == {}


class TestDataFeedGuards:
    def test_duplicate_closed_bar_no_double_exposure(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        bar = _closed_bar(START, HAPPY[0])
        env.clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        env.engine.on_closed_bar(bar)
        env.clock.set(datetime.fromtimestamp(START + 7200, tz=UTC))
        env.engine.on_closed_bar(bar)  # duplicado
        assert str(bar.open_time) in [b["ts"] for b in env.engine._bars]
        assert env.engine._bars.count(
            {"ts": str(START), "open": 50000, "high": 50100, "low": 49900,
             "close": 50050, "volume": 1000.0}
        ) == 1
        events = _audit_events(env)
        assert events.count(PaperEvent.RESTART_SKIP.value) == 1
        assert env.broker._orders == {}

    def _live_stack(self, events: list[MarketDataEvent]):
        env = _stack(strategy=_LevelLong(level=-1.0))
        adapter = ReplayMarketDataAdapter(events)
        live = LiveDataEngine(
            INSTRUMENT,
            3600,
            adapter,
            on_closed_bar=env.engine.on_closed_bar,
            clock=lambda: float(env.clock().timestamp()),
        )
        live.start()
        return env, live

    def test_live_gap_blocks_strategy(self):
        bar_t = ClosedBarEvent(
            INSTRUMENT, START, 50000, 50100, 49900, 50050, 1000.0, 3600, 1
        )
        events = [
            MarketDataEvent(INSTRUMENT, bar_t.open_time,
                            bar_t.open, bar_t.high, bar_t.low, bar_t.close,
                            1000.0, True),
            # Salta START+3600: llega START+7200 => GAP.
            MarketDataEvent(INSTRUMENT, START + 2 * 3600,
                            50200, 50300, 50100, 50250, 1000.0, True),
        ]
        env, live = self._live_stack(events)
        env.clock.set(datetime.fromtimestamp(START + 2 * 3600 + 3600, tz=UTC))
        live.poll()

        assert live.state == EngineState.DEGRADED
        closed_opens = {c.open_time for c in live.closed_bars}
        assert START in closed_opens
        assert START + 2 * 3600 not in closed_opens
        assert env.broker._orders == {}

    def test_live_out_of_order_blocked(self):
        ev1 = MarketDataEvent(INSTRUMENT, START + 3600, 50050, 50150,
                              50000, 50100, 1000.0, True)
        ev2 = MarketDataEvent(INSTRUMENT, START, 50000, 50100, 49900,
                              50050, 1000.0, True)
        env, live = self._live_stack([ev1, ev2])
        env.clock.set(datetime.fromtimestamp(START + 2 * 3600, tz=UTC))
        live.poll()
        assert any(q.value == "OUT_OF_ORDER" for q, _ in live.issues)
        # El segundo evento (inválido) no se convirtió en ClosedBar ni se
        # envió a la estrategia.
        assert all(b["ts"] != str(START) for b in env.engine._bars)

    def test_stale_data_blocks_via_monitoring_b5(self):
        env = _stack(strategy=_LevelLong(level=-1.0))
        _drive(env, [HAPPY[0]])
        assert env.kill_switch.status().can_trade
        # Envejece el último evento de mercado más allá del umbral.
        now = env.clock()
        env.engine._last_bar_close_dt = now - timedelta(seconds=2000)
        env.engine.run_monitoring(now=now)
        assert not env.kill_switch.status().can_trade
        assert env.kill_switch.status().state == KillSwitchState.HALT
        snap = env.engine.last_monitoring
        data_result = snap.get(MonitorComponent.DATA_FEED)
        assert data_result.status == HealthStatus.FAILED


class TestSessionNoAutoLiquidation:
    def test_closing_and_closed_do_not_liquidate(self):
        env = _stack(
            strategy=_LevelLong(level=-1.0),
            calendar=_calendar(closing_minutes=10),  # 10:00-12:00 + 10min
        )
        # Barras de 30 min: 10:30 OPEN, 11:00 OPEN (entrada), 11:30 OPEN,
        # 12:00 CLOSING, 12:30 CLOSED, 13:00 CLOSED.
        rows = [
            (50000, 50100, 49900, 50050),  # 10:30
            (50050, 50200, 50000, 50150),  # 11:00 entrada
            (50150, 50300, 50100, 50200),  # 11:30
            (50200, 50300, 50150, 50250),  # 12:00 CLOSING
            (50250, 50350, 50200, 50300),  # 12:30 CLOSED
            (50300, 50400, 50250, 50350),  # 13:00 CLOSED
        ]
        start = EPOCH + 10 * 3600 + 30 * 60  # 10:30Z (múltiplo de 1800)
        _drive(env, rows, start=start, interval=1800)

        pos = env.engine.position
        assert pos is not None  # sin liquidación automática
        assert pos.quantity == pytest.approx(2.5)
        events = _audit_events(env)
        assert PaperEvent.EXIT_STOP.value not in events
        assert PaperEvent.EXIT_TIME.value not in events
        assert PaperEvent.EXIT_SUBMITTED.value not in events
        assert PaperEvent.POSITION_CLOSED.value not in events


class TestMonitoringKillSwitchRecovery:
    def test_fail_then_recover_then_trade_again(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, [HAPPY[0]])
        assert env.kill_switch.status().can_trade

        # El broker cae -> monitoring bloquea vía B5.
        env.broker.disconnect()
        _drive(env, [HAPPY[1]], start=START + 3600)
        assert not env.kill_switch.status().can_trade
        assert env.kill_switch.status().state == KillSwitchState.HALT

        # El broker se recupera, pero B5 permanece latch (recovery requerido).
        env.broker.connect()
        _drive(env, [HAPPY[2]], start=START + 2 * 3600)
        assert env.kill_switch.status().state == (
            KillSwitchState.BLOCK_NEW_ORDERS
        )
        assert not env.kill_switch.status().can_trade

        # R1: recovery EXPLÍCITO -> volver a operar.
        result = env.kill_switch.recover(reason="health checks ok")
        assert result.accepted
        assert env.kill_switch.status().can_trade

        # La siguiente barra abre la posición de nuevo.
        _drive(env, [HAPPY[3]], start=START + 3 * 3600)
        assert env.engine.position is not None


class TestDeterminism:
    def test_same_input_same_result(self):
        def run():
            env = _stack(strategy=_LevelLong(level=LEVEL))
            _drive(env, HAPPY)
            return env

        a, b = run(), run()
        assert a.engine.trades == b.engine.trades
        assert a.engine.equity_curve == b.engine.equity_curve
        assert [e.event for e in a.engine.audit_log] == [
            e.event for e in b.engine.audit_log
        ]
        assert [t.pnl for t in a.engine.trades] == [t.pnl for t in b.engine.trades]

    def test_pnl_and_equity_deterministic(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        assert env.engine.trades[0].pnl == pytest.approx(-250.0)
        assert env.engine.equity_curve[-1][1] == pytest.approx(999_750.0)


class TestPaperOnly:
    def test_type_error_when_broker_has_no_price_source(self):
        class _RealLikeBroker:
            def __init__(self):
                self.market = "LIVE"

            def health(self):
                return True

        with pytest.raises(TypeError):
            PaperTradingEngine(
                _LevelLong(),
                OrderManager(),
                RiskEngine(RiskConfig()),
                _RealLikeBroker(),
                MarketSessionManager(),
                ReconciliationEngine(),
                KillSwitchCoordinator(),
                MonitoringEngine(),
            )


class TestFrozenPerimeter:
    def test_final_oos_untouched(self):
        root = Path(__file__).resolve().parents[1]
        frozen = root / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
        assert frozen.exists()
        assert "FROZEN" in frozen.read_text(encoding="utf-8")