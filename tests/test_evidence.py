"""Tests del framework EVIDENCE (H005): estados de evidencia por hipotesis,
ciclo de vida del FINAL_OOS (BLOCKED_BY_DATA_AVAILABILITY) y guard combinado
con DATA_ROLE para impedir reutilizacion de tramos consumidos."""

from __future__ import annotations

import pytest

from research.data_role import DataRole, DataRoleError, RoleRegistry
from research.evidence import (
    DEFAULT_EVIDENCE_PATH,
    ALLOWED_TRANSITIONS,
    EvidenceError,
    EvidenceRegistry,
    EvidenceState,
    FinalOosStatus,
)

DEV_START, DEV_END = 1_546_000_000, 1_727_000_000
OBS_START, OBS_END = 1_727_000_000, 1_788_000_000
NEW_START, NEW_END = 1_788_000_000, 1_800_000_000


def _roles() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register("development", DEV_START, DEV_END, DataRole.DEVELOPMENT)
    reg.register("observed", OBS_START, OBS_END, DataRole.OBSERVED)
    return reg


def _blocked_evidence() -> EvidenceRegistry:
    reg = EvidenceRegistry()
    reg.dataset.observed_through = OBS_END
    reg.block_final_oos(NEW_START, note="dump 2026-09 no publicado")
    return reg


def test_observed_through_derived_from_roles():
    roles = _roles()
    ev = EvidenceRegistry.from_roles(roles)
    assert ev.dataset.observed_through == OBS_END


def test_default_hypothesis_state():
    ev = EvidenceRegistry()
    assert ev.state("H999") is EvidenceState.HYPOTHESIS


def test_valid_transition():
    ev = EvidenceRegistry()
    ev.register("H1", state=EvidenceState.RESEARCH_REQUIRED)
    ev.transition("H1", EvidenceState.ROBUSTNESS_SUPPORTED)
    assert ev.state("H1") is EvidenceState.ROBUSTNESS_SUPPORTED


def test_final_oos_full_chain():
    ev = EvidenceRegistry()
    ev.register("H1", state=EvidenceState.ROBUSTNESS_SUPPORTED)
    ev.transition("H1", EvidenceState.FINAL_OOS_PENDING)
    ev.transition("H1", EvidenceState.FINAL_OOS_PASSED)
    assert ev.state("H1") is EvidenceState.FINAL_OOS_PASSED
    ev.transition("H1", EvidenceState.PAPER)
    assert ev.state("H1") is EvidenceState.PAPER


def test_invalid_transition_raises():
    ev = EvidenceRegistry()
    ev.register("H1", state=EvidenceState.RESEARCH_REQUIRED)
    with pytest.raises(EvidenceError, match="Transición inválida"):
        ev.transition("H1", EvidenceState.PAPER)


def test_blocked_final_oos_has_minimum_start():
    ev = _blocked_evidence()
    assert ev.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert ev.final_oos_available is False
    assert ev.dataset.final_oos.minimum_start == NEW_START


def test_guard_blocked_rejects_future_region():
    roles = _roles()
    ev = _blocked_evidence()
    with pytest.raises(EvidenceError, match="BLOCKED_BY_DATA_AVAILABILITY"):
        ev.guard(roles, NEW_START, NEW_START + 3600, purpose="evaluar FINAL_OOS")


def test_guard_blocked_allows_development_ranges():
    roles = _roles()
    ev = _blocked_evidence()
    ev.guard(roles, DEV_START, DEV_END, purpose="walk-forward en DEVELOPMENT")


def test_guard_rejects_observed_delegated_to_roles():
    roles = _roles()
    ev = _blocked_evidence()
    with pytest.raises(DataRoleError, match="OBSERVED"):
        ev.guard(roles, OBS_START, OBS_END, purpose="reutilizar OOS")


def test_guard_not_declared_only_rejects_future_region():
    roles = _roles()
    ev = EvidenceRegistry()
    ev.dataset.observed_through = OBS_END
    ev.guard(roles, DEV_START, DEV_END)  # ok: dentro de development
    with pytest.raises(EvidenceError, match="No hay FINAL_OOS declarado"):
        ev.guard(roles, NEW_START, NEW_START + 3600)


def test_unblock_requires_fresh_data_start():
    roles = _roles()
    ev = _blocked_evidence()
    with pytest.raises(EvidenceError, match="minimum_start"):
        ev.unblock_final_oos(roles, label="fake", start_epoch=OBS_START + 100,
                             end_epoch=OBS_END + 3600)
    assert ev.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY


def test_unblock_declares_and_consumes():
    roles = _roles()
    ev = _blocked_evidence()
    rng = ev.unblock_final_oos(roles, label="final_oos_v1", start_epoch=NEW_START,
                               end_epoch=NEW_END, note="primer FINAL_OOS limpio")
    assert rng.role is DataRole.FINAL_OOS
    assert ev.final_oos_status is FinalOosStatus.DECLARED
    assert ev.final_oos_available is True
    assert roles.final_oos_consumed is False

    ev.guard(roles, NEW_START, NEW_END, purpose="evaluar una unica vez")
    ev.mark_consumed(roles)
    assert roles.final_oos_consumed is True
    assert ev.final_oos_status is FinalOosStatus.CONSUMED
    with pytest.raises(DataRoleError, match="consumido"):
        roles.guard_final_oos(NEW_START, NEW_END)


def test_registry_persistence_roundtrip(tmp_path):
    ev = _blocked_evidence()
    ev.register("H1", state=EvidenceState.ROBUSTNESS_SUPPORTED, note="test")
    path = tmp_path / "evidence.json"
    ev.to_file(path)
    loaded = EvidenceRegistry.from_file(path)
    assert loaded.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert loaded.dataset.final_oos.minimum_start == NEW_START
    assert loaded.state("H1") is EvidenceState.ROBUSTNESS_SUPPORTED


def test_load_default_uses_committed_registry():
    ev = EvidenceRegistry.load_default()
    assert ev.dataset.observed_through is not None
    assert ev.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert ev.state("H005") is EvidenceState.RESEARCH_REQUIRED