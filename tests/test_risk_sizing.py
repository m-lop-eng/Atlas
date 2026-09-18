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