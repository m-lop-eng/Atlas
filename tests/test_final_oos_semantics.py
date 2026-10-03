"""Semantica documental del desbloqueo del FINAL_OOS (tests).

Establece la semantica congelada de disponibilidad de datos tal como la lee el
pipeline (research/final_oos_pipeline.py): un dataset nuevo desbloquea el FINAL_OOS
UNICAMENTE cuando el manifest cubre [start_epoch, start_epoch + min_span) con
start_epoch=1788220800 (2026-09-01T00:00Z) y min_span=31536000 (365 dias), i.e.
hasta >= 2027-09-01T00:00Z.

Un zip mensual concreto (p. ej. BTCUSDT-1h-2026-09.zip) NO es el gate: produce un
rango derivado de ~30 dias y el estado sigue BLOCKED_BY_DATA_AVAILABILITY. Esto
evita reintroducir la interpretacion operacional incorrecta "dump 2026-09 -> ejecutar".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from data.loader import read_ohlc_csv
from research.final_oos_pipeline import (
    FinalOosError,
    derive_end,
    load_plan,
    preflight,
)
from tests.test_final_oos_pipeline import make_dataset, make_registries
from tests.test_final_oos_preregistration import CONFIG_PATH, PLAN_PATH

ROOT = Path(__file__).resolve().parents[1]
PLAN = load_plan(PLAN_PATH)
CONFIG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

START_EPOCH = int(PLAN["esquema"]["start_epoch"])  # 2026-09-01T00:00Z
MIN_SPAN = int(PLAN["esquema"]["min_span_seconds"])  # 365 dias
INTERVAL = int(PLAN["esquema"]["interval_seconds"])

ELIGIBLE_END = START_EPOCH + MIN_SPAN  # 2027-09-01T00:00Z
LAST_TS_ELIGIBLE = ELIGIBLE_END - INTERVAL  # 2027-08-31T23:00Z


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _end_for_last(last_ts: int) -> int:
    """Regla de fin derivada de un manifest cuyo ultima barra es last_ts."""
    return derive_end(PLAN["esquema"], {"last_ts": str(last_ts)})


def _matriz():
    """Casos [ultima barra] -> [span solapado] documentales."""
    return {
        "2026-09-30T23:00:00Z": 30,      # dump mensual 2026-09 entero -> ~30d
        "2026-12-31T23:00:00Z": 122,     # trimestre completo -> ~122d
        "2027-08-30T23:00:00Z": 364,     # un dia antes del minimo -> ~364d
        "2027-08-31T23:00:00Z": 365,     # cobertura minima -> eligible
    }


def _epoch_iso(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Matriz documental: derive_end + min_span
# ---------------------------------------------------------------------------


def test_matriz_documental_span():
    """Cada `last_ts` produce el span esperado de [start, end)."""
    for last_iso, expected_days in _matriz().items():
        last = _epoch_iso(last_iso)
        end = _end_for_last(last)
        span = end - START_EPOCH
        assert span // 86_400 == expected_days, (last_iso, span)
        assert (span >= MIN_SPAN) == (expected_days >= 365), last_iso


def test_ultima_barra_required_para_desbloquear():
    """La ultima barra >= 2027-08-31T23:00Z es condicion necesaria y suficiente."""
    below = _end_for_last(LAST_TS_ELIGIBLE - INTERVAL)
    assert below - START_EPOCH < MIN_SPAN
    at = _end_for_last(LAST_TS_ELIGIBLE)
    assert at - START_EPOCH == MIN_SPAN
    assert at == ELIGIBLE_END


def test_dump_2026_09_no_desbloquea():
    """interpretacion operacional incorrecta que este test fija como bloqueada."""
    end = _end_for_last(_epoch_iso("2026-09-30T23:00:00Z"))
    assert end - START_EPOCH < MIN_SPAN
    assert _iso(end) == "2026-10-01T00:00:00Z"
    assert _iso(ELIGIBLE_END) == "2027-09-01T00:00:00Z"


# ---------------------------------------------------------------------------
# El guard real de preflight coincide con la matriz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [30, 122, 364])
def test_preflight_bloquea_span_corto(tmp_path, days: int):
    csv_path, manifest = make_dataset(tmp_path, n_days=days)
    roles, evidence = make_registries()
    end = START_EPOCH + days * 86_400
    with pytest.raises(FinalOosError, match="min_span"):
        preflight(
            PLAN, evidence, roles, CONFIG,
            csv_path=csv_path,
            manifest=manifest,
            start_epoch=START_EPOCH,
            end_epoch=end,
            bars=[],
        )
    assert evidence.final_oos_status.value == "BLOCKED_BY_DATA_AVAILABILITY"


def test_preflight_permite_span_completo(tmp_path):
    csv_path, manifest = make_dataset(tmp_path, n_days=370)
    roles, evidence = make_registries()
    bars = read_ohlc_csv(csv_path)
    preflight(
        PLAN, evidence, roles, CONFIG,
        csv_path=csv_path,
        manifest=manifest,
        start_epoch=START_EPOCH,
        end_epoch=START_EPOCH + MIN_SPAN,
        bars=bars,
    )
    assert evidence.final_oos_status.value == "BLOCKED_BY_DATA_AVAILABILITY"


def test_anclas_iso_congeladas():
    """Anclas temporales del esquema (evita desviaciones de fecha)."""
    s = PLAN["esquema"]
    assert s["iso_start"] == "2026-09-01T00:00:00Z"
    assert s["min_span_hint"] == "365 dias"
    assert s["min_span_seconds"] == 31_536_000