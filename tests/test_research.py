"""Tests del pipeline de investigación (research/)."""

import pytest

from research.experiment import (
    ExperimentRun,
    ExperimentStateError,
    ExperimentStatus,
    make_config_hash,
    run_status_transition,
)


class TestConfigHash:
    def test_stable_across_key_order(self) -> None:
        h1 = make_config_hash({"a": 1, "b": 2})
        h2 = make_config_hash({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 64

    def test_different_config_different_hash(self) -> None:
        assert make_config_hash({"a": 1}) != make_config_hash({"a": 2})


class TestTransitions:
    def test_valid_flow(self) -> None:
        run_status_transition(ExperimentStatus.DRAFT, ExperimentStatus.RUNNING)
        run_status_transition(ExperimentStatus.RUNNING, ExperimentStatus.COMPLETED)

    def test_invalid_transition_raises(self) -> None:
        with pytest.raises(ExperimentStateError, match="inválida"):
            run_status_transition(
                ExperimentStatus.DRAFT, ExperimentStatus.COMPLETED
            )


class TestExperimentRun:
    def test_requires_fields(self) -> None:
        with pytest.raises(ValueError, match="obligatorios"):
            ExperimentRun(
                strategy_id="s1",
                config_hash="",
                dataset_hash="d1",
            )

    def test_transition_method(self) -> None:
        run = ExperimentRun(
            strategy_id="s1", config_hash="c1", dataset_hash="d1"
        )
        run.transition(ExperimentStatus.RUNNING)
        assert run.status == ExperimentStatus.RUNNING

    def test_invalid_transition_method_raises(self) -> None:
        run = ExperimentRun(
            strategy_id="s1", config_hash="c1", dataset_hash="d1"
        )
        with pytest.raises(ExperimentStateError):
            run.transition(ExperimentStatus.COMPLETED)