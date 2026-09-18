"""Análisis diagnóstico de operaciones (E4): lados, distribución, ejecución.

Convierte el registro por-trade del engine (trade_id, gross/costs/net,
MAE/MFE, barras, razón de salida) en agregaciones para el reporte E4:

    * Sides LONG/SHORT/total: señales, trades, wins, losses, avg/median P&L,
      avg bars, stop rate.
    * TRADE_DISTRIBUTION: avg/median/std/min/max de net_pnl, MAE y MFE,
      duración, y cruce duración × exit_reason.

Todas las estadísticas son deterministas y manejan el caso "0 trades".
Las monedas de MAE/MFE son $-equivalentes (unidades × cantidad), igual que
`net_pnl`/`gross_pnl`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

DURATION_BUCKETS = (("0-5", 5), ("6-10", 10), ("11-20", 20), ("21-40", 40), ("40+", None))


def _stat(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "median": None, "std": None, "min": None, "max": None}
    return {
        "avg": round(statistics.mean(values), 6),
        "median": round(statistics.median(values), 6),
        "std": round(statistics.pstdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def duration_bucket(bars: int) -> str:
    for label, top in DURATION_BUCKETS:
        if top is None or bars <= top:
            return label
    return "40+"


def signal_side_counts(diag: dict) -> tuple[int, int]:
    return int(diag.get("raw_signals_long", 0)), int(diag.get("raw_signals_short", 0))


def trade_distribution(trades: list[dict]) -> dict[str, Any]:
    """Distribución de resultados, MAE/MFE y duración cruzada con salida."""
    net = [float(t["net_pnl"]) for t in trades]
    mae = [float(t["mae"]) for t in trades]
    mfe = [float(t["mfe"]) for t in trades]
    dur = [int(t["bars_in_trade"]) for t in trades]

    by_exit: dict[str, Any] = {}
    for t in trades:
        bucket = duration_bucket(int(t["bars_in_trade"]))
        reason = t.get("exit_reason", "?")
        cell = by_exit.setdefault(bucket, {}).setdefault(reason, {"count": 0, "net_pnl": 0.0})
        cell["count"] += 1
        cell["net_pnl"] += float(t["net_pnl"])

    return {
        "net_pnl": _stat(net),
        "mae": _stat(mae),
        "mfe": _stat(mfe),
        "duration": _stat(dur),
        "duration_by_exit": by_exit,
    }


def execution_stats(trades: list[dict], costs_cfg: dict) -> dict[str, Any]:
    """Costes de ejecución: spread+slippage (impacto) y coste total por trade.

    `average_slippage` mide la parte de mercado del coste (spread_ticks ×
    tick_size + slippage_per_side), por lado; `average_cost` incluye además
    comisión. Ambas expresadas como media de qty × 2 × componente.
    """
    tick_size = float(costs_cfg.get("tick_size", 0.0))
    spread_cost = float(costs_cfg.get("spread_ticks", 0.0)) * tick_size
    slippage_component = spread_cost + float(costs_cfg.get("slippage_per_side", 0.0))
    commission = float(costs_cfg.get("commission_per_unit", 0.0))
    per_side = slippage_component + commission

    slips = [float(t["quantity"]) * 2 * slippage_component for t in trades]
    costs = [float(t["costs"]) for t in trades]
    return {
        "average_slippage": round(statistics.mean(slips), 6) if slips else None,
        "average_cost": round(statistics.mean(costs), 6) if costs else None,
        "total_costs": round(sum(costs), 6),
    }


def build_report_sections(
    diag: dict,
    trades: list[dict],
    costs_cfg: dict,
    metrics: dict,
) -> dict[str, Any]:
    """Secciones nuevas del reporte (research engine, no solo net_return)."""
    raw_long, raw_short = signal_side_counts(diag)
    accepted = int(diag.get("orders_created", 0))
    rejected = int(diag.get("risk_rejected_signals", 0))

    trades_by_side = {"LONG": [t for t in trades if t["side"] == "LONG"],
                      "SHORT": [t for t in trades if t["side"] == "SHORT"]}

    sides = {
        "LONG": _side_table(trades_by_side["LONG"], raw_long),
        "SHORT": _side_table(trades_by_side["SHORT"], raw_short),
    }
    return {
        "signals": {
            "raw": int(diag.get("raw_signals", 0)),
            "accepted": accepted,
            "rejected": rejected,
            "rejected_by_reason": diag.get("risk_rejection_by_reason", {}),
            "long": raw_long,
            "short": raw_short,
        },
        "trades_breakdown": {
            "total": len(trades),
            "long": len(trades_by_side["LONG"]),
            "short": len(trades_by_side["SHORT"]),
            "wins": sum(1 for t in trades if t["net_pnl"] > 0),
            "losses": sum(1 for t in trades if t["net_pnl"] < 0),
            "stopped": sum(1 for t in trades if t.get("exit_reason") == "STOP"),
            "time_exits": sum(1 for t in trades if t.get("exit_reason") == "TIME"),
        },
        "performance": {
            "gross_pnl": round(sum(float(t["gross_pnl"]) for t in trades), 6),
            "costs": round(sum(float(t["costs"]) for t in trades), 6),
            "net_pnl": round(sum(float(t["net_pnl"]) for t in trades), 6),
            "return": metrics["net_return"],
            "max_drawdown": metrics["max_drawdown"],
        },
        "trade_distribution": trade_distribution(trades),
        "execution": execution_stats(trades, costs_cfg),
        "sides": sides,
    }


def _side_table(rows: list[dict], raw_signals: int) -> dict[str, Any]:
    pnls = [float(t["net_pnl"]) for t in rows]
    durations = [int(t["bars_in_trade"]) for t in rows]
    mae = [float(t["mae"]) for t in rows]
    mfe = [float(t["mfe"]) for t in rows]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    stopped = sum(1 for t in rows if t.get("exit_reason") == "STOP")
    n = len(rows)
    return {
        "signals": raw_signals,
        "trades": n,
        "wins": wins,
        "losses": losses,
        "avg_pnl": round(statistics.mean(pnls), 6) if n else None,
        "median_pnl": round(statistics.median(pnls), 6) if n else None,
        "avg_bars": round(statistics.mean(durations), 6) if n else None,
        "stop_rate": round(stopped / n, 6) if n else None,
        "avg_mae": round(statistics.mean(mae), 6) if n else None,
        "avg_mfe": round(statistics.mean(mfe), 6) if n else None,
    }