"""Kill Switches (B5): mecanismo de bloqueo global de NUEVAS operaciones.

Autoriza (ALLOW) o bloquea (BLOCK_NEW_ORDERS / HALT / EMERGENCY) la creación
de nuevas posiciones basado en condiciones reportadas por fuentes externas
(B1 data, B2 sesión, B4 reconciliación, broker y manual).

NO liquida posiciones, NO envía órdenes y NO se conecta a brokers: la
señal `status().can_trade` es consumida por OrderManager / Risk boundary.
La gestión de posiciones existentes es política de emergencia separada.
"""

from killswitches.coordinator import KillSwitchCoordinator
from killswitches.models import (
    AuditEntry,
    KillSource,
    KillSwitchState,
    KillSwitchStatus,
    RecoveryResult,
    SourceStatus,
)

__all__ = [
    "AuditEntry",
    "KillSource",
    "KillSwitchCoordinator",
    "KillSwitchState",
    "KillSwitchStatus",
    "RecoveryResult",
    "SourceStatus",
]