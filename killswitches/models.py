"""Modelos de Kill Switches (B5).

Distinguimos CUATRO posturas de trading que NO deben mezclarse:

    ALLOW             -> nuevas operaciones permitidas.
    BLOCK_NEW_ORDERS  -> no abrir nuevas posiciones; gestionar las
                         existentes según política.
    HALT              -> detener toda nueva actividad de trading.
    EMERGENCY         -> condición crítica que requiere una política de
                         emergencia EXPLÍCITA.

Regla de oro (B5): "bloquear nuevas órdenes" NO es "cerrar posiciones
automáticamente". El kill switch solo autoriza NUEVAS operaciones; la
liquidación de posiciones es una política de emergencia separada y posterior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class KillSwitchState(str, Enum):
    """Postura efectiva de trading (B5). Severidad creciente."""

    ALLOW = "ALLOW"
    BLOCK_NEW_ORDERS = "BLOCK_NEW_ORDERS"
    HALT = "HALT"
    EMERGENCY = "EMERGENCY"

    @property
    def severity(self) -> int:
        """Número de severidad para precedencia determinista."""
        return {
            KillSwitchState.ALLOW: 0,
            KillSwitchState.BLOCK_NEW_ORDERS: 1,
            KillSwitchState.HALT: 2,
            KillSwitchState.EMERGENCY: 3,
        }[self]

    @classmethod
    def most_severe(
        cls, states: "tuple[KillSwitchState, ...]"
    ) -> KillSwitchState:
        """Máxima severidad de un conjunto (precedencia determinista)."""
        return max(states, key=lambda s: s.severity)


class KillSource(str, Enum):
    """Fuente que puede emitir una condición de bloqueo.

    RECONCILIATION: reconciliation engine (B4): BLOCKED/UNKNOWN.
    BROKER:         broker desconectado o estado desconocido.
    DATA_FEED:      data feed inválido o stale (B1).
    SESSION:        MarketSessionManager UNKNOWN/HALTED (B2).
    SYSTEM:         error crítico del sistema.
    MANUAL:         kill switch manual del operador.
    """

    RECONCILIATION = "RECONCILIATION"
    BROKER = "BROKER"
    DATA_FEED = "DATA_FEED"
    SESSION = "SESSION"
    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Condición reportada por una fuente.

    `level == ALLOW` significa "sin condición" (la fuente está clara).
    """

    level: KillSwitchState
    detail: str = ""
    at: datetime = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Traza inmutable de una activación o recovery (auditoría B5)."""

    at: datetime
    action: str
    source: str | None
    previous_level: str
    new_level: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "action": self.action,
            "source": self.source,
            "previous_level": self.previous_level,
            "new_level": self.new_level,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    """Estado efectivo que consume OrderManager / Risk boundary.

    `can_trade` es la señal de autorización de NUEVAS operaciones; no
    implica nada sobre cierre/liquidación de posiciones existentes.
    """

    state: KillSwitchState
    recovery_required: bool
    active: tuple[tuple[KillSource, SourceStatus], ...] = ()

    @property
    def can_trade(self) -> bool:
        return self.state == KillSwitchState.ALLOW

    @property
    def blocked(self) -> bool:
        return self.state != KillSwitchState.ALLOW


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Resultado de un intento de recovery EXPLÍCITO.

    `accepted=False` deja el sistema bloqueado (sin auto-recovery).
    """

    accepted: bool
    state: KillSwitchState
    detail: str = ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)