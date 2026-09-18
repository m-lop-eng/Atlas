"""Monitoring: supervisión en vivo de estrategias.

El motor de alertas (FAST vs SLOW vías, reglas de salud) es parte del
Milestone 2: la interfaz queda definida para que el ejemplo end-to-end
pueda registrar estado y exponerlo sin depender de la infraestructura.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Estado de salud de un componente del motor."""

    component: str
    status: str
    message: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_ok(self) -> bool:
        return self.status == "OK"


@dataclass(frozen=True, slots=True)
class StrategyMonitor:
    """Snapshot de salud para una estrategia (00_MASTER_SPECIFICATION.md §21)."""

    strategy_id: str
    started_at: datetime
    last_signal: datetime | None = None
    signals_count: int = 0
    rejected_count: int = 0
    errors: list[str] = field(default_factory=list)
    last_error: str | None = None

    def record_rejection(self) -> "StrategyMonitor":
        return StrategyMonitor(
            strategy_id=self.strategy_id,
            started_at=self.started_at,
            last_signal=self.last_signal,
            signals_count=self.signals_count + 1,
            rejected_count=self.rejected_count + 1,
            errors=self.errors,
            last_error=self.last_error,
        )