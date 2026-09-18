"""Tests de la restricción direccional (H002) del breakout."""

from __future__ import annotations

from strategies.base.signal import Action
from strategies.base.strategy import StrategyMetadata
from strategies.breakout import BreakoutParams, BreakoutStrategy

META = StrategyMetadata(
    strategy_id="atlas-breakout",
    strategy_name="t",
    strategy_family="breakout",
    market="crypto",
    instrument="BTCUSDT",
    timeframe="1h",
    version="1.0.0",
    parameter_set_version="p",
    data_version="v",
)


def _bars(ascending: bool, n: int = 25) -> list[dict]:
    bars = []
    for i in range(n):
        px = 100.0 + i * 10 if ascending else 300.0 - i * 10
        bars.append(
            {
                "ts": str(i * 3600),
                "open": px,
                "high": px + 1,
                "low": px - 1,
                "close": px,
                "volume": 1.0,
            }
        )
    return bars


def _signal(direction: str, ascending: bool) -> Action:
    strat = BreakoutStrategy(META, BreakoutParams(direction=direction))
    return strat.generate_signal(_bars(ascending)).action


def test_invalid_direction_raises() -> None:
    try:
        BreakoutParams(direction="sideways")
    except ValueError:
        return
    raise AssertionError("esperaba ValueError")


def test_both_operates_both_sides() -> None:
    assert _signal("both", True) is Action.LONG
    assert _signal("both", False) is Action.SHORT


def test_long_only_blocks_shorts() -> None:
    assert _signal("long", True) is Action.LONG
    assert _signal("long", False) is Action.NO_TRADE


def test_short_only_blocks_longs() -> None:
    assert _signal("short", False) is Action.SHORT
    assert _signal("short", True) is Action.NO_TRADE