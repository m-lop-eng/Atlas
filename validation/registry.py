"""Embudo de validación de estrategias (05_RESEARCH.md §7, RULE-029).

Etapas disjuntas: una estrategia solo avanza cuando supera la puerta de
la etapa anterior. Las puertas requieren criterios explícitos que se
definen por estrategia (no hay guardas vacías silenciosas).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StrategyStage(str, Enum):
    PROTOTYPE = "prototype"
    BACKTEST = "backtest"
    PAPER = "paper"
    SMALL_PRODUCTION = "small_production"
    PRODUCTION = "production"


PROTOTYPE_LIMIT, BACKTEST_LIMIT, PAPER_LIMIT, SMALL_LIMIT, PRODUCTION_LIMIT = range(5)
STAGE_ORDER = [str(s.value) for s in StrategyStage]


class ValidationError(ValueError):
    pass


@dataclass(slots=True)
class GateResult:
    """Resultado de evaluar la puerta de una etapa."""

    stage: StrategyStage
    passed: bool
    requirements: dict[str, bool] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        failed = [k for k, v in self.requirements.items() if not v]
        return "PASS" if self.passed else f"FAIL: {', '.join(failed)}"


def is_gate_passed(stage: StrategyStage, requirements: dict[str, bool]) -> GateResult:
    """Evalúa una puerta: TODOS los requisitos deben cumplirse."""
    if not requirements:
        raise ValidationError(
            f"Etapa {stage.value}: una puerta sin requisitos no puede aprobarse"
        )
    result = GateResult(stage=stage, passed=all(requirements.values()), requirements=requirements)
    return result


# ---------------------------------------------------------------------------
# G1 — Integración de gates en el lifecycle (M4)
# ---------------------------------------------------------------------------
#
# Las puertas viven aquí (API pura); la autoridad de estado del lifecycle vive
# en research/evidence + research/hypothesis. `evaluate_transition` calcula el
# GateResult de una transición y NUNCA promueve por sí sola: el llamante (flujo
# de research) persiste el resultado y decide. Un fallo de gate = parada.


@dataclass(frozen=True, slots=True)
class GateTransitionResult:
    """Resultado persistible de evaluar una transición de etapa."""

    strategy_id: str
    from_stage: str
    to_stage: str
    passed: bool
    requirements: dict[str, bool]
    decided_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def gate_for_transition(
    from_stage: StrategyStage,
    to_stage: StrategyStage,
    requirements: dict[str, bool],
) -> GateResult:
    """Evalúa la puerta que habilita `from_stage -> to_stage`.

    La puerta evaluada es la de la etapa de ORIGEN (debe estar superada para
    avanzar). Requisitos explícitos obligatorios (no puertas vacías).
    """
    if from_stage is to_stage:
        raise ValidationError("Transición a la misma etapa: sin promoción.")
    return is_gate_passed(from_stage, requirements)


def evaluate_transition(
    strategy_id: str,
    from_stage: StrategyStage,
    to_stage: StrategyStage,
    requirements: dict[str, bool],
    *,
    decided_at: str,
) -> GateTransitionResult:
    """Devuelve el resultado de la transición SIN mutar estado.

    Invariante: `passed=False` ⇒ no hay promoción (el llamante no debe avanzar).
    """
    gate = gate_for_transition(from_stage, to_stage, requirements)
    return GateTransitionResult(
        strategy_id=strategy_id,
        from_stage=from_stage.value,
        to_stage=to_stage.value,
        passed=gate.passed,
        requirements=dict(gate.requirements),
        decided_at=decided_at,
    )


__all__ = [
    "GateResult",
    "GateTransitionResult",
    "PROTOTYPE_LIMIT",
    "STAGE_ORDER",
    "StrategyStage",
    "ValidationError",
    "evaluate_transition",
    "gate_for_transition",
    "is_gate_passed",
]