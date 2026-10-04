"""F-06B: derivaciones de valoracion del AccountLedger (equity/unrealized/
exposure) — estado DERIVADO, no primario. Fase 1 spot/point_value=1/USD.
"""

from __future__ import annotations

import pytest

from papertrading.ledger import AccountLedger


def _led(cash=100_000.0):
    return AccountLedger(initial_cash=cash)


def _fill(led, *, instrument="X", side="BUY", qty=1.0, price=100.0, cid=None, seq=0):
    # Identidad estable unica por defecto (evita colisiones entre instrumentos).
    cid = cid if cid is not None else f"{instrument}:{side}"
    led.apply_fill(
        instrument=instrument, side=side, quantity=qty, price=price,
        client_order_id=cid, fill_sequence=seq,
    )


class TestEquity:
    def test_no_positions_equity_equals_cash(self):
        led = _led(100_000.0)
        assert led.equity({}) == 100_000.0

    def test_long_winner(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)  # cash 99000
        assert led.equity({"X": 110.0}) == pytest.approx(100_100.0)

    def test_long_loser(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        assert led.equity({"X": 90.0}) == pytest.approx(99_900.0)

    def test_short_winner(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)  # cash 101000
        assert led.equity({"X": 90.0}) == pytest.approx(100_100.0)

    def test_short_loser(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        assert led.equity({"X": 110.0}) == pytest.approx(99_900.0)

    def test_multiple_instruments(self):
        led = _led(100_000.0)
        _fill(led, instrument="A", qty=10, price=100.0)
        _fill(led, instrument="B", side="SELL", qty=5, price=200.0)
        eq = led.equity({"A": 110.0, "B": 180.0})
        # cash=100000; +10*110 - 5*180 = +1100-900 = +200
        assert eq == pytest.approx(100_200.0)

    def test_no_abs_quantity_in_equity(self):
        """F-05: SHORT equity = cash + qty*mark (qty negativo)."""
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)  # cash 101000
        # con abs sería 101000 + 900 = 101900; correcto = 101000 - 900
        assert led.equity({"X": 90.0}) == pytest.approx(100_100.0)


class TestUnrealized:
    def test_long_positive(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        assert led.unrealized_pnl({"X": 110.0}) == pytest.approx(100.0)

    def test_long_negative(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        assert led.unrealized_pnl({"X": 90.0}) == pytest.approx(-100.0)

    def test_short_positive(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        assert led.unrealized_pnl({"X": 90.0}) == pytest.approx(100.0)

    def test_short_negative(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        assert led.unrealized_pnl({"X": 110.0}) == pytest.approx(-100.0)

    def test_multi_instrument_sum(self):
        led = _led(100_000.0)
        _fill(led, instrument="A", qty=10, price=100.0)
        _fill(led, instrument="B", side="SELL", qty=5, price=200.0)
        assert led.unrealized_pnl({"A": 110.0, "B": 190.0}) == pytest.approx(
            100.0 + 50.0
        )


class TestGrossNet:
    def test_gross_single_long(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        assert led.gross_exposure({"X": 105.0}) == pytest.approx(1050.0)

    def test_gross_single_short(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        assert led.gross_exposure({"X": 90.0}) == pytest.approx(900.0)

    def test_gross_long_plus_short_abs(self):
        led = _led(100_000.0)
        _fill(led, instrument="A", qty=10, price=100.0)
        _fill(led, instrument="B", side="SELL", qty=10, price=100.0)
        assert led.gross_exposure({"A": 100.0, "B": 100.0}) == pytest.approx(2000.0)

    def test_gross_no_positions_zero(self):
        assert _led().gross_exposure({}) == 0.0

    def test_net_long(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        assert led.net_exposure({"X": 105.0}) == pytest.approx(1050.0)

    def test_net_short_negative(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        assert led.net_exposure({"X": 90.0}) == pytest.approx(-900.0)

    def test_net_offsets_long_short(self):
        led = _led(100_000.0)
        _fill(led, instrument="A", qty=10, price=100.0)
        _fill(led, instrument="B", side="SELL", qty=10, price=100.0)
        assert led.net_exposure({"A": 100.0, "B": 100.0}) == pytest.approx(0.0)

    def test_net_no_positions_zero(self):
        assert _led().net_exposure({}) == 0.0

    def test_gross_ge_abs_net(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        _fill(led, side="SELL", qty=3, price=100.0)
        marks = {"X": 120.0}
        g = led.gross_exposure(marks)
        n = led.net_exposure(marks)
        assert g >= abs(n)


class TestByInstrument:
    def test_signed_notional_per_instrument(self):
        led = _led(100_000.0)
        _fill(led, instrument="BTCUSDT", qty=2, price=100.0)
        _fill(led, instrument="ETHUSDT", side="SELL", qty=3, price=50.0)
        val = led.valuation({"BTCUSDT": 110.0, "ETHUSDT": 40.0})
        assert val.exposure_by_instrument["BTCUSDT"] == pytest.approx(220.0)
        assert val.exposure_by_instrument["ETHUSDT"] == pytest.approx(-120.0)

    def test_sum_by_instrument_equals_net(self):
        led = _led(100_000.0)
        _fill(led, instrument="A", qty=2, price=100.0)
        _fill(led, instrument="B", side="SELL", qty=3, price=50.0)
        val = led.valuation({"A": 110.0, "B": 40.0})
        assert sum(val.exposure_by_instrument.values()) == pytest.approx(
            val.net_exposure
        )


class TestMarkPolicy:
    def test_valid_mark(self):
        led = _led(100_000.0)
        _fill(led, qty=1, price=100.0)
        assert led.equity({"X": 100.0}) is not None

    def test_missing_mark_open_position_fail_closed(self):
        led = _led(100_000.0)
        _fill(led, qty=1, price=100.0)
        assert led.valuation({}) is None
        assert led.equity({}) is None
        assert led.gross_exposure({}) is None

    def test_nan_mark_fail_closed(self):
        led = _led(100_000.0)
        _fill(led, qty=1, price=100.0)
        assert led.valuation({"X": float("nan")}) is None

    def test_inf_mark_fail_closed(self):
        led = _led(100_000.0)
        _fill(led, qty=1, price=100.0)
        assert led.valuation({"X": float("inf")}) is None

    def test_zero_or_negative_mark_fail_closed(self):
        led = _led(100_000.0)
        _fill(led, qty=1, price=100.0)
        assert led.valuation({"X": 0.0}) is None
        assert led.valuation({"X": -5.0}) is None

    def test_missing_mark_no_positions_ok(self):
        led = _led(100_000.0)
        assert led.equity({}) == 100_000.0  # sin posiciones: equity=cash


class TestAccountingImmutable:
    def test_mark_does_not_change_primary_state(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        cash = led.cash
        pnl = led.realized_pnl
        pos = led.position("X")
        led.equity({"X": 123.0})
        led.gross_exposure({"X": 123.0})
        assert led.cash == cash
        assert led.realized_pnl == pnl
        assert led.position("X") == pos
        assert led.position("X").quantity == 10.0
        assert led.position("X").avg_entry_price == 100.0


class TestFillThenMark:
    def test_long_sequence(self):
        led = _led(100_000.0)
        _fill(led, qty=10, price=100.0)
        val = led.valuation({"X": 110.0})
        assert led.cash == 99_000.0
        assert led.position("X").quantity == 10.0
        assert val.equity == pytest.approx(100_100.0)
        assert val.unrealized_pnl == pytest.approx(100.0)
        assert val.gross_exposure == pytest.approx(1100.0)
        assert val.net_exposure == pytest.approx(1100.0)

    def test_short_sequence(self):
        led = _led(100_000.0)
        _fill(led, side="SELL", qty=10, price=100.0)
        val = led.valuation({"X": 90.0})
        assert led.cash == 101_000.0
        assert val.equity == pytest.approx(100_100.0)
        assert val.unrealized_pnl == pytest.approx(100.0)
        assert val.gross_exposure == pytest.approx(900.0)
        assert val.net_exposure == pytest.approx(-900.0)
