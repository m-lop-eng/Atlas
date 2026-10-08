"""Tests MULTI-ASSET-03: InstrumentSpec y economia multi-activo.

Contrato (MULTI-ASSET-02 congelado, implementado aqui):
    exposure_quote = quantity * contract_size * price
    exposure_account = exposure_quote * FX(quote -> account)
    quantity normalizada por floor al lattice (nunca round-up).
Sin FX requerido y ausente => fail-closed. Legacy paper/backtest intacto.

Determinista: sin red, sin sleeps, sin MT5 real.
"""

from __future__ import annotations

import pytest

from execution.instrument import (
    AssetClass,
    ExposureError,
    FxMissingError,
    InstrumentSpec,
    InstrumentSpecError,
    exposure_account,
    exposure_quote,
)
from papertrading.ledger import AccountLedger, LedgerError
from risk.engine import RiskEngine
from risk.quantity import QuantityError, normalize_quantity
from risk.types import AccountRiskState, ProjectedExposure, RiskConfig
from strategies.base.signal import Action, Signal

from brokers.mt5 import MT5SymbolSpec, instrument_spec_from_mt5

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    _LevelLong,
    _drive,
    _stack,
)

USD = "USD"


# --------------------------------------------------------------------------
# INSTRUMENT SPEC
# --------------------------------------------------------------------------


class TestInstrumentSpec:
    def test_valid(self):
        spec = InstrumentSpec(
            "XAUUSD", AssetClass.METAL, contract_size=100.0, quote_currency="USD",
            quantity_min=0.01, quantity_step=0.01, quantity_max=100.0,
        )
        assert spec.contract_size == 100.0
        assert spec.quantity_step == 0.01

    def test_empty_id_rejected(self):
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("")

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_invalid_contract_size(self, bad):
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("X", contract_size=bad)

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_invalid_currency(self, bad):
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("X", quote_currency=bad)

    def test_invalid_min_step_max(self):
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("X", quantity_min=-1.0)
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("X", quantity_step=0.0)
        with pytest.raises(InstrumentSpecError):
            InstrumentSpec("X", quantity_min=10.0, quantity_max=1.0)

    def test_legacy_alias(self):
        spec = InstrumentSpec.legacy("BTCUSDT", point_value=50.0, currency="USD")
        assert spec.contract_size == 50.0
        assert spec.quote_currency == "USD"


# --------------------------------------------------------------------------
# QUANTITY CONTRACT
# --------------------------------------------------------------------------


class TestQuantityContract:
    def _spec(self, **over):
        base = dict(
            atlas_instrument_id="X", quantity_min=0.1, quantity_step=0.1,
            quantity_max=3.0,
        )
        base.update(over)
        return InstrumentSpec(**base)

    def test_exact_min(self):
        assert normalize_quantity(self._spec(), 0.1) == pytest.approx(0.1)

    def test_below_min_rejected(self):
        with pytest.raises(QuantityError):
            normalize_quantity(self._spec(), 0.05)

    def test_exact_step(self):
        assert normalize_quantity(self._spec(), 0.3) == pytest.approx(0.3)

    def test_between_steps_floored(self):
        assert normalize_quantity(self._spec(), 0.35) == pytest.approx(0.3)

    def test_floor_below_min_rejected(self):
        spec = self._spec(quantity_min=0.5, quantity_step=1.0, quantity_max=10.0)
        with pytest.raises(QuantityError):
            normalize_quantity(spec, 0.7)  # floor(0.7/1)=0 < 0.5

    def test_above_max_rejected(self):
        with pytest.raises(QuantityError):
            normalize_quantity(self._spec(), 5.0)

    def test_no_silent_round_up(self):
        assert normalize_quantity(self._spec(), 0.35) == pytest.approx(0.3)
        assert normalize_quantity(self._spec(), 0.35) < 0.35

    def test_target_not_mutated(self):
        target = 0.35
        normalize_quantity(self._spec(), target)
        assert target == 0.35

    def test_no_step_only_min_max(self):
        spec = InstrumentSpec("X", quantity_min=1.0, quantity_max=5.0)
        assert normalize_quantity(spec, 2.5) == pytest.approx(2.5)
        with pytest.raises(QuantityError):
            normalize_quantity(spec, 0.5)


