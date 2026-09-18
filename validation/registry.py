"""Embudo de validación de estrategias (05_RESEARCH.md §7, RULE-029).

Etapas disjuntas: una estrategia solo avanza cuando supera la puerta de
la etapa anterior. Las puertas requieren criterios explícitos que se
definen por estrategia (no hay guardas vacías silenciosas).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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