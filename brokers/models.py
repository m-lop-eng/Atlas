"""Modelos normalizados del contrato de broker (B3).

Todo adapter concreto (paper, IBKR, MT5, crypto) traduce su formato nativo a
este modelo broker-independiente. La lógica del broker concreto vive SOLO en
el adapter (06_EXECUTION_OPERATION.md §4.4).

Fuente externa de verdad: `BrokerOrder`, `Position` y `AccountState` reflejan
lo que el broker reporta, NO lo que Atlas cree tener.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from execution.order import OrderSide, OrderType


class BrokerOrderState(str, Enum):
    """Estado de una orden tal como lo reporta el broker (fuente de verdad)."""

    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in (
            BrokerOrderState.FILLED,
            BrokerOrderState.CANCELLED,
            BrokerOrderState.REJECTED,
            BrokerOrderState.EXPIRED,
            BrokerOrderState.UNKNOWN,
        )


class SubmitStatus(str, Enum):
    """Disposición de una llamada `submit_order` respecto al broker.

    CONFIRMED: el broker respondió y la orden quedó registrada.
    REJECTED: el broker rechazó explícitamente (OrderSubmission.broker_order
        puede contener el motivo).
    UNKNOWN: la respuesta se perdió tras el envío (06 §8). NO es prueba de
        fallo: resolver el estado con `get_order` / `reconcile.resolve_order`
        antes de cualquier reintento (jamás reenviar a ciegas una orden).
    """

    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class BrokerError(Exception):
    """Error base del dominio de broker."""


class BrokerConnectionError(BrokerError):
    """No se pudo contactar con el broker (desconectado / sin red)."""


class OrderNotFoundError(BrokerError):
    """La orden consultada no existe en el broker."""


class OrderRejectedError(BrokerError):
    """El broker rechazó la operación (motivo en `reason`)."""


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """Fill (total o parcial) reportado por el broker."""

    broker_order_id: str
    quantity: float
    price: float
    timestamp: datetime
    commission: float = 0.0


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """Vista broker de una orden (fuente externa de verdad)."""

    client_order_id: str
    broker_order_id: str
    instrument: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    state: BrokerOrderState
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    fills: tuple[BrokerFill, ...] = ()
    reject_reason: str | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def remaining(self) -> float:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True, slots=True)
class OrderSubmission:
    """Resultado de `BrokerAdapter.submit_order`.

    Cuando `status == UNKNOWN`, `broker_order` puede ser None: Atlas NO
    conoce el estado y debe resolverlo consultando al broker antes de
    cualquier reintento (06 §8, §67).
    """

    client_order_id: str
    status: SubmitStatus
    broker_order: BrokerOrder | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Position:
    """Posición neta reportada por el broker (fuente externa de verdad).

    `quantity` es dirigida: positivo = larga, negativo = corta.
    """

    instrument: str
    quantity: float
    avg_entry_price: float
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccountState:
    """Estado de cuenta reportado por el broker."""

    account_id: str
    cash: float
    equity: float
    buying_power: float
    currency: str
    as_of: datetime