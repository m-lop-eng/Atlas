"""OrderManager: autoridad del ciclo de vida de órdenes.

Responsabilidades (06_EXECUTION_OPERATION.md §4.3):
    * crear órdenes y asignar identificadores
    * validar transiciones de estado
    * prevenir duplicados (client_order_id)
    * gestionar fills parciales
    * mantener trazabilidad completa (signal → order → fill)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .order import Fill, Order, OrderStatus


class OrderStateError(ValueError):
    """Transición de estado inválida o duplicación de orden."""


@dataclass(slots=True)
class OrderManager:
    """Registra y controla órdenes en memoria (MVP).

    En producción el estado persistirá en PostgreSQL y se reconciliará
    contra el broker tras reinicios (06_EXECUTION_OPERATION.md §13).

    Attributes:
        allow_reduced_quantity: Si la cantidad solicitada excede el máximo
            permitido, la orden puede reducirse en lugar de rechazarse.
    """

    allow_reduced_quantity: bool = False
    orders: dict[str, Order] = field(default_factory=dict)
    _max_quantity: int | float | None = None

    def create_order(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        account_id: str,
        instrument: str,
        side,
        quantity: int | float,
        order_type,
        client_order_id: str,
        signal_id: str | None = None,
        **kwargs,
    ) -> Order:
        """Crea una orden con identificación única y contra duplicados.

        Raises:
            OrderStateError: si ya existe una orden activa con el mismo
                client_order_id (política idempotencia).
        """
        if not client_order_id:
            raise OrderStateError("client_order_id es obligatorio (idempotencia)")

        active = [
            o
            for o in self.orders.values()
            if o.client_order_id == client_order_id and not o.status.terminal
        ]
        if active:
            raise OrderStateError(
                f"Orden activa duplicada con client_order_id={client_order_id}"
            )
        if self._max_quantity is not None and quantity > self._max_quantity:
            if not self.allow_reduced_quantity:
                raise OrderStateError(
                    f"quantity {quantity} excede el máximo permitido "
                    f"{self._max_quantity}"
                )
            quantity = self._max_quantity

        order = Order(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            account_id=account_id,
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=order_type,
            client_order_id=client_order_id,
            signal_id=signal_id,
            **kwargs,
        )
        self.orders[order.order_id] = order
        return order

    def approve(self, order_id: str) -> Order:
        """Marca la orden como aprobada (tras el visto bueno del RiskEngine)."""
        return self._transition(order_id, OrderStatus.APPROVED)

    def submit(self, order_id: str) -> Order:
        return self._transition(order_id, OrderStatus.SUBMITTED)

    def acknowledge(self, order_id: str) -> Order:
        return self._transition(order_id, OrderStatus.ACKNOWLEDGED)

    def apply_fill(self, order_id: str, fill: Fill) -> Order:
        """Aplica un fill (total o parcial) sobre una orden vigente.

        Raises:
            OrderStateError: si el fill excede la cantidad solicitada o la
                orden está en estado terminal.
        """
        order = self.orders[order_id]
        if order.status.terminal:
            raise OrderStateError(
                f"Fill sobre orden terminal en estado {order.status.value}"
            )
        if fill.quantity <= 0:
            raise OrderStateError("Fill sin cantidad")
        if order.fill_quantity + fill.quantity > order.quantity:
            raise OrderStateError(
                f"Fill {fill.quantity} excede la cantidad restante de {order.order_id}"
            )

        new_status = (
            OrderStatus.FILLED
            if order.fill_quantity + fill.quantity >= order.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        old_fill = order.fill_quantity
        old_avg = order.average_fill_price
        new_fill = old_fill + fill.quantity

        if old_fill == 0:
            avg_price = fill.price
        else:
            avg_price = (old_avg * old_fill + fill.price * fill.quantity) / new_fill

        updated = replace(
            order,
            status=new_status,
            fill_quantity=new_fill,
            average_fill_price=avg_price,
            commission=order.commission + fill.commission,
            slippage=fill.slippage if fill.slippage is not None else order.slippage,
        )
        self.orders[order_id] = updated
        return updated

    def cancel(self, order_id: str) -> Order:
        return self._transition(order_id, OrderStatus.CANCELLED)

    def reject(self, order_id: str, reason: str) -> Order:
        order = self._transition(order_id, OrderStatus.REJECTED)
        updated = replace(order, reject_reason=reason)
        self.orders[order_id] = updated
        return updated

    def _transition(self, order_id: str, target: OrderStatus) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"Orden desconocida: {order_id}")
        if order.status.terminal:
            raise OrderStateError(
                f"No se puede pasar {order.status.value} → {target.value} "
                f"(orden terminal)"
            )
        self.orders[order_id] = replace(order, status=target)
        return self.orders[order_id]

    def load(self, order: Order) -> None:
        """Recarga una orden persistida (recuperación tras reinicio)."""
        self.orders[order.order_id] = order