"""Experimento 2 (variante controlada): breakout + filtro de tendencia SMA50.

Ejecución:  python experiments/trend_filter/run.py

Único cambio sobre el baseline exp-c494bacc5643: `params.trend_filter: 50`
(LONG solo si close > SMA(50); SHORT solo si close < SMA(50)).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.runner import run_experiment_dir


def main() -> int:
    return run_experiment_dir(Path(__file__).resolve().parent / "config.yaml")


if __name__ == "__main__":
    sys.exit(main())