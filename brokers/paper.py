"""Adaptador PAPER/sandbox determinista (B3).

Simula un broker en memoria SIN red ni dinero real:

  * fill de mercado inmediato contra un `price_source` inyectable
  * fill parcial programable (`simulate_fill`) para PARTIALLY_FILLED
  * rechazo programable (`simulate_rejection`)
  * pérdida de respuesta tras el envío (`simulate_response_loss`): la orden
    queda en UNKNOWN y se resuelve con `get_order` (06 §8)
  * idempotencia por `client_order_id`: reintentar la misma orden NUNCA
    duplica exposición

Es la pieza de prueba del contrato `BrokerAdapter` (B3) y la base de
integración con el OrderManager (fases posteriores).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count

from execution.order import Order, OrderType

from brokers.base import BrokerAdapter
from brokers.models import (
    AccountState,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderState,
    BrokerConnectionError,
    OrderNotFoundError,
    OrderRejectedError,
    OrderSubmission,
    Position,
    SubmitStatus,
)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class PaperBrokerAdapter(BrokerAdapter):
    """Broker simulado determinista para desarrollo/paper trading.

    Attributes:
        account_id: Cuenta simulada (papel).
        initial_cash: Caja inicial de la cuenta papel.
        price_source: cb(instrumento: str) -> precio de referencia para fills.
        clock: cb() -> datetime actual (inyectable para determinismo).
        currency: Divisa de la cuenta.
    """

    account_id: str = "PAPER-ACCOUNT"
    initial_cash: float = 100_000.0
    price_source: object = None
    clock: object = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.price_source is None:
            self.price_source = lambda instrument: 50_000.0
        if self.clock is None:
            self.clock = _default_clock
        self._connected = False
        self._orders: dict[str, BrokerOrder] = {}
        self._lossy: set[str] = set()  # client_order_ids con respuesta perdida
        self._broker_seq = count(1)
        self._cash = self.initial_cash

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> bool:
        return self._connected

    # -------------------------------------------------------------- orders

    def submit_order(self, order: Order) -> OrderSubmission:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (submit_order)"
            )
        if not order.quantity or order.quantity <= 0:
            raise BrokerError("quantity debe ser positivo")
        if order.client_order_id in self._orders:
            # Idempotencia: reintento de una orden ya conocida. Si la primera
            # respuesta se perdió, Atlas debe resolver con get_order; aquí
            # JAMÁS duplicamos exposición.
            return OrderSubmission(
                client_order_id=order.client_order_id,
                status=SubmitStatus.CONFIRMED,
                broker_order=self._orders[order.client_order_id],
                detail="client_order_id ya registrado (idempotente)",
            )

        now = self.clock()
        broker_order_id = f"BRK-{next(self._broker_seq):06d}"
        fills: tuple[BrokerFill, ...] = ()

        if order.order_type == OrderType.MARKET:
            price = self._price(order.instrument)
            if (order.side.value == "BUY"
                    and self._cash < order.quantity * price):
                broker_order = self._new_order(
                    order, broker_order_id, now, BrokerOrderState.REJECTED,
                    reject_reason="caja insuficiente",
                )
                self._orders[order.client_order_id] = broker_order
                return OrderSubmission(
                    client_order_id=order.client_order_id,
                    status=SubmitStatus.REJECTED,
                    broker_order=broker_order,
                    detail="rechazada por caja insuficiente",
                )
            qty = float(order.quantity)
            if order.side.value == "BUY":
                self._cash -= qty * price
            else:
                self._cash += qty * price
            broker_order = self._new_order(
                order, broker_order_id, now, BrokerOrderState.FILLED,
                filled_quantity=qty, avg_fill_price=price,
                fills=(BrokerFill(
                    broker_order_id=broker_order_id,
                    quantity=qty, price=price, timestamp=now,
                ),),
            )
        else:
            # LIMIT / STOP y resto: quedan pendientes (no se enfilan).
            broker_order = self._new_order(
                order, broker_order_id, now, BrokerOrderState.SUBMITTED,
                limit_price=order.limit_price,
            )

        self._orders[order.client_order_id] = broker_order

        if order.client_order_id in self._lossy:
            # La orden SÍ llegó al broker, pero la respuesta se perdió (06 §8).
            # Atlas debe resolver con get_order; no reenviar.
            return OrderSubmission(
                client_order_id=order.client_order_id,
                status=SubmitStatus.UNKNOWN,
                broker_order=None,
                detail="respuesta perdida tras el envío: resolver con get_order",
            )
        return OrderSubmission(
            client_order_id=order.client_order_id,
            status=SubmitStatus.CONFIRMED,
            broker_order=broker_order,
        )

    def cancel_order(self, client_order_id: str) -> BrokerOrder:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (cancel_order)"
            )
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError(
                f"Orden {client_order_id} desconocida para el broker"
            )
        if order.state in (BrokerOrderState.FILLED, BrokerOrderState.CANCELLED):
            raise OrderRejectedError(
                f"Orden {client_order_id} en estado {order.state.value}: "
                f"no se puede cancelar"
            )
        cancelled = self._update_state(
            order, BrokerOrderState.CANCELLED
        )
        self._orders[client_order_id] = cancelled
        return cancelled

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (get_order)"
            )
        return self._orders.get(client_order_id)

    def get_open_orders(self) -> list[BrokerOrder]:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (get_open_orders)"
            )
        return [
            o for o in self._orders.values()
            if o.state in (
                BrokerOrderState.SUBMITTED,
                BrokerOrderState.PARTIALLY_FILLED,
            )
        ]

    def get_positions(self) -> list[Position]:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (get_positions)"
            )
        # Posiciones de una sola cuenta papel: suma neta por instrumento.
        net: dict[str, float] = {}
        for o in self._orders.values():
            if not o.fills or o.state == BrokerOrderState.CANCELLED:
                continue
            sign = 1.0 if o.side.value == "BUY" else -1.0
            total_qty = sum(f.quantity for f in o.fills)
            net[o.instrument] = net.get(o.instrument, 0.0) + sign * total_qty
        return [
            Position(
                instrument=sym, quantity=qty,
                avg_entry_price=self._avg_entry(sym, qty),
                updated_at=self.clock(),
            )
            for sym, qty in net.items()
            if qty != 0
        ]

    def get_account_state(self) -> AccountState:
        if not self._connected:
            raise BrokerConnectionError(
                f"Broker {self.account_id}: no conectado (get_account_state)"
            )
        positions = self.get_positions()
        # Equity: caja + valor a mercado de las posiciones (precio de referencia).
        equity = self._cash + sum(
            abs(p.quantity) * self._price(p.instrument) for p in positions
        )
        return AccountState(
            account_id=self.account_id,
            cash=self._cash,
            equity=equity,
            buying_power=self._cash,
            currency=self.currency,
            as_of=self.clock(),
        )

    # -------------------------------------------------- simuladores (test)

    def simulate_fill(self, client_order_id: str, quantity: float,
                      price: float) -> BrokerOrder:
        """Simula un fill parcial/total sobre una orden pendiente."""
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError(
                f"Orden {client_order_id} desconocida para el broker"
            )
        if order.state in (BrokerOrderState.FILLED, BrokerOrderState.CANCELLED):
            raise OrderRejectedError(f"Orden {client_order_id} ya terminal")
        if quantity <= 0 or order.filled_quantity + quantity > order.quantity:
            raise BrokerError("fill supera la cantidad restante")

        if order.side.value == "BUY":
            self._cash -= quantity * price
        else:
            self._cash += quantity * price

        new_filled = order.filled_quantity + quantity
        new_fills = order.fills + (
            BrokerFill(
                broker_order_id=order.broker_order_id,
                quantity=quantity, price=price, timestamp=self.clock(),
            ),
        )
        if new_filled >= order.quantity - 1e-9:
            state = BrokerOrderState.FILLED
        else:
            state = BrokerOrderState.PARTIALLY_FILLED
        # promedio ponderado por precio de los fills
        total_qty = sum(f.quantity for f in new_fills)
        avg = sum(f.quantity * f.price for f in new_fills) / total_qty
        updated = self._update_state(
            order, state,
            filled_quantity=new_filled,
            avg_fill_price=avg,
            fills=new_fills,
        )
        self._orders[client_order_id] = updated
        return updated

    def simulate_rejection(self, client_order_id: str, reason: str) -> BrokerOrder:
        """Simula un rechazo tardío del broker."""
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError(
                f"Orden {client_order_id} desconocida para el broker"
            )
        rejected = self._update_state(
            order, BrokerOrderState.REJECTED, reject_reason=reason
        )
        self._orders[client_order_id] = rejected
        return rejected

    def simulate_response_loss(self, client_order_id: str) -> None:
        """Marca una orden para que `submit_order` devuelva UNKNOWN."""
        self._lossy.add(client_order_id)

    # -------------------------------------------------------------- helpers

    def _price(self, instrument: str) -> float:
        return float(self.price_source(instrument))

    def _avg_entry(self, instrument: str, net_qty: float) -> float:
        """Precio medio de la cantidad REALMENTE abierta (FIFO por lotes).

        El promedio bruto de TODOS los fills del lado de la posición (el código
        anterior) diluye el precio medio con la cantidad que YA se cerró en un
        trade previo: tras un cierre y reapertura, el broker divergía del precio
        medio del engine y la reconciliación bloqueaba por un falso positivo
        (segunda operación de una sesión multi-trade).

        Método: se reconstruye el coste en lotes FIFO tomando los fills por orden
        de envío y descontando los del lado contrario; el promedio solo cubre el
        neto abierto. Sin posición -> precio de referencia (compatibilidad).
        """
        lots: list[list[float]] = []  # [cantidad_abierta, precio]
        pos_sign = 1.0 if net_qty > 0 else -1.0
        orders = sorted(
            (
                o for o in self._orders.values()
                if o.instrument == instrument
                and o.state != BrokerOrderState.CANCELLED
                and o.fills
            ),
            key=lambda o: (o.submitted_at, o.broker_order_id),
        )
        for o in orders:
            o_sign = 1.0 if o.side.value == "BUY" else -1.0
            for f in o.fills:
                if o_sign == pos_sign:
                    lots.append([f.quantity, f.price])
                else:
                    rem = f.quantity
                    while rem > 1e-12 and lots:
                        take = min(lots[0][0], rem)
                        lots[0][0] -= take
                        rem -= take
                        if lots[0][0] <= 1e-12:
                            lots.pop(0)
        total_qty = sum(l[0] for l in lots)
        if not total_qty:
            return self._price(instrument)
        return sum(l[0] * l[1] for l in lots) / total_qty

    def _new_order(self, order: Order, broker_order_id: str, now: datetime,
                   state: BrokerOrderState, *, filled_quantity: float = 0.0,
                   avg_fill_price: float | None = None,
                   fills: tuple[BrokerFill, ...] = (),
                   limit_price: float | None = None,
                   reject_reason: str | None = None) -> BrokerOrder:
        return BrokerOrder(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            instrument=order.instrument,
            side=order.side,
            order_type=order.order_type,
            quantity=float(order.quantity),
            state=state,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
            limit_price=limit_price,
            fills=fills,
            reject_reason=reject_reason,
            submitted_at=now,
            updated_at=now,
        )

    def _update_state(self, order: BrokerOrder, state: BrokerOrderState, *,
                      filled_quantity: float | None = None,
                      avg_fill_price: float | None = None,
                      fills: tuple[BrokerFill, ...] | None = None,
                      reject_reason: str | None = None) -> BrokerOrder:
        from dataclasses import replace

        return replace(
            order,
            state=state,
            filled_quantity=(
                filled_quantity if filled_quantity is not None
                else order.filled_quantity
            ),
            avg_fill_price=(
                avg_fill_price if avg_fill_price is not None
                else order.avg_fill_price
            ),
            fills=fills if fills is not None else order.fills,
            reject_reason=(
                reject_reason if reject_reason is not None
                else order.reject_reason
            ),
            updated_at=self.clock(),
        )


__all__ = ["PaperBrokerAdapter"]