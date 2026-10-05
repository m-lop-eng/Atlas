"""Tests de F-03 (paper): gates de exposicion PROJECTED (enforcement pre-fill).

Contrato congelado:
  * El control material de F-03 es el estado PROJECTED (post-trade), evaluado
    inmediatamente antes del fill con el precio de ejecucion.
  * gross = Σ|q·mark|; net = Σ q·mark (signed); leverage = gross/equity;
    concentracion = max_i(|notional_i|/equity).
  * Limite None = gate deshabilitado; metrica None con gate activo = fail-closed;
    `0.0` = cero real (nunca "sin dato").
  * Fuente: AccountLedger (autoritativa). NUNCA portfolio.compute_portfolio_risk.
"""

from __future__ import annotations

import pytest

from risk.engine import RiskEngine
from risk.types import ProjectedExposure, RiskConfig, RiskDecisionState

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    _LevelLong,
    _drive,
    _stack,
)


def _pe(**overrides) -> ProjectedExposure:
    defaults = dict(
        equity=100_000.0,
        gross_exposure=None,
        net_exposure=None,
        exposure_by_instrument=None,
    )
    defaults.update(overrides)
    return ProjectedExposure(**defaults)


def _gate(config: RiskConfig, projected: ProjectedExposure):
    return RiskEngine(config=config).validate_projected_exposure(projected)


# --------------------------------------------------------------------------
# Gross
# --------------------------------------------------------------------------


