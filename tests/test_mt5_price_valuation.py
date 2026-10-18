"""ATLAS-MT5-ACCOUNTING-06I-C2: PnL lineal offline por movimiento de precio.

Tests offline y deterministas del motor puro `execution.mt5_price_valuation`.
Sin MT5, sin terminal, sin red, sin credenciales.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timezone

import pytest

from execution.mt5_price_valuation import (
    MT5PricePnlInput,
    MT5PricePnlMethod,
    MT5PricePnlResult,
    MT5PricePnlStatus,
    MT5PriceValuationError,
    estimate_mt5_price_pnl,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
RECV = datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)

_ALL_MODES = (
    "SYMBOL_CALC_MODE_FOREX",
    "SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE",
    "SYMBOL_CALC_MODE_CFD",
    "SYMBOL_CALC_MODE_CFDINDEX",
    "SYMBOL_CALC_MODE_CFDLEVERAGE",
    "SYMBOL_CALC_MODE_EXCH_STOCKS",
    "SYMBOL_CALC_MODE_EXCH_STOCKS_MOEX",
)


def _input(**over) -> MT5PricePnlInput:
    base = dict(
        position_id="PID-1",
        instrument="EURUSD",
        mt5_symbol="EURUSD.raw",
        quantity=2.0,
        avg_entry_price=1.10,
        contract_size=100_000.0,
        currency_profit="USD",
        trade_calc_mode_name="SYMBOL_CALC_MODE_FOREX",
        bid=1.105,
        ask=1.1052,
        tick_time=NOW,
        received_at=RECV,
        mark_instrument="EURUSD",
        mark_mt5_symbol="EURUSD.raw",
    )
    base.update(over)
    return MT5PricePnlInput(**base)


# ------------------------------------------------------- whitelist y metodo


class TestSupportedModes:
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_each_whitelisted_mode_is_valued(self, mode):
        res = estimate_mt5_price_pnl(_input(trade_calc_mode_name=mode))
        assert res.status is MT5PricePnlStatus.VALUED
        assert res.method is MT5PricePnlMethod.LINEAR_PRICE_MOVE
        assert res.reason_codes == ()
        assert res.reason_code is None


class TestFormula:
    def test_positive_pnl_long(self):
        # 2 lots * 100000 * (1.105 - 1.10) = 1000
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=1.105))
        assert res.price_pnl == pytest.approx(1000.0)

    def test_negative_pnl_long(self):
        # 2 * 100000 * (1.095 - 1.10) = -1000
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=1.095))
        assert res.price_pnl == pytest.approx(-1000.0)

    def test_zero_pnl(self):
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=1.10))
        assert res.price_pnl == pytest.approx(0.0)

    def test_short_uses_ask_and_negative(self):
        # -2 * 100000 * (1.1052 - 1.10) = -1040
        res = estimate_mt5_price_pnl(_input(quantity=-2.0, ask=1.1052))
        assert res.market_side == "ASK"
        assert res.valuation_price == pytest.approx(1.1052)
        assert res.price_pnl == pytest.approx(-1040.0)

    def test_long_uses_bid(self):
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=1.107, ask=9.999))
        assert res.market_side == "BID"
        assert res.valuation_price == pytest.approx(1.107)
        assert res.price_pnl == pytest.approx(2.0 * 100_000.0 * (1.107 - 1.10))

    def test_no_second_sign(self):
        # El signo lo lleva `quantity`; no se invierte de nuevo.
        short = estimate_mt5_price_pnl(_input(quantity=-3.0, ask=1.101))
        expected = -3.0 * 100_000.0 * (1.101 - 1.10)
        assert short.price_pnl == pytest.approx(expected)
        assert short.price_pnl < 0


class TestValuationPrice:
    def test_long_missing_bid_is_unverified(self):
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=None))
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert "VALUATION_PRICE_MISSING" in res.reason_codes
        assert res.price_pnl is None
        assert res.market_side is None

    def test_short_missing_ask_is_unverified(self):
        res = estimate_mt5_price_pnl(_input(quantity=-2.0, ask=None))
        assert res.reason_code == "VALUATION_PRICE_MISSING"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_bid_is_unverified(self, bad):
        res = estimate_mt5_price_pnl(_input(quantity=1.0, bid=bad))
        assert res.reason_code == "VALUATION_PRICE_MISSING"

    def test_no_fallback_to_other_side(self):
        # Falta bid (largo) pero ask existe: NO se usa ask como sustituto.
        res = estimate_mt5_price_pnl(_input(quantity=2.0, bid=None, ask=1.2))
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert res.valuation_price is None


class TestUnsupportedModes:
    @pytest.mark.parametrize(
        "mode",
        [
            None,
            "",
            3,
            "SYMBOL_CALC_MODE_FUTURES",
            "SYMBOL_CALC_MODE_EXCH_FUTURES",
            "SYMBOL_CALC_MODE_EXCH_FUTURES_FORTS",
            "SYMBOL_CALC_MODE_EXCH_BONDS",
            "SYMBOL_CALC_MODE_EXCH_BONDS_MOEX",
            "SYMBOL_CALC_MODE_SERV_COLLATERAL",
            "SYMBOL_CALC_MODE_EXCH_OPTIONS",
            "SYMBOL_CALC_MODE_EXCH_OPTIONS_MARGIN",
            "ARBITRARY_MODE",
        ],
    )
    def test_excluded_or_unknown_modes_unverified(self, mode):
        res = estimate_mt5_price_pnl(_input(trade_calc_mode_name=mode))
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert res.price_pnl is None
        assert res.method is None

    def test_none_is_mode_unknown(self):
        assert estimate_mt5_price_pnl(
            _input(trade_calc_mode_name=None)
        ).reason_code == "MODE_UNKNOWN"

    def test_non_whitelisted_name(self):
        assert estimate_mt5_price_pnl(
            _input(trade_calc_mode_name="SYMBOL_CALC_MODE_FUTURES")
        ).reason_code == "MODE_NOT_WHITELISTED"


class TestQuantity:
    def test_zero_quantity(self):
        res = estimate_mt5_price_pnl(_input(quantity=0.0))
        assert res.reason_code == "QUANTITY_ZERO"
        assert res.price_pnl is None

    @pytest.mark.parametrize(
        "bad", [True, False, "2.0", "x", object(), float("nan"),
                float("inf"), float("-inf"), 10 ** 400]
    )
    def test_invalid_quantity(self, bad):
        res = estimate_mt5_price_pnl(_input(quantity=bad))
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert res.price_pnl is None
        assert "QUANTITY_INVALID" in res.reason_codes


class TestOperands:
    @pytest.mark.parametrize("bad", [None, float("nan"), "x", True, 10 ** 400])
    def test_invalid_avg_entry_price(self, bad):
        res = estimate_mt5_price_pnl(_input(avg_entry_price=bad))
        assert "AVG_ENTRY_PRICE_INVALID" in res.reason_codes

    @pytest.mark.parametrize("bad", [0, 0.0, -1.0, None, float("nan"),
                                     float("inf"), "100"])
    def test_invalid_contract_size(self, bad):
        res = estimate_mt5_price_pnl(_input(contract_size=bad))
        assert "CONTRACT_SIZE_INVALID" in res.reason_codes

    @pytest.mark.parametrize("bad", [None, "", "   ", 123, True])
    def test_invalid_currency_profit(self, bad):
        res = estimate_mt5_price_pnl(_input(currency_profit=bad))
        assert "CURRENCY_PROFIT_MISSING" in res.reason_codes


class TestInstrumentCoherence:
    def test_instrument_missing(self):
        res = estimate_mt5_price_pnl(_input(instrument=None))
        assert res.reason_code == "INSTRUMENT_MISSING"

    def test_mark_instrument_mismatch(self):
        res = estimate_mt5_price_pnl(_input(mark_instrument="GBPUSD"))
        assert res.reason_code == "INSTRUMENT_MISMATCH"

    def test_symbol_missing_position(self):
        res = estimate_mt5_price_pnl(_input(mt5_symbol=""))
        assert res.reason_code == "SYMBOL_MISSING"

    def test_symbol_missing_mark(self):
        res = estimate_mt5_price_pnl(_input(mark_mt5_symbol=None))
        assert res.reason_code == "SYMBOL_MISSING"

    def test_symbols_differ(self):
        res = estimate_mt5_price_pnl(_input(mark_mt5_symbol="EURUSD.other"))
        assert "INSTRUMENT_MISMATCH" in res.reason_codes


class TestReasonPrecedence:
    def test_multiple_invalid_reasons_ordered(self):
        res = estimate_mt5_price_pnl(
            _input(
                instrument=None,
                mt5_symbol="",
                trade_calc_mode_name="ARBITRARY",
                currency_profit="",
                quantity=None,
                avg_entry_price=None,
                contract_size=0.0,
            )
        )
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert res.reason_codes == (
            "INSTRUMENT_MISSING",
            "SYMBOL_MISSING",
            "MODE_NOT_WHITELISTED",
            "CURRENCY_PROFIT_MISSING",
            "QUANTITY_INVALID",
            "AVG_ENTRY_PRICE_INVALID",
            "CONTRACT_SIZE_INVALID",
        )
        assert res.reason_code == "INSTRUMENT_MISSING"
        assert res.price_pnl is None

    def test_reason_codes_deduplicated_and_stable(self):
        res = estimate_mt5_price_pnl(_input(mt5_symbol="", mark_mt5_symbol=""))
        assert res.reason_codes == ("SYMBOL_MISSING",)


class TestOverflow:
    def test_non_finite_result(self):
        res = estimate_mt5_price_pnl(
            _input(quantity=1e308, contract_size=1e308,
                   avg_entry_price=0.0, bid=1e308)
        )
        assert res.status is MT5PricePnlStatus.UNVERIFIED
        assert res.reason_code == "PNL_NOT_FINITE"
        assert res.price_pnl is None


class TestInvocationContract:
    @pytest.mark.parametrize("bad", [None, 5, "x", {}, object()])
    def test_non_input_raises(self, bad):
        with pytest.raises(MT5PriceValuationError):
            estimate_mt5_price_pnl(bad)


class TestScopeAndPurity:
    def test_no_swap_or_fx_fields_in_input(self):
        names = {f.name for f in dataclasses.fields(MT5PricePnlInput)}
        assert "swap" not in names
        assert "last" not in names
        assert not any("fx" in n for n in names)

    def test_result_currency_is_currency_profit(self):
        res = estimate_mt5_price_pnl(_input(currency_profit="JPY"))
        assert res.result_currency == "JPY"
        assert res.status is MT5PricePnlStatus.VALUED

    def test_timestamp_verified_always_false(self):
        assert estimate_mt5_price_pnl(_input()).timestamp_verified is False
        assert estimate_mt5_price_pnl(
            _input(quantity=0.0)
        ).timestamp_verified is False

    def test_timestamps_preserved(self):
        res = estimate_mt5_price_pnl(_input())
        assert res.tick_time == NOW
        assert res.received_at == RECV

    def test_deterministic(self):
        a = estimate_mt5_price_pnl(_input())
        b = estimate_mt5_price_pnl(_input())
        assert a == b
        assert isinstance(a, MT5PricePnlResult)

    def test_varying_swap_does_not_change_input_contract(self):
        # El input no incluye swap: dos "posiciones" con distinto swap
        # (representado solo fuera del motor) producen el mismo resultado.
        r1 = estimate_mt5_price_pnl(_input(position_id="P-A"))
        r2 = estimate_mt5_price_pnl(_input(position_id="P-B"))
        assert r1.price_pnl == r2.price_pnl

    def test_no_terminal_imported_or_invoked(self):
        estimate_mt5_price_pnl(_input())
        assert "MetaTrader5" not in sys.modules
