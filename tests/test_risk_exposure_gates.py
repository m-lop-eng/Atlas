"""Tests de F-03 fase 1 (paper): gates de exposicion/leverage/concentracion.

Contrato congelado:
  * gross = Σ|q·mark|; net = Σ q·mark (signed); leverage = gross/equity;
    concentracion por instrumento = max_i(|exposure_i|/equity).
  * Limite None = gate deshabilitado; metrica None con gate activo = fail-closed;
    `0.0` = cero real (nunca "sin dato").
  * Fuente autoritativa: AccountLedger.valuation(). NUNCA
    portfolio.compute_portfolio_risk().
"""

from __future__ import annotations

import pytest

from risk.engine import RiskEngine
from risk.position_sizing import PositionSizingError
from risk.types import AccountRiskState, RiskConfig, RiskDecisionState
from strategies.base.signal import Action, Signal

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    _LevelLong,
    _drive,
    _stack,
)


def _signal() -> Signal:
    return Signal(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        instrument="NQ",
        action=Action.LONG,
        reference_price=100.0,
        stop_price=95.0,
        target_quantity=None,
    )


def _state(**overrides) -> AccountRiskState:
    defaults = dict(
        equity=100_000.0,
        open_positions=0,
        current_daily_pnl=0.0,
        current_drawdown=0.0,
        strategy_active=True,
        account_active=True,
        kill_switch=False,
    )
    defaults.update(overrides)
    return AccountRiskState(**defaults)


def _validate(config: RiskConfig, state: AccountRiskState):
    return RiskEngine(config=config).validate(_signal(), state, point_value=1.0)


# --------------------------------------------------------------------------
# Gross
# --------------------------------------------------------------------------


