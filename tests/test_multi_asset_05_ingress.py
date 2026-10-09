"""Tests MULTI-ASSET-05: ingress, routing y observabilidad multiinstrumento.

Vía: `MarketDataEvent(raw) -> LiveDataEngine (B1, autoridad) -> dispatcher ->
worker`. Reutiliza LIVE-03; sin reimplementar dedup/orden/gap. Determinista:
sin red, sin threads, sin MT5.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from execution.instrument import AssetClass, InstrumentSpec
from killswitches.models import KillSource, KillSwitchState
from live.adapter import MarketDataAdapter
from live.engine import EngineState
from live.events import ClosedBarEvent, MarketDataEvent
from monitoring.models import MonitorComponent
from papertrading.multi_asset_ingress import (
    IngressConfigError,
    InstrumentStreamConfig,
    MultiAssetIngress,
)
from risk.types import RiskConfig

from tests.test_multi_asset_04_orchestration import (  # noqa: E402
    SERIES,
    START,
    Clock,
    _add,
    _closed_calendar,
    _orch,
    _orch_bare,
    _spec,
)

UTC = timezone.utc


class _StubAdapter(MarketDataAdapter):
    def __init__(self) -> None:
        self._connected = False
        self.symbol = None
        self.interval_seconds = None

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbol, interval_seconds) -> None:
        self.symbol = symbol
        self.interval_seconds = interval_seconds

    def receive(self):
        return None


def _stream(atlas_id, *, market_symbol=None, interval=3600, spec=None, broker=None):
    return InstrumentStreamConfig(
        atlas_id=atlas_id,
        market_data_symbol=market_symbol or atlas_id,
        interval_seconds=interval,
        instrument_spec=spec or _spec(atlas_id),
        broker_symbol=broker,
    )


def _ingress(orch, mapping, *, engine_clock=None):
    streams = [(_stream(a, market_symbol=m), _StubAdapter()) for a, m in mapping.items()]
    return MultiAssetIngress(orch, streams, engine_clock=engine_clock)


def _raw(symbol, open_time, ohlc, *, interval=3600, is_closed=True):
    o, h, l, c = ohlc
    return MarketDataEvent(
        symbol=symbol, open_time=open_time, open=o, high=h, low=l, close=c,
        volume=1000.0, is_closed=is_closed,
    )


def _feed(ingress, clock, raw_symbol, series, *, start=START, interval=3600):
    for i, row in enumerate(series):
        ot = start + i * interval
        clock.set(datetime.fromtimestamp(ot + interval, tz=UTC))
        ingress.receive(_raw(raw_symbol, ot, row, interval=interval))


# --------------------------------------------------------------------------
# 1-2. INTERCALADO / ROUTING / IDENTIDAD
# --------------------------------------------------------------------------


class TestRouting:
    def test_interleaved_ab_ab(self):
        orch, clock = _orch()
        ea = _add(orch, "EURUSD")
        eb = _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        for i, row in enumerate(SERIES):
            t = START + i * 3600
            clock.set(datetime.fromtimestamp(t + 3600, tz=UTC))
            ing.receive(_raw("EURUSD", t, row))
            ing.receive(_raw("BTCUSD", t, row))
        assert len(ea._bars) == len(SERIES)
        assert len(eb._bars) == len(SERIES)
        assert orch.ledger.position("EURUSD") is not None
        assert orch.ledger.position("BTCUSD") is not None

    def test_identity_normalization_feed_symbol_differs(self):
        orch, clock = _orch()
        _add(orch, "BTCUSD")  # atlas_id
        ing = _ingress(orch, {"BTCUSD": "BTCUSDT"})  # feed symbol distinto
        ing.start()
        _feed(ing, clock, "BTCUSDT", SERIES[:3])
        # El worker (atlas_id) recibio y opera; metrics bajo atlas_id.
        assert orch.ledger.position("BTCUSD") is not None
        assert ing.metrics["BTCUSD"].emitted == 3
        assert ing.engine_for("BTCUSD").symbol == "BTCUSDT"

    def test_unknown_symbol_is_observable_rejection(self):
        orch, clock = _orch()
        ea = _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        assert ing.receive(_raw("ZZZUSD", START, SERIES[0])) is None
        assert ing.unknown_symbol_count == 1
        assert ing.unknown_symbol_rejections == ["ZZZUSD"]
        assert ea._bars == []

    def test_duplicate_market_symbol_is_fail_closed(self):
        orch, _ = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        with pytest.raises(IngressConfigError):
            _ingress(orch, {"EURUSD": "SAME", "BTCUSD": "SAME"})

    def test_unregistered_atlas_id_is_fail_closed(self):
        orch, _ = _orch()
        _add(orch, "EURUSD")
        with pytest.raises(IngressConfigError):
            _ingress(orch, {"BTCUSD": "BTCUSD"})


# --------------------------------------------------------------------------
# 3-4. CONFIG VALIDATION
# --------------------------------------------------------------------------


class TestStreamConfigValidation:
    def test_empty_ids_rejected(self):
        with pytest.raises(IngressConfigError):
            InstrumentStreamConfig(
                atlas_id="", market_data_symbol="X", interval_seconds=3600,
                instrument_spec=_spec("X"),
            )
        with pytest.raises(IngressConfigError):
            InstrumentStreamConfig(
                atlas_id="X", market_data_symbol="", interval_seconds=3600,
                instrument_spec=_spec("X"),
            )

    def test_invalid_interval_rejected(self):
        with pytest.raises(IngressConfigError):
            _stream("X", interval=0)
        with pytest.raises(IngressConfigError):
            _stream("X", interval=-1)

    def test_spec_id_mismatch_rejected(self):
        with pytest.raises(IngressConfigError):
            _stream("X", spec=_spec("Y"))


# --------------------------------------------------------------------------
# 5. DUPLICADOS / TARDIOS / GAP
# --------------------------------------------------------------------------


class TestOrderingSemantics:
    def test_duplicate_not_reprocessed(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        t0 = START
        clock.set(datetime.fromtimestamp(t0 + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", t0, SERIES[0]))
        bars_after_first = len(orch.workers["EURUSD"]._bars)
        ing.receive(_raw("EURUSD", t0, SERIES[0]))  # duplicado
        assert ing.metrics["EURUSD"].duplicates == 1
        assert len(orch.workers["EURUSD"]._bars) == bars_after_first
        assert orch.broker._orders == {}

    def test_out_of_order_is_late(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        clock.set(datetime.fromtimestamp(START + 7200, tz=UTC))
        ing.receive(_raw("EURUSD", START + 3600, SERIES[1]))
        ing.receive(_raw("EURUSD", START, SERIES[0]))  # anterior
        assert ing.metrics["EURUSD"].late == 1

    def test_gap_is_local_and_does_not_block_other(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        # EURUSD: t0, t1, luego salta t2 y llega t3 => GAP local.
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START, SERIES[0]))
        clock.set(datetime.fromtimestamp(START + 7200, tz=UTC))
        ing.receive(_raw("EURUSD", START + 3600, SERIES[1]))
        clock.set(datetime.fromtimestamp(START + 14400, tz=UTC))
        ing.receive(_raw("EURUSD", START + 3 * 3600, SERIES[3]))
        assert ing.metrics["EURUSD"].gaps >= 1
        assert ing.engine_for("EURUSD").state is EngineState.DEGRADED
        # BTCUSD NO se bloquea.
        _feed(ing, clock, "BTCUSD", SERIES[:3], start=START)
        assert orch.ledger.position("BTCUSD") is not None
        assert orch.kill_switch.status().can_trade is True

    def test_invalid_timestamp_not_aligned_rejected(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START + 1, SERIES[0]))  # no alineado
        assert ing.metrics["EURUSD"].rejected == 1
        assert orch.workers["EURUSD"]._bars == []

    def test_open_bar_does_not_reach_worker(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START, SERIES[0], is_closed=False))
        assert orch.workers["EURUSD"]._bars == []
        # Al cerrar la misma barra, sí llega.
        ing.receive(_raw("EURUSD", START, SERIES[0], is_closed=True))
        assert len(orch.workers["EURUSD"]._bars) == 1

    def test_future_timestamp_rejected_at_worker(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        # clock ANTES del cierre: la vela es futura respecto al reloj.
        clock.set(datetime.fromtimestamp(START, tz=UTC))
        ing.receive(_raw("EURUSD", START, SERIES[0]))
        assert orch.workers["EURUSD"]._bars == []
        assert orch.broker._orders == {}


# --------------------------------------------------------------------------
# 6. STALE / SALUD GLOBAL
# --------------------------------------------------------------------------


class TestStaleAndGlobalHealth:
    def test_stale_one_instrument_isolates(self):
        orch, clock = _orch()
        ea = _add(orch, "EURUSD")
        eb = _add(orch, "BTCUSD")
        ing = _ingress(
            orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"},
            engine_clock=lambda: clock().timestamp(),
        )
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:1])
        clock.set(datetime.fromtimestamp(START + 10_000, tz=UTC))
        ing.engine_for("EURUSD").check_stale()
        assert ing.engine_for("EURUSD").state is EngineState.STALE
        assert ing.engine_for("BTCUSD").state is EngineState.CONNECTED
        # No hay veto global por staleness local.
        assert orch.kill_switch.status().can_trade is True
        # BTC sigue operando.
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.position("BTCUSD") is not None

    def test_missing_global_health_fails_closed(self):
        orch, clock = _orch_bare()  # sin observacion global => DATA_FEED UNKNOWN
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.broker._orders == {}
        assert orch.kill_switch.status().can_trade is False

    def test_explicit_healthy_global_data_feed(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        assert orch.ledger.position("EURUSD") is not None
        result = orch._last_global_monitoring.get(MonitorComponent.DATA_FEED)
        assert result is not None  # salud global provino de la observacion inyectada


# --------------------------------------------------------------------------
# 7. SESIONES / VETOS / FALLOS GLOBALES
# --------------------------------------------------------------------------


class TestSessionsAndGlobalVeto:
    def test_closed_local_session_does_not_block_other(self):
        orch, clock = _orch()
        _add(orch, "EURUSD", calendar=_closed_calendar())
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert not any("EURUSD" in cid for cid in orch.broker._orders)
        assert any("BTCUSD" in cid for cid in orch.broker._orders)
        assert orch.kill_switch.status().can_trade is True

    def test_global_veto_blocks_all(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        orch.kill_switch.set_source(KillSource.SYSTEM, KillSwitchState.EMERGENCY)
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.broker._orders == {}

    def test_global_broker_failure_blocks_all(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        orch.broker.disconnect()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.positions() == ()
        assert not orch.kill_switch.status().can_trade

    def test_global_reconciliation_failure_blocks_all(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        orch.broker._cash += 5_000.0
        _feed(ing, clock, "EURUSD", SERIES[3:4], start=START + 3 * 3600)
        assert not orch.kill_switch.status().can_trade
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert not any("BTCUSD" in cid for cid in orch.broker._orders)


# --------------------------------------------------------------------------
# 8. RIESGO / LEDGER / BROKER / SIN ESCRITURAS GLOBALES DE WORKERS
# --------------------------------------------------------------------------


class TestSharedInvariants:
    def test_risk_aggregated_across_instruments(self):
        orch, clock = _orch(
            risk=RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        )
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        assert orch.ledger.position("EURUSD") is not None
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.ledger.position("BTCUSD") is None  # limite global

    def test_ledger_marks_and_broker_shared(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert set(orch.marks) >= {"EURUSD", "BTCUSD"}
        assert {p.instrument for p in orch.ledger.positions()} == {"EURUSD", "BTCUSD"}
        assert len(orch.broker._orders) == 2

    def test_workers_do_not_write_global_sources(self):
        orch, clock = _orch()
        a = _add(orch, "EURUSD")
        b = _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert a.enable_monitoring is False and b.enable_monitoring is False
        assert a.last_monitoring is None and b.last_monitoring is None
        assert orch._last_global_monitoring is not None


# --------------------------------------------------------------------------
# 9. NO MUTACION DE MARKS ANTE RECHAZO (correccion MULTI-ASSET-05)
# --------------------------------------------------------------------------


class TestRejectedBarDoesNotMutateSharedState:
    def test_future_candle_does_not_mutate_mark(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        # Vela valida: fija el mark.
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START, SERIES[0]))
        mark_before = orch.marks["EURUSD"]
        close_before = orch._last_close_dt
        assert mark_before == pytest.approx(SERIES[0][3])
        # Vela FUTURA (close_time > now): rechazada por LIVE-03 del worker.
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))  # < t1 close
        ing.receive(_raw("EURUSD", START + 3600, SERIES[1]))
        assert orch.marks["EURUSD"] == mark_before  # NO mutado
        assert orch._last_close_dt == close_before  # NO mutado
        assert orch.workers["EURUSD"]._bars == [
            {"ts": str(START), "open": SERIES[0][0], "high": SERIES[0][1],
             "low": SERIES[0][2], "close": SERIES[0][3], "volume": 1000.0}
        ]
        assert orch.broker._orders == {}

    def test_future_candle_does_not_change_risk_state(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])  # abre posicion
        assert orch.ledger.position("EURUSD") is not None
        mark_before = orch.marks["EURUSD"]
        equity_before = orch._global_risk_state().equity
        # Vela futura en t3 (clock < t3 close).
        clock.set(datetime.fromtimestamp(START + 3 * 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START + 3 * 3600, SERIES[3]))
        assert orch.marks["EURUSD"] == mark_before
        assert orch._global_risk_state().equity == pytest.approx(equity_before)

    def test_invalid_candle_does_not_mutate_marks_direct_entry(self):
        # Entrada de dominio directa (sin B1): evento invalido no debe mutar marks.
        from live.events import ClosedBarEvent

        orch, clock = _orch()
        _add(orch, "EURUSD")
        clock.set(datetime.fromtimestamp(START + 10 * 3600, tz=UTC))
        invalid = ClosedBarEvent(
            symbol="EURUSD", open_time=START, open=100.0, high=90.0,
            low=95.0, close=100.0, volume=1.0, interval_seconds=3600,
            emission_sequence=1,
        )
        orch.on_closed_bar(invalid)  # worker procesa y rechaza (sin mutar)
        events = [e.event for e in orch.workers["EURUSD"].audit_log]
        assert "BAR_SKIPPED" in events
        assert "EURUSD" not in orch.marks  # invalido: sin mark
        assert orch.broker._orders == {}

    def test_valid_event_updates_mark_and_worker_works(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])
        assert orch.marks["EURUSD"] == pytest.approx(SERIES[2][3])
        assert orch.ledger.position("EURUSD") is not None

    def test_local_rejection_isolated_from_other_instrument(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"})
        ing.start()
        # EURUSD valida t0 (mark) y luego futura (rechazada).
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START, SERIES[0]))
        eur_mark = orch.marks["EURUSD"]
        clock.set(datetime.fromtimestamp(START + 3600, tz=UTC))
        ing.receive(_raw("EURUSD", START + 3600, SERIES[1]))  # futura rechazada
        assert orch.marks["EURUSD"] == eur_mark
        # BTCUSD valido opera y fija su mark.
        _feed(ing, clock, "BTCUSD", SERIES[:3])
        assert orch.marks["BTCUSD"] == pytest.approx(SERIES[2][3])
        assert orch.ledger.position("BTCUSD") is not None
        assert orch.kill_switch.status().can_trade is True


class TestDuplicateOldBarDoesNotMutateSharedState:
    """Idempotencia: duplicado/antiguo no muta marks (prevalidacion completa)."""

    def _opened(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        ing = _ingress(orch, {"EURUSD": "EURUSD"})
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:3])  # abre posicion; mark = close t2
        return orch, clock

    def _domain(self, open_time, close, *, interval=3600, seq=9):
        return ClosedBarEvent(
            symbol="EURUSD", open_time=open_time, open=close, high=close,
            low=close, close=close, volume=1.0, interval_seconds=interval,
            emission_sequence=seq,
        )

    def test_duplicate_open_time_does_not_mutate(self):
        orch, clock = self._opened()
        assert orch.ledger.position("EURUSD") is not None
        mark_before = orch.marks["EURUSD"]
        close_before = orch._last_close_dt
        equity_before = orch._global_risk_state().equity
        orders_before = dict(orch.broker._orders)
        last_open = START + 2 * 3600

        # Duplicado del MISMO open_time con OTRO close.
        orch.on_closed_bar(self._domain(last_open, 99_999.0))

        assert orch.marks["EURUSD"] == mark_before
        assert orch._last_close_dt == close_before
        assert orch._global_risk_state().equity == pytest.approx(equity_before)
        assert orch.broker._orders == orders_before
        events = [e.event for e in orch.workers["EURUSD"].audit_log]
        assert "RESTART_SKIP" in events

    def test_old_open_time_does_not_mutate(self):
        orch, clock = self._opened()
        mark_before = orch.marks["EURUSD"]
        close_before = orch._last_close_dt
        orders_before = dict(orch.broker._orders)

        orch.on_closed_bar(self._domain(START + 3600, 88_888.0))  # anterior

        assert orch.marks["EURUSD"] == mark_before
        assert orch._last_close_dt == close_before
        assert orch.broker._orders == orders_before
        events = [e.event for e in orch.workers["EURUSD"].audit_log]
        assert "RESTART_SKIP" in events

    def test_new_valid_bar_updates_mark(self):
        orch, clock = self._opened()
        mark_before = orch.marks["EURUSD"]
        last_open = START + 2 * 3600
        new_open = START + 3 * 3600
        clock.set(datetime.fromtimestamp(new_open + 3600, tz=UTC))
        orch.on_closed_bar(self._domain(new_open, 51_234.0, seq=10))
        assert orch.marks["EURUSD"] == pytest.approx(51_234.0)
        assert orch.marks["EURUSD"] != mark_before
        assert orch.workers["EURUSD"]._last_open_time == new_open


class TestCheckStaleAll:
    def test_check_stale_all_isolates_and_never_touches_global(self):
        orch, clock = _orch()
        _add(orch, "EURUSD")
        _add(orch, "BTCUSD")
        ing = _ingress(
            orch, {"EURUSD": "EURUSD", "BTCUSD": "BTCUSD"},
            engine_clock=lambda: clock().timestamp(),
        )
        ing.start()
        _feed(ing, clock, "EURUSD", SERIES[:1])
        _feed(ing, clock, "BTCUSD", SERIES[:1])
        # Avanza el reloj mas alla del stale_timeout: ambos STALE (local).
        clock.set(datetime.fromtimestamp(START + 20_000, tz=UTC))
        states = ing.check_stale_all()
        assert states["EURUSD"] is EngineState.STALE
        assert states["BTCUSD"] is EngineState.STALE
        # BTC recibe un evento fresco -> vuelve a CONNECTED; EURUSD sigue STALE.
        ing.receive(_raw("BTCUSD", START + 3600, SERIES[1]))
        assert ing.engine_for("BTCUSD").state is EngineState.CONNECTED
        assert ing.engine_for("EURUSD").state is EngineState.STALE
        # La staleness local no deriva en veto global.
        assert orch.kill_switch.status().can_trade is True
