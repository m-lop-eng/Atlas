"""Reconciliación mínima de órdenes (B3).

ALCANCE: solo la primitiva que exige 06_EXECUTION_OPERATION.md §8 y §67:
cuando el estado de una orden es incierto (UNKNOWN), Atlas consulta al
BROKER como fuente externa de verdad y adopta lo que este reporte.

NO incluye aún el Reconciliation Engine completo (B4): comparación de
posiciones/cuenta completas, detección de discrepancias multifuente, ni
bloqueo de trading. Aquí se resuelve UNA orden incierta de forma segura.
"""

from __future__ import annotations

from brokers.base import BrokerAdapter
from brokers.models import (
    BrokerOrder,
    BrokerOrderState,
    OrderNotFoundError,
)


def resolve_order(
    adapter: BrokerAdapter, client_order_id: str
) -> BrokerOrder:
    """Devuelve lo que el broker reporta para una orden incierta.

    Prohibido por 06 §8: reenviar una orden a ciegas tras una respuesta
    perdida. Para resolverla: preguntar al broker primero.

    Raises:
        BrokerConnectionError: si el broker no responde (el estado sigue
            siendo incierto; NO reintentar aún).
        OrderNotFoundError: si el broker no conoce la orden (puede que
            nunca llegara; decisión de reintento queda en capas superiores).
    """
    order = adapter.get_order(client_order_id)
    if order is None:
        raise OrderNotFoundError(
            f"El broker no conoce la orden {client_order_id}: estado "
            f"indeterminado (OrderNotFoundError)"
        )
    return order


def is_uncertain(state: BrokerOrderState) -> bool:
    """True si el estado no puede tomarse como verdad certera del broker."""
    return state in (BrokerOrderState.UNKNOWN,)


__all__ = ["resolve_order", "is_uncertain"]