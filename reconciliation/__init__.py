"""Motor de reconciliación de estado (B4).

OBSERVAR → COMPARAR → CLASIFICAR → BLOQUEAR SI ES NECESARIO → RESOLVER
de forma EXPLÍCITA. El motor NUNCA cancela, cierra ni reenvía órdenes, ni
modifica el broker: el broker es la fuente externa de verdad.

La coordinación global del kill switch, incidentes, alertas y recovery
pertenece a la capa operacional siguiente (B5+).
"""

from reconciliation.engine import ReconciliationEngine
from reconciliation.models import (
    AccountSnapshot,
    BrokerSnapshot,
    DiffItem,
    DiscrepancyClass,
    Domain,
    FillSnapshot,
    InternalSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    ReconciliationReport,
    ReconciliationStatus,
)
from reconciliation.orchestrator import reconcile_with_broker, resolve_unknown_orders

__all__ = [
    "AccountSnapshot",
    "BrokerSnapshot",
    "DiffItem",
    "DiscrepancyClass",
    "Domain",
    "FillSnapshot",
    "InternalSnapshot",
    "OrderSnapshot",
    "PositionSnapshot",
    "ReconciliationEngine",
    "ReconciliationReport",
    "ReconciliationStatus",
    "reconcile_with_broker",
    "resolve_unknown_orders",
]