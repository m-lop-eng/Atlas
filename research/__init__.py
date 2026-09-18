"""Research: pipeline de experimentación (05_RESEARCH.md)."""

from .experiment import ExperimentRun, ExperimentStatus, run_status_transition
from .pipeline import (
    build_engine,
    build_strategy,
    experiment_id_for,
    run_experiment,
    track_experiment,
)

__all__ = [
    "ExperimentRun",
    "ExperimentStatus",
    "run_status_transition",
    "build_engine",
    "build_strategy",
    "experiment_id_for",
    "run_experiment",
    "track_experiment",
]