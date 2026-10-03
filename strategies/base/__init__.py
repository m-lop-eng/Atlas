"""Interfaces base de estrategia: Signal y Strategy."""

from .signal import Action, Signal
from .strategy import BaseStrategy

__all__ = ["Action", "BaseStrategy", "Signal"]