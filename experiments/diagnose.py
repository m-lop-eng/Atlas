"""Diagnóstico: por qué el baseline tiene solo 4 trades y 3 stops.

Replica la visibilidad del motor (strategy.generate_signal(bars[:i+1]))
sobre TODOS los prefijos, sin simular posiciones, para separar:

    * cuántas rupturas reales produce el breakout (LONG/SHORT),
    * cuántas elimina el filtro SMA50 (variante),
    * cuántas rupturas caen dentro de una posición abierta (se pierden
      por "una posición a la vez", no por la estrategia).

Salida: tabla plana impresa (no forma parte del reporte del experimento).
Ejecución:  python experiments/diagnose.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from data.loader import read_ohlc_csv
from research.pipeline import build_engine, build_strategy

BASELINE_CONFIG = Path(__file__).resolve().parent / "first_experiment" / "config.yaml"
TREND_CONFIG = Path(__file__).resolve().parent / "trend_filter" / "config.yaml"


def _csv_path(cfg: dict) -> Path:
    return ROOT / cfg["dataset"]["path"]


def _count_signals(strategy, bars) -> dict:
    longs = shorts = 0
    for i in range(len(bars)):
        signal = strategy.generate_signal(bars[: i + 1])
        if signal.action == "LONG":
            longs += 1
        elif signal.action == "SHORT":
            shorts += 1
    return {"long": longs, "short": shorts}


def _signals_lost_in_position(bars, report) -> int:
    """Rupturas que el motor no puede operar por estar ya en posición."""
    spans = [(t["entry_index"], t["exit_index"]) for t in report["trades"]]
    lost = 0
    for i in range(len(bars)):
        if any(e <= i < x for (e, x) in spans):
            last = bars[i]["close"]
            lost += 1
    return lost


def _report_for(cfg, csv_path: Path) -> dict:
    import json

    engine = build_engine(cfg)
    strategy = build_strategy(cfg["strategy"])
    result = engine.run(strategy, read_ohlc_csv(csv_path))
    trades = result.trades
    return {
        "trades": trades,
        "orders": len(engine.order_manager.orders),
        "stops": sum(1 for t in trades if t.get("exit_reason") == "STOP"),
        "time": sum(1 for t in trades if t.get("exit_reason") == "TIME"),
    }


def main() -> int:
    base_cfg = yaml.safe_load(BASELINE_CONFIG.read_text(encoding="utf-8"))
    trend_cfg = yaml.safe_load(TREND_CONFIG.read_text(encoding="utf-8"))
    csv_path = _csv_path(base_cfg)
    bars = read_ohlc_csv(csv_path)

    base_strategy = build_strategy(base_cfg["strategy"])
    trend_strategy = build_strategy(trend_cfg["strategy"])
    base_counts = _count_signals(base_strategy, bars)
    trend_counts = _count_signals(trend_strategy, bars)

    base_report = _report_for(base_cfg, csv_path)
    trend_report = _report_for(trend_cfg, csv_path)
    lost_in_pos = _signals_lost_in_position(bars, {"trades": base_report["trades"]})

    print(f"barras                    : {len(bars)}")
    print("--- señales crudas (breakout, visibilidad del motor) ---")
    print(f"baseline  LONG / SHORT    : {base_counts['long']} / {base_counts['short']}")
    print(f"variante  LONG / SHORT    : {trend_counts['long']} / {trend_counts['short']}")
    print(
        f"filtradas por SMA50        : LONG {base_counts['long'] - trend_counts['long']}, "
        f"SHORT {base_counts['short'] - trend_counts['short']}"
    )
    print("--- ejecución sobre el mismo dataset ---")
    print(f"baseline  trades/órdenes/stops/TIME : {base_report['trades'] and len(base_report['trades'])}/{base_report['orders']}/{base_report['stops']}/{base_report['time']}")
    print(f"variante  trades/órdenes/stops/TIME : {len(trend_report['trades'])}/{trend_report['orders']}/{trend_report['stops']}/{trend_report['time']}")
    print(f"rupturas perdidas por posición abierta (baseline): {lost_in_pos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())