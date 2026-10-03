"""Monitoring (B6): observabilidad y evaluación de salud independiente.

Fronteras: el monitor NO envía/cancela órdenes, NO decide la estrategia,
NO modifica riesgo, NO liquida posiciones. Detecta; B5 decide.
"""

from monitoring.checks import DEFAULT_CHECKS, utc_now
from monitoring.engine import (
    KillSwitchTransition,
    MonitoringEngine,
    apply_to_kill_switch,
)
from monitoring.models import (
    HealthCheckResult,
    HealthStatus,
    MonitorComponent,
    MonitorConfig,
    MonitorSnapshot,
)
from monitoring.observations import (
    BrokerObs,
    DataFeedObs,
    ExecutionObs,
    Observations,
    ReconciliationObs,
    SessionObs,
    SystemObs,
)
from monitoring.wiring import (
    collect,
    observe_broker,
    observe_data_feed,
    observe_execution,
    observe_reconciliation,
    observe_reconciliation_report,
    observe_session,
    observe_system,
)

__all__ = [
    "KillSwitchTransition",
    "MonitoringEngine",
    "apply_to_kill_switch",
    "MonitorComponent",
    "MonitorConfig",
    "MonitorSnapshot",
    "HealthCheckResult",
    "HealthStatus",
    "Observations",
    "DataFeedObs",
    "BrokerObs",
    "ReconciliationObs",
    "SessionObs",
    "SystemObs",
    "ExecutionObs",
    "collect",
    "observe_broker",
    "observe_data_feed",
    "observe_execution",
    "observe_reconciliation",
    "observe_reconciliation_report",
    "observe_session",
    "observe_system",
    "DEFAULT_CHECKS",
    "utc_now",
]