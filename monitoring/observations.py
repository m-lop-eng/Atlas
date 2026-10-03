"""Observaciones de monitoring (B6): entrada DETERMINISTA de los checks.

El MonitoringEngine trabaja con observaciones planas (dataclasses), no con
objetos vivos; así los checks son puros, deterministas y sin acoplamiento a
ningún broker/engine concreto (la capa `wiring` convierte los objetos de
B1/B2/B3/B4 en estas observaciones).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DataFeedObs:
    """Estado del feed de datos.

    engine_state: EngineState.value de B1 (CONNECTED/STALE/DEGRADED/...).
    quality: DataQuality.value de B1 (HEALTHY/GAP_DETECTED/...).
    last_event_at: último evento recibido (None si NUNCA hubo evento).
    consecutive_gaps: número de gaps consecutivos detectados.
    """

    engine_state: str | None = None
    quality: str | None = None
    last_event_at: datetime | None = None
    consecutive_gaps: int = 0


@dataclass(frozen=True, slots=True)
class BrokerObs:
    """Estado del broker (contrato B3, sin lógica específica de broker).

    connected:      adapter.health() (B3).
    last_success:   última respuesta correcta del broker (None si NUNCA hubo).
    consecutive_errors: errores consecutivos.
    last_latency_s: latencia básica de la última respuesta (None sin medir).
    """

    connected: bool = False
    last_success: datetime | None = None
    consecutive_errors: int = 0
    last_latency_s: float | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationObs:
    """Resultado de la reconciliación B4.

    status: ReconciliationStatus.value o None (sin reconciliación todavía).
    checked_at: cuándo se obtuvo el último resultado.
    """

    status: str | None = None
    checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionObs:
    """Estado de sesión de mercado (B2)."""

    state: str | None = None
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class SystemObs:
    """Salud del sistema.

    clock: reloj actual (debe ser UTC-aware; naive => FAILED).
    heartbeat_at: último heartbeat (None si nunca hubo).
    persistence_ok: None (desconocido) / True / False.
    unhandled_errors: excepciones no controladas recientes.
    """

    clock: datetime | None = None
    heartbeat_at: datetime | None = None
    persistence_ok: bool | None = None
    unhandled_errors: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionObs:
    """Salud de la capa de ejecución.

    open_orders:  número de órdenes abiertas (pendientes de gestión).
    unknown_orders: número de órdenes en estado UNKNOWN (material).
    stalled_orders: operaciones atascadas (ej. sin actualización reciente).
    """

    open_orders: int = 0
    unknown_orders: int = 0
    stalled_orders: int = 0


@dataclass(frozen=True, slots=True)
class Observations:
    """Observaciones disponibles en un tick de monitoring (opcionales)."""

    data_feed: DataFeedObs | None = None
    broker: BrokerObs | None = None
    reconciliation: ReconciliationObs | None = None
    session: SessionObs | None = None
    system: SystemObs | None = None
    execution: ExecutionObs | None = None