"""F-06A: AccountLedger — autoridad interna unica de cash/positions/realized.

Fase 1: spot/equity, point_value=1, moneda unica. Sin equity/exposure/
reconciliation (F-06B/C).
"""

from __future__ import annotations

import pytest

from papertrading.ledger import AccountLedger, LedgerError


def _open(led, *, cid="C", seq=0, side="BUY", qty=1.0, price=100.0, instrument="X"):
    return led.apply_fill(
        instrument=instrument, side=side, quantity=qty, price=price,
        client_order_id=cid, fill_sequence=seq,
    )


class TestBootstrap:
    def test_initial_cash_explicit(self):
        led = AccountLedger(initial_cash=10_000.0)
        assert led.cash == 10_000.0
        assert led.realized_pnl == 0.0
        assert led.positions() == ()

    def test_negative_or_none_initial_cash_rejected(self):
        with pytest.raises(LedgerError):
            AccountLedger(initial_cash=-1.0)
        with pytest.raises(LedgerError):
            AccountLedger(initial_cash=None)  # type: ignore[arg-type]


class TestBuyLong:
    def test_buy_opens_position_and_reduces_cash(self):
        led = AccountLedger(initial_cash=10_000.0)
        pos = _open(led, side="BUY", qty=2.0, price=100.0)
        assert pos.quantity == 2.0
        assert pos.avg_entry_price == 100.0
        assert led.cash == 10_000.0 - 200.0

    def test_multiple_buys_weighted_avg(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        pos = _open(led, cid="B", seq=0, qty=3.0, price=110.0)
        assert pos.quantity == 4.0
        assert pos.avg_entry_price == pytest.approx((1 * 100 + 3 * 110) / 4)
        assert led.cash == 10_000.0 - (100.0 + 330.0)


class TestSellLong:
    def test_partial_reduction_realizes_pnl(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", qty=4.0, price=100.0)
        pos = _open(led, cid="B", side="SELL", qty=1.0, price=110.0)
        assert pos.quantity == 3.0
        assert led.realized_pnl == pytest.approx(10.0)  # (110-100)*1

    def test_full_close_leaves_zero_and_no_position(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", qty=2.0, price=100.0)
        pos = _open(led, cid="B", side="SELL", qty=2.0, price=90.0)
        assert pos is None
        assert led.position("X") is None
        assert led.realized_pnl == pytest.approx(-20.0)


class TestShort:
    def test_open_short_negative_quantity(self):
        led = AccountLedger(initial_cash=10_000.0)
        pos = _open(led, side="SELL", qty=1.0, price=100.0)
        assert pos.quantity == -1.0
        assert led.cash == 10_100.0  # ingresa la venta

    def test_increase_short_weighted_avg(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", side="SELL", qty=1.0, price=100.0)
        pos = _open(led, cid="B", side="SELL", qty=1.0, price=110.0)
        assert pos.quantity == -2.0
        assert pos.avg_entry_price == pytest.approx(105.0)

    def test_reduce_short_realizes_pnl(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", side="SELL", qty=2.0, price=100.0)
        pos = _open(led, cid="B", side="BUY", qty=1.0, price=90.0)
        assert pos.quantity == -1.0
        assert led.realized_pnl == pytest.approx(10.0)  # (100-90)*1

    def test_close_short(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", side="SELL", qty=1.0, price=100.0)
        pos = _open(led, cid="B", side="BUY", qty=1.0, price=110.0)
        assert pos is None
        assert led.realized_pnl == pytest.approx(-10.0)  # (100-110)*1


class TestFillIdentity:
    def test_same_fill_twice_idempotent(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        cash_after = led.cash
        pnl_after = led.realized_pnl
        # reaplica identico
        pos = _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        assert led.cash == cash_after
        assert led.realized_pnl == pnl_after
        assert pos.quantity == 1.0

    def test_two_distinct_fills_same_order_both_applied(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        pos = _open(led, cid="A", seq=1, qty=1.0, price=110.0)
        assert pos.quantity == 2.0  # ambos fills aplicados

    def test_does_not_dedup_by_client_order_id_only(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        # otro cid, mismo seq -> se aplica (identidad incluye cid)
        pos = _open(led, cid="B", seq=0, qty=1.0, price=100.0)
        assert pos.quantity == 2.0

    def test_reapply_different_sequence_is_not_idempotent(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", seq=0, qty=1.0, price=100.0)
        _open(led, cid="A", seq=1, qty=1.0, price=100.0)
        # ambas en _applied_fills
        assert led.is_applied("A", 0)
        assert led.is_applied("A", 1)


class TestAtomicity:
    def test_invalid_fill_no_partial_mutation(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", qty=1.0, price=100.0)
        cash = led.cash
        pos = led.position("X")
        for bad in (
            dict(side="HOLD", quantity=1.0, price=100.0),
            dict(side="BUY", quantity=0.0, price=100.0),
            dict(side="BUY", quantity=-1.0, price=100.0),
            dict(side="BUY", quantity=float("nan"), price=100.0),
            dict(side="BUY", quantity=1.0, price=0.0),
        ):
            with pytest.raises(LedgerError):
                led.apply_fill(
                    instrument="X", client_order_id="Z", fill_sequence=0, **bad
                )
        assert led.cash == cash
        assert led.position("X") == pos

    def test_realized_not_added_to_cash_separately(self):
        """El efecto económico del realized ya está en cash; no se re-suma."""
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", qty=1.0, price=100.0)  # cash 9900
        _open(led, cid="B", side="SELL", qty=1.0, price=120.0)  # cash 10020
        assert led.cash == 10_020.0
        assert led.realized_pnl == pytest.approx(20.0)
        # cash NO es 10_020 + 20
        assert led.cash != 10_040.0


class TestSnapshotRestore:
    def test_roundtrip(self):
        led = AccountLedger(initial_cash=10_000.0)
        _open(led, cid="A", qty=2.0, price=100.0)
        _open(led, cid="B", side="SELL", qty=1.0, price=110.0)
        snap = led.snapshot()
        led2 = AccountLedger(initial_cash=10_000.0)
        led2.load_snapshot(snap)
        assert led2.cash == led.cash
        assert led2.realized_pnl == led.realized_pnl
        assert led2.position("X") == led.position("X")
        assert led2.is_applied("A", 0)
