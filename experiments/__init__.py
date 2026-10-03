"""Experimentos Atlas: configuración declarativa + runner compartido.

Cada experimento vive en experiments/<nombre>/config.yaml y se ejecuta con
`python experiments/<nombre>/run.py` (el mismo runner compartido). El
experiment_id es determinista (hash de config + sha256 del dataset).
"""

from experiments.runner import run_experiment_dir

__all__ = ["run_experiment_dir"]