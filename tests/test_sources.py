"""Tests de DataSource / DataManifest (sin red salvo petición explícita)."""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile

import pytest

from data.market import epoch_of
from data.sources import (
    BinanceVisionSource,
    CsvSource,
    DataConnectionError,
    DataManifest,
    DataTimeoutError,
    NotFoundError,
    _http_get_bytes,
    write_dataset,
)
from data.synthetic import write_ohlc_csv


def _zip_of(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("klines.csv", "\n".join(rows))
    return buf.getvalue()


def test_normalize_ts_units() -> None:
    n = BinanceVisionSource._normalize_ts
    assert n("1546300800") == "1546300800"  # segundos
    assert n("1546300800000") == "1546300800"  # milisegundos
    assert n("1546300800000000") == "1546300800"  # microsegundos (2025+)


def test_parse_zip_mixed_units() -> None:
    raw = _zip_of([
        "1546300800000,1,2,0.5,1.5,10",   # ms
        "1546304400000000,1.5,2.5,1,2,11",  # us
    ])
    bars = BinanceVisionSource._parse_zip(raw)
    assert [b["ts"] for b in bars] == ["1546300800", "1546304400"]
    assert bars[1]["close"] == 2.0


def test_fetch_concatenates_and_filters(monkeypatch) -> None:
    def opener(url: str, kw: dict) -> bytes:
        if "2025-03" in url:
            raise NotFoundError(f"No se pudo descargar {url}: HTTP 404 (no publicado)")
        month = int(url.rsplit("-", 1)[1].split(".")[0])
        base = 1735689600 + (month - 1) * 31 * 24 * 3600
        return _zip_of([
            f"{base}000,1,2,0.5,1.5,10",
            f"{base + 3600}000,1.5,2.5,1,2,11",
        ])

    src = BinanceVisionSource(opener=opener)
    bars = src.fetch(epoch_of("2025-01-01"), epoch_of("2025-04-01"))
    assert len(bars) == 4
    assert len(set(b["ts"] for b in bars)) == 4


def test_fetch_dedupes(monkeypatch) -> None:
    dup = "1735689600000000,1,2,0.5,1.5,10"

    def opener(url: str, kw: dict) -> bytes:
        return _zip_of([dup, dup])

    bars = BinanceVisionSource(opener=opener).fetch(
        epoch_of("2025-01-01"), epoch_of("2025-02-01")
    )
    assert len(bars) == 1


def test_csv_source_filters_range(tmp_path) -> None:
    bars = [
        {"ts": "100", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5},
        {"ts": "200", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 6},
    ]
    csv = tmp_path / "x.csv"
    write_ohlc_csv(bars, csv, include_volume=True)
    out = CsvSource(csv).fetch(150, 250)
    assert [b["ts"] for b in out] == ["200"]
    assert out[0]["volume"] == 6.0


def test_manifest_roundtrip_and_write(tmp_path) -> None:
    bars = [
        {"ts": "100", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5},
    ]
    csv = tmp_path / "d.csv"
    manifest = tmp_path / "d.json"
    m = write_dataset(
        bars, csv, manifest, source="csv", pair="X", interval_seconds=60
    )
    loaded = DataManifest.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
    assert loaded == m
    assert loaded.bars == 1
    assert loaded.sha256


# ---------------------------------------------------------------------------
# _http_get_bytes: timeout EXPLICITO y errores TIPIFICADOS (sin red)
# ---------------------------------------------------------------------------


def test_http_get_404_es_notfound_sin_reintentar(monkeypatch) -> None:
    calls: list[int] = []

    def fake_urlopen(url: str, timeout: int):
        calls.append(timeout)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(NotFoundError):
        _http_get_bytes("https://x/a.zip", timeout=7, retries=3)
    assert calls == [7], "un 404 NO se reintenta: no se convierte en exito"


def test_http_get_timeout_clasifica_timeout(monkeypatch) -> None:
    calls: list[int] = []

    def fake_urlopen(url: str, timeout: int):
        calls.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DataTimeoutError, match="timeout"):
        _http_get_bytes("https://x/a.zip", timeout=5, retries=2, wait=0)
    assert len(calls) == 2, "timeout es transitorio: se reintenta y luego aborta"


def test_http_get_conexion_clasifica_conexion(monkeypatch) -> None:
    err = urllib.error.URLError(ConnectionRefusedError("refused"))

    def fake_urlopen(url: str, timeout: int):
        raise err

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DataConnectionError):
        _http_get_bytes("https://x/a.zip", timeout=5, retries=2, wait=0)


def test_http_get_respuesta_valida_devuelve_bytes(monkeypatch) -> None:
    body = b"klines"

    def fake_urlopen(url: str, timeout: int):
        return io.BytesIO(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _http_get_bytes("https://x/a.zip", timeout=5, retries=2, wait=0) == body