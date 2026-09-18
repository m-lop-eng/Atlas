"""Tests del OrderManager y modelo Order (execution/)."""

import pytest

from execution.order import Order, OrderSide, OrderStatus, OrderType
from execution.order_manager import OrderManager, OrderStateError


def _make(
    *,
    client_order_id: str = "CLIENT-1",
    quantity: int | float = 2,
    **kwargs,
) -> dict:
    base = dict(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        account_id="acc-1",
        instrument="NQ",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
    )
    base.update(kwargs)
    return base


def _fill_order(manager: OrderManager, order: Order, quantity: int | float = 1, price: float = 101.0):
    from execution.order import Fill

    return manager.apply_fill(
        order.order_id,
        Fill(order_id=order.order_id, quantity=quantity, price=price, commission=1.5),
    )


class TestOrderCreation:
    def test_creates_and_assigns_ids(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        assert order.order_id.startswith("ORD-")
        assert order.status == OrderStatus.CREATED

    def test_client_order_id_required(self) -> None:
        manager = OrderManager()
        with pytest.raises(OrderStateError, match="client_order_id"):
            manager.create_order(**_make(client_order_id=""))

    def test_duplicate_active_client_order_id_raises(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        with pytest.raises(OrderStateError, match="duplicada"):
            manager.create_order(**_make(client_order_id=order.client_order_id))

    def test_duplicate_allowed_after_terminal(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        manager.cancel(order.order_id)
        order2 = manager.create_order(**_make())
        assert order2.order_id != order.order_id

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positivo"):
            OrderManager().create_order(**_make(quantity=0))

    def test_quantity_cap_allowed(self) -> None:
        manager = OrderManager(allow_reduced_quantity=True, _max_quantity=None)
        manager._max_quantity = 3
        order = manager.create_order(**_make(quantity=5))
        assert order.quantity == 3

    def test_quantity_cap_rejected_when_not_allowed(self) -> None:
        manager = OrderManager(allow_reduced_quantity=False)
        manager._max_quantity = 3
        with pytest.raises(OrderStateError, match="máximo"):
            manager.create_order(**_make(quantity=5))

    def test_missing_required_fields_raises(self) -> None:
        manager = OrderManager()
        kwargs = _make()
        del kwargs["strategy_id"]
        with pytest.raises(TypeError):
            manager.create_order(**kwargs)


class TestOrderTransitions:
    def test_lifecycle(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        assert manager.approve(order.order_id).status == OrderStatus.APPROVED
        assert manager.submit(order.order_id).status == OrderStatus.SUBMITTED
        assert manager.acknowledge(order.order_id).status == OrderStatus.ACKNOWLEDGED

    def test_transition_from_terminal_raises(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        manager.cancel(order.order_id)
        with pytest.raises(OrderStateError, match="terminal"):
            manager.approve(order.order_id)

    def test_unknown_order_raises_keyerror(self) -> None:
        manager = OrderManager()
        with pytest.raises(KeyError):
            manager.approve("ORD-NO-EXISTE")

    def test_reject_records_reason(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make())
        rejected = manager.reject(order.order_id, "riesgo")
        assert rejected.status == OrderStatus.REJECTED
        assert rejected.reject_reason == "riesgo"


class TestFills:
    def test_full_fill(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**{**_make(), "quantity": 2})
        filled = _fill_order(manager, order, quantity=2, price=101.0)
        assert filled.status == OrderStatus.FILLED
        assert filled.fill_quantity == 2
        assert filled.average_fill_price == 101.0
        assert filled.commission == 1.5

    def test_partial_then_full(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**{**_make(), "quantity": 4})
        first = _fill_order(manager, order, quantity=1, price=100.0)
        assert first.status == OrderStatus.PARTIALLY_FILLED
        assert first.fill_quantity == 1

        second = _fill_order(manager, first, quantity=3, price=102.0)
        assert second.status == OrderStatus.FILLED
        assert second.fill_quantity == 4
        assert abs(second.average_fill_price - 101.5) < 1e-9

    def test_partial_then_partial(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**{**_make(), "quantity": 5})
        first = _fill_order(manager, order, quantity=2, price=100.0)
        assert first.status == OrderStatus.PARTIALLY_FILLED

    def test_fill_exceeding_quantity_raises(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make(quantity=2))
        with pytest.raises(OrderStateError, match="excede"):
            _fill_order(manager, order, quantity=5)

    def test_fill_on_terminal_raises(self) -> None:
        manager = OrderManager()
        order = manager.create_order(**_make(quantity=2))
        manager.cancel(order.order_id)
        with pytest.raises(OrderStateError, match="terminal"):
            _fill_order(manager, order, quantity=1)

    def test_fill_zero_raises(self) -> None:
        from execution.order import Fill

        manager = OrderManager()
        order = manager.create_order(**_make(quantity=2))
        with pytest.raises(OrderStateError, match="cantidad"):
            manager.apply_fill(order.order_id, Fill(order_id=order.order_id, quantity=0, price=100.0))