"""Tests de FinalOOSDatasetBuilder (research/final_oos_dataset.py).

Fixtures SIN red: fuentes estaticas que imitan monthly/daily de binance.vision
y la integracion real BinanceVisionDailySource con opener inyectable.

Cubre: build correcto (daily consecutivos), dedupe idéntico determinista,
rechazo de conflicto (misma ts contenido distinto), merge monthly+daily,
ordenacion, gap -> rechazo, barra parcial -> excluida/rechazo por hueco,
intervalo incorrecto -> rechazo, manifest/sha256 reproducibles, no acceso a
registros reales, misma entrada -> mismo dataset.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from data.sources import BinanceVisionDailySource, DataSource, NotFoundError
from data.synthetic import generate_ohlc_bars
from research.final_oos_dataset import (
    DatasetBuilderError,
    FinalOosDatasetBuilder,
    binsance_required_files,
)

START = 1_788_220_800  # 2026-09-01T00:00Z
INTERVAL = 3_600
HOUR = 3_600
DAY = 24 * HOUR
MIN_SPAN = 31_536_000  # 365d
END = START + MIN_SPAN  # 2027-09-01T00:00Z

REAL_EVIDENCE = "research/evidence.json"
REAL_ROLES = "research/data_roles.json"


# ---------------------------------------------------------------------------
# Fixture: fuente estatica (simula monthly/daily sin red)
# ---------------------------------------------------------------------------


class StaticSource(DataSource):
    """Devuelve barras fijas en rango [start, end).

    `cuts_before`: deja "muerto" el rango en/despues de ese ts (simula data aun
    no publicada). `duplicates`: barras extra repetidas para probar dedupe.
    """

    def __init__(
        self,
        bars: list[dict],
        *,
        cuts_before: int | None = None,
        duplicates: list[dict] | None = None,
    ) -> None:
        self.bars = list(bars)
        self.cuts_before = cuts_before
        self.duplicates = list(duplicates or [])

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        out: list[dict] = []
        for b in self.bars + self.duplicates:
            ts = int(b["ts"])
            if self.cuts_before is not None and ts >= self.cuts_before:
                continue
            if start_epoch <= ts < end_epoch:
                out.append({k: v for k, v in b.items()})
        return out


def _clean(bars: list[dict]) -> list[dict]:
    """Schema Atlas + ts estandarizado a str-epoch."""
    return [
        {
            "ts": str(int(b["ts"])),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": float(b.get("volume", 1000.0)),
        }
        for b in bars
    ]


def _bars(n_days: int, *, start: int = START, seed: int = 11) -> list[dict]:
    """Barras 1h continuas de `n_days` dias desde `start`."""
    bars = generate_ohlc_bars(
        n_bars=n_days * 24,
        start_epoch=start,
        interval_seconds=HOUR,
        seed=seed,
        drift=0.00003,
        volatility=0.003,
    )
    return _clean(bars)


def make_builder(*, start: int = START, end: int = END) -> FinalOosDatasetBuilder:
    return FinalOosDatasetBuilder(
        start_epoch=start, end_epoch=end, interval_seconds=HOUR, symbol="BTCUSDT"
    )


# ---------------------------------------------------------------------------
# Build feliz: daily consecutivos sobre un rango objetivo completo
# ---------------------------------------------------------------------------


def test_daily_consecutivos_producen_dataset_correcto(tmp_path):
    """365 dias de data continua cubren [start, end) sin parciales."""
    target_end = START + 365 * DAY
    builder = make_builder(end=target_end)
    builder.add_source("daily", StaticSource(_bars(365)), kind="daily")
    out = builder.build(tmp_path / "oos.csv", tmp_path / "oos.manifest.json")

    assert out["bars"] == 365 * 24
    assert out["deduplicated"] == 0
    assert out["partial_dropped"] == 0
    assert out["coverage"]["first_ts"] == START
    assert out["coverage"]["last_ts"] + HOUR == target_end
    assert out["sha256"]
    assert (tmp_path / "oos.csv").exists()
    assert (tmp_path / "oos.manifest.json").exists()


def test_salida_ordenada_por_ts(tmp_path):
    bars = _bars(3)
    shuffled = bars[30:] + bars[:30]
    builder = make_builder(end=START + 3 * DAY)
    builder.add_source("monthly", StaticSource(shuffled), kind="monthly")
    out = builder.build(tmp_path / "a.csv", tmp_path / "a.json")
    assert out["manifest_dict"]["first_ts"] == str(START)
    loaded_csv = (tmp_path / "a.csv").read_text(encoding="utf-8").splitlines()[1:]
    ts_list = [int(line.split(",")[0]) for line in loaded_csv]
    assert ts_list == sorted(ts_list)


def test_misma_entrada_mismo_dataset_y_sha(tmp_path):
    src = StaticSource(_bars(60))
    b1 = make_builder(end=START + 60 * DAY)
    b1.add_source("daily", src, kind="daily")
    out1 = b1.build(tmp_path / "r1.csv", tmp_path / "r1.json")

    b2 = make_builder(end=START + 60 * DAY)
    b2.add_source("daily", src, kind="daily")
    out2 = b2.build(tmp_path / "r2.csv", tmp_path / "r2.json")

    assert out1["sha256"] == out2["sha256"]
    assert out1["bars"] == out2["bars"]
    assert (tmp_path / "r1.csv").read_bytes() == (tmp_path / "r2.csv").read_bytes()


# ---------------------------------------------------------------------------
# Dedupe estricto
# ---------------------------------------------------------------------------


def test_dedupe_identico_registrado(tmp_path):
    bars = _bars(1)  # 24 velas
    duplicated = _clean(bars) + _clean([bars[0]])  # una vela repetida (misma fuente)
    builder = make_builder(end=START + DAY)
    builder.add_source("daily", StaticSource(duplicated), kind="daily")
    out = builder.build(tmp_path / "d.csv", tmp_path / "d.json")
    assert out["deduplicated"] == 1
    assert out["bars"] == 24


def test_conflicto_misma_ts_contenido_distinto_rechazado(tmp_path):
    bar = _clean(_bars(1))[0]
    conflicting = dict(bar)
    conflicting["close"] = float(bar["close"]) * 1.001
    builder = make_builder(end=START + DAY)
    builder.add_source("monthly", StaticSource([bar]), kind="monthly")
    builder.add_source("daily", StaticSource([conflicting]), kind="daily")
    with pytest.raises(DatasetBuilderError, match="conflicto"):
        builder.build(tmp_path / "c.csv", tmp_path / "c.json")


def test_merge_monthly_daily_solapados(tmp_path):
    """Solape controlado: misma vela en ambos -> dedupe; resto se combina."""
    bars = _bars(2)
    monthly = bars[:24]  # dia 1 completo
    daily = bars[12:]  # dia 1 (media) + dia 2 -> solape de 12 velas con monthly
    builder = make_builder(end=START + 2 * DAY)
    builder.add_source("monthly", StaticSource(monthly), kind="monthly")
    builder.add_source("daily", StaticSource(daily), kind="daily")
    out = builder.build(tmp_path / "m.csv", tmp_path / "m.json")
    assert out["bars"] == 48  # 2 dias completos, unicos
    assert out["deduplicated"] == 12


# ---------------------------------------------------------------------------
# Rechazos
# ---------------------------------------------------------------------------


def test_gap_implica_rechazo(tmp_path):
    bars = _bars(3)
    del bars[36]  # hueco de 1h dentro del rango
    builder = make_builder(end=START + 3 * DAY)
    builder.add_source("daily", StaticSource(bars), kind="daily")
    with pytest.raises(DatasetBuilderError, match="gap"):
        builder.build(tmp_path / "g.csv", tmp_path / "g.json")


def test_barra_parcial_excluida_y_cobertura_rechaza(tmp_path):
    """Velas que no han cerrado dentro del tramo (ts+interval > end) se excluyen.

    Caso real: el tramo objetivo termina a mitad de hora (end no alineado). La
    vela en curso ts+interval > end es PARCIAL -> se excluye por _drop_partial.
    Su ausencia deja un hueco al final -> el build se RE. El dataset nunca
    incorpora una vela que no ha cerrado.
    """
    bars = _bars(1)  # velas 0h..23h
    end = START + 23 * HOUR + 1800  # tramo hasta las 23:30: vela de 23h a medias
    builder = make_builder(end=end)
    builder.add_source("daily", StaticSource(bars), kind="daily")
    with pytest.raises(DatasetBuilderError, match="cobertura|vacía"):
        builder.build(tmp_path / "p.csv", tmp_path / "p.json")
    assert not (tmp_path / "p.csv").exists()


def test_intervalo_incorrecto_rechazado(tmp_path):
    """Barras mas espaciadas que el intervalo declarado => gap -> rechazo."""
    bars = _bars(3)
    sparse = _clean([bars[i] for i in range(0, len(bars), 2)])  # una cada 2h
    builder = make_builder(end=START + 3 * DAY)
    builder.add_source("daily", StaticSource(sparse), kind="daily")
    with pytest.raises(DatasetBuilderError, match="gap"):
        builder.build(tmp_path / "i.csv", tmp_path / "i.json")


def test_sin_fuentes_y_archivo_existente(tmp_path):
    builder = make_builder(end=START + DAY)
    with pytest.raises(DatasetBuilderError, match="sin fuentes"):
        builder.build(tmp_path / "x.csv", tmp_path / "x.json")

    existing = tmp_path / "e.csv"
    existing.write_text("x", encoding="utf-8")
    b2 = make_builder(end=START + 2 * DAY)
    b2.add_source("m", StaticSource(_bars(2)), kind="monthly")
    with pytest.raises(DatasetBuilderError, match="inmutable"):
        b2.build(existing, tmp_path / "e.json")


def test_rango_invalido_rechazado():
    with pytest.raises(DatasetBuilderError, match="end_epoch"):
        FinalOosDatasetBuilder(start_epoch=END, end_epoch=START)


# ---------------------------------------------------------------------------
# No toca registros reales (evidence/data_roles)
# ---------------------------------------------------------------------------


def test_builder_no_toca_evidence_ni_data_roles(tmp_path):
    before_evidence = _read(REAL_EVIDENCE)
    before_roles = _read(REAL_ROLES)
    builder = make_builder(end=START + 2 * DAY)
    builder.add_source("daily", StaticSource(_bars(2)), kind="daily")
    builder.build(tmp_path / "n.csv", tmp_path / "n.json")
    assert _read(REAL_EVIDENCE) == before_evidence
    assert _read(REAL_ROLES) == before_roles


def _read(path: str) -> bytes:
    from pathlib import Path

    if not Path(path).exists():
        return b""
    return Path(path).read_bytes()


# ---------------------------------------------------------------------------
# BinanceVisionDailySource (opener inyectable, sin red)
# ---------------------------------------------------------------------------


def _zip_of(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("klines.csv", "\n".join(rows))
    return buf.getvalue()


def test_daily_source_fetch_por_dias():
    calls: list[str] = []
    day0 = 1_788_220_800  # 2026-09-01

    def opener(url: str, kw: dict) -> bytes:
        calls.append(url)
        if "2026-09-02" in url:
            raise NotFoundError(f"No se pudo descargar {url}: HTTP 404 (no publicado)")
        base = day0 if "2026-09-01" in url else day0 + 24 * HOUR
        return _zip_of(
            [
                f"{base}000000,1,2,0.5,1.5,10",
                f"{base + HOUR}000000,1.5,2.5,1,2,11",
            ]
        )

    src = BinanceVisionDailySource(opener=opener)
    bars = src.fetch(day0, day0 + 48 * HOUR)
    assert len(bars) == 2  # solo el dia 1: el dia 2 da 404 (no publicado)
    assert any("2026-09-01" in c for c in calls)
    assert bars[0]["ts"] == str(day0)


def test_daily_source_fetch_dedupea_mismo_ts():
    ts = 1_788_220_800
    row = f"{ts}000000,1,2,0.5,1.5,10"

    def opener(url: str, kw: dict) -> bytes:
        return _zip_of([row, row])

    bars = BinanceVisionDailySource(opener=opener).fetch(ts, ts + HOUR)
    assert len(bars) == 1


def test_daily_source_normaliza_ms_y_us():
    ts_s = 1_788_220_800
    rows = [f"{ts_s}000,1,2,0.5,1.5,10", f"{ts_s}000000,1.5,2.5,1,2,11"]

    def opener(url: str, kw: dict) -> bytes:
        return _zip_of(rows)

    bars = BinanceVisionDailySource(opener=opener).fetch(ts_s, ts_s + HOUR)
    assert len(bars) == 1
    assert bars[0]["ts"] == str(ts_s)


# ---------------------------------------------------------------------------
# Determinacion de archivos requeridos
# ---------------------------------------------------------------------------


def test_required_files_monthly_priority():
    files = binsance_required_files("BTCUSDT", START, START + 62 * DAY)
    assert files["monthly"]
    assert files["daily"] == []  # rango 2026-09-01 -> 2026-11: solo mensuales
    joined = " ".join(files["monthly"])
    assert "BTCUSDT-1h-2026-09.zip" in joined
    assert "BTCUSDT-1h-2026-10.zip" in joined


def test_required_files_daily_fallback_sin_monthly():
    files = binsance_required_files(
        "BTCUSDT", START, START + 2 * DAY, monthly=False, daily=True
    )
    assert files["monthly"] == []
    assert len(files["daily"]) == 2
    assert files["daily"][0].endswith("BTCUSDT-1h-2026-09-01.zip")