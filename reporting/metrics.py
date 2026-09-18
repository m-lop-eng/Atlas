"""Reporting: métricas de backtest (00_MASTER_SPECIFICATION.md §13).

El MVP calcula métricas sobre la curva de equity y el registro de operaciones.
Las métricas comparativas de H002 (expectancy, payoff, MAE/MFE, rachas) viven
aquí para comparar LONG vs SHORT con distinto número de trades.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean, median, pstdev

from backtesting.engine import BacktestResult


@dataclass(frozen=True, slots=True)
class Metrics:
    """Conjunto mínimo de métricas del proyecto.

    Convenciones monetarias: cantidades en la divisa de la cuenta (USD en el
    pipeline actual); `avg_loss` y `gross_loss` se expresan en POSITIVO
    (magnitud del riesgo); `expectancy_e` es la fórmula
    E = P(win)*AvgWin - P(loss)*AvgLoss (comparación LONG/SHORT
    independiente del número de operaciones).
    """

    net_return: float
    profit_factor: float | None
    expectancy: float | None
    win_rate: float | None
    max_drawdown: float
    num_trades: int
    equity_final: float
    equity_min: float
    equity_max: float
    avg_win: float | None = None
    avg_loss: float | None = None
    payoff_ratio: float | None = None
    median_trade: float | None = None
    trade_std: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    avg_mae: float | None = None
    avg_mfe: float | None = None
    mfe_mae_ratio: float | None = None
    max_consecutive_wins: int | None = None
    max_consecutive_losses: int | None = None
    costs_total: float | None = None
    expectancy_e: float | None = None
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
    """Calcula métricas agregadas a partir de un resultado de backtest."""
    curve = result.equity_curve or [result.initial_equity]
    trades = result.trades

    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = round(gross_wins / gross_losses, 4) if gross_losses else None

    num = len(pnls)
    avg_win = round(mean(wins), 4) if wins else None
    avg_loss = round(mean(abs(p) for p in losses), 4) if losses else None
    payoff_ratio = round(avg_win / avg_loss, 4) if avg_win is not None and avg_loss else None

    mid = median(pnls) if pnls else None
    std = round(pstdev(pnls), 4) if len(pnls) > 1 else None

    gross_loss = round(abs(sum(losses)), 4) if losses else None
    expectancy_e = None
    if avg_win is not None and avg_loss is not None and num:
        win_rate = len(wins) / num
        loss_rate = len(losses) / num
        expectancy_e = round(win_rate * avg_win - loss_rate * avg_loss, 4)

    maes = [float(t["mae"]) for t in trades if t.get("mae") is not None]
    mfes = [float(t["mfe"]) for t in trades if t.get("mfe") is not None]
    avg_mae = round(mean(maes), 4) if maes else None
    avg_mfe = round(mean(mfes), 4) if mfes else None
    mfe_mae_ratio = round(avg_mfe / avg_mae, 4) if avg_mfe is not None and avg_mae else None

    max_wins = max_losses = 0
    cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
            max_wins = max(max_wins, cur_w)
        elif p < 0:
            cur_l += 1
            cur_w = 0
            max_losses = max(max_losses, cur_l)

    costs_total = round(sum(float(t["costs"]) for t in trades if t.get("costs")), 4)

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
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff_ratio,
        median_trade=round(mid, 4) if mid is not None else None,
        trade_std=std,
        gross_profit=round(gross_wins, 4) if wins else None,
        gross_loss=gross_loss,
        avg_mae=avg_mae,
        avg_mfe=avg_mfe,
        mfe_mae_ratio=mfe_mae_ratio,
        max_consecutive_wins=max_wins or None,
        max_consecutive_losses=max_losses or None,
        costs_total=costs_total if trades else None,
        expectancy_e=expectancy_e,
        raw={
            "curve_points": len(curve),
            "initial_equity": result.initial_equity,
            "final_equity": result.final_equity,
            "execution_costs_included": True,
        },
    )