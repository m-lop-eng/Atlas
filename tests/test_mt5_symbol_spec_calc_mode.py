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


class _CalcGetterRaiser:
    """Symbol-info dobule con un getter `trade_calc_mode` DEFINIDO que falla."""

    def __init__(self, base, exc):
        self.__dict__.update(vars(base))
        self._exc = exc

    @property
    def trade_calc_mode(self):
        raise self._exc


class _CalcDynamic:
    """Symbol-info double cuyo `trade_calc_mode` procede de `__getattr__`."""

    def __init__(self, base, value):
        self.__dict__.update(vars(base))
        self._value = value

    def __getattr__(self, item):
        if item == "trade_calc_mode":
            return self._value
        raise AttributeError(item)


class TestAttributeAccess:
    def test_defined_getter_attributeerror_is_typed_error(self):
        fake = _fake()
        base = fake.symbols[INSTRUMENT]
        fake.symbols[INSTRUMENT] = _CalcGetterRaiser(base, AttributeError("boom"))
        with pytest.raises(BrokerInvalidOrderError):
            _read(fake)

    @pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("boom")])
    def test_defined_getter_other_exception_is_typed_error(self, exc):
        fake = _fake()
        base = fake.symbols[INSTRUMENT]
        fake.symbols[INSTRUMENT] = _CalcGetterRaiser(base, exc)
        with pytest.raises(BrokerInvalidOrderError):
            _read(fake)

    def test_dynamic_valid_getter_value_preserved(self):
        fake = _fake()
        base = fake.symbols[INSTRUMENT]
        fake.symbols[INSTRUMENT] = _CalcDynamic(base, 3)
        assert _read(fake).trade_calc_mode == 3

    def test_absent_is_none_not_defect(self):
        assert _read(_fake()).trade_calc_mode is None


# ------------------------------------------------------------------ aislamiento


