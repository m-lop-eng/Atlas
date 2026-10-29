"""Modelo de orden broker-independiente (06_EXECUTION_OPERATION.md §5).

El OrderManager traduce este modelo hacia cada broker mediante adapters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import count

# MT5-DEMO-EXECUTION-CONTROLS-02B: PROPOSITO tipado de ejecucion de una orden de
# broker. NO se infiere CLOSE frente a REDUCE solo por `reduce_only=True`.
PURPOSE_OPEN = "OPEN"
PURPOSE_CLOSE = "CLOSE"
PURPOSE_REDUCE = "REDUCE"
_KNOWN_PURPOSES = frozenset({PURPOSE_OPEN, PURPOSE_CLOSE, PURPOSE_REDUCE})

_order_seq = count(1)

_QUANTITY_EPS = 1e-9
_STEP_TOLERANCE = 1e-6


def validate_quantity(value: object, *, error_cls: type[Exception] = ValueError) -> float:
    """Valida una cantidad numerica real, positiva y finita.

    ORDER-08B: `bool` es rechazado aunque sea subtipo de `int`; no se aceptan
    strings ni conversiones implicitas. El retorno es `float` solo despues de
    validar el valor original.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_cls(
            f"quantity invalida: se requiere int/float no booleano, "
            f"recibido {value!r}"
        )
    try:
        qty = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise error_cls(f"quantity invalida: {value!r}") from exc
    if not math.isfinite(qty):
        raise error_cls(f"quantity debe ser finita, recibido {value!r}")
    if qty <= 0:
        raise error_cls(f"quantity debe ser positivo, recibido {value!r}")
    return qty


def validate_quantity_lattice(
    value: object,
    *,
    minimum: object | None = None,
    maximum: object | None = None,
    step: object | None = None,
    error_cls: type[Exception] = ValueError,
) -> float:
    """Valida cantidad y lattice con ancla en `minimum`.

    Convencion ORDER-08B: si `minimum` esta disponible, los volumenes validos
    son `minimum + N*step`; si no, el ancla es cero. Es la misma regla usada
    por el adapter y por la reduccion coordinada. La tolerancia es la existente
    en el proyecto (1e-9 para limites, 1e-6 para pasos).
    """
    qty = validate_quantity(value, error_cls=error_cls)

    def _metadata(raw: object, name: str) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise error_cls(
                f"{name} invalido: se requiere int/float no booleano, "
                f"recibido {raw!r}"
            )
        try:
            parsed = float(raw)
        except (OverflowError, TypeError, ValueError) as exc:
            raise error_cls(f"{name} invalido: {raw!r}") from exc
        if not math.isfinite(parsed):
            raise error_cls(f"{name} debe ser finito, recibido {raw!r}")
        return parsed

    min_value: float | None = None
    max_value: float | None = None
    step_value: float | None = None
    if minimum is not None:
        min_value = _metadata(minimum, "quantity_min")
        if min_value < 0:
            raise error_cls("quantity_min debe ser >= 0")
    if maximum is not None:
        max_value = _metadata(maximum, "quantity_max")
        if max_value <= 0:
            raise error_cls("quantity_max debe ser > 0")
    if step is not None:
        step_value = _metadata(step, "quantity_step")
        if step_value <= 0:
            raise error_cls("quantity_step debe ser > 0")
    if (
        min_value is not None
        and max_value is not None
        and max_value < min_value - _QUANTITY_EPS
    ):
        raise error_cls("quantity_max debe ser >= quantity_min")

    if min_value is not None and qty < min_value - _QUANTITY_EPS:
        raise error_cls(f"quantity {qty} < quantity_min {min_value}")
    if max_value is not None and qty > max_value + _QUANTITY_EPS:
        raise error_cls(f"quantity {qty} > quantity_max {max_value}")
    if step_value is not None:
        anchor = min_value if min_value is not None else 0.0
        steps = (qty - anchor) / step_value
        if abs(steps - round(steps)) > _STEP_TOLERANCE:
            raise error_cls(
                f"quantity {qty} no alineada al step {step_value} "
                f"(ancla {anchor})"
            )
    return qty


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
        # S-04-a: UNKNOWN es TRANSITORIO (no terminal): una orden cuyo resultado
        # no pudo determinarse tras el envio debe poder resolverse por broker
        # lookup. Terminales: FILLED/CANCELLED/REJECTED/EXPIRED/FAILED.
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
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
    # ATLAS-MT5-HEDGING-01: objetivo de posicion para CLOSE/REDUCE dirigidos
    # (POSITION_IDENTIFIER). OBLIGATORIO en HEDGING restringido; None en NETTING
    # (la posicion neta se resuelve por simbolo). NO representa el resultado de
    # una apertura.
    target_position_id: str | None = None
    # 02B: proposito de ejecucion (OPEN/CLOSE/REDUCE). CLOSE y REDUCE son
    # tipados distintos aunque ambos viajen con `reduce_only=True`.
    purpose: str = PURPOSE_OPEN
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
        validate_quantity(self.quantity, error_cls=ValueError)
        purpose = str(self.purpose)
        if purpose not in _KNOWN_PURPOSES:
            raise ValueError(f"purpose desconocido: {self.purpose!r}")
        object.__setattr__(self, "purpose", purpose)
        # CLOSE/REDUCE son operaciones explicitamente reductoras.
        if purpose in (PURPOSE_CLOSE, PURPOSE_REDUCE) and not self.reduce_only:
            raise ValueError(
                f"purpose {purpose} requiere reduce_only=True (no inversion)"
            )


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