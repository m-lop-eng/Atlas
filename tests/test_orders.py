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


class TestStrictQuantityValidation:
    @pytest.mark.parametrize(
        "quantity",
        [None, True, False, "0.1", float("nan"), float("inf"), float("-inf"), 0, -1],
    )
    def test_order_rejects_invalid_quantity(self, quantity) -> None:
        with pytest.raises(ValueError):
            Order(**_make(quantity=quantity))

    @pytest.mark.parametrize(
        "quantity",
        [None, True, False, "0.1", float("nan"), float("inf"), float("-inf"), 0, -1],
    )
    def test_manager_rejects_invalid_quantity_without_max(self, quantity) -> None:
        manager = OrderManager(_max_quantity=None)
        with pytest.raises(OrderStateError):
            manager.create_order(**_make(quantity=quantity))
        assert manager.orders == {}

    def test_valid_numeric_quantities_still_create(self) -> None:
        manager = OrderManager(_max_quantity=None)
        for value in (2, 0.1):
            order = manager.create_order(
                **_make(client_order_id=f"VALID-{value}", quantity=value)
            )
            assert order.quantity == pytest.approx(float(value))


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


class TestTransitionGraph:
    """F-07: `_transition` debe validar el grafo contractual."""

    def _order(self, cid: str = "G") -> tuple[OrderManager, Order]:
        m = OrderManager()
        return m, m.create_order(**_make(client_order_id=cid))

    def test_legal_lifecycle_sequence(self) -> None:
        m, o = self._order()
        assert m.approve(o.order_id).status == OrderStatus.APPROVED
        assert m.submit(o.order_id).status == OrderStatus.SUBMITTED
        assert m.acknowledge(o.order_id).status == OrderStatus.ACKNOWLEDGED

    def test_illegal_created_to_acknowledged_rejected(self) -> None:
        m, o = self._order()
        with pytest.raises(OrderStateError, match="inválida"):
            m.acknowledge(o.order_id)
        assert m.orders[o.order_id].status == OrderStatus.CREATED

    def test_illegal_created_to_submitted_rejected(self) -> None:
        m, o = self._order()
        with pytest.raises(OrderStateError, match="inválida"):
            m.submit(o.order_id)
        assert m.orders[o.order_id].status == OrderStatus.CREATED

    def test_illegal_approved_to_acknowledged_rejected(self) -> None:
        m, o = self._order()
        m.approve(o.order_id)
        with pytest.raises(OrderStateError, match="inválida"):
            m.acknowledge(o.order_id)
        assert m.orders[o.order_id].status == OrderStatus.APPROVED

    def test_illegal_submitted_to_approved_rejected(self) -> None:
        m, o = self._order()
        m.approve(o.order_id)
        m.submit(o.order_id)
        with pytest.raises(OrderStateError, match="inválida"):
            m.approve(o.order_id)
        assert m.orders[o.order_id].status == OrderStatus.SUBMITTED

    def test_invalid_transition_no_partial_mutation(self) -> None:
        m, o = self._order()
        before = m.orders[o.order_id]
        with pytest.raises(OrderStateError):
            m.acknowledge(o.order_id)
        after = m.orders[o.order_id]
        assert after.status == before.status == OrderStatus.CREATED
        assert after.fill_quantity == before.fill_quantity == 0
        assert after.average_fill_price == before.average_fill_price
        assert after.commission == before.commission
        assert after.reject_reason == before.reject_reason

    def test_terminal_cannot_leave(self) -> None:
        m, o = self._order()
        m.approve(o.order_id)
        m.submit(o.order_id)
        m.cancel(o.order_id)
        for target in (m.approve, m.submit, m.acknowledge, m.cancel):
            with pytest.raises(OrderStateError, match="terminal"):
                target(o.order_id)
        assert m.orders[o.order_id].status == OrderStatus.CANCELLED

    def test_created_to_rejected_and_cancelled_allowed(self) -> None:
        m, o = self._order("R1")
        assert m.reject(o.order_id, "risco").status == OrderStatus.REJECTED
        m2, o2 = self._order("C1")
        assert m2.cancel(o2.order_id).status == OrderStatus.CANCELLED

    def test_reject_reason_side_effect_preserved(self) -> None:
        m, o = self._order()
        rej = m.reject(o.order_id, "motivo")
        assert rej.reject_reason == "motivo"

    def test_self_transition_created_invalid(self) -> None:
        """CREATED -> CREATED no esta en el grafo (debe rechazarse)."""
        m, o = self._order()
        with pytest.raises(OrderStateError, match="inválida"):
            m._transition(o.order_id, OrderStatus.CREATED)
        assert m.orders[o.order_id].status == OrderStatus.CREATED

    def test_self_transition_partially_filled_allowed(self) -> None:
        """PARTIALLY_FILLED -> PARTIALLY_FILLED si esta permitido (grafo)."""
        from execution.order import Fill

        m = OrderManager()
        o = m.create_order(**_make(quantity=5))
        m.apply_fill(o.order_id, Fill(order_id=o.order_id, quantity=1, price=100.0))
        assert m.orders[o.order_id].status == OrderStatus.PARTIALLY_FILLED
        # self-transition explicita via _transition
        same = m._transition(o.order_id, OrderStatus.PARTIALLY_FILLED)
        assert same.status == OrderStatus.PARTIALLY_FILLED

    def test_all_terminal_states_have_no_outgoing(self) -> None:
        from execution.order_manager import ALLOWED_TRANSITIONS

        for status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        ):
            assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_unknown_is_not_terminal(self) -> None:
        # S-04-a: UNKNOWN es transitorio, NO terminal.
        assert OrderStatus.UNKNOWN.terminal is False
        for status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        ):
            assert status.terminal is True


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


