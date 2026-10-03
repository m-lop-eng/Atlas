"""Primer experimento Atlas: breakout simple sobre datos sintéticos.

Ejecución:  python experiments/first_experiment/run.py

Delega en el runner compartido (misma mecánica que cualquier variante,
p. ej. experiments/trend_filter/run.py).
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