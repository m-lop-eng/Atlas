"""Adaptadores de broker (00_MASTER_SPECIFICATION.md §29).

La estrategia nunca llama directamente a estos adapters.
El OrderManager es el único cliente autorizado.
"""

from .base import BrokerAdapter

__all__ = ["BrokerAdapter"]