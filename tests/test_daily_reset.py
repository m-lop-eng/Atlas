"""Tests del reset diario de `max_daily_loss` (ENGINE_VERSION=2).

La hipótesis: `max_daily_loss` debe limitar la pérdida DEL DÍA de trading
(calendar-day UTC), NO el P&L acumulado de todo el backtest. El bucle de
estas barras abarca 2 días; si se usara el acumulado, la 2ª operación
estaría vetada (día1 ya perdió 1.1% > 1%). Con el reset diario correcto,
el día2 comienza con `current_daily_pnl=0` y puede operar.

El RiskEngine NO cambia: solo el entorno (backtest) alimenta el estado
con el P&L diario correcto.
"""

from __future__ import annotations

from risk.engine import RiskEngine
from risk.types import RiskConfig
from backtesting.engine import BacktestEngine, ExecutionCosts
from execution.order_manager import OrderManager
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

D1 = 1_731_888_000  # 2024-11-18 00:00 UTC
D2 = 1_731_974_400  # 2024-11-19 00:00 UTC
HOUR = 3_600


def _ts(day: int, hour: int) -> str:
    return str(day + hour * HOUR)


class AlwaysLongStop10(BaseStrategy):
    metadata = StrategyMetadata(
        strategy_id="daily-reset", strategy_name="Daily Reset", strategy_family="demo",
        market="futures", instrument="NQ", timeframe="1m", version="1.0.0",
        parameter_set_version="p1", data_version="d1",
    )

    def generate_signal(self, data) -> Signal:
        last = float(data[-1]["close"])
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=last,
            stop_price=last - 10.0,
            target_quantity=None,
        )


def bars() -> list[dict]:
    """Día1: operación que pierde 1.1% y señal posterior vetada.
    Día2: se reactiva la operativa (reset diario del límite)."""
    return [
        {"ts": _ts(D1, 0), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        {"ts": _ts(D1, 1), "open": 101.0, "high": 101.0, "low": 85.0, "close": 86.0},
        {"ts": _ts(D1, 2), "open": 90.0, "high": 95.0, "low": 88.0, "close": 92.0},
        {"ts": _ts(D2, 0), "open": 92.0, "high": 94.0, "low": 91.0, "close": 94.0},
        {"ts": _ts(D2, 1), "open": 94.0, "high": 95.0, "low": 93.0, "close": 95.0},
    ]


def _engine() -> BacktestEngine:
    risk = RiskEngine(config=RiskConfig(risk_per_trade=0.01, max_daily_loss=0.01))
    return BacktestEngine(
        costs=ExecutionCosts(initial_equity=100_000.0),
        risk_engine=risk,
        order_manager=OrderManager(),
        point_value=1.0,
        min_size=0.01,
    )


class TestDailyLossReset:
    def test_second_day_trades_after_loss_day(self) -> None:
        result = _engine().run(AlwaysLongStop10(), bars())
        assert len(result.trades) == 2
        assert result.trades[0]["pnl"] == -1100.0
        assert result.trades[1]["pnl"] == 98.9
        assert result.final_equity == 98_998.9

    def test_veto_was_triggered_inside_day1(self) -> None:
        """La señal flat del día1 fue vetada por daily_loss (no por otra razón)."""
        result = _engine().run(AlwaysLongStop10(), bars())
        diag = result.diagnostics
        assert diag["risk_rejected_signals"] == 2
        assert diag["risk_rejection_by_reason"]["daily_loss"] == 2
        assert diag["orders_created"] == 2

    def test_diagnostics_counts(self) -> None:
        result = _engine().run(AlwaysLongStop10(), bars())
        diag = result.diagnostics
        assert diag["raw_signals"] == 4
        assert diag["signals_while_flat"] == 4
        assert diag["signals_while_in_position"] == 0
        assert diag["orders_created"] == 2
        assert diag["trades_completed"] == 2

    def test_day2_entry_not_lookahead(self) -> None:
        result = _engine().run(AlwaysLongStop10(), bars())
        assert result.trades[1]["entry_price"] == 94.0


class TestEngineDiagnostics:
    def test_three_bar_long_counters(self) -> None:
        from tests.test_backtest_engine import _bar, _engine as legacy_engine, AlwaysLong

        result = legacy_engine().run(AlwaysLong(), [_bar(100, 1), _bar(101, 2), _bar(102, 3)])
        diag = result.diagnostics
        assert diag["raw_signals"] == 2
        assert diag["signals_while_flat"] == 1
        assert diag["signals_while_in_position"] == 1
        assert diag["risk_rejected_signals"] == 0
        assert diag["orders_created"] == 1
        assert diag["trades_completed"] == 1

    def test_no_signal_no_orders(self) -> None:
        from tests.test_backtest_engine import _bar, _engine as legacy_engine, NoTrade

        result = legacy_engine().run(NoTrade(), [_bar(100, 1), _bar(101, 2), _bar(102, 3)])
        assert result.diagnostics["raw_signals"] == 0
        assert result.diagnostics["orders_created"] == 0
        assert result.trades == []