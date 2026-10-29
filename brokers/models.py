"""Modelos normalizados del contrato de broker (B3).

Todo adapter concreto (paper, IBKR, MT5, crypto) traduce su formato nativo a
este modelo broker-independiente. La lógica del broker concreto vive SOLO en
el adapter (06_EXECUTION_OPERATION.md §4.4).

Fuente externa de verdad: `BrokerOrder`, `Position` y `AccountState` reflejan
lo que el broker reporta, NO lo que Atlas cree tener.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


class BrokerAuthError(BrokerError):
    """Credenciales ausentes/inválidas o autenticación fallida (LIVE-09).

    No es un error de transporte: reintentar no ayuda. `submit_allowed=False`,
    `operator_required=True`.
    """


class BrokerPermissionError(BrokerError):
    """Permiso/entorno insuficiente (p.ej. producción prohibida en LIVE-09)."""


class BrokerRateLimitError(BrokerError):
    """Rate limit del proveedor. Reintentable con backoff, nunca submit ciego."""


class BrokerInsufficientFundsError(OrderRejectedError):
    """Rechazo por fondos/margen insuficiente (código del broker inequívoco)."""


class BrokerInvalidOrderError(OrderRejectedError):
    """Rechazo de validación: símbolo/volumen/tipo/precio no operables."""


class BrokerMarketClosedError(OrderRejectedError):
    """Mercado cerrado o trading deshabilitado para el símbolo."""


class BrokerDependencyError(BrokerError):
    """Dependencia externa del adapter no disponible (p.ej. runtime MT5)."""


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """Fill (total o parcial) reportado por el broker.

    `fill_sequence` es la identidad ESTABLE de la ejecucion dentro de la orden
    (monotona por `client_order_id`, asignada por el origen de ejecucion). NO se
    genera al releer/reprocesar; `client_order_id` por si solo NO identifica un
    fill (una orden puede tener varios fills legitimos). Ver F-06A.
    """

    broker_order_id: str
    quantity: float
    price: float
    timestamp: datetime
    commission: float = 0.0
    fill_sequence: int = 0
    # ORDER-07G: POSITION_IDENTIFIER transportado por el deal que origino el
    # fill. `None` = el origen no aporta evidencia valida; no se infiere.
    position_id: str | None = None


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
    """Posición reportada por el broker (fuente externa de verdad).

    `quantity` es dirigida: positivo = larga, negativo = corta.

    ATLAS-MT5-HEDGING-01: identidad de posición (campos ADITIVOS, opcionales).
    Se distinguen EXPLÍCITAMENTE los identificadores MT5:
      * `ticket`      -> POSITION_TICKET (ticket ACTUAL; es lo que MT5 exige en
        `MqlTradeRequest.position`; PUEDE cambiar por operaciones de servicio).
      * `position_id` -> POSITION_IDENTIFIER (identidad ESTABLE durante todo el
        ciclo de vida; es la que se persiste para atribuir la posición a Atlas).
      * `magic`/`comment` -> atributos AUXILIARES de atribución (nunca única
        prueba de propiedad).
    En NETTING pueden quedar en None (una posición neta por símbolo no requiere
    identidad separada).
    """

    instrument: str
    quantity: float
    avg_entry_price: float
    updated_at: datetime
    ticket: str | None = None
    position_id: str | None = None
    magic: int | None = None
    comment: str | None = None
    # ATLAS-MT5-ACCOUNTING-06E: simbolo MT5 ORIGINAL (procedencia), tal como lo
    # reporta el broker, para poder canonicalizar con el resolver sin depender del
    # fallback textual. `None` en caminos no-MT5 (SPOT/genericos).
    mt5_symbol: str | None = None
    # ATLAS-MT5-ACCOUNTING-06I-B3: swap ACUMULADO informado por el broker para la
    # posicion abierta (`POSITION_SWAP`). `None` = dato NO disponible o no
    # capturado, NUNCA cero. Puede ser negativo. Solo metadato: no se suma a
    # equity, no se deriva de dias/swap_long/swap_short ni se usa economicamente.
    swap: float | None = None


@dataclass(frozen=True, slots=True)
class HistoricalDeal:
    """Deal historico crudo reportado por el broker (capa broker, solo lectura).

    Representacion INTERMEDIA entre el adaptador y cualquier ledger contable: NO
    acopla el adaptador a `MT5AccountLedger`. Preserva los campos relevantes para
    contabilidad/trazabilidad SIN clasificar economicamente el deal.

    Identidad: `ticket` es unico por cuenta y estable. `order` PUEDE ser 0
    (depositos/retiradas/creditos) y NO identifica por si solo un fill. `time_msc`
    aporta resolucion de milisegundos para ordenar deals del mismo segundo.
    """

    ticket: int
    order: int
    time: datetime
    time_msc: int
    type: int
    entry: int | None
    position_id: int | None
    symbol: str | None
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float


@dataclass(frozen=True, slots=True)
class AccountState:
    """Estado de cuenta reportado por el broker.

    `account_id` se conserva para comparaciones/contratos, pero NO aparece en la
    representacion publica (`repr`): puede contener el login completo de la cuenta
    y no debe filtrarse a logs/diagnosticos.
    """

    account_id: str = field(repr=False)
    cash: float
    equity: float
    buying_power: float
    currency: str
    as_of: datetime