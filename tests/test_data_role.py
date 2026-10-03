"""Tests del framework DATA_ROLE (H004): roles de tramos temporales, registro,
guard de FINAL_OOS y marca de consumo irreversible."""

from __future__ import annotations

import pytest

from research.data_role import (
    DataRange,
    DataRole,
    DataRoleError,
    RoleRegistry,
)

DEV_START, DEV_END = 1_546_000_000, 1_727_000_000
OBS_START, OBS_END = 1_727_000_000, 1_788_000_000


def _registry() -> RoleRegistry:
    reg = RoleRegistry()
    reg.register("development", DEV_START, DEV_END, DataRole.DEVELOPMENT)
    reg.register("observed", OBS_START, OBS_END, DataRole.OBSERVED)
    return reg


def test_register_and_role_for_epoch():
    reg = _registry()
    assert reg.role_for_epoch(DEV_START + 1000) is DataRole.DEVELOPMENT
    assert reg.role_for_epoch(OBS_START + 1000) is DataRole.OBSERVED
    assert reg.role_for_epoch(DEV_END + (OBS_END - OBS_START) * 2) is None


def test_register_rejects_validation_over_observed():
    reg = _registry()
    with pytest.raises(DataRoleError):
        reg.register("fix", OBS_START + 100, OBS_END - 100, DataRole.VALIDATION)


def test_register_rejects_final_oos_over_observed():
    reg = _registry()
    with pytest.raises(DataRoleError):
        reg.declare_final_oos("fake", OBS_START, OBS_END)


def test_invalid_range():
    reg = RoleRegistry()
    with pytest.raises(DataRoleError):
        reg.register("bad", 100, 100, DataRole.DEVELOPMENT)


def test_guard_final_oos_allows_clean_development():
    reg = _registry()
    reg.guard_final_oos(DEV_START, DEV_END)  # no lanza
    reg.guard_final_oos(DEV_START + 10, DEV_START + 100)


def test_guard_final_oos_rejects_observed():
    reg = _registry()
    with pytest.raises(DataRoleError, match="OBSERVED"):
        reg.guard_final_oos(OBS_START, OBS_END)


def test_guard_final_oos_rejects_overlap_with_observed():
    reg = _registry()
    with pytest.raises(DataRoleError, match="OBSERVED"):
        reg.guard_final_oos(OBS_START - 7200, OBS_START + 7200)


def test_final_oos_consume_is_irreversible_via_guard():
    reg = _registry()
    reg.declare_final_oos("reserved_h004", OBS_END, OBS_END + 3600 * 24 * 30)
    reg.guard_final_oos(OBS_END, OBS_END + 3600 * 24 * 30)  # limpio: permite
    reg.mark_final_oos_consumed()
    assert reg.final_oos_consumed is True
    with pytest.raises(DataRoleError, match="consumido"):
        reg.guard_final_oos(OBS_END, OBS_END + 3600 * 24 * 30)


def test_mark_consumed_without_final_oos_raises():
    reg = _registry()
    with pytest.raises(DataRoleError):
        reg.mark_final_oos_consumed()


def test_previous_oos_consumed_flag():
    reg = _registry()
    assert reg.previous_oos_consumed is True


def test_registry_persistence_roundtrip(tmp_path):
    reg = _registry()
    reg.register("val_w1", DEV_END - 3600 * 24 * 365, DEV_END, DataRole.VALIDATION)
    path = tmp_path / "data_roles.json"
    reg.to_file(path)
    loaded = RoleRegistry.from_file(path)
    assert loaded.role_for_epoch(DEV_START + 1) is DataRole.DEVELOPMENT
    assert loaded.role_for_epoch(OBS_START + 1) is DataRole.OBSERVED
    assert loaded.chunks["val_w1"].role is DataRole.VALIDATION
    assert loaded.final_oos_consumed is False