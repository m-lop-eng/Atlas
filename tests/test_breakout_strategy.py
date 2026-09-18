"""Tests de la estrategia breakout (strategies/breakout.py)."""

from __future__ import annotations

import pytest

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


def _strategy(trend_filter: int | None = None) -> BreakoutStrategy:
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
    return BreakoutStrategy(
        meta,
        BreakoutParams(lookback=3, atr_period=3, stop_atr_mult=2.0, trend_filter=trend_filter),
    )


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


class TestTrendFilter:
    def test_none_preserves_original_signals(self) -> None:
        assert _strategy(None).generate_signal(_long_breakout()).action == Action.LONG
        assert _strategy(None).generate_signal(_short_breakout()).action == Action.SHORT

    def test_long_breakout_below_sma_blocked(self) -> None:
        """Ruptura alcista que deja al cierre por debajo del SMA → NO_TRADE."""
        bars = [
            {"ts": "0", "open": 106.0, "high": 106.0, "low": 105.0, "close": 106.0},
            {"ts": "1", "open": 99.0, "high": 99.0, "low": 98.2, "close": 99.0},
            {"ts": "2", "open": 99.0, "high": 99.0, "low": 98.5, "close": 99.0},
            {"ts": "3", "open": 99.2, "high": 99.6, "low": 99.0, "close": 99.4},
        ]
        meta_small = StrategyMetadata(
            strategy_id="b", strategy_name="B", strategy_family="breakout",
            market="cash", instrument="T", timeframe="1h", version="1.0.0",
            parameter_set_version="p1", data_version="d1",
        )
        filtered = BreakoutStrategy(meta_small, BreakoutParams(lookback=3, atr_period=2, stop_atr_mult=2.0, trend_filter=4))
        assert filtered.generate_signal(bars).action == Action.NO_TRADE
        plain = BreakoutStrategy(meta_small, BreakoutParams(lookback=3, atr_period=2, stop_atr_mult=2.0))
        assert plain.generate_signal(bars).action == Action.LONG

    def test_short_breakout_above_sma_blocked(self) -> None:
        """Ruptura bajista con cierre por encima del SMA → NO_TRADE."""
        bars = [
            {"ts": "0", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"ts": "1", "open": 84.0, "high": 85.0, "low": 83.0, "close": 84.0},
            {"ts": "2", "open": 97.0, "high": 98.0, "low": 96.0, "close": 97.0},
            {"ts": "3", "open": 91.0, "high": 91.5, "low": 90.0, "close": 91.0},
        ]
        meta_small = StrategyMetadata(
            strategy_id="b", strategy_name="B", strategy_family="breakout",
            market="cash", instrument="T", timeframe="1h", version="1.0.0",
            parameter_set_version="p1", data_version="d1",
        )
        filtered = BreakoutStrategy(meta_small, BreakoutParams(lookback=2, atr_period=2, stop_atr_mult=2.0, trend_filter=3))
        assert filtered.generate_signal(bars).action == Action.NO_TRADE
        plain = BreakoutStrategy(meta_small, BreakoutParams(lookback=2, atr_period=2, stop_atr_mult=2.0))
        signal = plain.generate_signal(bars)
        assert signal.action == Action.SHORT
        assert signal.stop_price > signal.reference_price

    def test_invalid_trend_filter_raises(self) -> None:
        with pytest.raises(ValueError, match="trend_filter"):
            BreakoutParams(trend_filter=0)

    def test_insufficient_bars_for_sma_no_filter(self) -> None:
        """SMA no computable (< period) → el filtro no se aplica."""
        signal = _strategy(50).generate_signal(_long_breakout())
        assert signal.action == Action.LONG
        assert signal.reference_price == 110.0