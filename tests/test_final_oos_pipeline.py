"""Integracion del protocolo FINAL_OOS (research/final_oos_pipeline.py).

Ejercita TODO el protocolo con un dataset sintetico que imita la estructura
del futuro OOS (schema OHLC, open-time epoch, 3600s, >= 365 dias), sin tocar
los registros reales (research/evidence.json, research/data_roles.json):
guards de calendario/span/calidad/manifest/hash, declaracion unica, evaluacion
con la estrategia congelada real (config/final_oos/config.yaml) y consumo
irreversible.
"""

from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy

import pytest
import yaml

from data.quality import check_required_columns, summarize_quality
from data.sources import write_dataset
from data.synthetic import generate_ohlc_bars
from research.data_role import DataRole, DataRoleError, RoleRegistry
from research.evidence import EvidenceError, EvidenceRegistry, FinalOosStatus
from research.experiment import make_config_hash
from research.final_oos_pipeline import (
    FinalOosError,
    declare_final_oos,
    derive_end,
    load_manifest,
    load_plan,
    preflight,
    require_valid_manifest,
    run_final_oos,
    validate_manifest,
    validate_quality,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN = load_plan(ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json")
CONFIG = yaml.safe_load(
    (ROOT / "config" / "final_oos" / "config.yaml").read_text(encoding="utf-8")
)

NEW_START = 1_788_220_800  # 2026-09-01T00:00Z (minimum_start de evidence.json)
OBSERVED_END = 1_788_217_200  # 2026-08-31T23:00Z
INTERVAL = 3_600
MIN_SPAN = 31_536_000
HOURS_IN_SPAN = MIN_SPAN // INTERVAL  # 8760


# ---------------------------------------------------------------------------
# Harness (dataset sintetico + registries limpios, ambos en tmp_path)
# ---------------------------------------------------------------------------


def make_dataset(tmp_path: Path, *, n_days: int = 400, seed: int = 7) -> tuple[Path, dict]:
    """Dataset sintetico tipo binance.vision: barras OHLC <ts,open,high,low,close,volume>."""
    bars = generate_ohlc_bars(
        n_bars=n_days * 24,
        start_epoch=NEW_START,
        interval_seconds=INTERVAL,
        seed=seed,
        drift=0.00003,
        volatility=0.003,
    )
    bars = [{**b, "volume": 1_000.0} for b in bars]
    out_dir = tmp_path / "oos"
    csv_path = out_dir / "BTCUSDT_60min.csv"
    manifest_path = out_dir / "manifest.json"
    write_dataset(
        bars,
        csv_path,
        manifest_path,
        source="test",
        pair="BTCUSDT",
        interval_seconds=INTERVAL,
    )
    return csv_path, json.loads(manifest_path.read_text(encoding="utf-8"))


def make_registries():
    """Registries limpios en estado pre-OOS: observed consumido + FINAL_OOS bloqueado."""
    roles = RoleRegistry()
    roles.register("development", 1_546_300_800, 1_727_787_600, DataRole.DEVELOPMENT)
    roles.register("observed", 1_727_787_600, OBSERVED_END, DataRole.OBSERVED)
    evidence = EvidenceRegistry()
    evidence.block_final_oos(NEW_START, note="test: datos nuevos a partir de 2026-09-01")
    return roles, evidence


# ---------------------------------------------------------------------------
# Calidad del dataset
# ---------------------------------------------------------------------------


def test_quality_rejects_missing_columns():
    bars = generate_ohlc_bars(n_bars=50, start_epoch=NEW_START, interval_seconds=INTERVAL)
    bars = [
        {"ts": b["ts"], "open": b["open"], "close": b["close"]} for b in bars
    ]  # sin high/low
    assert check_required_columns(bars)


def test_quality_detects_gap_de_una_hora():
    bars = generate_ohlc_bars(n_bars=120, start_epoch=NEW_START, interval_seconds=INTERVAL)
    del bars[60]
    summary = summarize_quality(bars, expected_interval_seconds=INTERVAL)
    assert not summary.ok
    assert any("gap" in str(i.issue) or "timestamps" in str(i.issue) for i in summary.issues)


def test_quality_coverage_rejects_start_tardio():
    bars = generate_ohlc_bars(n_bars=120, start_epoch=NEW_START + INTERVAL, interval_seconds=INTERVAL)
    q = validate_quality(bars, start_epoch=NEW_START, end_epoch=NEW_START + 10 * INTERVAL, interval_seconds=INTERVAL)
    assert not q["ok"]
    assert any("primera barra" in issue for issue in q["issues"])


def test_quality_coverage_rejects_fin_prematuro():
    bars = generate_ohlc_bars(n_bars=10, start_epoch=NEW_START, interval_seconds=INTERVAL)
    q = validate_quality(
        bars,
        start_epoch=NEW_START,
        end_epoch=NEW_START + 400 * 24 * INTERVAL,
        interval_seconds=INTERVAL,
    )
    assert not q["ok"]
    assert any("no cubre el final" in issue for issue in q["issues"])


# ---------------------------------------------------------------------------
# Manifest: sha256 y cobertura
# ---------------------------------------------------------------------------


def test_manifest_rejects_sha_tamperado(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    with csv_path.open("a", encoding="utf-8") as fh:
        fh.write("0,1,1,1,1,1\n")
    integrity = validate_manifest(
        csv_path,
        manifest,
        start_epoch=NEW_START,
        end_epoch=NEW_START + 400 * 24 * INTERVAL,
        interval_seconds=INTERVAL,
        bars=[],
    )
    assert not integrity["checks"]["sha256_matches_csv"]
    assert integrity["checks"]["bars_match"] is False
    with pytest.raises(FinalOosError):
        require_valid_manifest(integrity)


def test_manifest_rejects_cobertura_incompleta(tmp_path):
    csv_path, manifest = make_dataset(tmp_path, n_days=30)
    integrity = validate_manifest(
        csv_path,
        manifest,
        start_epoch=NEW_START,
        end_epoch=NEW_START + MIN_SPAN,
        interval_seconds=INTERVAL,
        bars=[],
    )
    assert not integrity["ok"]
    assert not integrity["checks"]["coverage_end"]


def test_derive_end_sigue_la_regla_preregistrada(tmp_path):
    _, manifest = make_dataset(tmp_path, n_days=400)
    end = derive_end(PLAN["esquema"], manifest)
    last = int(manifest["last_ts"])
    assert end == (last // INTERVAL + 1) * INTERVAL
    assert end - NEW_START >= MIN_SPAN


# ---------------------------------------------------------------------------
# Guards de preflight
# ---------------------------------------------------------------------------


def test_preflight_rejects_start_antes_de_minimum_start(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()
    with pytest.raises(FinalOosError, match="minimum_start"):
        preflight(
            PLAN, evidence, roles, CONFIG,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=NEW_START - INTERVAL,
            end_epoch=NEW_START - INTERVAL + 400 * 24 * INTERVAL,
            bars=[],
        )
    assert evidence.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY


def test_preflight_rejects_span_corto(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()
    with pytest.raises(FinalOosError, match="min_span"):
        preflight(
            PLAN, evidence, roles, CONFIG,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=NEW_START,
            end_epoch=NEW_START + 100 * 24 * INTERVAL,
            bars=[],
        )
    assert evidence.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY


def test_preflight_rejects_crossing_observed(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()
    evidence.dataset.final_oos.minimum_start = 0  # fuerza solape OBSERVED en DATA_ROLE
    with pytest.raises(DataRoleError, match="OBSERVED"):
        preflight(
            PLAN, evidence, roles, CONFIG,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=OBSERVED_END - 2 * INTERVAL,
            end_epoch=OBSERVED_END + MIN_SPAN,
            bars=[],
        )


def test_preflight_rejects_config_recalibrada(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()
    tampered = deepcopy(CONFIG)
    tampered["strategy"]["params"]["lookback"] = 25
    with pytest.raises(FinalOosError, match="recalibro"):
        preflight(
            PLAN, evidence, roles, tampered,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=NEW_START,
            end_epoch=NEW_START + 400 * 24 * INTERVAL,
            bars=[],
        )
    tampered2 = deepcopy(CONFIG)
    tampered2["costs"]["slippage_per_side"] = 0.002
    assert make_config_hash(tampered2) != PLAN["estrategia_congelada"]["config_hash"]
    with pytest.raises(FinalOosError):
        preflight(
            PLAN, evidence, roles, tampered2,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=NEW_START,
            end_epoch=NEW_START + 400 * 24 * INTERVAL,
            bars=[],
        )


def test_plan_json_no_mutado_por_guards(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()
    before = json.dumps(PLAN, sort_keys=True)
    with pytest.raises(FinalOosError):
        preflight(
            PLAN, evidence, roles, CONFIG,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=NEW_START,
            end_epoch=NEW_START + 400 * 24 * INTERVAL,
            bars=[],
        )
    assert json.dumps(PLAN, sort_keys=True) == before
    assert evidence.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert roles.final_oos is None


# ---------------------------------------------------------------------------
# Declaracion unica del FINAL_OOS
# ---------------------------------------------------------------------------


def test_registry_solo_admite_un_final_oos():
    roles, _ = make_registries()
    roles.declare_final_oos("final_oos_v1", NEW_START, NEW_START + MIN_SPAN)
    with pytest.raises(DataRoleError, match="solo existe|Ya existe"):
        roles.declare_final_oos("final_oos_v2", NEW_START + 1, NEW_START + MIN_SPAN + 1)
    with pytest.raises(DataRoleError, match="reservado"):
        roles.register("intruse", NEW_START, NEW_START + MIN_SPAN, DataRole.DEVELOPMENT)


def test_register_rechaza_segundo_final_oos_solapado():
    roles, _ = make_registries()
    roles.declare_final_oos("final_oos_v1", NEW_START, NEW_START + MIN_SPAN)
    with pytest.raises(DataRoleError, match="solo existe"):
        roles.register(
            "final_oos_otro", NEW_START + 10, NEW_START + MIN_SPAN, DataRole.FINAL_OOS
        )


# ---------------------------------------------------------------------------
# Pipeline completo
# ---------------------------------------------------------------------------


def test_happy_path_produce_reporte_preregistrado_y_consume(tmp_path):
    csv_path, manifest = make_dataset(tmp_path)
    roles, evidence = make_registries()

    out_path = tmp_path / "report" / "final_oos_report.json"
    report = run_final_oos(
        PLAN,
        CONFIG,
        csv_path=csv_path,
        manifest_path=csv_path.parent / "manifest.json",
        evidence=evidence,
        roles=roles,
        evidence_path=tmp_path / "evidence.json",
        roles_path=tmp_path / "data_roles.json",
        out_path=out_path,
    )

    for section in PLAN["secciones_del_reporte"]:
        assert section in report, section

    covered = set(report["metrics"]) | set(report["curve_metrics"]) | set(report["exposure"])
    missing = [m for m in PLAN["metricas_pre_registradas"] if m not in covered]
    assert not missing, f"metricas preregistradas ausentes del reporte: {missing}"

    reading = report["reading"]["primarios"]
    for key in ("net_return_gt_zero", "profit_factor_ge_one", "expectancy_gt_zero", "beats_neutral_control"):
        assert isinstance(reading[key], bool), key
    assert report["reading"]["conclusion"] in ("NO_FALSADO", "FALSADO")

    assert report["reading"]["conclusion"] == (
        "NO_FALSADO" if all(reading.values()) else "FALSADO"
    )

    assert evidence.final_oos_status is FinalOosStatus.CONSUMED
    assert roles.final_oos_consumed
    assert roles.final_oos is not None
    assert roles.final_oos.start_epoch == NEW_START
    assert roles.final_oos.end_epoch == report["lineage"]["final_oos"]["end_epoch"]

    persisted = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert persisted["dataset"]["final_oos"]["status"] == "CONSUMED"
    persisted_roles = json.loads((tmp_path / "data_roles.json").read_text(encoding="utf-8"))
    assert persisted_roles["final_oos"]["consumed"] is True
    assert out_path.exists()

    metrics = report["metrics"]
    assert metrics["max_drawdown"] >= 0.0
    assert metrics["num_trades"] >= 0
    assert report["dataset_integrity"]["quality"]["rows"] == 400 * 24
    assert report["reading"]


def test_doble_ejecucion_rechazada(tmp_path):
    csv_path, _ = make_dataset(tmp_path)
    roles, evidence = make_registries()
    run_final_oos(
        PLAN, CONFIG,
        csv_path=csv_path,
        manifest_path=csv_path.parent / "manifest.json",
        evidence=evidence,
        roles=roles,
        evidence_path=tmp_path / "evidence.json",
        roles_path=tmp_path / "data_roles.json",
    )
    assert evidence.final_oos_status is FinalOosStatus.CONSUMED
    with pytest.raises(FinalOosError, match="CONSUMIDO|consumido"):
        run_final_oos(
            PLAN, CONFIG,
            csv_path=csv_path,
            manifest_path=csv_path.parent / "manifest.json",
            evidence=evidence,
            roles=roles,
            evidence_path=tmp_path / "evidence.json",
            roles_path=tmp_path / "data_roles.json",
        )


def test_guards_bloquean_evaluacion_fuera_del_protocolo(tmp_path):
    """Mientras BLOCKED no se puede evaluar; la via sancionada es el protocolo."""
    csv_path, _ = make_dataset(tmp_path)
    roles, evidence = make_registries()
    with pytest.raises(EvidenceError, match="BLOCKED"):
        evidence.guard(roles, NEW_START, NEW_START + MIN_SPAN, purpose="test")
    declare_final_oos(
        PLAN, evidence, roles, start_epoch=NEW_START, end_epoch=NEW_START + MIN_SPAN
    )
    assert evidence.final_oos_status is FinalOosStatus.DECLARED
    assert roles.final_oos is not None
    assert roles.final_oos.start_epoch == NEW_START
    assert roles.final_oos.end_epoch == NEW_START + MIN_SPAN


def test_pipeline_no_toca_registros_reales():
    real_roles = RoleRegistry.load_default()
    real_evidence = EvidenceRegistry.load_default()
    assert real_evidence.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert real_evidence.dataset.final_oos.minimum_start == NEW_START
    assert not real_roles.final_oos_consumed
    assert real_roles.previous_oos_consumed