# --------------------------------------------------------------------------
# SIZING
# --------------------------------------------------------------------------


def _signal(entry: float, stop: float) -> Signal:
    return Signal(
        strategy_id="s", strategy_version="1.0.0", instrument="X",
        action=Action.LONG, reference_price=entry, stop_price=stop,
        target_quantity=None,
    )


class TestSizing:
    def _qty(self, contract_size: float) -> float:
        engine = RiskEngine(RiskConfig(risk_per_trade=0.00025))
        decision = engine.validate(
            _signal(100.0, 90.0), AccountRiskState(equity=1_000_000.0),
            contract_size=contract_size,
        )
        assert decision.approved
        return decision.approved_quantity

    def test_legacy_contract_size_one(self):
        # risk_cap = 250; distance = 10; qty = 250/(10*1) = 25
        assert self._qty(1.0) == pytest.approx(25.0)

    def test_forex_contract_size_100000(self):
        assert self._qty(100_000.0) == pytest.approx(250.0 / (10.0 * 100_000.0))

    def test_gold_contract_size_100(self):
        assert self._qty(100.0) == pytest.approx(0.25)

    def test_point_value_equals_contract_size(self):
        engine = RiskEngine(RiskConfig(risk_per_trade=0.00025))
        state = AccountRiskState(equity=1_000_000.0)
        sig = _signal(100.0, 90.0)
        a = engine.validate(sig, state, point_value=50.0)
        b = engine.validate(sig, state, contract_size=50.0)
        assert a.approved_quantity == b.approved_quantity

    def test_point_value_contract_size_mismatch_rejected(self):
        engine = RiskEngine(RiskConfig())
        with pytest.raises(ValueError):
            engine.validate(
                _signal(100.0, 90.0), AccountRiskState(equity=1_000_000.0),
                point_value=50.0, contract_size=1.0,
            )


# --------------------------------------------------------------------------
# EXPOSURE
# --------------------------------------------------------------------------


class TestExposure:
    def test_quote_equals_account(self):
        spec = InstrumentSpec("BTCUSDT", contract_size=2.0, quote_currency="USD")
        assert exposure_quote(spec, 3.0, 100.0) == pytest.approx(600.0)
        assert exposure_account(
            spec, 3.0, 100.0, account_currency="USD"
        ) == pytest.approx(600.0)

    def test_quote_differs_with_fx(self):
        spec = InstrumentSpec("X", contract_size=1.0, quote_currency="EUR")
        assert exposure_account(
            spec, 10.0, 100.0, account_currency="USD", fx_rate=1.1
        ) == pytest.approx(1100.0)

    def test_missing_fx_fail_closed(self):
        spec = InstrumentSpec("X", contract_size=1.0, quote_currency="EUR")
        with pytest.raises(FxMissingError):
            exposure_account(spec, 10.0, 100.0, account_currency="USD")

    def test_nonfinite_inputs(self):
        spec = InstrumentSpec("X", contract_size=1.0, quote_currency="USD")
        with pytest.raises(ExposureError):
            exposure_quote(spec, 1.0, float("nan"))
        with pytest.raises(ExposureError):
            exposure_quote(spec, 1.0, 0.0)
        spec_eur = InstrumentSpec("X", contract_size=1.0, quote_currency="EUR")
        with pytest.raises(ExposureError):
            exposure_account(
                spec_eur, 1.0, 100.0, account_currency="USD", fx_rate=float("inf")
            )


# --------------------------------------------------------------------------
# RISK ENGINE GATES (exposure en account currency)
# --------------------------------------------------------------------------