class TestIsolation:
    def test_no_trading_calls(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 2
        _read(fake)
        assert fake.send_count == 0
        assert fake.check_count == 0
        assert fake.cancel_count == 0


# ---------------------------------- 06I-B5: resolucion del NOMBRE del modo


def _with_consts(fake, **consts):
    for name, value in consts.items():
        setattr(fake, name, value)
    return fake


class TestCalcModeNameResolution:
    def test_raw_int_preserved_when_name_resolved(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=0)
        fake.symbols[INSTRUMENT].trade_calc_mode = 0
        spec = _read(fake)
        assert spec.trade_calc_mode == 0
        assert spec.trade_calc_mode_name == "SYMBOL_CALC_MODE_FOREX"

    @pytest.mark.parametrize(
        "name,value",
        [
            ("SYMBOL_CALC_MODE_FOREX", 0),
            ("SYMBOL_CALC_MODE_FUTURES", 1),
            ("SYMBOL_CALC_MODE_CFD", 2),
            ("SYMBOL_CALC_MODE_CFDINDEX", 3),
            ("SYMBOL_CALC_MODE_CFDLEVERAGE", 4),
            ("SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE", 5),
            ("SYMBOL_CALC_MODE_EXCH_STOCKS", 32),
            ("SYMBOL_CALC_MODE_EXCH_BONDS", 37),
            ("SYMBOL_CALC_MODE_SERV_COLLATERAL", 64),
        ],
    )
    def test_known_constant_maps_to_exact_name(self, name, value):
        fake = _with_consts(_fake(), **{name: value})
        fake.symbols[INSTRUMENT].trade_calc_mode = value
        spec = _read(fake)
        assert spec.trade_calc_mode == value
        assert spec.trade_calc_mode_name == name

    def test_uses_module_values_not_hardcoded_table(self):
        # La constante del modulo vale 999 (NO 0): el nombre debe resolverse por
        # el VALOR del modulo, no por una tabla documental hardcodeada.
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=999)
        fake.symbols[INSTRUMENT].trade_calc_mode = 999
        assert _read(fake).trade_calc_mode_name == "SYMBOL_CALC_MODE_FOREX"
        fake.symbols[INSTRUMENT].trade_calc_mode = 0
        assert _read(fake).trade_calc_mode_name is None

    def test_unknown_mode_keeps_int_and_none_name(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=0)
        fake.symbols[INSTRUMENT].trade_calc_mode = 4
        spec = _read(fake)
        assert spec.trade_calc_mode == 4
        assert spec.trade_calc_mode_name is None

    def test_35_not_inferred_as_forts(self):
        # El modulo de prueba NO expone EXCH_FUTURES_FORTS: 35 no se etiqueta.
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=0)
        fake.symbols[INSTRUMENT].trade_calc_mode = 35
        spec = _read(fake)
        assert spec.trade_calc_mode == 35
        assert spec.trade_calc_mode_name is None

    def test_missing_constants_no_error(self):
        # Solo una constante presente: las ausentes no rompen la lectura.
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_CFD=2)
        fake.symbols[INSTRUMENT].trade_calc_mode = 2
        assert _read(fake).trade_calc_mode_name == "SYMBOL_CALC_MODE_CFD"

    def test_ambiguous_duplicate_values_not_resolved(self):
        fake = _with_consts(
            _fake(), SYMBOL_CALC_MODE_CFD=7, SYMBOL_CALC_MODE_CFDINDEX=7
        )
        fake.symbols[INSTRUMENT].trade_calc_mode = 7
        spec = _read(fake)
        assert spec.trade_calc_mode == 7
        assert spec.trade_calc_mode_name is None  # ambiguedad: sin eleccion

    def test_bool_constant_does_not_match(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=True)
        fake.symbols[INSTRUMENT].trade_calc_mode = 1
        assert _read(fake).trade_calc_mode_name is None

    def test_non_int_constant_does_not_match(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX="0")
        fake.symbols[INSTRUMENT].trade_calc_mode = 0
        assert _read(fake).trade_calc_mode_name is None

    def test_module_without_constants_yields_none(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 3
        spec = _read(fake)
        assert spec.trade_calc_mode == 3
        assert spec.trade_calc_mode_name is None

    def test_calc_mode_none_keeps_name_none(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=0)
        fake.symbols[INSTRUMENT].trade_calc_mode = None
        spec = _read(fake)
        assert spec.trade_calc_mode is None
        assert spec.trade_calc_mode_name is None

    def test_old_positional_constructor_still_works(self):
        spec = MT5SymbolSpec(
            "EURUSD", "EURUSD", True, 0.01, 0.01, 100.0, 0.00001, 0.00001,
            1.0, 100000.0, "EUR", "USD", "USD", SYMBOL_TRADE_MODE_FULL, 1, 5,
        )
        assert spec.trade_calc_mode is None
        assert spec.trade_calc_mode_name is None

    def test_other_contracts_unchanged(self):
        fake = _with_consts(_fake(), SYMBOL_CALC_MODE_FOREX=0)
        fake.symbols[INSTRUMENT].trade_calc_mode = 0
        fake.symbols[INSTRUMENT].trade_tick_value = 2.5
        fake.symbols[INSTRUMENT].trade_tick_value_profit = 2.5
        fake.symbols[INSTRUMENT].trade_tick_value_loss = 3.5
        spec = _read(fake)
        assert spec.trade_mode == SYMBOL_TRADE_MODE_FULL
        assert spec.trade_tick_value == pytest.approx(2.5)
        assert spec.trade_tick_value_profit == pytest.approx(2.5)
        assert spec.trade_tick_value_loss == pytest.approx(3.5)
        atlas = instrument_spec_from_mt5(spec, atlas_instrument_id="EURUSD")
        assert not hasattr(atlas, "trade_calc_mode_name")


# ---------------------------------- 06I-B1: tick value profit/loss


def _property_raising(base, name, exc):
    """Symbol-info double con una `@property` (DEFINIDA) que lanza `exc`."""

    def _getter(self):
        raise exc

    cls = type("_PropTickValue", (), {name: property(_getter)})
    obj = cls()
    obj.__dict__.update(vars(base))
    return obj


class _TickValueDynamic:
    def __init__(self, base, name, value):
        self.__dict__.update(vars(base))
        self._name = name
        self._value = value

    def __getattr__(self, item):
        if item == self._name:
            return self._value
        raise AttributeError(item)


class TestTickValueCapture:
    def test_new_fields_default_none(self):
        spec = _read(_fake())
        assert spec.trade_tick_value_profit is None
        assert spec.trade_tick_value_loss is None

    def test_two_distinct_values_captured(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_tick_value_profit = 1.0
        fake.symbols[INSTRUMENT].trade_tick_value_loss = 1.25
        spec = _read(fake)
        assert spec.trade_tick_value_profit == pytest.approx(1.0)
        assert spec.trade_tick_value_loss == pytest.approx(1.25)

    def test_present_none_is_none(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_tick_value_profit = None
        fake.symbols[INSTRUMENT].trade_tick_value_loss = None
        spec = _read(fake)
        assert spec.trade_tick_value_profit is None
        assert spec.trade_tick_value_loss is None

    def test_each_absent_independently_not_inferred(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_tick_value_profit = 2.0
        # trade_tick_value_loss ausente
        spec = _read(fake)
        assert spec.trade_tick_value_profit == pytest.approx(2.0)
        assert spec.trade_tick_value_loss is None  # NO se infiere del profit

    def test_generic_tick_value_not_copied(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_tick_value = 9.0
        spec = _read(fake)
        assert spec.trade_tick_value == pytest.approx(9.0)
        assert spec.trade_tick_value_profit is None
        assert spec.trade_tick_value_loss is None

    @pytest.mark.parametrize(
        "field", ["trade_tick_value_profit", "trade_tick_value_loss"]
    )
    @pytest.mark.parametrize(
        "value", [True, False, "1.0", "x", object(), float("nan"),
                  float("inf"), float("-inf"), 10 ** 400]
    )
    def test_invalid_value_typed_error(self, field, value):
        fake = _fake()
        setattr(fake.symbols[INSTRUMENT], field, value)
        with pytest.raises(BrokerInvalidOrderError):
            _read(fake)

    @pytest.mark.parametrize(
        "field", ["trade_tick_value_profit", "trade_tick_value_loss"]
    )
    @pytest.mark.parametrize(
        "exc", [AttributeError("x"), RuntimeError("x"), ValueError("x")]
    )
    def test_defective_getter_typed_error(self, field, exc):
        fake = _fake()
        base = fake.symbols[INSTRUMENT]
        fake.symbols[INSTRUMENT] = _property_raising(base, field, exc)
        with pytest.raises(BrokerInvalidOrderError):
            _read(fake)

    def test_dynamic_valid_value_captured(self):
        fake = _fake()
        base = fake.symbols[INSTRUMENT]
        fake.symbols[INSTRUMENT] = _TickValueDynamic(
            base, "trade_tick_value_profit", 3.5
        )
        assert _read(fake).trade_tick_value_profit == pytest.approx(3.5)


class TestTickValueCompat:
    def _base(self):
        return dict(
            strategy_instrument="EURUSD", name="EURUSD", visible=True,
            volume_min=0.01, volume_step=0.01, volume_max=100.0, point=0.00001,
            trade_tick_size=0.00001, trade_tick_value=1.0, trade_contract_size=100000.0,
            currency_base="EUR", currency_profit="USD", currency_margin="USD",
            trade_mode=SYMBOL_TRADE_MODE_FULL, filling_mode=1, digits=5,
        )

    def test_keyword_constructor_back_compat(self):
        spec = MT5SymbolSpec(**self._base())
        assert spec.trade_tick_value_profit is None
        assert spec.trade_tick_value_loss is None

    def test_calc_mode_and_trade_mode_unchanged(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_calc_mode = 4
        spec = _read(fake)
        assert spec.trade_calc_mode == 4
        assert spec.trade_mode == SYMBOL_TRADE_MODE_FULL

    def test_instrument_spec_from_mt5_unchanged(self):
        fake = _fake()
        fake.symbols[INSTRUMENT].trade_tick_value_profit = 1.0
        atlas = instrument_spec_from_mt5(_read(fake), atlas_instrument_id="EURUSD")
        assert not hasattr(atlas, "trade_tick_value_profit")
