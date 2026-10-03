"""Interfaz abstracta de estrategia.

Principio arquitectónico obligatorio (00_MASTER_SPECIFICATION.md §8):

    La estrategia no sabe dónde se ejecutará.

Una estrategia recibe datos de mercado y devuelve un Signal.
Nunca llama a brokers, nunca decide tamaño de posición final,
nunca impone límites de cuenta.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .signal import Signal


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    """Identidad inmutable de una estrategia (09_STRATEGY_LIFECYCLE.md §3)."""

    strategy_id: str
    strategy_name: str
    strategy_family: str
    market: str
    instrument: str
    timeframe: str
    version: str
    parameter_set_version: str
    data_version: str
    status: str = "IDEA"
    creation_date: str | None = None
    owner: str | None = None


class BaseStrategy(ABC):
    """Contrato mínimo que toda estrategia debe implementar."""

    metadata: StrategyMetadata

    @abstractmethod
    def generate_signal(self, data: Any) -> Signal:
        """Genera una señal a partir de datos disponibles en el momento T.

        Obligatorio (RULE-005 / 04_BACKTEST_VALIDATION.md §8):
        solo puede usar información disponible en el momento de la decisión.
        """
        raise NotImplementedError

    def describe(self) -> dict[str, str]:
        """Metadatos trazables de la estrategia."""
        return {
            "strategy_id": self.metadata.strategy_id,
            "strategy_name": self.metadata.strategy_name,
            "strategy_family": self.metadata.strategy_family,
            "market": self.metadata.market,
            "instrument": self.metadata.instrument,
            "timeframe": self.metadata.timeframe,
            "version": self.metadata.version,
            "parameter_set_version": self.metadata.parameter_set_version,
            "data_version": self.metadata.data_version,
            "status": self.metadata.status,
        }