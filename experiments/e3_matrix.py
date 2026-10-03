"""Matriz diagnóstica E3: separar la hipótesis de trading del entorno.

Contexto (hallazgo de la fase anterior):
    exp-c494bacc5643 estaba condicionado por 2 restricciones del entorno:
        * max_daily_loss aplicado al P&L ACUMULADO de todo el backtest
          (semántica corregida en ENGINE_VERSION=2: ahora es pérdida del
          día de trading, calendar-day UTC; el RiskEngine NO cambia).
        * max_bars_in_trade que mantiene la posición abierta décimas de
          barras, descartando la mayoría de señales.

Matriz (solo se parametriza la configuración del experimento):
    E3-A   max_daily_loss 5%    max_bars 48    referencia (restricciones reales)
    E3-B   max_daily_loss 20%   max_bars 48    efecto del veto de riesgo
    E3-C   max_daily_loss 20%   max_bars 120   efecto de la ocupación
    E3-D   max_daily_loss ~100% max_bars ∞     capacidad bruta de señales

Los valores laxos de E3-D son EXCLUSIVAMENTE diagnósticos: permiten leer la
hipótesis breakout sin el techo artificial. Tras esta fase, las restricciones
reales del RiskEngine se reintroducen y se revalida cualquier hallazgo.

Ejecución:  python experiments/e3_matrix.py
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
BASELINE_CONFIG = EXPERIMENTS / "first_experiment" / "config.yaml"
PARENT = "exp-bb2cb3867462"


def _exp(type_: str) -> dict:
    return {"experiment": {"type": type_, "parent": PARENT, "hypothesis": "h001-breakout-env"}}


ROWS = [
    (
        "E3-A",
        {
            **_exp("SENSITIVITY"),
            "risk": {"max_daily_loss": 0.05},
            "engine": {"max_bars_in_trade": 48},
        },
    ),
    (
        "E3-B",
        {
            **_exp("SENSITIVITY"),
            "risk": {"max_daily_loss": 0.20},
            "engine": {"max_bars_in_trade": 48},
        },
    ),
    (
        "E3-C",
        {
            **_exp("SENSITIVITY"),
            "risk": {"max_daily_loss": 0.20},
            "engine": {"max_bars_in_trade": 120},
        },
    ),
    (
        "E3-D",
        {
            **_exp("SENSITIVITY"),
            "risk": {"max_daily_loss": 0.9999},
            "engine": {"max_bars_in_trade": 10000},
        },
    ),
]


def _apply(cfg: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(cfg)
    for section, values in overrides.items():
        for key, value in values.items():
            merged[section][key] = value
    return merged


def main() -> int:
    base = yaml.safe_load(BASELINE_CONFIG.read_text(encoding="utf-8"))
    csv_path = ROOT / base["dataset"]["path"]

    rows = []
    for label, overrides in ROWS:
        cfg = _apply(base, overrides)
        report = run_experiment(cfg, csv_path)
        diag = report["diagnostics"]
        rows.append(
            {
                "label": label,
                "experiment_id": report["experiment_id"],
                "max_daily_loss": cfg["risk"]["max_daily_loss"],
                "max_bars_in_trade": cfg["engine"]["max_bars_in_trade"],
                "diagnostics": diag,
                "metrics": report["metrics"],
                "trades_summary": report["trades_summary"],
            }
        )

    header = (
        f"{'Exp':<6}{'id':<18}{'raw':>5}{'flat':>5}{'enpos':>6}"
        f"{'rech':>5}{'ord':>5}{'trd':>5}{'W/L':>7}{'stops':>6}"
        f"{'net%':>8}{'maxDD%':>8}"
    )
    print(header)
    for r in rows:
        ts = r["trades_summary"]
        wl = f"{ts['wins']}/{ts['losses']}"
        print(
            f"{r['label']:<6}{r['experiment_id']:<18}"
            f"{r['diagnostics']['raw_signals']:>5}"
            f"{r['diagnostics']['signals_while_flat']:>5}"
            f"{r['diagnostics']['signals_while_in_position']:>6}"
            f"{r['diagnostics']['risk_rejected_signals']:>5}"
            f"{r['diagnostics']['orders_created']:>5}"
            f"{r['diagnostics']['trades_completed']:>5}"
            f"{wl:>7}"
            f"{ts['stopped']:>6}"
            f"{r['metrics']['net_return'] * 100:>8.2f}"
            f"{r['metrics']['max_drawdown'] * 100:>8.2f}"
        )

    print("\nrechazadas por razón (E3-A):")
    a = rows[0]
    for reason, count in sorted(a["diagnostics"]["risk_rejection_by_reason"].items()):
        print(f"  {reason:<20} {count}")
    print("\nrechazadas por razón (E3-B):")
    b = rows[1]
    for reason, count in sorted(b["diagnostics"]["risk_rejection_by_reason"].items()):
        print(f"  {reason:<20} {count}")
    print("\nrechazadas por razón (E3-D):")
    d = rows[3]
    for reason, count in sorted(d["diagnostics"]["risk_rejection_by_reason"].items()):
        print(f"  {reason:<20} {count}")

    out = EXPERIMENTS / "outputs" / "e3_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())