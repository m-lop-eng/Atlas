"""Validation: validación entre research y producción (05_RESEARCH.md).

Prototipo → paper → producción miniatura → producción: la validación es
el embudo que decide si una estrategia pasa de etapa (RULE-029).
"""

from .registry import (
    GateResult,
    GateTransitionResult,
    StrategyStage,
    ValidationError,
    evaluate_transition,
    gate_for_transition,
    is_gate_passed,
)

__all__ = [
    "GateResult",
    "GateTransitionResult",
    "StrategyStage",
    "ValidationError",
    "evaluate_transition",
    "gate_for_transition",
    "is_gate_passed",
]
