"""Tests LIVE-03: guardia de market data (validez estructural + temporal).

Contrato congelado (LIVE-03):
  * validez estructural: OHLC finito/positivo/coherente, volumen valido;
  * validez temporal: timestamp positivo/alineado y NO futuro respecto al reloj
    autoritativo (`self._clock()`); epoch-based => independiente del timezone;
  * una barra invalida/futura NUNCA llega a `BrokerAdapter.submit_order` ni muta
    ledger/posicion;
  * freshness (antiguedad maxima) se evalua por monitoring (`data_max_age_s`):
    `now - last_event_at > max_age` => FAILED => kill switch (bloqueo).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live.events import ClosedBarEvent

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    START,
    _LevelLong,
    _audit_events,
    _closed_bar,
    _drive,
    _stack,
)

UTC = timezone.utc


def _bar(open_time: int, ohlc, *, interval: int = 3600) -> ClosedBarEvent:
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
        emission_sequence=1,
    )


def _at(open_time: int, interval: int = 3600) -> datetime:
    return datetime.fromtimestamp(open_time + interval, tz=UTC)


class TestStructuralValidity:
    def test_valid_fresh_bar_continues(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # barra valida -> señal -> submit
        assert env.engine.position is not None
        assert "BAR_SKIPPED" not in _audit_events(env)

    @pytest.mark.parametrize(
        "ohlc",
        [
            (50000.0, 50100.0, 49900.0, 50200.0),  # high < close
            (50000.0, 49900.0, 50100.0, 50050.0),  # high < low
            (0.0, 50100.0, 49900.0, 50050.0),      # open <= 0
            (float("nan"), 50100.0, 49900.0, 50050.0),  # NaN
            (float("inf"), 50100.0, 49900.0, 50050.0),  # Inf
        ],
    )
    def test_invalid_ohlc_blocks_before_submit(self, ohlc):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.clock.set(_at(START))
        env.engine.on_closed_bar(_bar(START, ohlc))
        events = _audit_events(env)
        assert "BAR_SKIPPED" in events
        # No llega al broker y no muta ledger.
        assert env.broker._orders == {}
        assert env.engine.ledger.cash == 1_000_000.0
        assert env.engine.ledger.positions() == ()


class TestTemporalValidity:
    def test_future_timestamp_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        # now < close_time => la vela aun no habria cerrado.
        env.clock.set(datetime.fromtimestamp(START, tz=UTC))
        env.engine.on_closed_bar(_bar(START, (50000, 50100, 49900, 50050)))
        assert "BAR_SKIPPED" in _audit_events(env)
        assert env.broker._orders == {}
        assert env.engine.ledger.cash == 1_000_000.0

    def test_unaligned_timestamp_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.clock.set(_at(START + 1))
        env.engine.on_closed_bar(
            _bar(START + 1, (50000, 50100, 49900, 50050))
        )
        assert "BAR_SKIPPED" in _audit_events(env)

    def test_timezone_equivalent_same_result(self):
        # Mismo instante expresado en timezones distintos => mismo resultado.
        epoch = START + 3600
        for tz in (timezone.utc, timezone(timedelta(hours=5))):
            env = _stack(strategy=_LevelLong(level=LEVEL))
            env.clock.set(datetime.fromtimestamp(epoch, tz=tz))
            env.engine.on_closed_bar(_bar(START, (50000, 50100, 49900, 50050)))
            assert "BAR_SKIPPED" not in _audit_events(env)

    def test_clock_deterministic(self):
        # Mismos inputs + mismo clock => mismo resultado (rechazo).
        results = []
        for _ in range(2):
            env = _stack(strategy=_LevelLong(level=LEVEL))
            env.clock.set(datetime.fromtimestamp(START, tz=UTC))
            env.engine.on_closed_bar(_bar(START, (50000, 50100, 49900, 50050)))
            results.append("BAR_SKIPPED" in _audit_events(env))
        assert results == [True, True]


class TestFreshness:
    def test_stale_data_blocks_via_monitoring(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # fija _last_bar_close_dt = close(B2)
        # El reloj autoritativo avanza: el dato queda obsoleto (> max_age 30s).
        env.clock.set(env.engine._last_bar_close_dt + timedelta(seconds=120))
        env.engine.run_monitoring()
        status = env.kill_switch.status()
        assert not status.can_trade
        assert status.state.value in ("BLOCK_NEW_ORDERS", "HALT")

    def test_fresh_data_does_not_block(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])
        env.clock.set(env.engine._last_bar_close_dt)  # dato fresco
        env.engine.run_monitoring()
        assert env.kill_switch.status().can_trade


class TestPreSubmitFreshness:
    """G-MD-D: revalidacion de freshness inmediatamente antes del submit."""

    def test_stale_before_submit_blocks_no_broker_call(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # B2 decide LONG (fresco); clock=close(B2)
        # B3: la senal era fresca, pero el reloj avanza > data_max_age_s (30s)
        # antes del submit (TOCTOU).
        bar3 = _closed_bar(START + 2 * 3600, HAPPY[2])
        env.clock.set(
            datetime.fromtimestamp(START + 3 * 3600, tz=UTC)
            + timedelta(seconds=120)
        )
        env.engine.on_closed_bar(bar3)
        events = _audit_events(env)
        assert "ENTRY_SUBMITTED" not in events  # no llego a submit_order
        assert env.broker._orders == {}
        assert env.engine.ledger.cash == 1_000_000.0
        assert env.engine.ledger.positions() == ()

    def test_fresh_signal_submits(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        assert "ENTRY_SUBMITTED" in _audit_events(env)
        assert len(env.broker._orders) == 1

    def test_no_reference_fails_closed_no_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # stagea pending
        env.engine._last_bar_close_dt = None  # referencia ausente
        bar3 = _closed_bar(START + 2 * 3600, HAPPY[2])
        env.engine._execute_pending(
            bar3.atlas_bar, datetime.fromtimestamp(START + 3 * 3600, tz=UTC)
        )
        assert "ENTRY_SUBMITTED" not in _audit_events(env)
        assert env.broker._orders == {}

    def test_future_reference_fails_closed(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        ref = datetime.fromtimestamp(START + 3600, tz=UTC)
        env.engine._last_bar_close_dt = ref
        earlier = datetime.fromtimestamp(START, tz=UTC)
        assert env.engine._freshness_rejection(earlier) == "future_market_data"


class TestMarketDataIdentity:
    """LIVE-03: instrumento/intervalo esperados enforced antes de submit."""

    def test_wrong_symbol_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.clock.set(_at(START))
        bar = ClosedBarEvent(
            symbol="ETHUSDT", open_time=START, open=50000, high=50100,
            low=49900, close=50050, volume=1000.0, interval_seconds=3600,
            emission_sequence=1,
        )
        env.engine.on_closed_bar(bar)
        assert "BAR_SKIPPED" in _audit_events(env)
        assert "ENTRY_SUBMITTED" not in _audit_events(env)
        assert env.broker._orders == {}
        assert env.engine.ledger.cash == 1_000_000.0

    def test_wrong_interval_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:1])  # primer bar fija interval=3600
        env.clock.set(_at(START + 3600, interval=1800))
        bar = _bar(START + 3600, (50000, 50100, 49900, 50050), interval=1800)
        env.engine.on_closed_bar(bar)
        assert "BAR_SKIPPED" in _audit_events(env)
        assert "ENTRY_SUBMITTED" not in _audit_events(env)
        assert env.broker._orders == {}
        assert env.engine.ledger.cash == 1_000_000.0