class TestRiskGatesAccountCurrency:
    def test_multiple_instruments_aggregate(self):
        engine = RiskEngine(
            RiskConfig(max_gross_exposure=20_000.0, max_net_exposure=20_000.0)
        )
        # Exposiciones ya en account currency (USD): 10k + 5k + 2k.
        projected = ProjectedExposure(
            equity=1_000_000.0,
            gross_exposure=17_000.0,
            net_exposure=17_000.0,
            exposure_by_instrument={"EURUSD": 10_000.0, "BTCUSD": 5_000.0,
                                    "XAUUSD": 2_000.0},
        )
        assert engine.validate_projected_exposure(projected).approved

    def test_gross_limit_blocks(self):
        engine = RiskEngine(RiskConfig(max_gross_exposure=10_000.0))
        projected = ProjectedExposure(
            equity=1_000_000.0, gross_exposure=17_000.0,
            net_exposure=0.0, exposure_by_instrument={},
        )
        assert not engine.validate_projected_exposure(projected).approved

    def test_missing_metric_fail_closed(self):
        engine = RiskEngine(RiskConfig(max_gross_exposure=10_000.0))
        projected = ProjectedExposure(
            equity=1_000_000.0, gross_exposure=None, net_exposure=None,
            exposure_by_instrument=None,
        )
        assert not engine.validate_projected_exposure(projected).approved


# --------------------------------------------------------------------------
# LEDGER
# --------------------------------------------------------------------------


class TestLedgerEconomics:
    def test_realized_pnl_with_contract_size(self):
        led = AccountLedger(initial_cash=10_000.0, account_currency=USD)
        spec = InstrumentSpec("X", contract_size=100.0, quote_currency=USD)
        led.register_instrument(spec)
        led.apply_fill(
            instrument="X", side="BUY", quantity=2.0, price=50.0,
            client_order_id="C1", fill_sequence=0,
        )
        assert led.cash == pytest.approx(10_000.0 - 2.0 * 50.0 * 100.0)
        led.apply_fill(
            instrument="X", side="SELL", quantity=2.0, price=60.0,
            client_order_id="C2", fill_sequence=0,
        )
        assert led.realized_pnl == pytest.approx((60.0 - 50.0) * 2.0 * 100.0)

    def test_commission_separate_from_gross_realized(self):
        led = AccountLedger(initial_cash=10_000.0)
        led.register_instrument(
            InstrumentSpec("X", contract_size=100.0, quote_currency=USD)
        )
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, commission=5.0,
        )
        assert led.cash == pytest.approx(10_000.0 - 100.0 * 100.0 - 5.0)
        assert led.realized_pnl == 0.0

    def test_unrealized_and_equity_with_contract_size(self):
        led = AccountLedger(initial_cash=10_000.0)
        led.register_instrument(
            InstrumentSpec("X", contract_size=100.0, quote_currency=USD)
        )
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0,
        )
        val = led.valuation({"X": 110.0})
        assert val is not None
        assert val.unrealized_pnl == pytest.approx(1.0 * (110.0 - 100.0) * 100.0)
        assert val.gross_exposure == pytest.approx(1.0 * 110.0 * 100.0)
        assert val.equity == pytest.approx(10_000.0 + val.unrealized_pnl)

    def test_missing_fx_apply_fill_fail_closed(self):
        led = AccountLedger(initial_cash=10_000.0, account_currency=USD)
        led.register_instrument(
            InstrumentSpec("X", contract_size=1.0, quote_currency="EUR")
        )
        with pytest.raises(LedgerError):
            led.apply_fill(
                instrument="X", side="BUY", quantity=1.0, price=100.0,
                client_order_id="C", fill_sequence=0,
            )

    def test_explicit_fx_applied(self):
        led = AccountLedger(initial_cash=10_000.0, account_currency=USD)
        led.register_instrument(
            InstrumentSpec("X", contract_size=1.0, quote_currency="EUR")
        )
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0, fx_rate=1.1,
        )
        assert led.cash == pytest.approx(10_000.0 - 100.0 * 1.1)
        # valuation sin FX => None (fail-closed)
        assert led.valuation({"X": 100.0}) is None
        val = led.valuation({"X": 100.0}, {"EUR": 1.1})
        assert val is not None and val.gross_exposure == pytest.approx(100.0 * 1.1)

    def test_duplicate_fill_idempotent_with_contract_size(self):
        led = AccountLedger(initial_cash=10_000.0)
        led.register_instrument(
            InstrumentSpec("X", contract_size=100.0, quote_currency=USD)
        )
        kw = dict(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C", fill_sequence=0,
        )
        led.apply_fill(**kw)
        cash = led.cash
        led.apply_fill(**kw)
        assert led.cash == cash


