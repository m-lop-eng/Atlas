"""Modelos de Monitoring (B6): estados de salud independientes por componente.

El monitor DETECTA; el Kill Switch (B5) DECIDE el bloqueo. Aquí no se
duplica la precedencia de B5: Monitoring produce `HealthStatus` por
componente y un `MonitorSnapshot`; el bloqueo lo resuelve
`KillSwitchCoordinator` a través `apply_to_kill_switch`.

HealthStatus:
    HEALTHY:  todo correcto (y con evidencia reciente de éxito).
    DEGRADED: no crítico (latencia alta, gaps, congestión): NO bloquea.
    FAILED:   condición crítica (stale, desconectado, UNKNOWN de
              reconciliación, reloj inválido...): bloquea.
    UNKNOWN:  sin evidencia (nunca hubo un éxito o no hay observación):
              NUNCA es HEALTHY; fail-safe: bloquea.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MonitorComponent(str, Enum):
    """Componentes independientes con chequeo de salud propio (B6)."""

    DATA_FEED = "DATA_FEED"
    BROKER = "BROKER"
    RECONCILIATION = "RECONCILIATION"
    SESSION = "SESSION"
    SYSTEM = "SYSTEM"
    EXECUTION = "EXECUTION"

    @classmethod
    def ordered(cls) -> "tuple[MonitorComponent, ...]":
        return tuple(cls)  # orden definido del enum = orden determinista


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @property
    def blocks(self) -> bool:
        """FAILED/UNKNOWN son las condiciones que B5 debe tratar como fallo."""
        return self in (HealthStatus.FAILED, HealthStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """Umbrales de salud (configurables, nunca constantes enterradas).

    La "freshness" deriva de `AHORA - last_success > max_age_s` => STALE.
    """

    data_max_age_s: float = 30.0
    data_max_consecutive_gaps: int = 3
    broker_max_age_s: float = 30.0
    broker_max_latency_s: float = 5.0
    broker_max_consecutive_errors: int = 3
    system_max_heartbeat_age_s: float = 60.0
    reconciliation_max_ok_age_s: float = 300.0
    execution_max_pending_orders: int = 10
    execution_max_unknown_orders: int = 0
    execution_max_stalled_orders: int = 3


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """Resultado de un chequeo (con métricas y trazabilidad de auditoría)."""

    component: MonitorComponent
    status: HealthStatus
    at: datetime
    reason: str
    last_success: datetime | None = None
    consecutive_failures: int = 0
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    """Estado de salud agregado (para observabilidad; NO decide bloqueos)."""

    at: datetime
    results: tuple[HealthCheckResult, ...]

    def get(self, component: MonitorComponent) -> HealthCheckResult | None:
        for r in self.results:
            if r.component == component:
                return r
        return None

    def by_status(self, status: HealthStatus) -> tuple[HealthCheckResult, ...]:
        return tuple(r for r in self.results if r.status == status)