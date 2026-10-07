"""Tests LIVE-06: commissions/fills reales y reconciliacion esperado/observado.

Contrato congelado (LIVE-06):
  * `commission` = importe monetario (moneda de cuenta), finito y >= 0; se aplica
    EXACTAMENTE UNA VEZ por fill como COSTE que reduce cash (afecta equity); NO
    altera avg_entry ni el realized P&L (gross por precio);
  * identidad de fill `(client_order_id, fill_sequence)`; re-entrega = no-op;
  * multi-fill: filled_quantity acumulada, avg ponderado, commissions por fill;
  * reconciliacion detecta divergencias esperado vs observado SIN adoptar el
    broker como estado interno;
  * finding critico => RECONCILIATION_BLOCKED (fail-safe para nuevas entradas).

No incluye secretos ni valores de credenciales.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers.models import BrokerFill, BrokerOrder, BrokerOrderState
from execution.order import OrderSide, OrderType
from papertrading.ledger import AccountLedger, LedgerError
from reconciliation.engine import ReconciliationEngine
from reconciliation.models import (
    AccountSnapshot,
    BrokerSnapshot,
    DiscrepancyClass,
    InternalSnapshot,
    OrderSnapshot,
    ReconciliationStatus,
)

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    _LevelLong,
    _drive,
    _stack,
)

UTC = timezone.utc
_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _led(**kw) -> AccountLedger:
    return AccountLedger(initial_cash=kw.pop("initial_cash", 10_000.0))


class TestCommission:
    def test_single_buy_fill_commission_reduces_cash(self):
        led = _led()
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=5.0,
        )
        assert led.cash == pytest.approx(10_000.0 - 100.0 - 5.0)
        # La comision NO altera avg_entry ni el realized (gross).
        assert led.position("X").avg_entry_price == pytest.approx(100.0)
        assert led.realized_pnl == 0.0

    def test_single_sell_fill_commission_reduces_cash(self):
        led = _led(initial_cash=0.0)
        led.apply_fill(
            instrument="X", side="SELL", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=3.0,
        )
        assert led.cash == pytest.approx(100.0 - 3.0)

    def test_two_fills_different_commissions(self):
        led = _led()
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=2.0,
        )
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=1, commission=3.0,
        )
        assert led.cash == pytest.approx(10_000.0 - 200.0 - 5.0)
        assert led.position("X").quantity == 2.0

    def test_duplicate_fill_does_not_duplicate_commission(self):
        led = _led()
        kw = dict(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=5.0,
        )
        led.apply_fill(**kw)
        cash = led.cash
        led.apply_fill(**kw)  # re-entrega -> no-op
        assert led.cash == cash

    def test_zero_commission_is_noop(self):
        led = _led()
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=0.0,
        )
        assert led.cash == pytest.approx(9_900.0)

    @pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
    def test_invalid_commission_rejected(self, bad):
        led = _led()
        with pytest.raises(LedgerError):
            led.apply_fill(
                instrument="X", side="BUY", quantity=1.0, price=100.0,
                client_order_id="C", fill_sequence=0, commission=bad,
            )


class TestEngineCommissions:
    def _order(self, env, cid: str, quantity: float):
        o = env.order_manager.create_order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=quantity,
            order_type=OrderType.MARKET, client_order_id=cid,
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        return o

    def test_engine_applies_observed_commission_and_is_idempotent(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        cid = "C-L6"
        self._order(env, cid, 1.0)
        cash_before = env.engine.ledger.cash
        bo = BrokerOrder(
            client_order_id=cid, broker_order_id="BRK-L6", instrument=INSTRUMENT,
            side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1.0,
            state=BrokerOrderState.FILLED, filled_quantity=1.0,
            avg_fill_price=100.0,
            fills=(BrokerFill(
                broker_order_id="BRK-L6", quantity=1.0, price=100.0,
                timestamp=_NOW, commission=2.5, fill_sequence=0,
            ),),
        )
        env.engine._apply_fills(cid, bo, _NOW)
        assert env.engine.ledger.cash == pytest.approx(cash_before - 100.0 - 2.5)
        # Re-entrega del mismo lote -> no-op (sin doble commission).
        cash_after = env.engine.ledger.cash
        env.engine._apply_fills(cid, bo, _NOW)
        assert env.engine.ledger.cash == cash_after

    def test_engine_multi_fill_avg_and_commissions(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        cid = "C-MF"
        self._order(env, cid, 3.0)
        cash_before = env.engine.ledger.cash
        fills = (
            BrokerFill("BRK-MF", 1.0, 100.0, _NOW, commission=1.0, fill_sequence=0),
            BrokerFill("BRK-MF", 1.0, 110.0, _NOW, commission=1.0, fill_sequence=1),
            BrokerFill("BRK-MF", 1.0, 120.0, _NOW, commission=1.0, fill_sequence=2),
        )
        bo = BrokerOrder(
            client_order_id=cid, broker_order_id="BRK-MF", instrument=INSTRUMENT,
            side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=3.0,
            state=BrokerOrderState.FILLED, filled_quantity=3.0,
            avg_fill_price=110.0, fills=fills,
        )
        env.engine._apply_fills(cid, bo, _NOW)
        pos = env.engine.ledger.position(INSTRUMENT)
        assert pos.quantity == pytest.approx(3.0)
        assert pos.avg_entry_price == pytest.approx(110.0)
        assert env.engine.ledger.cash == pytest.approx(
            cash_before - (100.0 + 110.0 + 120.0) - 3.0
        )
        # Redelivery del lote completo -> no-op.
        cash_after = env.engine.ledger.cash
        env.engine._apply_fills(cid, bo, _NOW)
        assert env.engine.ledger.cash == cash_after


class TestReconciliationNoAdoption:
    def test_account_mismatch_blocks_without_adopting_broker(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        ledger_cash = env.engine.ledger.cash
        env.broker._cash += 1_000.0  # observed divergente
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        # NO adopcion silenciosa del broker como estado interno.
        assert env.engine.ledger.cash == ledger_cash

    def test_position_mismatch_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        ledger_qty = env.engine.ledger.position(INSTRUMENT).quantity
        # Manipular la vista observada del broker (posicion extra).
        from brokers.models import Position
        env.broker.get_positions = lambda: [
            Position(INSTRUMENT, ledger_qty + 5.0, 50_000.0, _NOW)
        ]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert env.engine.ledger.position(INSTRUMENT).quantity == ledger_qty

    def test_reconciliation_does_not_reapply_commission(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        cash = env.engine.ledger.cash
        env.engine.reconcile()
        env.engine.reconcile()  # reconciliar NO aplica fees ni muta el ledger
        assert env.engine.ledger.cash == cash


def _order_snap(cid: str, *, state: str = "FILLED",
                broker_order_id: str | None = None) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=cid, instrument="X", quantity=1.0, state=state,
        broker_order_id=broker_order_id,
    )


class TestOrderIdentityReconciliation:
    def test_broker_order_id_equal_is_match(self):
        internal = InternalSnapshot(orders=(_order_snap("C", broker_order_id="BRK-1"),))
        broker = BrokerSnapshot(orders=(_order_snap("C", broker_order_id="BRK-1"),))
        report = ReconciliationEngine().reconcile(internal, broker)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK

    def test_broker_order_id_mismatch_blocks(self):
        internal = InternalSnapshot(orders=(_order_snap("C", broker_order_id="BRK-1"),))
        broker = BrokerSnapshot(orders=(_order_snap("C", broker_order_id="BRK-2"),))
        report = ReconciliationEngine().reconcile(internal, broker)
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any(
            d.key == "C/broker_order_id"
            and d.classification is DiscrepancyClass.MISMATCH
            for d in report.diffs
        )

    def test_broker_order_id_unknown_no_false_positive(self):
        # Interno sin broker_order_id vs observado con id -> NO mismatch.
        internal = InternalSnapshot(orders=(_order_snap("C"),))
        broker = BrokerSnapshot(orders=(_order_snap("C", broker_order_id="BRK-1"),))
        assert (
            ReconciliationEngine().reconcile(internal, broker).status
            is ReconciliationStatus.RECONCILIATION_OK
        )
        # Observado sin broker_order_id vs interno con id -> NO mismatch.
        internal2 = InternalSnapshot(orders=(_order_snap("C", broker_order_id="BRK-1"),))
        broker2 = BrokerSnapshot(orders=(_order_snap("C"),))
        assert (
            ReconciliationEngine().reconcile(internal2, broker2).status
            is ReconciliationStatus.RECONCILIATION_OK
        )


def _acct(*, currency: str | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="A", cash=100.0, equity=100.0, currency=currency
    )


class TestCurrencyReconciliation:
    def test_currency_match_ok(self):
        report = ReconciliationEngine().reconcile(
            InternalSnapshot(account=_acct(currency="USD")),
            BrokerSnapshot(account=_acct(currency="USD")),
        )
        assert report.status is ReconciliationStatus.RECONCILIATION_OK

    def test_currency_mismatch_blocks(self):
        report = ReconciliationEngine().reconcile(
            InternalSnapshot(account=_acct(currency="USD")),
            BrokerSnapshot(account=_acct(currency="EUR")),
        )
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any(d.key == "account/currency" for d in report.diffs)

    def test_currency_unknown_no_false_positive(self):
        # Falta la moneda en un lado -> no comparable (sin falso positivo).
        assert (
            ReconciliationEngine().reconcile(
                InternalSnapshot(account=_acct()),
                BrokerSnapshot(account=_acct(currency="EUR")),
            ).status
            is ReconciliationStatus.RECONCILIATION_OK
        )
        assert (
            ReconciliationEngine().reconcile(
                InternalSnapshot(account=_acct(currency="USD")),
                BrokerSnapshot(account=_acct()),
            ).status
            is ReconciliationStatus.RECONCILIATION_OK
        )
