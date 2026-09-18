"""Reporting: métricas de backtest (00_MASTER_SPECIFICATION.md §13).

El MVP calcula las métricas esenciales sobre la curva de equity y las
operaciones. Métricas avanzadas (CAGR, Sortino, Calmar, MAE/MFE, VaR)
se incorporan en Milestone 2 con la base de datos de experimentos.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from backtesting.engine import BacktestResult


@dataclass(frozen=True, slots=True)
class Metrics:
    """Conjunto mínimo de métricas del proyecto."""

    net_return: float
    profit_factor: float | None
    expectancy: float | None
    win_rate: float | None
    max_drawdown: float
    num_trades: int
    equity_final: float
    equity_min: float
    equity_max: float
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return asdict(self)


def _max_drawdown(curve: list[float]) -> float:
    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def calculate_metrics(result: BacktestResult) -> Metrics:
    """Calcula métricas agregadas a partir de un resultado de backtest.

    `exposure`, `turnover` y `total_commission` requieren el registro de
    operaciones y posición del engine (Milestone 2): se expone la curva
    completa en `raw` para que ninguna métrica se subestime silenciosamente.
    """
    curve = result.equity_curve or [result.initial_equity]
    trades = result.trades

    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses else None

    num = len(pnls)
    expectancy = round(sum(pnls) / num, 4) if num else None
    win_rate = round(len(wins) / num, 4) if num else None

    return Metrics(
        net_return=round(result.net_return, 6),
        profit_factor=profit_factor,
        expectancy=expectancy,
        win_rate=win_rate,
        max_drawdown=round(_max_drawdown(curve), 6),
        num_trades=num,
        equity_final=round(curve[-1], 2),
        equity_min=round(min(curve), 2),
        equity_max=round(max(curve), 2),
        raw={
            "curve_points": len(curve),
            "initial_equity": result.initial_equity,
            "final_equity": result.final_equity,
            "execution_costs_unaccounted": (
                "exposure/turnover/commission requieren Milestone 2"
            ),
        },
    )