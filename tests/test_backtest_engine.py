"""Tests del motor de backtest (backtesting/engine.py)."""

from __future__ import annotations

import pytest

from backtesting.engine import BacktestEngine, ExecutionCosts
from reporting.metrics import calculate_metrics
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata


def _bar(close: float, ts: int) -> dict:
    return {"ts": str(ts), "open": close, "high": close, "low": close, "close": close}


class AlwaysLong(BaseStrategy):
    metadata = StrategyMetadata(
        strategy_id="always-long",
        strategy_name="Always Long",
        strategy_family="demo",
        market="futures",
        instrument="NQ",
        timeframe="1m",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
    )

    def generate_signal(self, data) -> Signal:
        last = data[-1]["close"]
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=float(last),
            stop_price=float(last) - 1.0,
            target_quantity=1,
        )


class NoTrade(BaseStrategy):
    metadata = StrategyMetadata(
        strategy_id="no-trade",
        strategy_name="No Trade",
        strategy_family="demo",
        market="futures",
        instrument="NQ",
        timeframe="1m",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
    )

    def generate_signal(self, data) -> Signal:
        last = data[-1]["close"]
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.NO_TRADE,
            reference_price=float(last),
        )


def _engine(**cost_kwargs) -> BacktestEngine:
    defaults = dict(initial_equity=100_000.0)
    defaults.update(cost_kwargs)
    return BacktestEngine(costs=ExecutionCosts(**defaults))


class TestEngineExecution:
    def test_requires_two_bars(self) -> None:
        with pytest.raises(ValueError, match="2 barras"):
            _engine().run(AlwaysLong(), [_bar(100, 1)])

    def test_no_lookahead_entry_at_next_open(self) -> None:
        """La señal de la barra t se ejecuta en el open de la barra t+1."""
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        result = _engine().run(AlwaysLong(), bars)
        assert result.trades
        assert result.trades[0]["entry_price"] == bars[1]["open"]

    def test_uptrend_long_profitable(self) -> None:
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        result = _engine().run(AlwaysLong(), bars)
        assert result.final_equity > result.initial_equity

    def test_downtrend_long_loses(self) -> None:
        bars = [_bar(100, 1), _bar(99, 2), _bar(98, 3)]
        result = _engine().run(AlwaysLong(), bars)
        assert result.final_equity < result.initial_equity

    def test_no_trade_keeps_equity(self) -> None:
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        result = _engine().run(NoTrade(), bars)
        assert result.final_equity == result.initial_equity
        assert result.trades == []
        assert result.net_return == 0.0

    def test_costs_reduce_pnl(self) -> None:
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        no_cost = _engine().run(AlwaysLong(), bars)
        with_cost = _engine(spread_ticks=1, tick_size=0.5).run(AlwaysLong(), bars)
        assert with_cost.final_equity < no_cost.final_equity


class TestMetrics:
    def test_metrics_fields_present(self) -> None:
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        result = _engine().run(AlwaysLong(), bars)
        metrics = calculate_metrics(result)
        assert metrics.to_dict() is not None
        assert metrics.num_trades == len(result.trades)

    def test_drawdown_detected_in_losing_run(self) -> None:
        bars = [_bar(100, 1), _bar(99, 2), _bar(98, 3)]
        result = _engine().run(AlwaysLong(), bars)
        metrics = calculate_metrics(result)
        assert metrics.max_drawdown > 0.0
        assert metrics.net_return < 0.0

    def test_trade_count_matches(self) -> None:
        bars = [_bar(100, 1), _bar(101, 2), _bar(102, 3)]
        result = _engine().run(AlwaysLong(), bars)
        assert len(result.trades) == 2