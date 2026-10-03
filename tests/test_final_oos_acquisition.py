"""Integracion de la adquisicion del dataset FINAL_OOS (research/final_oos_acquisition.py).

Cubre el flujo del commit 3 SIN red: un proveedor sintetico inyectado
(`FakeVision`) sirve zips klines 1h de binance.vision (monthly/daily) con
cobertura controlada, y verifica:

  1. monthly completo          -> usa monthly, NO descarga daily innecesarios
  2. monthly ausente (404)     -> cae a daily y construye
  3. monthly + daily           -> combinacion correcta a traves de meses
  4. gap real                  -> BLOCKED_BY_DATA_AVAILABILITY, sin rellenar
  5. reproducibilidad          -> mismos inputs => mismo dataset (offline, sin red)
  6. no consumo                -> tras construir, final_oos_consumed sigue False
  7. no mutacion               -> evidence/data_roles/config/preregistro intactos
  8. hardening de red          -> timeout / sin conexion / zip corrupto terminan en
                                 ACQUISITION_ERROR determinista (no cuelgan); el 404
                                 es cobertura (BLOCKED), nunca error; el scan daily se
                                 acota al primer dia sin publicar; los registros
                                 congelados siguen intactos ante un error operacional

Los registros reales se toman SOLO en modo lectura (tests 6 y 7).
"""

from __future__ import annotations

import calendar
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from research.final_oos_acquisition import (
    AcquisitionStatus,
    FinalOosAcquisition,
)
from data.sources import DataConnectionError, DataTimeoutError, NotFoundError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "research" / "evidence.json"
ROLES = ROOT / "research" / "data_roles.json"
CONFIG = ROOT / "config" / "final_oos" / "config.yaml"
PLAN = ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"

DAY = 86_400
HOUR = 3_600
START = 1_788_220_800  # 2026-09-01T00:00Z


# ---------------------------------------------------------------------------
# Proveedor sintetico de binance.vision (opener inyectable, sin red)
# ---------------------------------------------------------------------------


