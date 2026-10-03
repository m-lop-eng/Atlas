"""Modelo de dominio Signal: la intención de trading emitida por una estrategia.

Un Signal es broker-independent. La ejecución y el dimensionamiento pertenecen
a capas posteriores (RiskEngine, OrderManager, BrokerAdapter).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True, slots=True)
class Signal:
    """Intención de trading generada por una estrategia.

    Attributes:
        strategy_id: Identificador estable del emisor (09_STRATEGY_LIFECYCLE.md §3).
        strategy_version: Versión del código/lógica (semver).
        instrument: Identificador del instrumento (p. ej. "NQ").
        action: Acción propuesta. La cantidad final la decide el RiskEngine.
        timestamp: Momento UTC en que se genera la señal.
        reference_price: Precio de referencia en el momento de la señal.
        target_quantity: Cantidad propuesta (opcional). Puede ser reducida por riesgo.
        stop_price: Precio de stop-loss propuesto (opcional).
        take_profit: Precio de take-profit propuesto (opcional).
        hypothesis_ref: Referencia a la hipótesis (ID de experimento, opcional).
        meta: Campos adicionales trazables.
    """

    strategy_id: str
    strategy_version: str
    instrument: str
    action: Action
    timestamp: datetime | None = None
    reference_price: float | None = None
    target_quantity: int | float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    hypothesis_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", datetime.now(timezone.utc))
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc)
            )
        if not self.strategy_id or not self.strategy_version:
            raise ValueError("strategy_id y strategy_version son obligatorios")