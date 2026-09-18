"""Seguimiento de experimentos de investigación (05_RESEARCH.md §10).

Disciplina: un experimento sin hash de configuración y sin datos
versionados no es válido para decidir despliegues.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ExperimentType(str, Enum):
    """Papel del experimento en el grafo de investigación (05_RESEARCH.md §10).

    El tipo pertenece a la METADATA del experimento (sección `experiment:`
    de la config), por lo que formar parte del hash lo invalida si cambia.
    `parent` encadena con el experimento base (p. ej. un ABLATION sobre un
    BASELINE); `hypothesis` agrupa la familia de hipótesis que se está
    destruyendo.
    """

    HYPOTHESIS = "HYPOTHESIS"
    BASELINE = "BASELINE"
    ABLATION = "ABLATION"
    NULL_CONTROL = "NULL_CONTROL"
    SENSITIVITY = "SENSITIVITY"
    STRESS = "STRESS"
    ROBUSTNESS = "ROBUSTNESS"
    OOS = "OOS"
    WALK_FORWARD = "WALK_FORWARD"
    EXECUTION = "EXECUTION"


ZERO_TRADES_AS_TEXT = "0-trades"


def experiment_meta(config: dict) -> dict:
    """Extrae y valida la metadata de experimento desde la config."""
    exp = config.get("experiment", {})
    etype = exp.get("type", ExperimentType.HYPOTHESIS.value)
    if etype not in ExperimentType._value2member_map_:
        raise ValueError(f"Tipo de experimento no reconocido: {etype}")
    return {
        "experiment_type": etype,
        "parent_experiment_id": exp.get("parent"),
        "change": exp.get("change"),
        "hypothesis": exp.get("hypothesis"),
    }


ALLOWED_TRANSITIONS = {
    ExperimentStatus.DRAFT: {ExperimentStatus.RUNNING, ExperimentStatus.ABANDONED},
    ExperimentStatus.RUNNING: {ExperimentStatus.COMPLETED, ExperimentStatus.ABANDONED},
    ExperimentStatus.COMPLETED: {ExperimentStatus.ABANDONED},
    ExperimentStatus.ABANDONED: set(),
}


class ExperimentStateError(ValueError):
    pass


def run_status_transition(current: ExperimentStatus, target: ExperimentStatus) -> None:
    """Valida una transición de estado de experimento."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ExperimentStateError(
            f"Transición inválida: {current.value} → {target.value}"
        )


@dataclass(slots=True)
class ExperimentRun:
    """Un experimento en el pipeline de investigación."""

    strategy_id: str
    config_hash: str
    dataset_hash: str
    status: ExperimentStatus = ExperimentStatus.DRAFT
    experiment_id: str | None = None
    metrics: dict = field(default_factory=dict)
    git_commit: str | None = None
    notes: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if not self.strategy_id or not self.config_hash or not self.dataset_hash:
            raise ValueError("strategy_id, config_hash y dataset_hash son obligatorios")

    def transition(self, target: ExperimentStatus) -> None:
        run_status_transition(self.status, target)
        self.status = target


def make_config_hash(config: dict) -> str:
    """Hash estable de la configuración (orden de claves inmune)."""
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()