def _zip_bytes(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BTCUSDT-1h-klines.csv", "\n".join(rows))
    return buf.getvalue()


def _rows(start_ts: int, n: int, step: int = HOUR, base: float = 50_000.0) -> list[str]:
    """Klines sinteticas continuas (OHLC coherente, una por intervalo)."""
    return [
        f"{start_ts + i * step},{base:.1f},{base + 1:.1f},{base - 1:.1f},{base:.1f},1000"
        for i in range(n)
    ]


def _month_rows(year: int, month: int) -> list[str]:
    nd = calendar.monthrange(year, month)[1]
    start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    return _rows(start, nd * 24)


def _day_rows(year: int, month: int, day: int) -> list[str]:
    start = int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    return _rows(start, 24)


def month_zip(year: int, month: int) -> bytes:
    return _zip_bytes(_month_rows(year, month))


def day_zip(year: int, month: int, day: int) -> bytes:
    return _zip_bytes(_day_rows(year, month, day))


def _url_key(kind: str, url: str) -> str:
    """Extrae '2026-09' (monthly) | '2026-09-01' (daily) de la URL."""
    name = url.rsplit("/", 1)[-1].removesuffix(".zip")
    parts = name.split("-")
    if kind == "monthly":
        return f"{parts[-2]}-{parts[-1]}"
    return "-".join(parts[-3:])


class FakeVision:
    """Binance Vision sintetico: `available` con URLs completas, 404 o corruptas."""

    def __init__(
        self, monthly: set[str], daily: set[str], corrupt: set[str] | None = None
    ) -> None:
        self.monthly = set(monthly)
        self.daily = set(daily)
        self.corrupt = set(corrupt or ())
        self.calls: list[str] = []

    def opener(self, url: str, kw: dict) -> bytes:
        self.calls.append(url)
        if "/monthly/" in url:
            key = _url_key("monthly", url)
            if key in self.corrupt:
                return b"contenido corrupto: no es un zip"
            if key in self.monthly:
                y, m = (int(p) for p in key.split("-"))
                return month_zip(y, m)
        else:
            key = _url_key("daily", url)
            if key in self.corrupt:
                return b"contenido corrupto: no es un zip"
            if key in self.daily:
                y, m, d = (int(p) for p in key.split("-"))
                return day_zip(y, m, d)
        raise NotFoundError(f"No se pudo descargar {url}: HTTP 404 (no publicado)")


def make_acquire(
    fake: FakeVision,
    tmp_path: Path,
    *,
    start: int = START,
    end: int | None = None,
    span_days: int | None = None,
    min_span: int | None = None,
) -> FinalOosAcquisition:
    end = end if end is not None else start + (span_days or 3) * DAY
    return FinalOosAcquisition(
        start_epoch=start,
        end_epoch=end,
        min_span_seconds=min_span if min_span is not None else (end - start),
        raw_dir=tmp_path / "raw",
        out_dir=tmp_path / "out",
        opener=fake.opener,
    )


def _bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


# ---------------------------------------------------------------------------
# 1. Monthly completo: monthly preferred, sin daily innecesario
# ---------------------------------------------------------------------------


def test_monthly_completo_no_descarga_daily(tmp_path):
    fake = FakeVision(monthly={"2026-09", "2026-10"}, daily=set())
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
    daily_calls = [u for u in fake.calls if "/daily/" in u]
    assert daily_calls == [], "no se debe descargar daily si hay monthly"
    assert {r.kind for r in res["inputs"]} == {"monthly"}
    assert res["coverage"]["last_ts"] + HOUR == START + 33 * DAY


# ---------------------------------------------------------------------------
# 2. Monthly ausente (404) -> daily fallback
# ---------------------------------------------------------------------------


def test_monthly_404_cae_a_daily(tmp_path):
    fake = FakeVision(
        monthly=set(),
        daily={"2026-09-01", "2026-09-02", "2026-09-03"},
    )
    acq = make_acquire(fake, tmp_path, end=START + 3 * DAY)
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
    assert {r.kind for r in res["inputs"]} == {"daily"}
    assert res["dataset_bars"] == 3 * 24
    assert any("/monthly/" in u for u in fake.calls)  # monthly intentado (404)


# ---------------------------------------------------------------------------
# 3. Monthly (2026-09) + daily (octubre): combinacion correcta
# ---------------------------------------------------------------------------


def test_monthly_mas_daily_combinacion(tmp_path):
    fake = FakeVision(
        monthly={"2026-09"},
        daily={"2026-10-01", "2026-10-02", "2026-10-03"},
    )
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)  # hasta 2026-10-04
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
    kinds = {(r.kind, r.date_key) for r in res["inputs"]}
    assert ("monthly", "2026-09") in kinds
    assert ("daily", "2026-10-01") in kinds
    assert ("daily", "2026-10-03") in kinds
    assert res["dataset_bars"] == 33 * 24
    # el 1 de septiembre NO se pidio como daily: lo cubrio monthly
    sept_daily = [u for u in fake.calls if "/daily/" in u and "2026-09" in u]
    assert sept_daily == []


# ---------------------------------------------------------------------------
# 4. Gap real -> BLOCKED, sin rellenado silencioso
# ---------------------------------------------------------------------------


def test_gap_real_bloquea_sin_rellenar(tmp_path):
    # monthly 2026-09 disponible, pero octubre sin venir -> faltan daily
    fake = FakeVision(
        monthly={"2026-09"},
        daily={"2026-10-01", "2026-10-02"},
    )
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)  # falta 10-03 y 10-04
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res["dataset_sha256"] is None
    assert "cobertura" in res["reason"] or "gap" in res["reason"]
    assert not res["eligible"]


def test_hueco_interno_bloquea(tmp_path):
    # fallback monthly ausente + hueco en medio de la serie diaria
    fake = FakeVision(
        monthly=set(),
        daily={"2026-09-01", "2026-09-02", "2026-09-04"},  # falta 09-03
    )
    acq = make_acquire(fake, tmp_path, end=START + 4 * DAY)
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res["dataset_sha256"] is None


# ---------------------------------------------------------------------------
# 5. Reproducibilidad: mismos inputs -> mismo dataset (offline, sin red)
# ---------------------------------------------------------------------------


