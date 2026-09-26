"""Cableado Monitoring <-> objetos vivos (B1/B2/B3/B4).

Esta capa convierte los objetos del sistema en observaciones planas y
deterministas para el MonitoringEngine. Es la ÚNICA capa que conocen los
objetos concretos; los checks de Monitoring siguen acoplados solo a las
observaciones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from monitoring.models import MonitorComponent
from monitoring.observations import (
    BrokerObs,
    DataFeedObs,
    ExecutionObs,
    Observations,
    ReconciliationObs,
    SessionObs,
    SystemObs,
)

TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED", "UNKNOWN")


def observe_data_feed(
    engine_state: str | None,
    quality: str | None = None,
    last_event_at: datetime | None = None,
    consecutive_gaps: int = 0,
) -> DataFeedObs:
    return DataFeedObs(
        engine_state=engine_state,
        quality=quality,
        last_event_at=last_event_at,
        consecutive_gaps=consecutive_gaps,
    )


def observe_broker(
    adapter,
    last_success: datetime | None = None,
    consecutive_errors: int = 0,
    latency_s: float | None = None,
) -> BrokerObs:
    """Lee `adapter.health()` (contrato B3); sin lógica específica de broker."""
    connected = False
    try:
        connected = bool(adapter.health())
    except Exception:  # noqa: BLE001 - la conexión rota no debe reventar el monitor
        connected = False
    return BrokerObs(
        connected=connected,
        last_success=last_success,
        consecutive_errors=consecutive_errors,
        last_latency_s=latency_s,
    )


def observe_reconciliation(
    status: str | None,
    checked_at: datetime | None = None,
) -> ReconciliationObs:
    return ReconciliationObs(status=status, checked_at=checked_at)


def observe_reconciliation_report(
    report,
    checked_at: datetime | None = None,
) -> ReconciliationObs:
    """Desde un ReconciliationReport (B4)."""
    status = report.status.value if report is not None else None
    return ReconciliationObs(status=status, checked_at=checked_at)


def observe_session(state: str | None, as_of: datetime | None = None) -> SessionObs:
    return SessionObs(state=state, as_of=as_of)


def observe_system(
    clock: datetime,
    heartbeat_at: datetime | None = None,
    persistence_ok: bool | None = None,
    unhandled_errors: int = 0,
) -> SystemObs:
    return SystemObs(
        clock=clock,
        heartbeat_at=heartbeat_at,
        persistence_ok=persistence_ok,
        unhandled_errors=unhandled_errors,
    )


def observe_execution(
    orders: Iterable | None = None,
    stalled_orders: int = 0,
) -> ExecutionObs:
    """Cuenta abiertas/pendientes y UNKNOWN a partir de estados OrderStatus."""
    open_count = 0
    unknown_count = 0
    for order in orders or ():
        status = getattr(order, "status", None)
        value = getattr(status, "value", str(status))
        if value == "UNKNOWN":
            unknown_count += 1
        elif value not in TERMINAL:
            open_count += 1
    return ExecutionObs(
        open_orders=open_count,
        unknown_orders=unknown_count,
        stalled_orders=stalled_orders,
    )


def collect(
    data_feed: DataFeedObs | None = None,
    broker: BrokerObs | None = None,
    reconciliation: ReconciliationObs | None = None,
    session: SessionObs | None = None,
    system: SystemObs | None = None,
    execution: ExecutionObs | None = None,
) -> Observations:
    return Observations(
        data_feed=data_feed,
        broker=broker,
        reconciliation=reconciliation,
        session=session,
        system=system,
        execution=execution,
    )