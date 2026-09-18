"""Tests del embudo de validación (validation/)."""

import pytest

from validation.registry import (
    StrategyStage,
    ValidationError,
    is_gate_passed,
)


class TestGate:
    def test_all_requirements_pass(self) -> None:
        result = is_gate_passed(
            StrategyStage.BACKTEST,
            {"backtest_score": True, "costs_excluded": True},
        )
        assert result.passed
        assert result.summary == "PASS"

    def test_any_failure_fails_gate(self) -> None:
        result = is_gate_passed(
            StrategyStage.PAPER,
            {"paper_equity": True, "max_drawdown_ok": False},
        )
        assert not result.passed
        assert "max_drawdown_ok" in result.summary

    def test_empty_requirements_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requisitos"):
            is_gate_passed(StrategyStage.PROTOTYPE, {})

    def test_stage_values(self) -> None:
        assert StrategyStage.PROTOTYPE.value == "prototype"
        assert StrategyStage.PRODUCTION.value == "production"