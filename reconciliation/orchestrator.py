"""Orquestador de reconciliación (B4): observa, no destruye.

A diferencia de `ReconciliationEngine` (puro), aquí se conecta con el broker
real vía `BrokerAdapter` y con los snapshots internos de Atlas. Sigue siendo
de SOLO LECTURA: no envía órdenes, no cancela, no cierra posiciones.

Resolución explícita: `resolve_unknown_orders` consulta al broker la verdad
de una orden UNKNOWN y devuelve un snapshot actualizado (06 §8, §67). La
coordinación global del kill switch/alertas/recovery es de la siguiente
capa operacional, NO de este módulo.
"""

from __future__ import annotations

from brokers.base import BrokerAdapter
from brokers.models import BrokerOrderState
from brokers.reconcile import resolve_order
from reconciliation.models import (
    AccountSnapshot,
    BrokerSnapshot,
    FillSnapshot,
    InternalSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    ReconciliationReport,
    ReconciliationStatus,
)
from reconciliation.engine import ReconciliationEngine


def broker_state_from_adapter(
    adapter: BrokerAdapter,
    *,
    known_client_order_ids: tuple[str, ...] = (),
) -> BrokerSnapshot:
    """Construye el `BrokerSnapshot` leyendo del broker (solo lectura).

    Si el broker no responde (BrokerConnectionError) o no está sano,
    devuelve un snapshot con `available=False` => el motor responderá
    RECONCILIATION_UNKNOWN (fail-safe), nunca un MATCH falso.

    Los fills del broker se toman de la verdad autoritativa (`get_order`)
    para las órdenes que Atlas conoce (`known_client_order_ids`), además de
    los pending que devuelve `get_open_orders`; así se comparan los fills de
    órdenes ya FILLED/CANCELLED, no solo los pendientes.
    """
    if not adapter.health():
        return BrokerSnapshot(available=False)
    try:
        orders = tuple(
            OrderSnapshot(
                client_order_id=o.client_order_id,
                instrument=o.instrument,
                quantity=o.quantity,
                state=o.state.value,
                filled_quantity=o.filled_quantity,
                avg_fill_price=o.avg_fill_price,
            )
            for o in adapter.get_open_orders()
        )
        broker_fills: dict[str, FillSnapshot] = {}
        for o in adapter.get_open_orders():
            if o.fills:
                broker_fills[o.client_order_id] = FillSnapshot(
                    client_order_id=o.client_order_id,
                    quantity=o.filled_quantity,
                    avg_price=o.avg_fill_price,
                    fill_count=len(o.fills),
                )
        for cid in known_client_order_ids:
            if cid in broker_fills:
                continue
            authoritative = adapter.get_order(cid)
            if authoritative is None or not authoritative.fills:
                continue
            broker_fills[cid] = FillSnapshot(
                client_order_id=cid,
                quantity=authoritative.filled_quantity,
                avg_price=authoritative.avg_fill_price,
                fill_count=len(authoritative.fills),
            )
        fills = tuple(broker_fills.values())
        positions = tuple(
            PositionSnapshot(
                instrument=p.instrument,
                quantity=p.quantity,
                avg_entry_price=p.avg_entry_price,
            )
            for p in adapter.get_positions()
        )
        account_state = adapter.get_account_state()
        account = AccountSnapshot(
            account_id=account_state.account_id,
            cash=account_state.cash,
            equity=account_state.equity,
        )
        return BrokerSnapshot(
            orders=orders,
            fills=fills,
            positions=positions,
            account=account,
            available=True,
        )
    except Exception:
        return BrokerSnapshot(available=False)


def reconcile_with_broker(
    engine: ReconciliationEngine,
    internal: InternalSnapshot,
    adapter: BrokerAdapter,
) -> ReconciliationReport:
    """Reconcilia el estado interno de Atlas contra el broker real.

    Solo lectura (OBSERVAR → COMPARAR → CLASIFICAR → BLOQUEAR). Nada de
    acciones destructivas; la resolución es explícita y posterior.
    """
    known = tuple(o.client_order_id for o in internal.orders)
    broker = broker_state_from_adapter(
        adapter, known_client_order_ids=known
    )
    return engine.reconcile(internal, broker)


def resolve_unknown_orders(
    internal: InternalSnapshot, adapter: BrokerAdapter
) -> InternalSnapshot:
    """Resuelve explícitamente las órdenes internas en UNKNOWN consultando
    al broker (06 §8): Atlas NO asume nada, adopta la verdad del broker.

    Es la PRIMITIVA de resolución: produce un nuevo snapshot interno
    actualizado; NO abre/cancela/cierra nada por sí misma.
    """
    resolved_orders = []
    resolved_fills: dict[str, FillSnapshot] = {
        f.client_order_id: f for f in internal.fills
    }
    for order in internal.orders:
        if order.state != BrokerOrderState.UNKNOWN.value:
            resolved_orders.append(order)
            continue
        try:
            authoritative = resolve_order(adapter, order.client_order_id)
            resolved_orders.append(
                OrderSnapshot(
                    client_order_id=order.client_order_id,
                    instrument=authoritative.instrument,
                    quantity=authoritative.quantity,
                    state=authoritative.state.value,
                    filled_quantity=authoritative.filled_quantity,
                    avg_fill_price=authoritative.avg_fill_price,
                )
            )
            if authoritative.fills:
                resolved_fills[order.client_order_id] = FillSnapshot(
                    client_order_id=order.client_order_id,
                    quantity=authoritative.filled_quantity,
                    avg_price=authoritative.avg_fill_price,
                    fill_count=len(authoritative.fills),
                )
        except Exception:
            # El broker tampoco puede determinarlo: seguir en UNKNOWN
            # (fail-safe; el motor seguirá devolviendo BLOCKED/UNKNOWN).
            resolved_orders.append(order)

    return InternalSnapshot(
        orders=tuple(resolved_orders),
        fills=tuple(resolved_fills.values()),
        positions=internal.positions,
        account=internal.account,
    )


__all__ = [
    "broker_state_from_adapter",
    "reconcile_with_broker",
    "resolve_unknown_orders",
]