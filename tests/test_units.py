"""Auditoría de unidades: risk in $ ≡ P&L in $ con point_value != 1."""

from __future__ import annotations

from typing import Any

from backtesting.engine import BacktestEngine, ExecutionCosts
from execution.order_manager import OrderManager
from risk.engine import RiskEngine
from risk.types import RiskConfig
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

META = StrategyMetadata(
    strategy_id="atlas-one-shot",
    strategy_name="one-shot",
    strategy_family="test",
    market="futures",
    instrument="NQ",
    timeframe="1h",
    version="1.0.0",
    parameter_set_version="t",
    data_version="t",
)


class OneShotStrategy(BaseStrategy):
    """Emite una única señal LONG con stop a `stop_distance` de precio."""

    def __init__(self, stop_distance: float) -> None:
        self.metadata = META
        self.stop_distance = stop_distance
        self._fired = False

    def generate_signal(self, data: Any) -> Signal:
        bars = list(data)
        if self._fired or len(bars) < 1:
            return Signal(
                strategy_id=META.strategy_id,
                strategy_version=META.version,
                instrument=META.instrument,
                action=Action.NO_TRADE,
            )
        self._fired = True
        close = float(bars[-1]["close"])
        return Signal(
            strategy_id=META.strategy_id,
            strategy_version=META.version,
            instrument=META.instrument,
            action=Action.LONG,
            reference_price=close,
            stop_price=close - self.stop_distance,
        )


def _run(point_value: float) -> dict:
    bars = [
        {"ts": "0", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
        {"ts": "3600", "open": 100, "high": 110, "low": 80, "close": 95, "volume": 1},
    ]
    engine = BacktestEngine(
        costs=ExecutionCosts(initial_equity=100_000.0),
        risk_engine=RiskEngine(config=RiskConfig(risk_per_trade=0.01)),
        order_manager=OrderManager(),
        point_value=point_value,
        min_size=0.0,
    )
    result = engine.run(OneShotStrategy(stop_distance=10.0), bars)
    assert len(result.trades) == 1
    return result.trades[0]


def test_risk_dollars_equal_pnl_dollars() -> None:
    pv = 50.0
    trade = _run(pv)
    cap = 100_000.0 * 0.01

    qty = trade["quantity"]
    assert qty == cap / (10.0 * pv)
    assert abs(trade["gross_pnl"] - (-cap)) < 1e-6
    assert abs(trade["net_pnl"] - (-cap)) < 1e-6


def test_mae_mfe_scaled_by_point_value() -> None:
    pv = 50.0
    trade = _run(pv)
    qty = trade["quantity"]
    assert abs(trade["mae"] - 20.0 * qty * pv) < 1e-6
    assert abs(trade["mfe"] - 10.0 * qty * pv) < 1e-6


def test_point_value_one_preserves_legacy_numbers() -> None:
    trade = _run(1.0)
    assert abs(trade["gross_pnl"] - (-1000.0)) < 1e-6
    assert abs(trade["mae"] - 2000.0) < 1e-6