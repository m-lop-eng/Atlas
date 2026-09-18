"""Tests de métricas comparativas de H002 (expectancy, payoff, rachas)."""

from __future__ import annotations

from statistics import pstdev

from backtesting.engine import BacktestResult
from reporting.metrics import calculate_metrics


def _result(pnls: list[float], costs: float = 0.0, mae: float = 5.0, mfe: float = 8.0):
    trades = [
        {"pnl": p, "net_pnl": p, "gross_pnl": p, "costs": costs, "mae": mae, "mfe": mfe}
        for p in pnls
    ]
    return BacktestResult(
        initial_equity=100_000.0,
        final_equity=100_000.0 + sum(pnls),
        trades=trades,
        equity_curve=[100_000.0, 100_000.0 + sum(pnls)],
    )


def test_comparative_metrics() -> None:
    pnls = [10.0, 10.0, -5.0, -5.0, -5.0, 20.0, -5.0]
    m = calculate_metrics(_result(pnls, costs=1.0, mae=5.0, mfe=8.0))

    assert m.num_trades == 7
    assert m.avg_win == round(40.0 / 3, 4)
    assert m.avg_loss == 5.0
    assert round(m.payoff_ratio, 4) == round((40.0 / 3) / 5, 4)
    assert m.profit_factor == 2.0
    assert m.median_trade == -5.0
    assert m.trade_std is not None and abs(m.trade_std - pstdev(pnls)) < 1e-3
    assert m.gross_profit == 40.0
    assert m.gross_loss == 20.0
    assert m.costs_total == 7.0
    assert m.max_consecutive_losses == 3
    assert m.max_consecutive_wins == 2
    assert m.avg_mae == 5.0
    assert m.avg_mfe == 8.0
    assert m.mfe_mae_ratio == 1.6

    wr, lr = 3 / 7, 4 / 7
    expected_e = wr * (40.0 / 3) - lr * 5.0
    assert abs(m.expectancy_e - round(expected_e, 4)) < 1e-9


def test_empty_trades_are_none() -> None:
    m = calculate_metrics(_result([]))
    assert m.num_trades == 0
    assert m.avg_win is None
    assert m.expectancy_e is None
    assert m.profit_factor is None
    assert m.max_consecutive_wins is None
    assert m.max_consecutive_losses is None