"""Modelos del Reconciliation Engine (B4).

La reconciliación compara el estado INTERNO de Atlas contra el estado que
el BROKER declara como real (fuente externa de verdad, 06 §11, §59) y los
clasifica por dominio: órdenes (+fills), posiciones y cuenta.

El motor es de OBSERVACIÓN PURA: produce una clasificación y una señal de
bloqueo; nunca cancela, cierra ni reenvía nada por sí mismo.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReconciliationStatus(str, Enum):
    """Estado global de una reconciliación (B4, sin coordinar kill switches).

    RECONCILIATION_OK:      interno coincide con broker (nada bloquea).
    RECONCILIATION_BLOCKED: hay discrepancia material (MISMATCH /
                            MISSING_INTERNAL / MISSING_BROKER): NO abrir
                            nuevas operaciones hasta resolver.
    RECONCILIATION_UNKNOWN: no se pudo determinar el estado (broker no
                            disponible, o alguna entidad en UNKNOWN): también
                            bloquea por fail-safe (06 §31, §59).
    La coordinación global del kill switch/alertas/recovery es de la capa
    operacional siguiente, NO de este motor.
    """

    RECONCILIATION_OK = "RECONCILIATION_OK"
    RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"
    RECONCILIATION_UNKNOWN = "RECONCILIATION_UNKNOWN"


class DiscrepancyClass(str, Enum):
    """Clasificación de cada entidad comparada.

    MATCH: interno == broker (o ausencia concordante).
    MISMATCH: discrepancia de valor/estado sobre una entidad conocida.
    UNKNOWN: no se puede clasificar (broker no disponible, entidad interna
        o broker-side en estado incierto). Fail-safe: bloquea.
    MISSING_INTERNAL: la entidad existe en el broker pero Atlas no la tiene
        (orden/posición inesperada en broker).
    MISSING_BROKER: la entidad existe en Atlas pero el broker no la reporta
        (orden/posición interna que falta en broker).
    """

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"
    MISSING_INTERNAL = "MISSING_INTERNAL"
    MISSING_BROKER = "MISSING_BROKER"

    @property
    def blocks(self) -> bool:
        """True si esta clasificación impide abrir nuevas operaciones."""
        return self in (
            DiscrepancyClass.MISMATCH,
            DiscrepancyClass.UNKNOWN,
            DiscrepancyClass.MISSING_INTERNAL,
            DiscrepancyClass.MISSING_BROKER,
        )


class Domain(str, Enum):
    ORDER = "ORDER"
    FILL = "FILL"
    POSITION = "POSITION"
    ACCOUNT = "ACCOUNT"
    SYSTEM = "SYSTEM"


# ------------------------------------------------------------------ snapshots


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    """Vista mínima de una orden para comparar (client_order_id como clave)."""

    client_order_id: str
    instrument: str
    quantity: float
    state: str
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None


@dataclass(frozen=True, slots=True)
class FillSnapshot:
    """Vista mínima de un fill agregado por orden."""

    client_order_id: str
    quantity: float
    avg_price: float | None = None
    fill_count: int = 0

    @property
    def filled(self) -> float:
        return self.quantity


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Vista mínima de una posición (quantity dirigida: +long / -short)."""

    instrument: str
    quantity: float
    avg_entry_price: float


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Vista mínima de la cuenta (cash/equity)."""

    account_id: str
    cash: float
    equity: float


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Estado declarado por el broker (fuente externa de verdad).

    `available` False cuando el broker no respondió: el motor devuelve
    RECONCILIATION_UNKNOWN (fail-safe), nunca un MATCH falso.
    """

    orders: tuple[OrderSnapshot, ...] = ()
    fills: tuple[FillSnapshot, ...] = ()
    positions: tuple[PositionSnapshot, ...] = ()
    account: AccountSnapshot | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class InternalSnapshot:
    """Estado que Atlas cree tener (lo que debe verificarse contra el broker)."""

    orders: tuple[OrderSnapshot, ...] = ()
    fills: tuple[FillSnapshot, ...] = ()
    positions: tuple[PositionSnapshot, ...] = ()
    account: AccountSnapshot | None = None


# ------------------------------------------------------------------- diffs


@dataclass(frozen=True, slots=True)
class DiffItem:
    """Entidad comparada y su clasificación (traza determinista)."""

    domain: Domain
    key: str
    classification: DiscrepancyClass
    detail: str = ""
    internal_value: object = None
    broker_value: object = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Resultado de una reconciliación (pura, sin efectos).

    Attributes:
        status: Estado global (OK/BLOCKED/UNKNOWN).
        diffs: Clasificación por entidad (traza completa).
        broker_available: Si el broker respondió (False => UNKNOWN).
        trading_allowed: False si `status != OK` (bloqueo de nuevas
            operaciones; las posiciones existentes NO se tocan).
    """

    status: ReconciliationStatus
    diffs: tuple[DiffItem, ...] = ()
    broker_available: bool = True

    @property
    def trading_allowed(self) -> bool:
        """Regla crítica B4: discrepancia material => NO abrir nuevas ops."""
        return self.status == ReconciliationStatus.RECONCILIATION_OK

    @property
    def blocked(self) -> bool:
        return self.status != ReconciliationStatus.RECONCILIATION_OK

    def by_class(
        self, classification: DiscrepancyClass
    ) -> tuple[DiffItem, ...]:
        return tuple(d for d in self.diffs if d.classification == classification)

    def has(
        self, classification: DiscrepancyClass
    ) -> bool:
        return any(d.classification == classification for d in self.diffs)