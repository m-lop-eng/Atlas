"""Baseline de la primera hipótesis sobre mercado real (Kraken BTC 1h).

Ejecución:  python experiments/real_market_first/run.py
(requiere descargar el dataset antes: python data/market.py XXBTZUSD 60 ...)
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