def test_reproducibilidad_mismos_inputs_mismo_dataset(tmp_path):
    fake = FakeVision(monthly={"2026-09", "2026-10"}, daily=set())
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)

    res1 = acq.acquire()
    manifest1 = _bytes(tmp_path / "out" / "inputs.manifest.json")

    # segunda ejecucion: inputs ya en cache -> NO se debe llamar a la red
    fake.calls.clear()

    def no_network(url: str, kw: dict) -> bytes:
        raise AssertionError("offline-first roto: se llamo a la red")

    acq2 = make_acquire(
        FakeVision(monthly=set(), daily=set()), tmp_path,
        end=START + 33 * DAY,
    )
    acq2._opener = no_network
    res2 = acq2.acquire()

    assert fake.calls == [], "no se debe volver a internet con inputs en cache"
    assert res1["dataset_sha256"] == res2["dataset_sha256"]
    assert res1["dataset_bars"] == res2["dataset_bars"]
    assert res1["status"] is res2["status"]
    assert manifest1 == _bytes(tmp_path / "out" / "inputs.manifest.json")
    assert res1["coverage"] == res2["coverage"]


# ---------------------------------------------------------------------------
# 6. No consumo: construir dataset NO consume el FINAL_OOS
# ---------------------------------------------------------------------------


def test_construir_dataset_no_consume(tmp_path):
    fake = FakeVision(monthly={"2026-09", "2026-10"}, daily=set())
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)
    res = acq.acquire()
    assert res["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN

    from research.data_role import RoleRegistry
    from research.evidence import EvidenceRegistry

    roles = RoleRegistry.load_default()
    evidence = EvidenceRegistry.load_default()
    assert roles.final_oos_consumed is False
    assert evidence.final_oos_status.value == "BLOCKED_BY_DATA_AVAILABILITY"


# ---------------------------------------------------------------------------
# 7. No mutacion: registros reales intactos tras la adquisicion
# ---------------------------------------------------------------------------


def test_adquisicion_no_muta_registros(tmp_path):
    before = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert before["evidence"] and before["roles"], "prerequisito: registros reales"

    fake = FakeVision(monthly={"2026-09", "2026-10"}, daily=set())
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)
    acq.acquire()

    after = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert after == before, "la adquisicion NO debe mutar los registros congelados"


# ---------------------------------------------------------------------------
# Sanidad del planificador/lineage
# ---------------------------------------------------------------------------


def test_required_files_y_inputs_consolean(tmp_path):
    fake = FakeVision(monthly={"2026-09"}, daily={"2026-10-01"})
    acq = make_acquire(fake, tmp_path, end=START + 31 * DAY)
    plan = acq.required_files()
    assert plan["monthly"] and plan["daily"] == []  # plan ideal: monthly preferred
    res = acq.acquire()
    # el plan ideal NO promete daily; los inputs reales reflejan el fallback
    assert {"monthly", "daily"} <= {r.kind for r in res["inputs"]}
    assert res["manifest_inputs"] == "inputs.manifest.json"


# ---------------------------------------------------------------------------
# 8. Hardening de red: estados deterministas, sin colgarse nunca
# ---------------------------------------------------------------------------


def _timeout_opener(url: str, kw: dict) -> bytes:
    raise DataTimeoutError(f"No se pudo descargar {url}: timeout")


def _connection_opener(url: str, kw: dict) -> bytes:
    raise DataConnectionError(f"No se pudo descargar {url}: [Errno 11001] getaddrinfo failed")


def test_timeout_aborta_en_estado_determinista_no_cuelga(tmp_path):
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 3 * DAY)
    acq._opener = _timeout_opener
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "timeout"
    assert not res["eligible"]
    assert res["dataset_sha256"] is None


def test_error_de_conexion_aborta_en_estado_determinista(tmp_path):
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 3 * DAY)
    acq._opener = _connection_opener
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "connection_error"
    assert not res["eligible"]


def test_404_es_cobertura_no_error(tmp_path):
    # nada publicado aun: todos los probe 404 -> BLOCKED (cobertura), NUNCA error
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 3 * DAY)
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res["cause"] == "coverage_incomplete"
    assert res["dataset_sha256"] is None


def test_zip_corrupto_clasifica_corrupt_file(tmp_path):
    fake = FakeVision(monthly={"2026-09"}, daily={"2026-10-01"}, corrupt={"2026-09"})
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"
    assert "zip corrupto" in res["reason"]
    assert res["dataset_sha256"] is None
    assert not res["eligible"]


