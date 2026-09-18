"""Tests de la interfaz de estrategia (strategies/base/)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

META = StrategyMetadata(
    strategy_id="demo-bias",
    strategy_name="Demo Bias",
    strategy_family="momentum",
    market="futures",
    instrument="NQ",
    timeframe="5m",
    version="1.0.0",
    parameter_set_version="p1",
    data_version="d1",
    status="IDEA",
)


class BiasStrategy(BaseStrategy):
    """Estrategia de prueba: siempre LONG."""

    metadata = META

    def generate_signal(self, data) -> Signal:
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=100.0,
            stop_price=98.0,
        )


class TestBaseStrategy:
    def test_describe_returns_metadata(self) -> None:
        strat = BiasStrategy()
        info = strat.describe()
        assert info["strategy_id"] == "demo-bias"
        assert info["version"] == "1.0.0"
        assert info["timeframe"] == "5m"

    def test_generate_signal_abstract(self) -> None:
        class Empty(BaseStrategy):
            metadata = META

        with pytest.raises(TypeError):
            Empty()


class TestSignal:
    def test_default_timestamp_is_utc(self) -> None:
        s = Signal(
            strategy_id="a",
            strategy_version="1",
            instrument="NQ",
            action=Action.LONG,
        )
        assert s.timestamp.tzinfo is not None

    def test_naive_timestamp_promoted_to_utc(self) -> None:
        s = Signal(
            strategy_id="a",
            strategy_version="1",
            instrument="NQ",
            action=Action.LONG,
            timestamp=datetime(2026, 1, 1, 12, 0, 0),
        )
        assert s.timestamp.tzinfo == timezone.utc

    def test_missing_strategy_id_raises(self) -> None:
        with pytest.raises(ValueError, match="strategy_id"):
            Signal(strategy_id="", strategy_version="1", instrument="NQ", action=Action.LONG)

    def test_signal_is_frozen(self) -> None:
        s = Signal(strategy_id="a", strategy_version="1", instrument="NQ", action=Action.LONG)
        with pytest.raises(Exception):
            s.action = Action.SHORT

    def test_actions_enum(self) -> None:
        assert Action.LONG.value == "LONG"
        assert Action.SHORT.value == "SHORT"
        assert Action.NO_TRADE.value == "NO_TRADE"