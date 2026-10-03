"""E6 — Primera hipótesis sobre mercado real: ¿señal diferenciadora?

Repite el mismo pipeline (mismo breakout, mismas salidas, mismos costes,
mismo riesgo) sobre el primer dataset OHLCV real de 1h (Kraken, BTC).

    E6-A  Breakout baseline (real)       (mismo params que E1)
    E6-B  Entrada aleatoria (real)       (control nulo, misma seed)
    E6-C  Breakout invertido (real)      (reversión)

Objetivo: separar "la hipótesis no funciona" de "la hipótesis no funciona
sobre GBM sintético". Solo se comparan comportamientos; no se decide nada
aún sin robustez/OOS.

Ejecución: python experiments/e6_real_market.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline import run_experiment

EXPERIMENTS = Path(__file__).resolve().parent
REAL_CONFIG = EXPERIMENTS / "real_market_first" / "config.yaml"

# Entorno neutralizado (mismo criterio que E5): sin veto diario.
NEUTRAL_ENV = {
    "experiment": {"type": "NULL_CONTROL", "hypothesis": "null"},
    "risk": {"max_daily_loss": 0.9999},
    "engine": {"max_bars_in_trade": 48},
}


def _apply(cfg: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(cfg)
    for section, values in overrides.items():
        for key, value in values.items():
            merged[section][key] = value
    return merged


def main() -> int:
    base = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    csv_path = ROOT / base["dataset"]["path"]

    a = _apply(base, {"experiment": {"type": "BASELINE", "hypothesis": "h002-real-btc-1h"}})

    b = copy.deepcopy(base)
    b["experiment"]["parent"] = None
    b["strategy"] = {
        "strategy_id": "atlas-random-entry",
        "strategy_name": "Random Entry (real, null)",
        "strategy_family": "control",
        "market": base["strategy"]["market"],
        "instrument": base["strategy"]["instrument"],
        "timeframe": base["strategy"]["timeframe"],
        "version": "1.0.0",
        "parameter_set_version": "n1",
        "data_version": base["strategy"]["data_version"],
        "params": {"atr_period": 14, "stop_atr_mult": 2.0, "seed": 42},
    }
    b = _apply(b, NEUTRAL_ENV)

    c = _apply(a, NEUTRAL_ENV)
    c["strategy"]["params"] = dict(base["strategy"]["params"], **{"invert": True})

    rows = []
    for label, cfg in (("E6-A", a), ("E6-B", b), ("E6-C", c)):
        report = run_experiment(cfg, csv_path)
        d = report["diagnostics"]
        perf = report["performance"]
        dist = report["trade_distribution"]
        tb = report["trades_breakdown"]
        rows.append(
            {
                "label": label,
                "experiment_id": report["experiment_id"],
                "strategy_id": cfg["strategy"]["strategy_id"],
                "raw_signals": d["raw_signals"],
                "accepted": d["orders_created"],
                "trades": tb["total"],
                "wins": tb["wins"],
                "losses": tb["losses"],
                "stopped": tb["stopped"],
                "time_exits": tb["time_exits"],
                "gross_pnl": perf["gross_pnl"],
                "costs": perf["costs"],
                "net_pnl": perf["net_pnl"],
                "net_return": perf["return"],
                "max_drawdown": perf["max_drawdown"],
                "avg_net_pnl": dist["net_pnl"]["avg"],
                "avg_mae": dist["mae"]["avg"],
                "avg_mfe": dist["mfe"]["avg"],
                "long": report["sides"]["LONG"],
                "short": report["sides"]["SHORT"],
            }
        )

    header = (
        f"{'Exp':<6}{'id':<18}{'raw':>5}{'acc':>5}{'trd':>5}{'W/L':>7}{'stop':>6}"
        f"{'TIME':>6}{'gross':>10}{'net':>10}{'net%':>8}{'maxDD%':>8}"
    )
    print(header)
    for r in rows:
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"{r['label']:<6}{r['experiment_id']:<18}{r['raw_signals']:>5}"
            f"{r['accepted']:>5}{r['trades']:>5}{wl:>7}{r['stopped']:>6}"
            f"{r['time_exits']:>6}{r['gross_pnl']:>10.0f}{r['net_pnl']:>10.0f}"
            f"{r['net_return'] * 100:>8.2f}{r['max_drawdown'] * 100:>8.2f}"
        )

    print("\nLONG / SHORT por experimento (real):")
    for r in rows:
        lo, sh = r["long"], r["short"]
        print(
            f"  {r['label']} LONG : señales={lo['signals']:>3} trades={lo['trades']:>3} "
            f"W/L={lo['wins']}/{lo['losses']} avg={lo['avg_pnl']} stop={lo['stop_rate']}"
        )
        print(
            f"  {r['label']} SHORT: señales={sh['signals']:>3} trades={sh['trades']:>3} "
            f"W/L={sh['wins']}/{sh['losses']} avg={sh['avg_pnl']} stop={sh['stop_rate']}"
        )

    out = EXPERIMENTS / "outputs" / "e6_real_market.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())