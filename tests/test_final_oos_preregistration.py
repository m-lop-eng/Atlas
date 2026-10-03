"""Validacion mecanica del PRE-REGISTRO del FINAL_OOS (tests).

Verifica que el plan congelado en research/decisions/FINAL_OOS_PRE_REGISTRATION.json
sea inequivoco ANTES de autorizar la ejecucion: hashes de configuracion (params y
config completa), rango (start_epoch / min_span / interval), parametros efectivos,
metricas, secciones del reporte y criterios de lectura.

NO ejecuta la estrategia: es solo verificacion de plan (mismo espiritu que
test_h004b_scheme.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from research.experiment import make_config_hash

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
CONFIG_PATH = ROOT / "config" / "final_oos" / "config.yaml"

FROZEN_STRATEGY_HASH = "491ed76d79f1034452e98f72453141a4c69110cc8d487f166a89072df332687f"
START_EPOCH = 1_788_220_800  # 2026-09-01T00:00Z
MIN_SPAN = 31_536_000  # 365 dias
INTERVAL = 3_600


def _load() -> dict:
    assert PLAN_PATH.exists(), f"falta {PLAN_PATH}"
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def _config() -> dict:
    assert CONFIG_PATH.exists(), f"falta {CONFIG_PATH}"
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_plan_existe_y_esta_frozen():
    raw = _load()
    assert raw["id"] == "FINAL_OOS_PRE_REGISTRATION"
    assert raw["estado"] == "FROZEN"
    assert raw["clasificacion"] == "EVALUACION_FINAL_OOS"


def test_hashes_coinciden_con_la_config_congelada():
    raw = _load()
    cfg = _config()
    p = raw["estrategia_congelada"]
    assert p["strategy_config_hash"] == FROZEN_STRATEGY_HASH
    assert make_config_hash(cfg["strategy"]["params"]) == FROZEN_STRATEGY_HASH
    assert p["config_hash"] == make_config_hash(cfg)
    assert p["params"] == cfg["strategy"]["params"]


def test_params_congelados_identicos_a_h004_h004b():
    raw = _load()
    p = raw["estrategia_congelada"]["params"]
    assert p == {
        "lookback": 20,
        "atr_period": 14,
        "stop_atr_mult": 2.0,
        "direction": "long",
    }


def test_rango_y_anclas_fijados():
    raw = _load()
    s = raw["esquema"]
    assert s["start_epoch"] == START_EPOCH
    assert s["iso_start"] == "2026-09-01T00:00:00Z"
    assert s["interval_seconds"] == INTERVAL
    assert s["min_span_seconds"] == MIN_SPAN
    assert s["regla_de_fin"]
    assert s["semantica_de_limite_inclusion"] == "MITAD_ABIERTA [start, end): una barra con open-time ts pertenece al FINAL_OOS sii start <= ts < end. Identica a _slice() de e9/e10/e11."


def test_parametros_efectivos_coinciden_con_config():
    raw = _load()
    cfg = _config()
    eff = raw["estrategia_congelada"]["parametros_efectivos"]
    assert float(eff["risk_per_trade"]) == float(cfg["risk"]["risk_per_trade"])
    assert float(eff["initial_equity"]) == float(cfg["costs"]["initial_equity"])
    assert float(eff["max_bars_in_trade"]) == float(cfg["engine"]["max_bars_in_trade"])
    assert float(eff["point_value"]) == float(cfg["engine"]["point_value"])
    assert float(eff["interval_seconds"]) == float(cfg["dataset"]["interval_seconds"])
    assert eff["direction"] == "long"
    assert eff["trend_filter"] is None
    assert eff["invert"] is False


def test_metricas_y_secciones_del_reporte_no_vacias():
    raw = _load()
    assert len(raw["metricas_pre_registradas"]) >= 20
    for key in (
        "net_return",
        "max_drawdown",
        "profit_factor",
        "expectancy",
        "win_rate",
        "num_trades",
        "avg_mae",
        "avg_mfe",
        "sharpe",
        "sortino",
        "calmar",
        "exposure_time",
        "costs_total",
    ):
        assert key in raw["metricas_pre_registradas"], key
    for section in ("lineage", "dataset_integrity", "metrics", "benchmarks",
                    "contrasts", "reading", "evidence", "trades_summary"):
        assert section in raw["secciones_del_reporte"], section


def test_criterios_de_lectura_null_sin_umbral_de_rentabilidad():
    raw = _load()
    crit = raw["criterios_de_lectura_preregistrados"]
    for key in ("net_return_gt_zero", "profit_factor_ge_one", "expectancy_gt_zero",
                "beats_neutral_control"):
        assert isinstance(crit["primarios"][key], str) and len(crit["primarios"][key]) > 0
    assert "precedencia" in crit
    assert isinstance(crit["nota_no_sesgo"], str)
    assert "NULOS" in crit["nota_no_sesgo"]
    assert "umbrales" in crit["nota_no_sesgo"]


def test_guard_mecanicos_y_validacion_cubren_requisitos_clave():
    raw = _load()
    guards = " ".join(raw["guard_mecanicos"]).lower()
    for needle in ("minimum_start", "observed", "dos veces", "config_hash",
                   "sha256", "gaps", "365 dias", "recalibracion"):
        assert needle in guards, needle
    assert raw["validacion_mecanica"]["archivo"].endswith("test_final_oos_preregistration.py")
    assert raw["ejecucion"]["gate"]
    assert raw["ejecucion"]["autorizacion_requerida"] is True


def test_benchmarks_congruentes_con_preregistros_previos():
    raw = _load()
    b = raw["benchmarks"]
    assert float(b["per_side"]) == 0.15
    assert float(b["initial_equity"]) == 100000.0
    assert float(b["point_value"]) == 1.0
    assert int(b["neutral_cycle_bars"]) == 48
    assert int(b["neutral_offset"]) == 7