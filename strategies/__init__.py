"""Estrategias: paquete público.

La capa de estrategia SOLO genera intenciones de trading (Signals).
No debe contener llamadas a brokers, límites de cuenta ni lógica de riesgo.
Ver 01_GENERAL_RULES.md RULE-032 y 06_EXECUTION_OPERATION.md §4.1.
"""

from .base.signal import Action, Signal
from .base.strategy import BaseStrategy, StrategyMetadata
from .breakout import BreakoutParams, BreakoutStrategy

__all__ = [
    "Action",
    "Signal",
    "BaseStrategy",
    "StrategyMetadata",
    "BreakoutParams",
    "BreakoutStrategy",
]