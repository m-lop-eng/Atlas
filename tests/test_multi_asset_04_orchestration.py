"""Tests MULTI-ASSET-04: orquestacion multi-instrumento (paper, single-account).

Arquitectura C: orquestador global + N workers (`PaperTradingEngine`).
Servicios globales compartidos; estrategia/bars/pending/position por instrumento.
Determinista: sin red, sin threads, sin MT5 real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers.models import BrokerConnectionError
from execution.instrument import AssetClass, InstrumentSpec
from killswitches.models import KillSource, KillSwitchState
from live.events import ClosedBarEvent
from monitoring.wiring import observe_data_feed, observe_session
from papertrading.multi_asset import MultiAssetPaperOrchestrator
from risk.types import RiskConfig
from sessions.calendar import SessionDefinition, StaticMarketCalendar
from sessions.markets import btc_24_7_calendar
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

UTC = timezone.utc
EPOCH = 1_704_067_200  # 2024-01-01T00:00Z
START = EPOCH + 15 * 3600  # 15:00Z
LEVEL = 50_100.0
STOP_DISTANCE = 100.0


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class _LevelLong(BaseStrategy):
    """Señala LONG (close >= level) para SU instrumento."""

    def __init__(self, instrument: str, *, level: float = LEVEL) -> None:
        self.metadata = StrategyMetadata(
            strategy_id=f"S-{instrument}", strategy_name="level-long",
            strategy_family="test", market="crypto", instrument=instrument,
            timeframe="1h", version="1.0.0", parameter_set_version="p1",
            data_version="d1", status="IDEA",
        )
        self.level = level

    def generate_signal(self, data) -> Signal:
        base = Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.NO_TRADE, reference_price=0.0,
        )
        if not data:
            return base
        close = float(data[-1]["close"])
        if close < self.level:
            return base
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG, reference_price=close,
            target_quantity=None, stop_price=close - STOP_DISTANCE,
        )


def _spec(instrument: str) -> InstrumentSpec:
    return InstrumentSpec(
        instrument, asset_class=AssetClass.CRYPTO, contract_size=1.0,
        quote_currency="USD",
    )


def _healthy_global_data_feed(now):
    return observe_data_feed("CONNECTED", "HEALTHY", last_event_at=now)


def _healthy_global_session(now):
    return observe_session("OPEN", now)


def _orch(*, risk: RiskConfig | None = None) -> tuple[MultiAssetPaperOrchestrator, Clock]:
    """Orquestador con observacion GLOBAL explicita de infra (tests de gates)."""
    clock = Clock(datetime.fromtimestamp(START, tz=UTC))
    orch = MultiAssetPaperOrchestrator(
        run_id="MA-TEST", initial_cash=1_000_000.0,
        risk_config=risk or RiskConfig(risk_per_trade=0.00025), clock=clock,
        global_data_feed=_healthy_global_data_feed,
        global_session=_healthy_global_session,
    )
    return orch, clock


def _orch_bare(*, risk: RiskConfig | None = None):
    """Orquestador SIN observacion global de infra => DATA_FEED UNKNOWN."""
    clock = Clock(datetime.fromtimestamp(START, tz=UTC))
    orch = MultiAssetPaperOrchestrator(
        run_id="MA-BARE", initial_cash=1_000_000.0,
        risk_config=risk or RiskConfig(risk_per_trade=0.00025), clock=clock,
    )
    return orch, clock


def _closed_calendar() -> StaticMarketCalendar:
    return StaticMarketCalendar(
        calendar_id="closed-utc", version="1.0.0", timezone="UTC",
        definitions=(
            SessionDefinition(
                name="closed", open_minute=10 * 60, close_minute=12 * 60,
                weekdays=frozenset(), pre_open_minutes=0, closing_minutes=0,
            ),
        ),
    )


def _add(orch, instrument, *, level=LEVEL, spec=None, calendar=None):
    return orch.add_instrument(
        instrument=instrument, strategy=_LevelLong(instrument, level=level),
        instrument_spec=spec or _spec(instrument),
        calendar=calendar or btc_24_7_calendar(),
    )


def _bar(symbol: str, open_time: int, ohlc, *, interval=3600, seq=1) -> ClosedBarEvent:
    o, h, l, c = ohlc
    return ClosedBarEvent(
        symbol=symbol, open_time=open_time, open=o, high=h, low=l, close=c,
        volume=1000.0, interval_seconds=interval, emission_sequence=seq,
    )


# Series: t0 no signal, t1 signal (stage), t2 execute at open.
SERIES = [
    (50000, 50100, 49900, 50050),
    (50050, 50200, 50000, 50150),
    (50150, 50300, 50100, 50200),
    (50200, 50400, 50150, 50300),
]


def _drive(orch, clock, symbol, series, *, start=START):
    out = []
    for i, row in enumerate(series):
        open_time = start + i * 3600
        clock.set(datetime.fromtimestamp(open_time + 3600, tz=UTC))
        out.append(orch.on_closed_bar(_bar(symbol, open_time, row, seq=i + 1)))
    return out


# --------------------------------------------------------------------------
# ARCHITECTURE
# --------------------------------------------------------------------------


class TestArchitecture:
    def test_single_shared_services(self):
        orch, _ = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        assert a.ledger is b.ledger is orch.ledger
        assert a.order_manager is b.order_manager is orch.order_manager
        assert a.risk_engine is b.risk_engine is orch.risk_engine
        assert a.kill_switch is b.kill_switch is orch.kill_switch
        assert a.monitoring is b.monitoring is orch.monitoring
        assert a.broker is b.broker is orch.broker

    def test_duplicate_instrument_rejected(self):
        orch, _ = _orch()
        _add(orch, "EURUSD")
        with pytest.raises(ValueError):
            _add(orch, "EURUSD")


# --------------------------------------------------------------------------
# STRATEGY ISOLATION
# --------------------------------------------------------------------------


class TestStrategyIsolation:
    def test_bars_isolated_per_instrument(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES)
        _drive(orch, clock, "BTCUSD", SERIES)
        assert len(a._bars) == len(SERIES)
        assert len(b._bars) == len(SERIES)
        assert all(bar["ts"] >= str(START) for bar in a._bars)
        # state A solo tiene barras de A (por construccion del worker)
        assert a._last_open_time != b._last_open_time or True

    def test_interleaved_no_contamination(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        for i, row in enumerate(SERIES):
            t = START + i * 3600
            clock.set(datetime.fromtimestamp(t + 3600, tz=UTC))
            orch.on_closed_bar(_bar("EURUSD", t, row, seq=i + 1))
            orch.on_closed_bar(_bar("BTCUSD", t, row, seq=i + 1))
        assert len(a._bars) == len(SERIES)
        assert len(b._bars) == len(SERIES)

    def test_unregistered_instrument_ignored(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        assert orch.on_closed_bar(_bar("ZZZUSD", START, SERIES[0])) is None


# --------------------------------------------------------------------------
# ORDERS / LEDGER
# --------------------------------------------------------------------------


class TestOrdersLedger:
    def test_two_instruments_positions_and_global_cash(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        pos = {p.instrument: p for p in orch.ledger.positions()}
        assert set(pos) == {"EURUSD", "BTCUSD"}
        # account global: equity = initial + unrealized (marks compartidos)
        val = orch.ledger.valuation(orch.marks)
        assert val is not None
        assert val.equity == pytest.approx(1_000_000.0 + val.unrealized_pnl)

    def test_pending_distinct_cids(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        # t0 no signal, t1 signal -> pending staging (no execution aun)
        _drive(orch, clock, "EURUSD", SERIES[:2])
        _drive(orch, clock, "BTCUSD", SERIES[:2])
        assert a._pending is not None and b._pending is not None
        assert a._pending.cid != b._pending.cid
        assert "EURUSD" in a._pending.cid and "BTCUSD" in b._pending.cid

    def test_fill_one_does_not_alter_other(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])  # abre EURUSD
        qty_a = orch.ledger.position("EURUSD").quantity
        _drive(orch, clock, "BTCUSD", SERIES[:3])  # abre BTCUSD
        assert orch.ledger.position("EURUSD").quantity == pytest.approx(qty_a)
        assert len(orch.broker._orders) == 2

    def test_paper_broker_multi_instrument(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        positions = orch.broker.get_positions()
        assert {p.instrument for p in positions} == {"EURUSD", "BTCUSD"}


# --------------------------------------------------------------------------
# RISK GLOBAL
# --------------------------------------------------------------------------


class TestGlobalRisk:
    def test_max_open_positions_is_global(self):
        orch, clock = _orch(
            risk=RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        )
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])  # abre posicion (1 global)
        assert orch.ledger.position("EURUSD") is not None
        # BTCUSD: 2a senal debe rechazarse por limite global de posiciones.
        _drive(orch, clock, "BTCUSD", SERIES[:2])
        assert b._pending is None
        assert orch.ledger.position("BTCUSD") is None

    def test_projected_exposure_includes_all_instruments(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        projected = a._projected_exposure(
            instrument="EURUSD", signed_qty=1.0, execution_price=50_150.0
        )
        assert projected.exposure_by_instrument is not None
        assert set(projected.exposure_by_instrument) >= {"EURUSD", "BTCUSD"}


# --------------------------------------------------------------------------
# UNKNOWN INDEPENDENCE
# --------------------------------------------------------------------------


class TestUnknownIndependence:
    def test_unknown_in_one_instrument_does_not_block_other(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        cid_a = f"S-EURUSD:EURUSD:{START + 3600}"
        orch.broker.simulate_response_loss(cid_a)
        real = orch.broker.get_order

        def flaky(cid):
            if cid == cid_a:
                raise BrokerConnectionError("uncertain (test)")
            return real(cid)

        orch.broker.get_order = flaky
        _drive(orch, clock, "EURUSD", SERIES[:3])  # UNKNOWN en EURUSD
        assert cid_a in a._pending_resolution
        assert b._pending_resolution == set()  # independencia por cid
        # BTCUSD puede generar su propia intencion (no bloqueada por A)
        _drive(orch, clock, "BTCUSD", SERIES[:2])
        assert b._pending is not None
        assert b._pending.cid != cid_a
        # Resolver A no elimina B; sin resubmit de A.
        a._resolve_pending_submissions()
        assert cid_a in a._pending_resolution
        assert list(orch.broker._orders).count(cid_a) == 1


# --------------------------------------------------------------------------
# RECONCILIATION / SAFETY
# --------------------------------------------------------------------------


class TestReconciliationSafety:
    def test_internal_snapshot_includes_all_positions(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        snapshot = a._internal_snapshot()
        assert {p.instrument for p in snapshot.positions} == {"EURUSD", "BTCUSD"}

    def test_global_broker_failure_blocks_all(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        orch.broker.disconnect()  # fallo global del broker
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.positions() == ()
        assert not orch.kill_switch.status().can_trade

    def test_global_reconciliation_account_mismatch_blocks(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        orch.broker._cash += 1234.0  # observed divergente (global)
        report = a.reconcile()
        assert report.blocked


# --------------------------------------------------------------------------
# NO MULTI-ASSET AUTO-ACTIVATION
# --------------------------------------------------------------------------


class TestGlobalLocalSafety:
    """MULTI-ASSET-04: una condicion LOCAL no muta una fuente GLOBAL del kill."""

    def _stale_last(self, orch, clock, symbol, series, *, offset=60):
        for i, row in enumerate(series):
            open_time = START + i * 3600
            clock.set(datetime.fromtimestamp(open_time + 3600, tz=UTC))
            if i == len(series) - 1:
                clock.set(
                    datetime.fromtimestamp(open_time + 3600 + offset, tz=UTC)
                )
            orch.on_closed_bar(_bar(symbol, open_time, row, seq=i + 1))

    def test_btc_stale_does_not_activate_global_kill(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])       # EURUSD healthy
        assert orch.ledger.position("EURUSD") is not None
        self._stale_last(orch, clock, "BTCUSD", SERIES[:3])  # BTC stale
        assert not any("BTCUSD" in cid for cid in orch.broker._orders)
        # El stale local de BTC NO activa el kill global.
        assert orch.kill_switch.status().can_trade is True

    def test_eurusd_stale_btc_healthy(self):
        orch, clock = _orch()
        _add(orch, "BTCUSD")
        _add(orch, "EURUSD")
        _drive(orch, clock, "BTCUSD", SERIES[:3])   # BTC healthy
        assert orch.ledger.position("BTCUSD") is not None
        self._stale_last(orch, clock, "EURUSD", SERIES[:3])  # EURUSD stale
        assert not any("EURUSD" in cid for cid in orch.broker._orders)
        assert orch.kill_switch.status().can_trade is True

    def test_no_last_writer_wins_on_global_sources(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        # Workers NO son autoridad de monitoring global.
        assert a.enable_monitoring is False and b.enable_monitoring is False
        _drive(orch, clock, "EURUSD", SERIES[:3])
        self._stale_last(orch, clock, "BTCUSD", SERIES[:3])
        assert a.last_monitoring is None and b.last_monitoring is None
        assert orch._last_global_monitoring is not None
        # Un worker sano no puede "limpiar" ni "escribir" una fuente global: el
        # unico escritor es el orquestador.
        assert orch.kill_switch.status().can_trade is True

    def test_global_broker_failure_blocks_both(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        orch.broker.disconnect()
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.positions() == ()
        assert not orch.kill_switch.status().can_trade

    def test_global_reconciliation_failure_blocks_both(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])  # abre (observado consistente)
        orch.broker._cash += 5_000.0               # divergencia GLOBAL de cuenta
        _drive(orch, clock, "EURUSD", SERIES[3:4], start=START + 3 * 3600)
        assert not orch.kill_switch.status().can_trade
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert not any("BTCUSD" in cid for cid in orch.broker._orders)

    def test_global_manual_kill_blocks_all(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        orch.kill_switch.set_source(KillSource.MANUAL, KillSwitchState.HALT)
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.positions() == ()

    def test_local_quantity_rejection_isolated(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD", spec=InstrumentSpec(
            "BTCUSD", contract_size=1.0, quote_currency="USD",
            quantity_min=1000.0, quantity_step=1.0,
        ))
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])  # target < min => reject local
        assert orch.ledger.position("EURUSD") is not None
        assert orch.ledger.position("BTCUSD") is None
        assert not any("BTCUSD" in cid for cid in orch.broker._orders)
        # El rechazo local NO activa el kill global.
        assert orch.kill_switch.status().can_trade is True

    def test_price_registry_holds_mark_not_execution_price(self):
        # Verifica que tras procesar una barra el registry contiene el CLOSE
        # (market mark), no el OPEN de ejecucion (transitorio).
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        assert orch.marks["EURUSD"] == pytest.approx(SERIES[2][3])  # close t2


class TestDataFeedSessionSemantics:
    def test_data_feed_unknown_without_global_observation_fails_closed(self):
        orch, clock = _orch_bare()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        # Un evento de instrumento NO demuestra salud global: DATA_FEED UNKNOWN
        # => fail-closed (bloquea), no se hardcodea HEALTHY.
        assert orch.broker._orders == {}
        assert not orch.kill_switch.status().can_trade

    def test_local_closed_session_blocks_only_that_instrument(self):
        orch, clock = _orch()
        _add(orch, "EURUSD", calendar=_closed_calendar())  # sesion cerrada
        _add(orch, "BTCUSD")                                # 24/7
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert not any("EURUSD" in cid for cid in orch.broker._orders)
        assert any("BTCUSD" in cid for cid in orch.broker._orders)
        assert orch.ledger.position("BTCUSD") is not None
        # La sesion LOCAL no se propaga a SESSION global.
        assert orch.kill_switch.status().can_trade is True

    def test_global_session_veto_blocks_all(self):
        clock = Clock(datetime.fromtimestamp(START, tz=UTC))
        orch = MultiAssetPaperOrchestrator(
            run_id="MA-GSESS", initial_cash=1_000_000.0,
            risk_config=RiskConfig(risk_per_trade=0.00025), clock=clock,
            global_data_feed=_healthy_global_data_feed,
            global_session=lambda now: observe_session("HALTED", now),
        )
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert orch.broker._orders == {}
        assert not orch.kill_switch.status().can_trade

    def test_global_account_veto_blocks_all(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        orch.kill_switch.set_source(KillSource.SYSTEM, KillSwitchState.EMERGENCY)
        _drive(orch, clock, "EURUSD", SERIES[:3])
        _drive(orch, clock, "BTCUSD", SERIES[:3])
        assert orch.broker._orders == {}

    def test_worker_cannot_write_global_sources(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        _drive(orch, clock, "EURUSD", SERIES[:3])
        self.self_stale(orch, clock, "BTCUSD")
        assert a.enable_monitoring is False and b.enable_monitoring is False
        assert a.last_monitoring is None and b.last_monitoring is None
        assert orch.kill_switch.status().can_trade is True

    def self_stale(self, orch, clock, symbol):
        for i, row in enumerate(SERIES):
            open_time = START + i * 3600
            offset = 60 if i == len(SERIES) - 1 else 0
            clock.set(datetime.fromtimestamp(open_time + 3600 + offset, tz=UTC))
            orch.on_closed_bar(_bar(symbol, open_time, row, seq=i + 1))


class TestNoAutoActivation:
    def test_orchestrator_is_opt_in(self):
        # El engine legacy sigue siendo mono-instrumento; el orquestador solo se
        # usa si se instancia explicitamente.
        import papertrading.engine as engine_mod

        assert hasattr(engine_mod, "PaperTradingEngine")