class TestUnknownTransient:
    """S-04-a: UNKNOWN es transitorio (no terminal), resoluble por broker lookup."""

    def _unknown_order(
        self, *, quantity: int | float = 2, cid: str = "UNK"
    ) -> tuple[OrderManager, Order]:
        from execution.order_manager import ALLOWED_TRANSITIONS

        m = OrderManager()
        o = m.create_order(**_make(client_order_id=cid, quantity=quantity))
        m.approve(o.order_id)
        m.submit(o.order_id)
        # SUBMITTED -> UNKNOWN (producer ya presente en el grafo).
        assert OrderStatus.UNKNOWN in ALLOWED_TRANSITIONS[OrderStatus.SUBMITTED]
        m._transition(o.order_id, OrderStatus.UNKNOWN)
        return m, m.orders[o.order_id]

    def test_unknown_reachable_from_submitted(self) -> None:
        _, o = self._unknown_order()
        assert o.status == OrderStatus.UNKNOWN

    def test_valid_exits(self) -> None:
        for target in (
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        ):
            m, o = self._unknown_order(cid=f"V-{target.value}")
            assert m._transition(o.order_id, target).status == target

    def test_self_loop_rejected(self) -> None:
        m, o = self._unknown_order()
        with pytest.raises(OrderStateError, match="inválida"):
            m._transition(o.order_id, OrderStatus.UNKNOWN)
        assert m.orders[o.order_id].status == OrderStatus.UNKNOWN

    def test_excluded_targets_rejected(self) -> None:
        for target in (
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        ):
            m, o = self._unknown_order(cid=f"X-{target.value}")
            with pytest.raises(OrderStateError, match="inválida"):
                m._transition(o.order_id, target)
            assert m.orders[o.order_id].status == OrderStatus.UNKNOWN

    def test_unknown_partial_fill_resolves_via_apply_fill(self) -> None:
        from execution.order import Fill

        m, o = self._unknown_order(quantity=4)
        resolved = m.apply_fill(
            o.order_id, Fill(order_id=o.order_id, quantity=1, price=100.0)
        )
        assert resolved.status == OrderStatus.PARTIALLY_FILLED
        assert resolved.fill_quantity == 1

    def test_unknown_full_fill_resolves_via_apply_fill(self) -> None:
        from execution.order import Fill

        m, o = self._unknown_order(quantity=2)
        resolved = m.apply_fill(
            o.order_id, Fill(order_id=o.order_id, quantity=2, price=100.0)
        )
        assert resolved.status == OrderStatus.FILLED
        assert resolved.fill_quantity == 2

    def test_unknown_invalid_fill_no_partial_mutation(self) -> None:
        from execution.order import Fill

        m, o = self._unknown_order(quantity=2)
        with pytest.raises(OrderStateError, match="excede"):
            m.apply_fill(
                o.order_id, Fill(order_id=o.order_id, quantity=5, price=100.0)
            )
        after = m.orders[o.order_id]
        assert after.status == OrderStatus.UNKNOWN
        assert after.fill_quantity == 0

    def test_unknown_unresolved_stays_unknown(self) -> None:
        # Lookup sin resolver (broker sigue UNKNOWN / no resuelve): no muta.
        m, o = self._unknown_order()
        assert m.orders[o.order_id].status == OrderStatus.UNKNOWN
        assert m.orders[o.order_id].fill_quantity == 0

    def test_unknown_still_blocks_duplicate_active_cid(self) -> None:
        # UNKNOWN no terminal => la orden sigue activa => no recrear/re-enviar.
        m, _ = self._unknown_order(cid="DUP")
        with pytest.raises(OrderStateError, match="duplicada"):
            m.create_order(**_make(client_order_id="DUP"))

    def test_terminal_states_still_reject_fills(self) -> None:
        from execution.order import Fill

        # CANCELLED
        m = OrderManager()
        o = m.create_order(**_make(quantity=2))
        m.cancel(o.order_id)
        with pytest.raises(OrderStateError, match="terminal"):
            m.apply_fill(
                o.order_id, Fill(order_id=o.order_id, quantity=1, price=100.0)
            )
        # REJECTED
        m2 = OrderManager()
        o2 = m2.create_order(**_make(client_order_id="REJ", quantity=2))
        m2.reject(o2.order_id, "x")
        with pytest.raises(OrderStateError, match="terminal"):
            m2.apply_fill(
                o2.order_id, Fill(order_id=o2.order_id, quantity=1, price=100.0)
            )
