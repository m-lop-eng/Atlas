"""Validation: validación entre research y producción (05_RESEARCH.md).

Prototipo → paper → producción miniatura → producción: la validación es
el embudo que decide si una estrategia pasa de etapa (RULE-029).
"""

from .registry import StrategyStage, ValidationError, is_gate_passed

__all__ = ["StrategyStage", "ValidationError", "is_gate_passed"]