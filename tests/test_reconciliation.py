"""Tests del Reconciliation Engine (B4).

Casos obligatorios:

  1. estado perfectamente reconciliado
  2. orden UNKNOWN que posteriormente aparece como FILLED
  3. fill parcial
  4. dos fills con precio medio concreto
  5. orden broker inexistente (MISSING_BROKER)
  6. orden broker desconocida para Atlas (MISSING_INTERNAL)
  7. posición con cantidad diferente
  8. dirección diferente
  9. posición inesperada en broker
 10. posición interna que falta en broker
 11. discrepancia de equity
 12. broker desconectado → UNKNOWN, no MATCH
 13. reconciliación repetida → determinista
 14. discrepancia → trading_allowed=False
 15. reconciliación posterior correcta → desbloqueo explícito
 16. ninguna acción destructiva automática
 17. ningún envío de órdenes desde el reconciliador
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers import (
    BrokerOrderState,
    PaperBrokerAdapter,
    SubmitStatus,
)
from brokers.reconcile import resolve_order
from execution.order import Order, OrderSide, OrderType
from reconciliation import (
    AccountSnapshot,
    BrokerSnapshot,
    DiscrepancyClass,
    FillSnapshot,
    InternalSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)


def _engine() -> ReconciliationEngine:
    return ReconciliationEngine()


def _account(*, cash: float, equity: float, account_id: str = "ACC") -> AccountSnapshot:
    return AccountSnapshot(account_id=account_id, cash=cash, equity=equity)


def _order(
    cid: str, state: str, *, quantity: float = 1.0, filled: float = 0.0,
    avg: float | None = None, instrument: str = "BTCUSDT",
) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=cid,
        instrument=instrument,
        quantity=quantity,
        state=state,
        filled_quantity=filled,
        avg_fill_price=avg,
    )


def _position(
    instrument: str, quantity: float, avg: float
) -> PositionSnapshot:
    return PositionSnapshot(
        instrument=instrument, quantity=quantity, avg_entry_price=avg
    )


class TestPerfectReconciliation:
    def test_perfectly_reconciled(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("O1", "SUBMITTED", quantity=2.0),),
            positions=(_position("BTCUSDT", 1.5, 50_000.0),),
            account=_account(cash=100_000.0, equity=150_000.0),
        )
        broker = BrokerSnapshot(
            orders=(_order("O1", "SUBMITTED", quantity=2.0),),
            positions=(_position("BTCUSDT", 1.5, 50_000.0),),
            account=_account(cash=100_000.0, equity=150_000.0),
        )
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK
        assert report.trading_allowed is True
        assert report.blocked is False
        assert not report.has(DiscrepancyClass.MISMATCH)

    def test_empty_snapshots_match(self) -> None:
        report = _engine().reconcile(
            InternalSnapshot(), BrokerSnapshot()
        )
        assert report.status == ReconciliationStatus.RECONCILIATION_OK
        assert report.trading_allowed is True


class TestUnknown:
    def test_unknown_order_blocks_and_is_unknown(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("U1", "UNKNOWN"),),
        )
        broker = BrokerSnapshot(orders=(_order("U1", "FILLED"),))
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN
        assert report.trading_allowed is False
        unknown = report.by_class(DiscrepancyClass.UNKNOWN)
        assert len(unknown) == 1

    def test_unknown_resolved_to_filled_then_match(self) -> None:
        """06 §8: primero resolver UNKNOWN (get_order), después clasificar."""
        engine = _engine()
        internal_unknown = InternalSnapshot(orders=(_order("U2", "UNKNOWN"),))
        broker = BrokerSnapshot(orders=(_order("U2", "FILLED", filled=1.0, avg=10.0),))

        report_before = engine.reconcile(internal_unknown, broker)
        assert report_before.trading_allowed is False

        # resolver: adoptar la verdad del broker
        resolved = InternalSnapshot(orders=(_order(
            "U2", "FILLED", filled=1.0, avg=10.0
        ),))
        report_after = engine.reconcile(resolved, broker)
        assert report_after.status == ReconciliationStatus.RECONCILIATION_OK
        assert report_after.trading_allowed is True

    def test_unknown_internal_order_missing_fill_still_unknown(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("U3", "UNKNOWN"),),
        )
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN


class TestFills:
    def test_partial_fill_matches(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("P1", "PARTIALLY_FILLED", quantity=4.0,
                           filled=1.0, avg=10.0),),
            fills=(FillSnapshot("P1", quantity=1.0, avg_price=10.0,
                                fill_count=1),),
        )
        broker = BrokerSnapshot(
            orders=(_order("P1", "PARTIALLY_FILLED", quantity=4.0,
                           filled=1.0, avg=10.0),),
            fills=(FillSnapshot("P1", quantity=1.0, avg_price=10.0,
                                fill_count=1),),
        )
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK

    def test_two_fills_weighted_avg_price(self) -> None:
        # 1 a 100 + 3 a 108 => qty 4, media (1*100 + 3*108)/4 = 106.0
        internal = InternalSnapshot(
            orders=(_order("W1", "FILLED", quantity=4.0, filled=4.0,
                           avg=106.0),),
            fills=(FillSnapshot("W1", quantity=4.0, avg_price=106.0,
                                fill_count=2),),
        )
        broker = BrokerSnapshot(
            orders=(_order("W1", "FILLED", quantity=4.0, filled=4.0,
                           avg=106.0),),
            fills=(FillSnapshot("W1", quantity=4.0, avg_price=106.0,
                                fill_count=2),),
        )
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK

    def test_partial_fill_filled_quantity_mismatch_broker_has_more(self) -> None:
        internal = InternalSnapshot(
            fills=(FillSnapshot("F1", quantity=1.0, avg_price=10.0,
                                fill_count=1),),
        )
        broker = BrokerSnapshot(
            fills=(FillSnapshot("F1", quantity=1.5, avg_price=10.0,
                                fill_count=1),),
        )
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.trading_allowed is False

    def test_fill_avg_price_mismatch(self) -> None:
        internal = InternalSnapshot(
            fills=(FillSnapshot("F2", quantity=2.0, avg_price=10.0,
                                fill_count=1),),
        )
        broker = BrokerSnapshot(
            fills=(FillSnapshot("F2", quantity=2.0, avg_price=10.5,
                                fill_count=1),),
        )
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False

    def test_duplicate_fills_in_broker_detected_by_count(self) -> None:
        internal = InternalSnapshot(
            fills=(FillSnapshot("F3", quantity=2.0, avg_price=10.0,
                                fill_count=1),),
        )
        broker = BrokerSnapshot(
            fills=(FillSnapshot("F3", quantity=2.0, avg_price=10.0,
                                fill_count=2),),
        )
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any("fills" in d.detail or "número" in d.detail
                   for d in report.by_class(DiscrepancyClass.MISMATCH))

    def test_missing_fill_broker_side(self) -> None:
        internal = InternalSnapshot(
            fills=(FillSnapshot("F4", quantity=1.0, avg_price=10.0,
                                fill_count=1),),
        )
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.has(DiscrepancyClass.MISSING_BROKER)


class TestOrders:
    def test_broker_missing_internal_pending_order(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("M1", "SUBMITTED"),),
        )
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.has(DiscrepancyClass.MISSING_BROKER)

    def test_broker_has_unknown_order_to_atlas(self) -> None:
        internal = InternalSnapshot()
        broker = BrokerSnapshot(orders=(_order("M2", "SUBMITTED"),))
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.has(DiscrepancyClass.MISSING_INTERNAL)

    def test_terminal_order_absent_from_broker_is_match(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("T1", "FILLED", filled=1.0, avg=10.0),),
        )
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK
        assert report.trading_allowed is True

    def test_state_divergence_submitted_vs_filled(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("S1", "SUBMITTED"),),
        )
        broker = BrokerSnapshot(orders=(_order("S1", "FILLED", filled=1.0, avg=9.0),))
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        mismatch = report.by_class(DiscrepancyClass.MISMATCH)
        assert len(mismatch) == 1


class TestPositions:
    def test_quantity_different(self) -> None:
        internal = InternalSnapshot(positions=(_position("BTC", 2.0, 50_000),))
        broker = BrokerSnapshot(positions=(_position("BTC", 1.75, 50_000),))
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False
        assert report.has(DiscrepancyClass.MISMATCH)

    def test_direction_different(self) -> None:
        internal = InternalSnapshot(positions=(_position("BTC", 2.0, 50_000),))
        broker = BrokerSnapshot(positions=(_position("BTC", -2.0, 50_000),))
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False
        assert "dirección" in (
            report.by_class(DiscrepancyClass.MISMATCH)[0].detail
        )

    def test_unexpected_position_in_broker(self) -> None:
        internal = InternalSnapshot()
        broker = BrokerSnapshot(positions=(_position("ETH", 1.0, 3_000),))
        report = _engine().reconcile(internal, broker)
        assert report.has(DiscrepancyClass.MISSING_INTERNAL)
        assert report.trading_allowed is False

    def test_internal_position_missing_in_broker(self) -> None:
        internal = InternalSnapshot(positions=(_position("ETH", 1.0, 3_000),))
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.has(DiscrepancyClass.MISSING_BROKER)
        assert report.trading_allowed is False

    def test_avg_entry_price_mismatch(self) -> None:
        internal = InternalSnapshot(positions=(_position("BTC", 1.0, 50_000),))
        broker = BrokerSnapshot(positions=(_position("BTC", 1.0, 50_500),))
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False

    def test_zero_quantity_positions_ignored(self) -> None:
        internal = InternalSnapshot(positions=(_position("BTC", 0.0, 50_000),))
        broker = BrokerSnapshot()
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK


class TestAccount:
    def test_equity_discrepancy_blocks(self) -> None:
        internal = InternalSnapshot(account=_account(cash=100_000, equity=100_000))
        broker = BrokerSnapshot(account=_account(cash=100_000, equity=95_000))
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.trading_allowed is False
        assert any("equity" in d.key for d in report.by_class(DiscrepancyClass.MISMATCH))

    def test_account_state_unavailable_is_unknown(self) -> None:
        internal = InternalSnapshot(account=_account(cash=1, equity=1))
        broker = BrokerSnapshot()  # sin account
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN
        assert report.trading_allowed is False

    def test_cash_discrepancy_blocks(self) -> None:
        internal = InternalSnapshot(account=_account(cash=100_000, equity=100_000))
        broker = BrokerSnapshot(account=_account(cash=99_000, equity=99_000))
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False


class TestFailSafe:
    def test_broker_disconnected_is_unknown_not_match(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("OK1", "FILLED", filled=1.0, avg=10.0),),
            positions=(_position("BTC", 1.0, 10.0),),
            account=_account(cash=1.0, equity=1.0),
        )
        broker = BrokerSnapshot(available=False)
        report = _engine().reconcile(internal, broker)
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN
        assert report.trading_allowed is False
        assert report.broker_available is False
        assert not report.has(DiscrepancyClass.MATCH)

    def test_no_destructive_action_on_blocked(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("OK2", "SUBMITTED"),),
            positions=(_position("BTC", 1.0, 10.0),),
        )
        broker = BrokerSnapshot(positions=(_position("BTC", 2.0, 10.0),))
        before = (internal, broker)
        report = _engine().reconcile(internal, broker)
        assert report.trading_allowed is False
        # el motor NO mutó sus entradas
        assert (internal, broker) == before


class TestDeterminismAndUnlock:
    def test_repeated_reconciliation_is_deterministic(self) -> None:
        internal = InternalSnapshot(
            orders=(_order("D1", "PARTIALLY_FILLED", quantity=4.0,
                           filled=2.0, avg=10.0),),
            positions=(_position("BTC", 2.0, 50_000),),
            account=_account(cash=50_000, equity=150_000),
        )
        broker = BrokerSnapshot(
            orders=(_order("D1", "PARTIALLY_FILLED", quantity=4.0,
                           filled=2.0, avg=10.0),),
            positions=(_position("BTC", 2.0, 50_000),),
            account=_account(cash=50_000, equity=150_000),
        )
        engine = _engine()
        a = engine.reconcile(internal, broker)
        for _ in range(20):
            b = engine.reconcile(internal, broker)
            assert b == a
            assert a.status == b.status

    def test_discrepancy_then_correct_reconciliation_unlock(self) -> None:
        engine = _engine()
        good_internal = InternalSnapshot(
            positions=(_position("BTC", 1.0, 50_000),),
            account=_account(cash=50_000, equity=100_000),
        )
        bad_broker = BrokerSnapshot(
            positions=(_position("BTC", 1.0, 52_000),),
            account=_account(cash=50_000, equity=102_000),
        )
        blocked = engine.reconcile(good_internal, bad_broker)
        assert blocked.trading_allowed is False

        good_broker = BrokerSnapshot(
            positions=(_position("BTC", 1.0, 50_000),),
            account=_account(cash=50_000, equity=100_000),
        )
        unlocked = engine.reconcile(good_internal, good_broker)
        assert unlocked.status == ReconciliationStatus.RECONCILIATION_OK
        assert unlocked.trading_allowed is True


# ---------------------------------------------------------------------------
# Orquestador + PaperBrokerAdapter: integración read-only end-to-end
# ---------------------------------------------------------------------------


def _execution_order(cid: str, *, instrument: str = "BTCUSDT",
                     side: OrderSide = OrderSide.BUY, quantity: float = 1.0,
                     order_type: OrderType = OrderType.MARKET) -> Order:
    return Order(
        strategy_id="s1", strategy_version="1.0.0", account_id="acc-1",
        instrument=instrument, side=side, quantity=quantity,
        order_type=order_type, client_order_id=cid,
    )


def _paper(*, price: float = 50_000.0,
           clock=None) -> PaperBrokerAdapter:
    adapter = PaperBrokerAdapter(
        account_id="PAPER-ACC",
        price_source=lambda i: price,
        clock=clock or (lambda: datetime(2026, 9, 1, 12, 0, 0,
                                          tzinfo=timezone.utc)),
    )
    adapter.connect()
    return adapter


class TestOracleWithPaperBroker:
    def test_submit_and_reconcile_matches_via_adapter(self) -> None:
        from reconciliation.orchestrator import (
            broker_state_from_adapter, reconcile_with_broker,
        )

        adapter = _paper(price=10_000.0)
        adapter.submit_order(_execution_order("LIVE-1", quantity=2.0))
        internal = InternalSnapshot(
            orders=(_order("LIVE-1", "FILLED", quantity=2.0, filled=2.0,
                           avg=10_000.0),),
            fills=(FillSnapshot("LIVE-1", quantity=2.0, avg_price=10_000.0,
                                fill_count=1),),
            positions=(_position("BTCUSDT", 2.0, 10_000.0),),
            account=_account(cash=80_000.0, equity=100_000.0, account_id="PAPER-ACC"),
        )
        report = reconcile_with_broker(
            _engine(), internal, adapter
        )
        assert report.status == ReconciliationStatus.RECONCILIATION_OK
        assert report.trading_allowed is True

    def test_partial_fill_via_paper_broker(self) -> None:
        from reconciliation.orchestrator import reconcile_with_broker

        adapter = _paper(price=10_000.0)
        adapter.submit_order(_execution_order(
            "LIVE-2", quantity=4.0, order_type=OrderType.LIMIT,
        ))
        adapter.simulate_fill("LIVE-2", 1.0, 9_900.0)
        # equity del broker = cash + |q| * price_source(10_000), NO fill price
        internal = InternalSnapshot(
            orders=(_order("LIVE-2", "PARTIALLY_FILLED", quantity=4.0,
                           filled=1.0, avg=9_900.0),),
            fills=(FillSnapshot("LIVE-2", quantity=1.0, avg_price=9_900.0,
                                fill_count=1),),
            positions=(_position("BTCUSDT", 1.0, 9_900.0),),
            account=_account(cash=90_100.0, equity=100_100.0, account_id="PAPER-ACC"),
        )
        report = reconcile_with_broker(_engine(), internal, adapter)
        assert report.status == ReconciliationStatus.RECONCILIATION_OK

    def test_disconnected_broker_yields_unknown(self) -> None:
        from reconciliation.orchestrator import reconcile_with_broker

        adapter = _paper()
        adapter.disconnect()
        internal = InternalSnapshot(positions=(_position("BTC", 1.0, 10.0),))
        report = reconcile_with_broker(_engine(), internal, adapter)
        assert report.status == ReconciliationStatus.RECONCILIATION_UNKNOWN
        assert report.trading_allowed is False

    def test_resolve_unknown_live_order_to_filled(self) -> None:
        """UNKNOWN -> get_order (verdad del broker) -> desbloqueo."""
        from reconciliation.orchestrator import (
            reconcile_with_broker, resolve_unknown_orders,
        )

        adapter = _paper(price=10_000.0)
        adapter.simulate_response_loss("LOSSY-1")
        submission = adapter.submit_order(_execution_order("LOSSY-1", quantity=1.0))
        assert submission.status == SubmitStatus.UNKNOWN

        internal = InternalSnapshot(
            orders=(_order("LOSSY-1", "UNKNOWN"),),
            positions=(_position("BTCUSDT", 1.0, 10_000.0),),
            account=_account(cash=90_000.0, equity=100_000.0, account_id="PAPER-ACC"),
        )
        first = reconcile_with_broker(_engine(), internal, adapter)
        assert first.trading_allowed is False

        resolved_internal = resolve_unknown_orders(internal, adapter)
        second = reconcile_with_broker(_engine(), resolved_internal, adapter)
        assert second.status == ReconciliationStatus.RECONCILIATION_OK
        assert second.trading_allowed is True

    def test_reconcile_resolves_mismatch_blocking(self) -> None:
        from reconciliation.orchestrator import reconcile_with_broker

        adapter = _paper(price=50_000.0)
        adapter.submit_order(_execution_order("LIVE-3", quantity=2.0))
        # Atlas cree tener 3 (error interno): debe bloquear
        internal = InternalSnapshot(
            positions=(_position("BTCUSDT", 3.0, 50_000.0),),
            account=_account(cash=0.0, equity=150_000.0, account_id="PAPER-ACC"),
        )
        report = reconcile_with_broker(_engine(), internal, adapter)
        assert report.trading_allowed is False
        assert report.status == ReconciliationStatus.RECONCILIATION_BLOCKED

    def test_reconcile_is_read_only_no_orders_sent(self) -> None:
        """El reconciliador solo OBSERVA: get_* y nunca submit_order/cancel."""
        from reconciliation.orchestrator import (
            broker_state_from_adapter, reconcile_with_broker,
        )

        calls: list[str] = []

        class RecordingAdapter(PaperBrokerAdapter):
            def submit_order(self, order):  # pragma: no cover
                calls.append("submit_order")
                return super().submit_order(order)

            def cancel_order(self, cid):  # pragma: no cover
                calls.append("cancel_order")
                return super().cancel_order(cid)

            def get_open_orders(self):
                calls.append("get_open_orders")
                return super().get_open_orders()

            def get_positions(self):
                calls.append("get_positions")
                return super().get_positions()

            def get_account_state(self):
                calls.append("get_account_state")
                return super().get_account_state()

        adapter = RecordingAdapter(
            account_id="REC", clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        adapter.connect()
        adapter.submit_order(_execution_order("REC-1", quantity=1.0))
        calls.clear()

        broker_state_from_adapter(adapter)
        report = reconcile_with_broker(_engine(), InternalSnapshot(), adapter)
        assert report.trading_allowed is False
        assert "submit_order" not in calls
        assert "cancel_order" not in calls
        assert calls  # el reconciliador SÍ usó lecturas get_*


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))