def test_scan_diario_se_detiene_en_primer_hueco(tmp_path):
    # hueco en medio de la serie diaria: el scan NO sondea dias posteriores al
    # primer dia sin publicar (publicacion monotona); ejecucion acotada.
    fake = FakeVision(monthly=set(), daily={"2026-09-01", "2026-09-02", "2026-09-04"})
    acq = make_acquire(fake, tmp_path, end=START + 4 * DAY)
    res = acq.acquire()

    daily_calls = [u for u in fake.calls if "/daily/" in u]
    assert [_url_key("daily", u) for u in daily_calls] == [
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
    ]
    assert {r.date_key for r in res["inputs"]} == {"2026-09-01", "2026-09-02"}
    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY


def test_scan_diario_acotado_a_primer_dia_faltante(tmp_path):
    # estado real hoy: monthly 404 + daily publicados hasta 09-25. La ventana
    # completa son ~365 dias; el scan DEBE parar en el primer 404 (09-26) en
    # lugar de sondear ~340 dias futuros (el bug que colgaba el proceso).
    fake = FakeVision(
        monthly=set(),
        daily={f"2026-09-{d:02d}" for d in range(1, 26)},
    )
    acq = make_acquire(fake, tmp_path, end=START + 365 * DAY)
    res = acq.acquire()

    daily_calls = [u for u in fake.calls if "/daily/" in u]
    assert len(daily_calls) == 26  # 09-01..09-25 (hit) + 09-26 (primer 404, corte)
    assert daily_calls[-1].endswith("2026-09-26.zip")
    assert not any("/daily/2026-10" in u for u in fake.calls)
    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res["cause"] == "coverage_incomplete"


def test_error_de_red_no_muta_registros(tmp_path):
    before = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert before["evidence"] and before["roles"], "prerequisito: registros reales"

    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 3 * DAY)
    acq._opener = _timeout_opener
    res = acq.acquire()
    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR

    after = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert after == before, "un fallo de adquisicion NO debe mutar los registros congelados"


# ---------------------------------------------------------------------------
# 9. Data hardening — integridad del dataset existente (Bloque 1)
# ---------------------------------------------------------------------------