# --------------------------------------------------------------------------
# ENGINE INTEGRATION (single-instrument, sin multi-orquestacion)
# --------------------------------------------------------------------------


class TestEngineEconomics:
    def _gold_engine(self):
        spec = InstrumentSpec(
            INSTRUMENT, asset_class=AssetClass.METAL, contract_size=100.0,
            quote_currency=USD, quantity_min=0.01, quantity_step=0.01,
            quantity_max=100.0,
        )
        return _stack(strategy=_LevelLong(level=LEVEL), instrument_spec=spec)

    def test_quantity_normalized_floor(self):
        env = self._gold_engine()
        _drive(env, HAPPY[:3])
        pos = env.engine.position
        assert pos is not None
        # target = 250/(100*100) = 0.025 -> floor step 0.01 => 0.02
        assert pos.quantity == pytest.approx(0.02)

    def test_projected_exposure_uses_contract_size(self):
        env = self._gold_engine()
        _drive(env, HAPPY[:3])
        qty = env.engine.position.quantity
        projected = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=0.0, execution_price=50_150.0
        )
        assert projected.exposure_by_instrument is not None
        assert projected.exposure_by_instrument[INSTRUMENT] == pytest.approx(
            qty * 50_150.0 * 100.0
        )

    def test_legacy_paper_unchanged_when_contract_size_one(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        # legacy: qty 2.5, pnl -250 (sin cambio por MULTI-ASSET-03)
        assert env.engine.trades[0].quantity == pytest.approx(2.5)
        assert env.engine.trades[0].pnl == pytest.approx(-250.0)

    def test_spec_instrument_mismatch_fails(self):
        spec = InstrumentSpec("OTRO", contract_size=1.0)
        with pytest.raises(InstrumentSpecError):
            _stack(strategy=_LevelLong(level=LEVEL), instrument_spec=spec)

    def test_point_value_contract_size_mismatch_fails(self):
        spec = InstrumentSpec(INSTRUMENT, contract_size=100.0)
        with pytest.raises(InstrumentSpecError):
            _build_engine(point_value=50.0, instrument_spec=spec)


def _build_engine(*, point_value: float, instrument_spec):
    from brokers.paper import PaperBrokerAdapter
    from execution.order_manager import OrderManager
    from killswitches.coordinator import KillSwitchCoordinator
    from monitoring.engine import MonitoringEngine
    from papertrading.engine import PaperTradingEngine
    from papertrading.models import PaperEngineConfig
    from reconciliation.engine import ReconciliationEngine
    from sessions.manager import MarketSessionManager
    from sessions.markets import btc_24_7_calendar

    broker = PaperBrokerAdapter(initial_cash=1_000_000.0)
    sessions = MarketSessionManager()
    sessions.register(INSTRUMENT, btc_24_7_calendar())
    return PaperTradingEngine(
        _LevelLong(level=LEVEL), OrderManager(), RiskEngine(RiskConfig()),
        broker, sessions, ReconciliationEngine(), KillSwitchCoordinator(),
        MonitoringEngine(), initial_cash=1_000_000.0,
        config=PaperEngineConfig(
            instrument=INSTRUMENT, account_id="PAPER-01",
            point_value=point_value, instrument_spec=instrument_spec,
        ),
    )


# --------------------------------------------------------------------------
# MT5 MAPPING / CROSS-VALIDATION
# --------------------------------------------------------------------------


def _mt5_spec(**over) -> MT5SymbolSpec:
    base = dict(
        strategy_instrument=INSTRUMENT, name="XAUUSD.a", visible=True,
        volume_min=0.01, volume_step=0.01, volume_max=100.0, point=0.01,
        trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100.0,
        currency_base="XAU", currency_profit="USD", currency_margin="USD",
        trade_mode=4, filling_mode=1, digits=2,
    )
    base.update(over)
    return MT5SymbolSpec(**base)


class TestMT5Mapping:
    def test_derive_contract_size_from_ticks(self):
        spec = instrument_spec_from_mt5(_mt5_spec(), atlas_instrument_id="XAUUSD")
        assert spec.contract_size == pytest.approx(100.0)  # 1.0/0.01
        assert spec.quote_currency == "USD"

    def test_explicit_contract_size_matching(self):
        spec = instrument_spec_from_mt5(
            _mt5_spec(), atlas_instrument_id="XAUUSD", contract_size=100.0
        )
        assert spec.contract_size == 100.0

    def test_explicit_contract_size_mismatch_fails(self):
        with pytest.raises(InstrumentSpecError):
            instrument_spec_from_mt5(
                _mt5_spec(), atlas_instrument_id="XAUUSD", contract_size=1.0
            )

    def test_quantity_constraints_preserved(self):
        spec = instrument_spec_from_mt5(
            _mt5_spec(volume_min=0.1, volume_step=0.1, volume_max=50.0),
            atlas_instrument_id="XAUUSD",
        )
        assert (spec.quantity_min, spec.quantity_step, spec.quantity_max) == (
            0.1, 0.1, 50.0
        )

    def test_quote_currency_mismatch_fails(self):
        with pytest.raises(InstrumentSpecError):
            instrument_spec_from_mt5(
                _mt5_spec(), atlas_instrument_id="XAUUSD", quote_currency="EUR"
            )


# --------------------------------------------------------------------------
# BACKTEST COMPATIBILITY / SAFETY
# --------------------------------------------------------------------------


class TestBacktestCompat:
    def test_point_value_one_unchanged(self):
        engine = RiskEngine(RiskConfig(risk_per_trade=0.00025))
        state = AccountRiskState(equity=1_000_000.0)
        d1 = engine.validate(_signal(100.0, 90.0), state, point_value=1.0)
        d2 = engine.validate(_signal(100.0, 90.0), state, contract_size=1.0)
        assert d1.approved_quantity == pytest.approx(d2.approved_quantity)


class TestSafetyNoFallback:
    def test_hedging_not_relevant_here(self):
        # Placeholder de intencion: sin fallback contract_size=1 silencioso en
        # live. El ledger exige FX cuando quote!=account y no la hay.
        led = AccountLedger(initial_cash=1_000.0, account_currency="USD")
        led.register_instrument(
            InstrumentSpec("X", contract_size=1.0, quote_currency="JPY")
        )
        with pytest.raises(LedgerError):
            led.apply_fill(
                instrument="X", side="BUY", quantity=1.0, price=150.0,
                client_order_id="C", fill_sequence=0,
            )

    def test_valuation_missing_fx_returns_none(self):
        led = AccountLedger(initial_cash=1_000.0, account_currency="USD")
        led.register_instrument(
            InstrumentSpec("X", contract_size=1.0, quote_currency="JPY")
        )
        led.apply_fill(
            instrument="X", side="BUY", quantity=1.0, price=150.0,
            client_order_id="C", fill_sequence=0, fx_rate=0.0067,
        )
        assert led.valuation({"X": 150.0}) is None
