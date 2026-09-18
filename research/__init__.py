"""Research: pipeline de experimentación (05_RESEARCH.md)."""

from .experiment import ExperimentRun, ExperimentStatus, run_status_transition

__all__ = ["ExperimentRun", "ExperimentStatus", "run_status_transition"]