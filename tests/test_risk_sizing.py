"""Tests del sizing fixed-fractional de RiskEngine (risk/engine.py)."""

from __future__ import annotations

import pytest

from risk.engine import RiskEngine
from risk.types import AccountRiskState, RiskConfig, RiskDecisionState
from strategies.base.signal import Action, Signal


def _signal(
    *,
    action: Action = Action.LONG,
    reference_price: float | None = 100.0,
    stop_price: float | None = 95.0,
) -> Signal:
    return Signal(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        instrument="NQ",
        action=action,
        reference_price=reference_price,
        stop_price=stop_price,
        target_quantity=None,
    )


def _state(**overrides) -> AccountRiskState:
    defaults = dict(
        equity=100_000,
        open_positions=0,
        current_daily_pnl=0.0,
        current_drawdown=0.0,
        strategy_active=True,
        account_active=True,
        kill_switch=False,
    )
    defaults.update(overrides)
    return AccountRiskState(**defaults)


@pytest.fixture()
def engine() -> RiskEngine:
    return RiskEngine(config=RiskConfig(risk_per_trade=0.01))


class TestSizingFixedFractional:
    def test_quantity_equals_cap_over_unit_risk(self, engine: RiskEngine) -> None:
        """cap = equity*risk_per_trade = 1000; distancia 5 → qty 200."""
        decision = engine.validate(_signal(), _state(), point_value=1.0)
        assert decision.decision == RiskDecisionState.APPROVED
        assert abs(decision.approved_quantity - 200.0) < 1e-9

    def test_point_value_scales_quantity(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(), _state(), point_value=0.5)
        assert abs(decision.approved_quantity - 400.0) < 1e-9

    def test_quantity_matches_risk_config(self, engine: RiskEngine) -> None:
        """La ecuación completa: qty * distancia * punto = capital_riesgo."""
        decision = engine.validate(_signal(), _state(), point_value=1.0)
        q = decision.approved_quantity
        distance = abs(_signal().reference_price - _signal().stop_price)
        assert abs(q * distance * 1.0 - 1000.0) < 1e-9

    def test_stop_without_distance_rejected(self, engine: RiskEngine) -> None:
        decision = engine.validate(
            _signal(reference_price=100.0, stop_price=100.0), _state(), point_value=1.0
        )
        assert decision.decision == RiskDecisionState.REJECTED
        assert "distancia" in decision.reason.lower()

    def test_missing_stop_rejected_in_sizing_mode(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(stop_price=None), _state(), point_value=1.0)
        assert decision.decision == RiskDecisionState.REJECTED

    def test_negative_point_value_rejected(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(), _state(), point_value=-1.0)
        assert decision.decision == RiskDecisionState.REJECTED

    def test_no_estimator_no_point_value_rejected(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(), _state())
        assert decision.decision == RiskDecisionState.REJECTED

    def test_daily_loss_veto_keeps_applying(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(), _state(current_daily_pnl=-1500.0), point_value=1.0)
        assert decision.decision == RiskDecisionState.REJECTED
        assert "diaria" in decision.reason.lower()

    def test_approved_checks_all_true(self, engine: RiskEngine) -> None:
        decision = engine.validate(_signal(), _state(), point_value=1.0)
        assert all(v is True for v in decision.checks.values())


class TestMaxStrategyRiskContract:
    """F-02: `max_strategy_risk` = max riesgo por operación / equity.

    Contrato: `risk_per_trade <= max_strategy_risk`. En sizing, una config
    coherente usa el sizing existente; una incoherente se rechaza.
    """

    def _engine(self, rpt: float, msr: float) -> RiskEngine:
        return RiskEngine(config=RiskConfig(risk_per_trade=rpt, max_strategy_risk=msr))

    def test_below_ceiling_approved_with_sizing(self) -> None:
        engine = self._engine(rpt=0.01, msr=0.02)
        d = engine.validate(_signal(), _state(), point_value=1.0)
        assert d.decision == RiskDecisionState.APPROVED
        # sizing intacto: cap=1000, distancia 5, pv 1 -> qty 200
        assert abs(d.approved_quantity - 200.0) < 1e-9
        assert d.checks["max_strategy_risk"] is True

    def test_at_ceiling_approved(self) -> None:
        engine = self._engine(rpt=0.01, msr=0.01)
        d = engine.validate(_signal(), _state(), point_value=1.0)
        assert d.decision == RiskDecisionState.APPROVED
        assert abs(d.approved_quantity - 200.0) < 1e-9

    def test_above_ceiling_rejected(self) -> None:
        engine = self._engine(rpt=0.01, msr=0.005)
        d = engine.validate(_signal(), _state(), point_value=1.0)
        assert d.decision == RiskDecisionState.REJECTED
        assert d.checks["max_strategy_risk"] is False
        assert d.approved_quantity == 0

    def test_bug_reproduction_point_value_now_honours_ceiling(self) -> None:
        """Antes el modo point_value ignoraba max_strategy_risk."""
        engine = self._engine(rpt=0.01, msr=0.005)
        d = engine.validate(_signal(), _state(), point_value=1.0)
        # Antes: APPROVED qty=200 ignorando msr. Ahora: REJECTED.
        assert d.decision == RiskDecisionState.REJECTED

    def test_units_numeric_risk_equals_equity_times_rpt(self) -> None:
        engine = self._engine(rpt=0.01, msr=0.02)
        d = engine.validate(_signal(), _state(), point_value=2.0)
        dist = abs(_signal().reference_price - _signal().stop_price)
        assert abs(d.approved_quantity * dist * 2.0 - 1000.0) < 1e-9
        assert d.checks["max_strategy_risk"] is True

    def test_rejected_order_does_not_approve(self) -> None:
        engine = self._engine(rpt=0.05, msr=0.01)
        d = engine.validate(_signal(), _state(), point_value=1.0)
        assert not d.approved
        assert d.approved_quantity == 0


class TestEstimatorMaxStrategyRiskContract:
    """F-02: en estimator el límite es `max_strategy_risk * equity`."""

    def _engine(self, msr: float, estimator) -> RiskEngine:
        return RiskEngine(
            config=RiskConfig(risk_per_trade=0.01, max_strategy_risk=msr),
            trade_risk_estimator=estimator,
        )

    def test_estimator_below_ceiling_approved(self) -> None:
        d = self._engine(msr=0.02, estimator=lambda s: 100.0).validate(
            _signal(), _state()
        )
        assert d.decision == RiskDecisionState.APPROVED  # 100 <= 0.02*100000=2000

    def test_estimator_exactly_at_ceiling_approved(self) -> None:
        d = self._engine(msr=0.001, estimator=lambda s: 100.0).validate(
            _signal(), _state()
        )
        # 100 <= 0.001*100000 = 100
        assert d.decision == RiskDecisionState.APPROVED
        assert d.checks["risk_per_trade"] is True

    def test_estimator_above_ceiling_rejected(self) -> None:
        d = self._engine(msr=0.0005, estimator=lambda s: 100.0).validate(
            _signal(), _state()
        )
        # 100 > 0.0005*100000 = 50
        assert d.decision == RiskDecisionState.REJECTED
        assert d.checks["risk_per_trade"] is False