class TestOrder07BSendBoundaryState:
    def test_presend_states_can_fail_terminal(self):
        m = OrderManager()
        created = m.create_order(**_make(client_order_id="BOUND-1", quantity=2))
        failed = m.mark_failed(created.order_id, "journal failure")
        assert failed.status is OrderStatus.FAILED
        assert failed.reject_reason == "journal failure"

        approved = m.create_order(**_make(client_order_id="BOUND-2", quantity=2))
        m.approve(approved.order_id)
        submitting = m.mark_submitting(approved.order_id)
        assert submitting.status is OrderStatus.SUBMITTING
        failed = m.mark_failed(submitting.order_id, "gate failure")
        assert failed.status is OrderStatus.FAILED

    def test_sync_placed_does_not_imply_fill(self):
        m = OrderManager()
        o = m.create_order(**_make(client_order_id="SYNC-PLACED", quantity=2))
        m.approve(o.order_id)
        m.mark_submitting(o.order_id)
        synced = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.SUBMITTED,
            filled_quantity=0,
        )
        assert synced.status is OrderStatus.SUBMITTED
        assert synced.fill_quantity == 0

    def test_sync_partial_then_filled_is_idempotent(self):
        m = OrderManager()
        o = m.create_order(**_make(client_order_id="SYNC-FILL", quantity=2))
        m.approve(o.order_id)
        m.mark_submitting(o.order_id)
        partial = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=1,
            average_fill_price=100.0,
            commission=0.5,
        )
        assert partial.status is OrderStatus.PARTIALLY_FILLED
        assert partial.fill_quantity == 1
        assert partial.average_fill_price == 100.0
        assert partial.commission == 0.5

        repeated = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=1,
            average_fill_price=100.0,
            commission=0.5,
        )
        assert repeated == partial

        filled = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.FILLED,
            filled_quantity=2,
            average_fill_price=101.0,
            commission=0.75,
        )
        assert filled.status is OrderStatus.FILLED
        assert filled.fill_quantity == 2
        assert filled.average_fill_price == 101.0
        assert filled.commission == 0.75

        repeated_filled = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.FILLED,
            filled_quantity=2,
            average_fill_price=101.0,
            commission=0.75,
        )
        assert repeated_filled == filled

    def test_sync_rejects_overfill_and_fill_regression(self):
        m = OrderManager()
        o = m.create_order(**_make(client_order_id="SYNC-BAD", quantity=2))
        m.approve(o.order_id)
        m.mark_submitting(o.order_id)
        with pytest.raises(OrderStateError):
            m.sync_broker_state(
                o.order_id,
                state=OrderStatus.FILLED,
                filled_quantity=3,
            )
        m.sync_broker_state(
            o.order_id,
            state=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=1,
        )
        # No retrocede la cantidad acumulada.
        same = m.sync_broker_state(
            o.order_id,
            state=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=0.5,
        )
        assert same.fill_quantity == 1
