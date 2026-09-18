"""Tests de la estrategia breakout (strategies/breakout.py)."""

from __future__ import annotations

from strategies.base.signal import Action
from strategies.base.strategy import StrategyMetadata
from strategies.breakout import BreakoutParams, BreakoutStrategy


def _flat(bars: int) -> list[dict]:
    return [
        {
            "ts": str(i),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
        }
        for i in range(bars)
    ]


def _long_breakout() -> list[dict]:
    bars = _flat(5)
    bars.append(
        {"ts": "5", "open": 100.0, "high": 120.0, "low": 100.0, "close": 110.0}
    )
    return bars


def _short_breakout() -> list[dict]:
    bars = _flat(5)
    bars.append(
        {"ts": "5", "open": 100.0, "high": 100.5, "low": 99.0, "close": 99.0}
    )
    return bars


def _strategy() -> BreakoutStrategy:
    meta = StrategyMetadata(
        strategy_id="breakout-test",
        strategy_name="Breakout Test",
        strategy_family="breakout",
        market="cash",
        instrument="TST",
        timeframe="1h",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
    )
    return BreakoutStrategy(meta, BreakoutParams(lookback=3, atr_period=3, stop_atr_mult=2.0))


class TestBreakoutStrategy:
    def test_insufficient_data_no_trade(self) -> None:
        assert _strategy().generate_signal(_flat(3)).action == Action.NO_TRADE

    def test_flat_atr_zero_no_trade(self) -> None:
        assert _strategy().generate_signal(_flat(30)).action == Action.NO_TRADE

    def test_long_breakout_above_previous_highs(self) -> None:
        signal = _strategy().generate_signal(_long_breakout())
        assert signal.action == Action.LONG
        assert signal.stop_price < signal.reference_price

    def test_short_breakout_below_previous_lows(self) -> None:
        signal = _strategy().generate_signal(_short_breakout())
        assert signal.action == Action.SHORT
        assert signal.stop_price > signal.reference_price

    def test_no_trade_without_breakout(self) -> None:
        bars = _flat(6)
        signal = _strategy().generate_signal(bars)
        assert signal.action == Action.NO_TRADE

    def test_stop_distance_equals_atr_multiple(self) -> None:
        bars = _long_breakout()
        signal = _strategy().generate_signal(bars)
        ref, stop = signal.reference_price, signal.stop_price
        assert abs((ref - stop) - (2.0 * 20.0 / 3.0)) < 1e-9

    def test_signal_carries_metadata(self) -> None:
        signal = _strategy().generate_signal(_long_breakout())
        assert signal.strategy_id == "breakout-test"
        assert signal.strategy_version == "1.0.0"
        assert signal.instrument == "TST"
        assert signal.target_quantity is None