"""Tests de RiskEngine (risk/engine.py)."""

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
    target_quantity: int | float = 1,
) -> Signal:
    return Signal(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        instrument="NQ",
        action=action,
        reference_price=reference_price,
        stop_price=stop_price,
        target_quantity=target_quantity,
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


class TestRiskEngineApproved:
    def test_estimator_over_cap_rejected(self) -> None:
        """Estimator sobre el límite (100 > 0.01*250=2.5) → REJECTED."""
        engine = RiskEngine(
            config=RiskConfig(),
            trade_risk_estimator=lambda s: 100.0,
        )
        decision = engine.validate(_signal(), _state())
        assert decision.decision == RiskDecisionState.REJECTED
        assert decision.checks["risk_per_trade"] is False

    def test_approved_estimator_under_cap(self) -> None:
        """Estimator que respeta el límite → APPROVED."""
        engine = RiskEngine(
            config=RiskConfig(risk_per_trade=0.01, max_strategy_risk=0.9),
            trade_risk_estimator=lambda s: 100.0,
        )
        decision = engine.validate(_signal(), _state())
        assert decision.decision == RiskDecisionState.APPROVED
        assert decision.approved
        assert decision.approved_quantity == 1


class TestRiskEngineRejected:
    def test_no_trade_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(action=Action.NO_TRADE), _state())
        assert decision.decision == RiskDecisionState.REJECTED
        assert not decision.approved
        assert "NO_TRADE" in decision.reason

    def test_kill_switch_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(), _state(kill_switch=True))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "kill switch" in decision.reason.lower()

    def test_strategy_inactive_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(), _state(strategy_active=False))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "estrategia" in decision.reason.lower()

    def test_account_inactive_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(), _state(account_active=False))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "cuenta" in decision.reason.lower()

    def test_daily_loss_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(), _state(current_daily_pnl=-1500.0))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "diaria" in decision.reason.lower()

    def test_drawdown_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(), _state(current_drawdown=0.06))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "drawdown" in decision.reason.lower()

    def test_positions_full_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig(max_open_positions=3))
        decision = engine.validate(_signal(), _state(open_positions=3))
        assert decision.decision == RiskDecisionState.REJECTED
        assert "máximo" in decision.reason.lower()

    def test_missing_stop_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(stop_price=None), _state())
        assert decision.decision == RiskDecisionState.REJECTED
        assert "cuantificable" in decision.reason.lower()

    def test_missing_reference_price_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(reference_price=None), _state())
        assert decision.decision == RiskDecisionState.REJECTED

    def test_no_estimator_rejected(self) -> None:
        engine = RiskEngine(config=RiskConfig(), trade_risk_estimator=None)
        decision = engine.validate(_signal(), _state())
        assert decision.decision == RiskDecisionState.REJECTED
        assert "estimator" in decision.reason.lower()

    def test_risk_per_trade_exceeded(self) -> None:
        engine = RiskEngine(
            config=RiskConfig(risk_per_trade=0.0025, max_strategy_risk=0.01),
            trade_risk_estimator=lambda s: 100.0,
        )
        decision = engine.validate(_signal(), _state())
        assert decision.decision == RiskDecisionState.REJECTED
        assert "risk_per_trade" in decision.checks

    def test_check_names_present(self) -> None:
        engine = RiskEngine(config=RiskConfig(), trade_risk_estimator=lambda s: 1.0)
        decision = engine.validate(_signal(), _state())
        for key in (
            "strategy_active", "account_active", "kill_switch",
            "daily_loss", "drawdown", "positions_slot",
        ):
            assert key in decision.checks


class TestRiskEngineChecksMap:
    def test_all_checks_true_when_approved(self) -> None:
        engine = RiskEngine(
            config=RiskConfig(risk_per_trade=0.01, max_strategy_risk=0.9),
            trade_risk_estimator=lambda s: 1.0,
        )
        decision = engine.validate(_signal(), _state())
        assert all(v is True for v in decision.checks.values())

    def test_rejected_has_false_checks(self) -> None:
        engine = RiskEngine(config=RiskConfig())
        decision = engine.validate(_signal(action=Action.NO_TRADE), _state())
        assert any(v is False for v in decision.checks.values())