def _build_valid(tmp_path, fake: FakeVision | None = None):
    fake = fake or FakeVision(monthly={"2026-09", "2026-10"}, daily=set())
    acq = make_acquire(fake, tmp_path, end=START + 33 * DAY)
    res = acq.acquire()
    assert res["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
    out = tmp_path / "out"
    return (
        res,
        out / "BTCUSDT_final_oos.csv",
        out / "BTCUSDT_final_oos.manifest.json",
    )


def test_dataset_valido_se_reutiliza_sin_red(tmp_path):
    res1, _csv, _man = _build_valid(tmp_path)

    def no_network(url, kw):
        raise AssertionError("offline-first roto: se llamo a la red")

    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    acq2._opener = no_network
    res2 = acq2.acquire()

    assert res2["status"] is AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
    assert res2["cause"] is None
    assert res2["dataset_sha256"] == res1["dataset_sha256"]
    assert "inmutable" in res2["reason"]


def test_dataset_sha_incorrecto_rechaza(tmp_path):
    res1, csv, _man = _build_valid(tmp_path)
    data = csv.read_bytes()
    csv.write_bytes(data.replace(b"50000.0", b"50001.0", 1))

    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    res2 = acq2.acquire()
    assert res2["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res2["cause"] == "dataset_integrity"
    assert res2["dataset_sha256"] is None


def test_manifest_ausente_dataset_parcial(tmp_path):
    _res1, _csv, man = _build_valid(tmp_path)
    man.unlink()

    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    res2 = acq2.acquire()
    assert res2["cause"] == "dataset_integrity"


def test_manifest_incompleto_rechaza(tmp_path):
    import json as _json

    _res1, _csv, man = _build_valid(tmp_path)
    payload = _json.loads(man.read_text(encoding="utf-8"))
    payload.pop("interval_seconds", None)
    man.write_text(_json.dumps(payload), encoding="utf-8")

    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    assert acq2.acquire()["cause"] == "dataset_integrity"


def test_dataset_truncado_rechaza(tmp_path):
    _res1, csv, _man = _build_valid(tmp_path)
    data = csv.read_bytes()
    csv.write_bytes(data[: len(data) // 2])

    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    res2 = acq2.acquire()
    assert res2["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res2["cause"] == "dataset_integrity"


def test_dataset_fuera_de_rango_bloquea(tmp_path):
    _build_valid(tmp_path)  # cubre [START, START+33d)
    acq2 = make_acquire(
        FakeVision(monthly={"2026-09", "2026-10"}, daily=set()),
        tmp_path,
        end=START + 40 * DAY,  # pide mas de lo que cubre el dataset existente
    )
    res2 = acq2.acquire()
    assert res2["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res2["cause"] == "coverage_incomplete"
    assert not res2["eligible"]


def test_gap_interno_es_coverage_incomplete(tmp_path):
    fake = FakeVision(monthly=set(), daily={"2026-09-01", "2026-09-02", "2026-09-04"})
    acq = make_acquire(fake, tmp_path, end=START + 4 * DAY)
    res = acq.acquire()
    assert res["status"] is AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
    assert res["cause"] == "coverage_incomplete"


# ---------------------------------------------------------------------------
# 10. Data hardening — validacion de filas/volumen (Bloque 3)
# ---------------------------------------------------------------------------


def _acq_with_rows(tmp_path, rows):
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + DAY)
    acq._opener = lambda url, kw: _zip_bytes(rows)
    return acq


def test_timestamp_duplicado_es_corrupt_file(tmp_path):
    rows = _rows(START, 1) + _rows(START, 1)  # mismo ts dos veces
    res = _acq_with_rows(tmp_path, rows).acquire()
    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"


def test_timestamp_desalineado_es_corrupt_file(tmp_path):
    rows = [f"{START + 1800},50000.0,50001.0,49999.0,50000.0,1000"]
    res = _acq_with_rows(tmp_path, rows).acquire()
    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"


def test_ohlc_invalido_es_corrupt_file(tmp_path):
    # high 49000 < max(open, close) = 50000
    rows = [f"{START},50000.0,49000.0,48000.0,50000.0,1000"]
    res = _acq_with_rows(tmp_path, rows).acquire()
    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"


def test_volumen_invalido_es_corrupt_file(tmp_path):
    rows = [f"{START},50000.0,50001.0,49999.0,50000.0,-1"]
    res = _acq_with_rows(tmp_path, rows).acquire()
    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"


# ---------------------------------------------------------------------------
# 11. Data hardening — raw atomico (Bloque 2)
# ---------------------------------------------------------------------------


def test_zip_parcial_no_deja_archivo_final(tmp_path):
    valid = month_zip(2026, 9)
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + DAY)
    acq._opener = lambda url, kw: valid[: len(valid) // 2]  # zip truncado
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    assert res["cause"] == "corrupt_file"
    raw = tmp_path / "raw"
    leftovers = list(raw.rglob("*")) if raw.exists() else []
    assert leftovers == [], f"no debe quedar raw final ni .tmp: {leftovers}"


def test_fallo_de_descarga_no_deja_archivo(tmp_path):
    acq = make_acquire(FakeVision(set(), set()), tmp_path, end=START + DAY)
    acq._opener = _connection_opener
    res = acq.acquire()

    assert res["status"] is AcquisitionStatus.ACQUISITION_ERROR
    raw = tmp_path / "raw"
    leftovers = list(raw.rglob("*")) if raw.exists() else []
    assert leftovers == []


# ---------------------------------------------------------------------------
# 12. Data hardening — no mutacion ante un fallo de integridad
# ---------------------------------------------------------------------------


def test_dataset_integrity_no_muta_registros(tmp_path):
    before = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert before["evidence"] and before["roles"], "prerequisito: registros reales"

    _res1, csv, _man = _build_valid(tmp_path)
    csv.write_bytes(csv.read_bytes() + b"\ncorrupto")
    acq2 = make_acquire(FakeVision(set(), set()), tmp_path, end=START + 33 * DAY)
    assert acq2.acquire()["cause"] == "dataset_integrity"

    after = {
        "evidence": _bytes(EVIDENCE),
        "roles": _bytes(ROLES),
        "config": _bytes(CONFIG),
        "plan": _bytes(PLAN),
    }
    assert after == before