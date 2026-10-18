"""ATLAS-MT5-ACCOUNTING-06I-A: captura del modo de calculo MT5 (SYMBOL_TRADE_CALC_MODE).

Offline, sin MT5 real. Solo captura/valida el dato; NO implementa valoracion.
"""

from __future__ import annotations

import pytest

from tests.test_mt5_execution_foundation import INSTRUMENT, _fake

from brokers.mt5 import (
    BrokerInvalidOrderError,
    MT5SymbolSpec,
    SYMBOL_TRADE_MODE_FULL,
    instrument_spec_from_mt5,
    read_mt5_symbol_spec,
)


def _read(fake):
    return read_mt5_symbol_spec(
        fake, INSTRUMENT, INSTRUMENT, filling_mode_default=1
    )


# ------------------------------------------------------------ propagacion


class TestPropagation:
    def test_absent_calc_mode_is_unknown(self):
        spec = _read(_fake())
        assert spec.trade_calc_mode is None

    def test_present_none_is_unknown(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = None
        assert _read(fake).trade_calc_mode is None

    def test_valid_int_preserved_exactly(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 2
        assert _read(fake).trade_calc_mode == 2

    def test_unrecognized_non_negative_preserved(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 999
        assert _read(fake).trade_calc_mode == 999

    def test_trade_mode_and_calc_mode_are_distinct(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 1
        spec = _read(fake)
        assert spec.trade_mode == SYMBOL_TRADE_MODE_FULL
        assert spec.trade_calc_mode == 1
        assert spec.trade_mode != spec.trade_calc_mode


# ------------------------------------------------ ausencias y compatibilidad


class TestCompatibility:
    def test_default_constructor_value_is_none(self):
        base = dict(
            strategy_instrument="EURUSD", name="EURUSD", visible=True,
            volume_min=0.01, volume_step=0.01, volume_max=100.0, point=0.00001,
            trade_tick_size=0.00001, trade_tick_value=1.0, trade_contract_size=100000.0,
            currency_base="EUR", currency_profit="USD", currency_margin="USD",
            trade_mode=SYMBOL_TRADE_MODE_FULL, filling_mode=1, digits=5,
        )
        spec = MT5SymbolSpec(**base)
        assert spec.trade_calc_mode is None

    def test_existing_fields_unchanged(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 0
        spec = _read(fake)
        assert spec.name == INSTRUMENT
        assert spec.trade_contract_size == pytest.approx(100000.0)
        assert spec.currency_profit == "USD"

    def test_instrument_spec_from_mt5_unchanged(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 7
        spec = _read(fake)
        atlas = instrument_spec_from_mt5(spec, atlas_instrument_id="EURUSD")
        # El contrato generico no incorpora el modo MT5 (responsabilidades separadas).
        assert not hasattr(atlas, "trade_calc_mode")
        assert atlas.contract_size == pytest.approx(100000.0)


# -------------------------------------------------------- validacion estricta


class TestStrictValidation:
    @pytest.mark.parametrize("value", [True, False, "2", "x", 2.0, 2.5, object(), -1, -5])
    def test_invalid_calc_mode_raises_typed_error(self, value):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = value
        with pytest.raises(BrokerInvalidOrderError):
            _read(fake)


# ------------------------------------------------------------------ aislamiento


class TestIsolation:
    def test_no_trading_calls(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 2
        _read(fake)
        assert fake.send_count == 0
        assert fake.check_count == 0
        assert fake.cancel_count == 0
