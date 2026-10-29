"""OrderManager: autoridad del ciclo de vida de órdenes.

Responsabilidades (06_EXECUTION_OPERATION.md §4.3):
    * crear órdenes y asignar identificadores
    * validar transiciones de estado
    * prevenir duplicados (client_order_id)
    * gestionar fills parciales
    * mantener trazabilidad completa (signal → order → fill)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .order import Fill, Order, OrderStatus


class OrderStateError(ValueError):
    """Transición de estado inválida o duplicación de orden."""


# F-07: grafo contractual de transiciones (fuente unica de verdad).
# Reconstruido de 06_EXECUTION_OPERATION.md §7 (lifecycle) y de los callers
# reales (backtesting/papertrading: create -> approve -> submit ->
# acknowledge/fill/reject/cancel). Los estados terminales no admiten salida.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({
        OrderStatus.RISK_CHECKED,
        OrderStatus.APPROVED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    }),
    OrderStatus.RISK_CHECKED: frozenset({
        OrderStatus.APPROVED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    }),
    OrderStatus.APPROVED: frozenset({
        OrderStatus.SUBMITTING,
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    }),
    OrderStatus.SUBMITTING: frozenset({
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
        OrderStatus.FAILED,
    }),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
        OrderStatus.FAILED,
    }),
    OrderStatus.ACKNOWLEDGED: frozenset({
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
        OrderStatus.FAILED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
        OrderStatus.FAILED,
    }),
    # S-04-a: UNKNOWN es TRANSITORIO (no terminal). Salidas congeladas = estados
    # que el adapter concreto puede devolver en `get_order` (broker lookup):
    # SUBMITTED/PARTIALLY_FILLED/FILLED/REJECTED/CANCELLED. NO self-loop.
    OrderStatus.UNKNOWN: frozenset({
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    }),
    # Terminales: sin salidas.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


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

    def mark_submitting(self, order_id: str) -> Order:
        """Registra que el envio va a intentarse (frontera ORDER-07B)."""
        return self._transition(order_id, OrderStatus.SUBMITTING)

    def mark_failed(self, order_id: str, reason: str = "") -> Order:
        """Fallo operativo terminal demostrado sin envio."""
        order = self._transition(order_id, OrderStatus.FAILED)
        if reason:
            updated = replace(order, reject_reason=reason)
            self.orders[order_id] = updated
            return updated
        return order

    def sync_broker_state(
        self,
        order_id: str,
        *,
        state: OrderStatus,
        filled_quantity: int | float | None = None,
        average_fill_price: float | None = None,
        commission: float | None = None,
    ) -> Order:
        """Sincroniza el estado agregado observado en el broker (idempotente).

        No crea fills ni infiere ejecucion: `state`/`filled_quantity`/precio son
        evidencia ya observada. No permite retroceder desde un estado terminal ni
        reducir la cantidad acumulada.
        """
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"Orden desconocida: {order_id}")
        if not isinstance(state, OrderStatus):
            raise OrderStateError("state debe ser OrderStatus")
        if order.status.terminal:
            return order

        qty = order.fill_quantity
        if filled_quantity is not None:
            try:
                candidate = float(filled_quantity)
            except (TypeError, ValueError) as exc:
                raise OrderStateError(
                    f"filled_quantity invalida: {filled_quantity!r}"
                ) from exc
            if not math.isfinite(candidate) or candidate < 0.0:
                raise OrderStateError(
                    f"filled_quantity invalida: {filled_quantity!r}"
                )
            if candidate > float(order.quantity) + 1e-9:
                raise OrderStateError(
                    f"filled_quantity {candidate} excede quantity {order.quantity}"
                )
            if candidate + 1e-9 < float(order.fill_quantity):
                return order  # no regresion de fills
            qty = candidate

        avg = order.average_fill_price
        if average_fill_price is not None:
            try:
                candidate_avg = float(average_fill_price)
            except (TypeError, ValueError) as exc:
                raise OrderStateError(
                    f"average_fill_price invalido: {average_fill_price!r}"
                ) from exc
            if not math.isfinite(candidate_avg):
                raise OrderStateError(
                    f"average_fill_price invalido: {average_fill_price!r}"
                )
            avg = candidate_avg

        comm = order.commission
        if commission is not None:
            try:
                candidate_comm = float(commission)
            except (TypeError, ValueError) as exc:
                raise OrderStateError(
                    f"commission invalida: {commission!r}"
                ) from exc
            if not math.isfinite(candidate_comm):
                raise OrderStateError(f"commission invalida: {commission!r}")
            comm = candidate_comm

        updated = replace(
            order,
            status=state,
            fill_quantity=qty,
            average_fill_price=avg,
            commission=comm,
        )
        self.orders[order_id] = updated
        return updated

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

    def mark_unknown(self, order_id: str) -> Order:
        """Marca una orden enviada como UNKNOWN (resultado no determinable).

        LIVE-07: tras un submit cuya respuesta se pierde, el estado interno debe
        reflejar la incertidumbre (`OrderStatus.UNKNOWN`, transitorio; S-04-a)
        para que Monitoring/EXECUTION y la reconciliacion la OBSERVEN. NO es una
        transicion terminal: solo se sale por resolución explicita del broker
        (`submit`/`cancel`/`reject`/`apply_fill`), nunca por reenvio ciego.
        """
        return self._transition(order_id, OrderStatus.UNKNOWN)

    def reject(self, order_id: str, reason: str) -> Order:
        order = self._transition(order_id, OrderStatus.REJECTED)
        updated = replace(order, reject_reason=reason)
        self.orders[order_id] = updated
        return updated

    def _transition(self, order_id: str, target: OrderStatus) -> Order:
        """Aplica una transición SOLO si pertenece al grafo contractual.

        La validez se comprueba ANTES de cualquier mutación: una transición
        inválida deja la orden intacta y no produce side effects. Estados
        terminales no admiten salida. `from == to` solo es válido si el grafo
        lo permite explícitamente (p. ej. PARTIALLY_FILLED → PARTIALLY_FILLED).
        """
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"Orden desconocida: {order_id}")
        if order.status.terminal:
            raise OrderStateError(
                f"No se puede pasar {order.status.value} → {target.value} "
                f"(orden terminal)"
            )
        allowed = ALLOWED_TRANSITIONS.get(order.status, frozenset())
        if target not in allowed:
            raise OrderStateError(
                f"Transición de estado inválida: {order.status.value} → "
                f"{target.value}"
            )
        self.orders[order_id] = replace(order, status=target)
        return self.orders[order_id]

    def set_broker_order_id(self, order_id: str, broker_order_id: str) -> Order:
        """Registra el id asignado por el broker (correlacion client<->broker).

        Es **metadata de correlacion**, NO una transicion de estado: no valida el
        grafo ni exige un estado concreto. Idempotente. Permite mantener la
        relacion estable `client_order_id <-> broker_order_id` durante toda la
        vida de la orden (submit confirmado, lookup de UNKNOWN, fills, cancel).
        """
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"Orden desconocida: {order_id}")
        if order.broker_order_id == broker_order_id:
            return order
        updated = replace(order, broker_order_id=broker_order_id)
        self.orders[order_id] = updated
        return updated

    def load(self, order: Order) -> None:
        """Recarga una orden persistida (recuperación tras reinicio)."""
        self.orders[order.order_id] = order