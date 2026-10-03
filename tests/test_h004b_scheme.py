"""Validacion mecanica del PRE-REGISTRO H004-B (tests).

Verifica que el esquema congelado en research/decisions/H004B_PRE_REGISTRATION.json
sea inequivoco ANTES de autorizar la ejecucion: epochs coincidentes con las fechas
ISO, 12 ventanas consecutivas y sin solape, dentro de DEVELOPMENT, sin tocar
OBSERVED/FINAL_OOS, cubiertas por el dataset del manifest.

NO ejecuta la estrategia: es solo verificacion de esquema.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREREG_PATH = ROOT / "research" / "decisions" / "H004B_PRE_REGISTRATION.json"
ROLES_PATH = ROOT / "research" / "data_roles.json"
MANIFEST_PATH = ROOT / "experiments" / "data" / "market_BTCUSDT_60min.manifest.json"

DEV_START = 1_546_300_800  # 2019-01-01 (data_roles.json)
DEV_END = 1_727_787_600  # 2024-10-01T13:00Z (frontera registrada del registro)
F1_START = 1_617_235_200  # 2021-04-01 00:00 UTC
F12_END = 1_711_929_600  # 2024-04-01 00:00 UTC


def _load() -> dict:
    assert PREREG_PATH.exists(), f"falta {PREREG_PATH}"
    return json.loads(PREREG_PATH.read_text(encoding="utf-8"))


def _epoch(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
    )


def test_esquema_tiene_12_folds_congelados():
    raw = _load()
    folds = raw["esquema_de_folds"]["folds"]
    assert raw["esquema_de_folds"]["cantidad_congelada"] == 12
    assert len(folds) == 12
    assert [f["fold"] for f in folds] == [f"F{i}" for i in range(1, 13)]


def test_epochs_coinciden_con_iso_utc():
    raw = _load()
    for f in raw["esquema_de_folds"]["folds"]:
        assert f["start_epoch"] == _epoch(f["iso_start"]), f["label"]
        assert f["end_epoch"] == _epoch(f["iso_end"]), f["label"]


def test_anclas_de_intervalo_exactas():
    raw = _load()
    folds = raw["esquema_de_folds"]["folds"]
    assert folds[0]["start_epoch"] == F1_START
    assert folds[0]["iso_start"] == "2021-04-01T00:00:00Z"
    assert folds[-1]["end_epoch"] == F12_END
    assert folds[-1]["iso_end"] == "2024-04-01T00:00:00Z"


def test_ventanas_consecutivas_sin_solape_y_duracion_trimestral():
    raw = _load()
    folds = raw["esquema_de_folds"]["folds"]
    for k in range(1, len(folds)):
        assert folds[k]["start_epoch"] == folds[k - 1]["end_epoch"], folds[k]["label"]
    for f in folds:
        span_days = (f["end_epoch"] - f["start_epoch"]) / 86400
        assert f["span_days"] == int(span_days), f["label"]
        assert int(span_days) in (90, 91, 92), f["label"]
        assert f["end_epoch"] % 3600 == 0 and f["start_epoch"] % 3600 == 0


def test_ventanas_dentro_de_development_sin_tocar_observed():
    raw = _load()
    roles = json.loads(ROLES_PATH.read_text(encoding="utf-8"))
    for f in raw["esquema_de_folds"]["folds"]:
        assert DEV_START <= f["start_epoch"] < f["end_epoch"] <= DEV_END, f["label"]
    observed = next(c for c in roles["chunks"] if c["role"] == "OBSERVED")
    for f in raw["esquema_de_folds"]["folds"]:
        assert f["end_epoch"] <= observed["start_epoch"], f["label"]


def test_dataset_cubre_todas_las_ventanas():
    raw = _load()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    first = int(manifest["first_ts"])
    last = int(manifest["last_ts"])
    for f in raw["esquema_de_folds"]["folds"]:
        assert first <= f["start_epoch"], f["label"]
        assert last >= f["end_epoch"] - 3600, f["label"]


def test_guard_de_roles_y_evidence_dejan_ejecutar_el_esquema():
    from research.data_role import DataRoleError, RoleRegistry
    from research.evidence import EvidenceError, EvidenceRegistry

    raw = _load()
    roles = RoleRegistry.load_default()
    evidence = EvidenceRegistry.load_default()
    total_start = raw["data_roles"]["rango_total_diagnostico"]["start_epoch"]
    total_end = raw["data_roles"]["rango_total_diagnostico"]["end_epoch"]
    evidence.guard(roles, total_start, total_end, purpose="H004-B pre-registration check")


def test_criterios_de_lectura_definidos_sin_ambiguedad():
    raw = _load()
    crit = raw["criterios_de_lectura_preregistrados"]
    for key in ("LOCALIZADO", "SISTEMATICO", "INCONCLUSO"):
        assert isinstance(crit[key], str) and len(crit[key]) > 20, key
    assert "fold_negativo" in crit["definiciones"]
    assert "vecindad_negativa" in crit["definiciones"]
    assert crit["precedencia"]