class TestGrossGate:
    def test_below_limit_approved(self):
        d = _validate(RiskConfig(max_gross_exposure=2000.0),
                      _state(gross_exposure=1000.0))
        assert d.decision is RiskDecisionState.APPROVED
        assert d.checks["max_gross_exposure"] is True

    def test_exactly_at_limit_approved(self):
        d = _validate(RiskConfig(max_gross_exposure=2000.0),
                      _state(gross_exposure=2000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_above_limit_rejected(self):
        d = _validate(RiskConfig(max_gross_exposure=2000.0),
                      _state(gross_exposure=2000.01))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_gross_exposure"] is False

    def test_disabled_none_does_not_evaluate(self):
        d = _validate(RiskConfig(max_gross_exposure=None),
                      _state(gross_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_gross_exposure" not in d.checks

    def test_metric_none_with_active_gate_fails_closed(self):
        d = _validate(RiskConfig(max_gross_exposure=2000.0),
                      _state(gross_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_gross_exposure"] is False


# --------------------------------------------------------------------------
# Net
# --------------------------------------------------------------------------


class TestNetGate:
    def test_positive_within_approved(self):
        d = _validate(RiskConfig(max_net_exposure=2000.0),
                      _state(net_exposure=1000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_negative_within_approved(self):
        d = _validate(RiskConfig(max_net_exposure=2000.0),
                      _state(net_exposure=-1500.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_negative_outside_rejected(self):
        d = _validate(RiskConfig(max_net_exposure=2000.0),
                      _state(net_exposure=-2500.0))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_net_exposure"] is False

    def test_uses_signed_net_not_portfolio_abs_long_short(self):
        # net = +3000 (una sola direccion). El gate usa abs(net)=3000.
        # Si usara abs(long-short) con long=3000, short=0 daria 3000 igual;
        # para distinguir, net=+3000 con magnitud 3000 > 2000 => rechaza.
        d = _validate(RiskConfig(max_net_exposure=2000.0),
                      _state(net_exposure=3000.0))
        assert d.decision is RiskDecisionState.REJECTED

    def test_disabled_none_does_not_evaluate(self):
        d = _validate(RiskConfig(max_net_exposure=None),
                      _state(net_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_net_exposure" not in d.checks

    def test_metric_none_with_active_gate_fails_closed(self):
        d = _validate(RiskConfig(max_net_exposure=2000.0),
                      _state(net_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED


# --------------------------------------------------------------------------
# Leverage
# --------------------------------------------------------------------------


class TestLeverageGate:
    def test_within_approved(self):
        d = _validate(RiskConfig(max_leverage=3.0),
                      _state(equity=100_000.0, gross_exposure=200_000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_exactly_at_limit_approved(self):
        d = _validate(RiskConfig(max_leverage=3.0),
                      _state(equity=100_000.0, gross_exposure=300_000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_outside_rejected(self):
        d = _validate(RiskConfig(max_leverage=3.0),
                      _state(equity=100_000.0, gross_exposure=300_001.0))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_leverage"] is False

    def test_equity_non_positive_fails_closed(self):
        # El engine ya es fail-safe ante equity<=0 (PositionSizingError), antes
        # de cualquier gate; nunca aprueba con equity no positiva.
        with pytest.raises(PositionSizingError):
            _validate(RiskConfig(max_leverage=3.0),
                      _state(equity=0.0, gross_exposure=100.0))

    def test_disabled_none_approved(self):
        d = _validate(RiskConfig(max_leverage=None),
                      _state(equity=100_000.0, gross_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_leverage" not in d.checks

    def test_gross_unavailable_with_active_gate_fails_closed(self):
        d = _validate(RiskConfig(max_leverage=3.0),
                      _state(equity=100_000.0, gross_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED


# --------------------------------------------------------------------------
# Concentration (instrumento)
# --------------------------------------------------------------------------


class TestConcentrationGate:
    def test_single_position_within_approved(self):
        d = _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=100_000.0,
                             exposure_by_instrument={"A": 10_000.0}))
        assert d.decision is RiskDecisionState.APPROVED

    def test_single_position_outside_rejected(self):
        d = _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=100_000.0,
                             exposure_by_instrument={"A": 30_000.0}))
        assert d.decision is RiskDecisionState.REJECTED

    def test_multiple_positions_uses_max_instrument(self):
        d = _validate(
            RiskConfig(max_instrument_concentration=0.2),
            _state(equity=100_000.0,
                   exposure_by_instrument={"A": 10_000.0, "B": 25_000.0}),
        )
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_instrument_concentration"] is False

    def test_short_uses_absolute_magnitude(self):
        d = _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=100_000.0,
                             exposure_by_instrument={"A": -30_000.0}))
        assert d.decision is RiskDecisionState.REJECTED

    def test_no_positions_is_real_zero_approved(self):
        d = _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=100_000.0, exposure_by_instrument={}))
        assert d.decision is RiskDecisionState.APPROVED

    def test_equity_non_positive_fails_closed(self):
        # Fail-safe existente del engine ante equity<=0 (nunca aprueba).
        with pytest.raises(PositionSizingError):
            _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=0.0,
                             exposure_by_instrument={"A": 1.0}))

    def test_disabled_none_approved(self):
        d = _validate(RiskConfig(max_instrument_concentration=None),
                      _state(equity=100_000.0, exposure_by_instrument=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_instrument_concentration" not in d.checks

    def test_data_unavailable_with_active_gate_fails_closed(self):
        d = _validate(RiskConfig(max_instrument_concentration=0.2),
                      _state(equity=100_000.0, exposure_by_instrument=None))
        assert d.decision is RiskDecisionState.REJECTED


# --------------------------------------------------------------------------
# Config: validacion y None
# --------------------------------------------------------------------------


class TestConfigLimits:
    def test_defaults_are_none_disabled(self):
        cfg = RiskConfig()
        assert cfg.max_gross_exposure is None
        assert cfg.max_net_exposure is None
        assert cfg.max_leverage is None
        assert cfg.max_instrument_concentration is None

    def test_zero_is_not_disabled_rejected(self):
        for name in (
            "max_gross_exposure",
            "max_net_exposure",
            "max_leverage",
            "max_instrument_concentration",
        ):
            with pytest.raises(ValueError):
                RiskConfig(**{name: 0.0})

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            RiskConfig(max_leverage=-1.0)


# --------------------------------------------------------------------------
# Integracion paper: fuente autoritativa = ledger, no broker/portfolio
# --------------------------------------------------------------------------


class TestPaperRiskStateIntegration:
    def test_risk_state_metrics_come_from_ledger(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # posicion abierta
        assert env.engine.position is not None
        val = env.engine.ledger.valuation(env.engine._marks())
        state = env.engine._risk_state()
        assert state.gross_exposure == pytest.approx(val.gross_exposure)
        assert state.net_exposure == pytest.approx(val.net_exposure)
        assert state.exposure_by_instrument == val.exposure_by_instrument
        assert state.equity == pytest.approx(val.equity)

    def test_mutating_broker_does_not_change_expected_risk_state(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        before = env.engine._risk_state()
        env.broker._cash += 12_345.0  # solo observed
        after = env.engine._risk_state()
        assert after.gross_exposure == before.gross_exposure
        assert after.net_exposure == before.net_exposure
        assert after.equity == before.equity

    def test_does_not_use_portfolio_compute_portfolio_risk(self, monkeypatch):
        import portfolio.engine as pf

        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError(
                "F-03 no debe usar portfolio.compute_portfolio_risk()"
            )

        monkeypatch.setattr(pf, "compute_portfolio_risk", _boom)
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        state = env.engine._risk_state()  # no debe explotar
        assert state.gross_exposure is not None

    def test_incomplete_valuation_yields_none_metrics(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # posicion abierta
        env.engine._set_price(0.0)  # mark invalido -> valuation None
        state = env.engine._risk_state()
        assert state.gross_exposure is None
        assert state.net_exposure is None
        assert state.exposure_by_instrument is None

    def test_gate_rejects_when_paper_exposure_exceeds_limit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # posicion abierta -> gross > 0
        state = env.engine._risk_state()
        assert state.gross_exposure and state.gross_exposure > 0
        # Limite por debajo de la exposicion autoritativa real del ledger.
        engine = RiskEngine(
            config=RiskConfig(risk_per_trade=0.00025,
                              max_gross_exposure=state.gross_exposure - 1.0)
        )
        decision = engine.validate(_signal(), state, point_value=1.0)
        assert decision.decision is RiskDecisionState.REJECTED
        assert decision.checks["max_gross_exposure"] is False

    def test_gate_approves_when_paper_exposure_within_limit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        state = env.engine._risk_state()
        engine = RiskEngine(
            config=RiskConfig(risk_per_trade=0.00025,
                              max_gross_exposure=state.gross_exposure + 1.0)
        )
        decision = engine.validate(_signal(), state, point_value=1.0)
        assert decision.decision is RiskDecisionState.APPROVED
