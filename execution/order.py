"""Modelo de orden broker-independiente (06_EXECUTION_OPERATION.md §5).

El OrderManager traduce este modelo hacia cada broker mediante adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import count

_order_seq = count(1)


def reset_order_sequence() -> None:
    """Reinicia el secuenciador global de `order_id`.

    El `order_id` de Order se acuña contra un contador de MÓDULO (histórico de
    B7). Al estar compartido entre instancias del proceso, dos reposiciones del
    mismo input dentro de un mismo proceso producirían IDs distintos en el audit
    trail del ForwardRunner. Único uso previsto: build_forward_runner lo llama
    antes de construir su engine para que el replay dtype por barra sea
    byte-a-byte reproducible. `order_id` solo debe ser único DENTRO de cada
    OrderManager; reiniciar el contador no genera colisiones entre instancias.
    """
    global _order_seq
    _order_seq = count(1)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_CHECKED = "RISK_CHECKED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
            OrderStatus.UNKNOWN,
        )


@dataclass(frozen=True, slots=True)
class Order:
    """Orden normalizada, independiente del broker."""

    strategy_id: str
    strategy_version: str
    account_id: str
    instrument: str
    side: OrderSide
    quantity: int | float
    order_type: OrderType
    signal_id: str | None = None
    risk_check_id: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    parent_order_id: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.CREATED
    fill_quantity: int | float = 0
    average_fill_price: float | None = None
    commission: float = 0.0
    slippage: float | None = None
    reject_reason: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))
        if self.order_id is None:
            object.__setattr__(self, "order_id", f"ORD-{next(_order_seq):010d}")
        if not self.strategy_id or not self.account_id or not self.instrument:
            raise ValueError("strategy_id, account_id e instrument son obligatorios")
        if self.quantity <= 0:
            raise ValueError(f"quantity debe ser positivo, recibido {self.quantity}")


@dataclass(frozen=True, slots=True)
class Fill:
    """Ejecución de una orden (total o parcial)."""

    order_id: str
    quantity: int | float
    price: float
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    commission: float = 0.0
    slippage: float | None = None