class TestGrossGate:
    def test_below_limit_approved(self):
        d = _gate(RiskConfig(max_gross_exposure=2000.0),
                  _pe(gross_exposure=1000.0))
        assert d.decision is RiskDecisionState.APPROVED
        assert d.checks["max_gross_exposure"] is True

    def test_exactly_at_limit_approved(self):
        d = _gate(RiskConfig(max_gross_exposure=2000.0),
                  _pe(gross_exposure=2000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_above_limit_rejected(self):
        d = _gate(RiskConfig(max_gross_exposure=2000.0),
                  _pe(gross_exposure=2000.01))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_gross_exposure"] is False

    def test_disabled_none_does_not_evaluate(self):
        d = _gate(RiskConfig(max_gross_exposure=None),
                  _pe(gross_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_gross_exposure" not in d.checks

    def test_metric_none_with_active_gate_fails_closed(self):
        d = _gate(RiskConfig(max_gross_exposure=2000.0),
                  _pe(gross_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_gross_exposure"] is False


# --------------------------------------------------------------------------
# Net
# --------------------------------------------------------------------------


class TestNetGate:
    def test_positive_within_approved(self):
        d = _gate(RiskConfig(max_net_exposure=2000.0),
                  _pe(net_exposure=1000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_negative_within_approved(self):
        d = _gate(RiskConfig(max_net_exposure=2000.0),
                  _pe(net_exposure=-1500.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_negative_outside_rejected(self):
        d = _gate(RiskConfig(max_net_exposure=2000.0),
                  _pe(net_exposure=-2500.0))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_net_exposure"] is False

    def test_uses_signed_net_magnitude(self):
        # net signed +3000 -> abs 3000 > 2000 => reject.
        d = _gate(RiskConfig(max_net_exposure=2000.0),
                  _pe(net_exposure=3000.0))
        assert d.decision is RiskDecisionState.REJECTED

    def test_disabled_none_does_not_evaluate(self):
        d = _gate(RiskConfig(max_net_exposure=None), _pe(net_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_net_exposure" not in d.checks

    def test_metric_none_with_active_gate_fails_closed(self):
        d = _gate(RiskConfig(max_net_exposure=2000.0), _pe(net_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED


# --------------------------------------------------------------------------
# Leverage
# --------------------------------------------------------------------------


class TestLeverageGate:
    def test_within_approved(self):
        d = _gate(RiskConfig(max_leverage=3.0),
                  _pe(equity=100_000.0, gross_exposure=200_000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_exactly_at_limit_approved(self):
        d = _gate(RiskConfig(max_leverage=3.0),
                  _pe(equity=100_000.0, gross_exposure=300_000.0))
        assert d.decision is RiskDecisionState.APPROVED

    def test_outside_rejected(self):
        d = _gate(RiskConfig(max_leverage=3.0),
                  _pe(equity=100_000.0, gross_exposure=300_001.0))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_leverage"] is False

    def test_equity_non_positive_fails_closed(self):
        d = _gate(RiskConfig(max_leverage=3.0),
                  _pe(equity=0.0, gross_exposure=100.0))
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_leverage"] is False

    def test_disabled_none_approved(self):
        d = _gate(RiskConfig(max_leverage=None),
                  _pe(equity=0.0, gross_exposure=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_leverage" not in d.checks

    def test_gross_unavailable_with_active_gate_fails_closed(self):
        d = _gate(RiskConfig(max_leverage=3.0),
                  _pe(equity=100_000.0, gross_exposure=None))
        assert d.decision is RiskDecisionState.REJECTED


# --------------------------------------------------------------------------
# Concentration (instrumento)
# --------------------------------------------------------------------------


class TestConcentrationGate:
    def test_single_position_within_approved(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=100_000.0,
                      exposure_by_instrument={"A": 10_000.0}))
        assert d.decision is RiskDecisionState.APPROVED

    def test_single_position_outside_rejected(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=100_000.0,
                      exposure_by_instrument={"A": 30_000.0}))
        assert d.decision is RiskDecisionState.REJECTED

    def test_multiple_positions_uses_max_instrument(self):
        d = _gate(
            RiskConfig(max_instrument_concentration=0.2),
            _pe(equity=100_000.0,
                exposure_by_instrument={"A": 10_000.0, "B": 25_000.0}),
        )
        assert d.decision is RiskDecisionState.REJECTED
        assert d.checks["max_instrument_concentration"] is False

    def test_short_uses_absolute_magnitude(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=100_000.0,
                      exposure_by_instrument={"A": -30_000.0}))
        assert d.decision is RiskDecisionState.REJECTED

    def test_no_positions_is_real_zero_approved(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=100_000.0, exposure_by_instrument={}))
        assert d.decision is RiskDecisionState.APPROVED

    def test_equity_non_positive_fails_closed(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=0.0,
                      exposure_by_instrument={"A": 1.0}))
        assert d.decision is RiskDecisionState.REJECTED

    def test_disabled_none_approved(self):
        d = _gate(RiskConfig(max_instrument_concentration=None),
                  _pe(equity=100_000.0, exposure_by_instrument=None))
        assert d.decision is RiskDecisionState.APPROVED
        assert "max_instrument_concentration" not in d.checks

    def test_data_unavailable_with_active_gate_fails_closed(self):
        d = _gate(RiskConfig(max_instrument_concentration=0.2),
                  _pe(equity=100_000.0, exposure_by_instrument=None))
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
# validate() ya NO aplica los gates F-03 sobre el estado actual
# --------------------------------------------------------------------------


class TestValidateNoLongerGatesCurrentState:
    def test_validate_ignores_exposure_limits(self):
        from strategies.base.signal import Action, Signal

        signal = Signal(
            strategy_id="s", strategy_version="1.0.0", instrument="NQ",
            action=Action.LONG, reference_price=100.0, stop_price=95.0,
        )
        from risk.types import AccountRiskState

        state = AccountRiskState(equity=100_000.0)
        engine = RiskEngine(RiskConfig(max_gross_exposure=1.0,
                                       max_leverage=0.0001))
        # validate (no proyectado) no veta por exposicion; el control material
        # es validate_projected_exposure.
        decision = engine.validate(signal, state, point_value=1.0)
        assert decision.decision is RiskDecisionState.APPROVED
        assert "max_gross_exposure" not in decision.checks


# --------------------------------------------------------------------------
# Integracion paper: projected state desde ledger + enforcement pre-fill
# --------------------------------------------------------------------------


class TestPaperProjectedIntegration:
    def test_projected_from_ledger_flat_plus_order(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        proj = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=2.5, execution_price=50_150.0
        )
        # Cuenta flat + orden: solo el instrumento propuesto.
        assert proj.exposure_by_instrument == {INSTRUMENT: 125_375.0}
        assert proj.gross_exposure == pytest.approx(125_375.0)
        assert proj.net_exposure == pytest.approx(125_375.0)
        assert proj.equity == pytest.approx(1_000_000.0)

    def test_projected_reversal_uses_signed_quantity(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        # Sembrar posicion +10 en el ledger (autoridad).
        env.engine.ledger.apply_fill(
            instrument=INSTRUMENT, side="BUY", quantity=10.0, price=100.0,
            client_order_id="seed", fill_sequence=0,
        )
        proj = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=-15.0, execution_price=100.0
        )
        # +10 + (-15) = -5 (nunca suma de magnitudes).
        assert proj.exposure_by_instrument == {INSTRUMENT: -500.0}
        assert proj.gross_exposure == pytest.approx(500.0)
        assert proj.net_exposure == pytest.approx(-500.0)

    def test_projected_unavailable_when_execution_price_invalid(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        proj = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=2.5, execution_price=0.0
        )
        assert proj.gross_exposure is None
        assert proj.net_exposure is None
        assert proj.exposure_by_instrument is None

    def test_mutating_broker_does_not_change_projected(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        before = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=2.5, execution_price=50_150.0
        )
        env.broker._cash += 12_345.0  # solo observed
        after = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=2.5, execution_price=50_150.0
        )
        assert after == before

    def test_does_not_use_portfolio_compute_portfolio_risk(self, monkeypatch):
        import portfolio.engine as pf

        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("F-03 no debe usar compute_portfolio_risk()")

        monkeypatch.setattr(pf, "compute_portfolio_risk", _boom)
        env = _stack(strategy=_LevelLong(level=LEVEL))
        proj = env.engine._projected_exposure(
            instrument=INSTRUMENT, signed_qty=2.5, execution_price=50_150.0
        )
        assert proj.gross_exposure is not None

    # ---- ejemplo obligatorio de la auditoria ----

    def test_projected_gross_exceeded_rejects_before_fill(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.risk_engine = RiskEngine(
            RiskConfig(risk_per_trade=0.00025, max_gross_exposure=100_000.0)
        )
        _drive(env, HAPPY[:3])  # B2 señal LONG (qty 2.5); B3 ejecuta @ open 50150
        # projected gross = 2.5 * 50150 = 125_375 > 100_000 -> REJECT pre-fill.
        assert env.engine.position is None
        assert env.broker._orders == {}
        assert env.engine.ledger.position(INSTRUMENT) is None
        events = [e.event for e in env.engine.audit_log]
        assert "RISK_REJECTED" in events

    def test_projected_gross_within_limit_approves_and_fills(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.risk_engine = RiskEngine(
            RiskConfig(risk_per_trade=0.00025, max_gross_exposure=200_000.0)
        )
        _drive(env, HAPPY[:3])
        assert env.engine.position is not None
        assert len(env.broker._orders) == 1
        assert env.engine.ledger.position(INSTRUMENT) is not None

    def test_projected_leverage_rejects_before_fill(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.risk_engine = RiskEngine(
            RiskConfig(risk_per_trade=0.00025, max_leverage=0.1)
        )
        _drive(env, HAPPY[:3])
        # leverage proyectado = 125_375 / 1_000_000 = 0.125375 > 0.1 -> reject.
        assert env.engine.position is None
        assert env.broker._orders == {}

    def test_projected_concentration_new_instrument_rejects(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.risk_engine = RiskEngine(
            RiskConfig(risk_per_trade=0.00025,
                       max_instrument_concentration=0.1)
        )
        _drive(env, HAPPY[:3])
        # concentracion = 125_375 / 1_000_000 = 0.125375 > 0.1 -> reject.
        assert env.engine.position is None
        assert env.broker._orders == {}
