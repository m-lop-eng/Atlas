"""Tests deterministas de S8: fuente REST read-only + provider de backfill.

Sin red: el `opener` inyectado simula respuestas/errores del endpoint publico.
Cubren: normalizacion/filtrado, respuesta vacia, vela no cerrada, alineacion,
malformado, timeout->ConnectionError, retry acotado/recuperacion, 404 sin
reintento, rate-limit, mapeo del provider, sin barras sinteticas y sin
credenciales/ordenes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from data.binance_rest import BinanceRestKlinesSource
from data.sources import (
    DataConnectionError,
    DataFetchError,
    DataTimeoutError,
    NotFoundError,
)
from papertrading.streaming_backfill_rest import RestStreamingBackfillProvider

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"
NOW = BASE + 2 * HOUR + 100  # las velas <= BASE+HOUR estan cerradas


def _row(open_s: int, *, o=100.0, h=105.0, l=99.0, c=104.0, v=10.0):
    ms = open_s * 1000
    return [ms, f"{o}", f"{h}", f"{l}", f"{c}", f"{v}", ms + HOUR * 1000 - 1]


def _bytes(rows) -> bytes:
    return json.dumps(rows).encode("utf-8")


class _ScriptOpener:
    def __init__(self, actions) -> None:
        self._actions = list(actions)
        self.calls = 0

    def __call__(self, url, timeout):
        self.calls += 1
        action = self._actions[min(self.calls - 1, len(self._actions) - 1)]
        if isinstance(action, BaseException):
            raise action
        return action


def _source(opener, *, retries=1, max_pages=8):
    return BinanceRestKlinesSource(
        symbol=SYMBOL,
        interval_seconds=HOUR,
        opener=opener,
        retries=retries,
        wait=0.0,
        max_pages=max_pages,
        clock=lambda: NOW,
        sleep=lambda _s: None,
    )


class _FakeSource:
    def __init__(self, bars=None, error=None) -> None:
        self._bars = list(bars or [])
        self._error = error

    def fetch(self, start, end):
        if self._error is not None:
            raise self._error
        return [b for b in self._bars if start <= int(b["ts"]) <= end]


# --------------------------------------------------------------- fuente


def test_fetch_normaliza_y_filtra_rango():
    rows = [
        _row(BASE - HOUR),  # fuera de rango
        _row(BASE),
        _row(BASE + HOUR),
        _row(BASE + 2 * HOUR),  # aun abierta (NOW < BASE+3H)
    ]
    src = _source(_ScriptOpener([_bytes(rows)]))
    bars = src.fetch(BASE, BASE + 2 * HOUR)
    assert [b["ts"] for b in bars] == [str(BASE), str(BASE + HOUR)]
    assert bars[0] == {
        "ts": str(BASE),
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 104.0,
        "volume": 10.0,
    }


def test_fetch_respuesta_vacia():
    src = _source(_ScriptOpener([b"[]"]))
    assert src.fetch(BASE, BASE + 2 * HOUR) == []


def test_fetch_vela_no_cerrada_descartada():
    rows = [_row(BASE), _row(BASE + 2 * HOUR)]  # BASE+2H aun abierta (NOW=BASE+2H+100)
    src = _source(_ScriptOpener([_bytes(rows)]))
    bars = src.fetch(BASE, BASE + 3 * HOUR)
    assert [b["ts"] for b in bars] == [str(BASE)]


def test_fetch_desalineacion_descartada():
    rows = [_row(BASE + 1_800)]
    src = _source(_ScriptOpener([_bytes(rows)]))
    assert src.fetch(BASE, BASE + 2 * HOUR) == []


def test_fetch_malformado_raises_datafetcherror():
    src = _source(_ScriptOpener([_bytes([[1, 2]])]))
    with pytest.raises(DataFetchError):
        src.fetch(BASE, BASE + HOUR)


def test_fetch_no_json_raises_datafetcherror():
    src = _source(_ScriptOpener([b"<html>error</html>"]))
    with pytest.raises(DataFetchError):
        src.fetch(BASE, BASE + HOUR)


def test_fetch_rango_invalido_vacio():
    src = _source(_ScriptOpener([_bytes([_row(BASE)])]))
    assert src.fetch(BASE, BASE) == []


def test_timeout_retries_acotado_y_falla():
    opener = _ScriptOpener([DataTimeoutError("t1"), DataTimeoutError("t2")])
    src = _source(opener, retries=2)
    with pytest.raises(DataTimeoutError):
        src.fetch(BASE, BASE + HOUR)
    assert opener.calls == 2


def test_retry_con_recuperacion():
    rows = [_row(BASE)]
    opener = _ScriptOpener(
        [DataTimeoutError("t1"), DataTimeoutError("t2"), _bytes(rows)]
    )
    src = _source(opener, retries=3, max_pages=1)
    bars = src.fetch(BASE, BASE + HOUR)
    assert [b["ts"] for b in bars] == [str(BASE)]
    assert opener.calls == 3


def test_rate_limit_retry_acotado():
    opener = _ScriptOpener([DataConnectionError("HTTP 429")])
    src = _source(opener, retries=3)
    with pytest.raises(DataConnectionError):
        src.fetch(BASE, BASE + HOUR)
    assert opener.calls == 3  # acotado, sin loop infinito


def test_not_found_no_reintenta():
    opener = _ScriptOpener([NotFoundError("404")])
    src = _source(opener, retries=3)
    with pytest.raises(NotFoundError):
        src.fetch(BASE, BASE + HOUR)
    assert opener.calls == 1


# --------------------------------------------------------------- provider


def _provider(source) -> RestStreamingBackfillProvider:
    return RestStreamingBackfillProvider(source, clock=lambda: NOW)


def _atlas(ts: int, *, o=100.0, h=105.0, l=99.0, c=104.0, v=1.0) -> dict:
    return {"ts": str(ts), "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_provider_mapea_barras():
    src = _FakeSource([_atlas(BASE), _atlas(BASE + HOUR)])
    out = _provider(src).fetch_closed_bars(SYMBOL, HOUR, BASE)
    assert [e.open_time for e in out] == [BASE, BASE + HOUR]
    assert all(e.is_closed and e.symbol == SYMBOL for e in out)
    assert out[0].open == 100.0 and out[0].close == 104.0 and out[0].volume == 1.0


def test_provider_filtra_fuera_de_rango_y_desalineadas():
    src = _FakeSource(
        [
            _atlas(BASE - HOUR),  # < start
            _atlas(BASE + 1_800),  # desalineada
            _atlas(BASE + HOUR),  # valida
        ]
    )
    out = _provider(src).fetch_closed_bars(SYMBOL, HOUR, BASE + HOUR)
    assert [e.open_time for e in out] == [BASE + HOUR]


def test_provider_datafetcherror_a_connectionerror():
    src = _FakeSource(error=DataTimeoutError("timeout"))
    with pytest.raises(ConnectionError):
        _provider(src).fetch_closed_bars(SYMBOL, HOUR, BASE)


def test_provider_404_a_connectionerror():
    src = _FakeSource(error=NotFoundError("404"))
    with pytest.raises(ConnectionError):
        _provider(src).fetch_closed_bars(SYMBOL, HOUR, BASE)


def test_provider_respuesta_vacia_en_rango():
    out = _provider(_FakeSource([])).fetch_closed_bars(SYMBOL, HOUR, BASE)
    assert out == []


# --------------------------------------------------------------- source-scan


def test_source_scan_sin_sinteticas():
    src = (ROOT / "data" / "binance_rest.py").read_text(encoding="utf-8")
    for pattern in (r"random", r"synthetic", r"interpolat", r"fabricat"):
        assert re.search(pattern, src, re.IGNORECASE) is None, pattern
