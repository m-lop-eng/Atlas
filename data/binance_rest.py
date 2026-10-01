"""Fuente read-only de klines por REST publico de Binance (S8, backfill oportuno).

SOLO LECTURA: endpoint publico `/api/v3/klines`, sin autenticacion, sin claves,
sin endpoints de ordenes. Su unico proposito es alimentar el backfill de S4
(`IntegrityReconciler.reconcile_open_ended`) para cubrir gaps recientes que los
dumps de binance.vision (~1 dia de retraso) no pueden recuperar a tiempo.

No fabrica barras: devuelve exclusivamente lo que publica el endpoint, filtrado
a vela cerrada, alineada y dentro del rango. Cualquier fallo se propaga como
`DataFetchError` (tipificado en `data.sources`) para que el provider S8 lo
convierta en `ConnectionError` y S4 lo marque BLOCKED (fail-safe).

Reutiliza la clasificacion de errores y el patron `opener` inyectable ya
existentes en `data/sources.py`; no introduce dependencias nuevas (urllib/std).
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Callable

from data.sources import DataFetchError, NotFoundError, _http_get_bytes

BINANCE_REST_BASE = "https://api.binance.com"
BINANCE_KLINES_PATH = "/api/v3/klines"
BINANCE_KLINES_URL = BINANCE_REST_BASE + BINANCE_KLINES_PATH


def _default_opener(url: str, timeout: float) -> bytes:
    """Un intento HTTP con errores tipificados (reutiliza la logica existente)."""
    return _http_get_bytes(url, timeout=timeout, retries=1, wait=0.0)


class BinanceRestKlinesSource:
    """Klines spot por REST publico (read-only), normalizadas a barras Atlas.

    Args:
        symbol / interval_seconds: instrumento y temporalidad (p. ej. BTCUSDT/3600).
        interval: nombre de intervalo del proveedor; si None se deriva de
            `interval_to_binance(interval_seconds)`.
        base_url: host (mismo venue que el WebSocket).
        opener: `(url, timeout) -> bytes`; un solo intento, errores tipificados.
            Inyectable para tests deterministas sin red.
        timeout: plazo por request (segundos).
        retries: intentos totales por pagina (>= 1). Bounded, sin loops infinitos.
        wait: espera base entre reintentos (backoff lineal).
        limit: tamano de pagina del endpoint (max 1000).
        max_pages: cota dura de paginacion.
        min_request_interval: espera minima entre paginas (rate limiting).
        clock / sleep: inyectables para determinismo.
    """

    def __init__(
        self,
        *,
        symbol: str = "BTCUSDT",
        interval_seconds: int = 3600,
        interval: str | None = None,
        base_url: str = BINANCE_REST_BASE,
        opener: Callable[[str, float], bytes] | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        wait: float = 1.0,
        limit: int = 1000,
        max_pages: int = 8,
        min_request_interval: float = 0.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds debe ser > 0")
        if retries < 1:
            raise ValueError("retries debe ser >= 1")
        if limit < 1 or limit > 1000:
            raise ValueError("limit debe estar en [1, 1000]")
        if max_pages < 1:
            raise ValueError("max_pages debe ser >= 1")
        if interval is None:
            from streaming import interval_to_binance

            interval = interval_to_binance(interval_seconds)
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.interval = interval
        self.base_url = base_url.rstrip("/")
        self._opener = opener if opener is not None else _default_opener
        self._timeout = timeout
        self._retries = retries
        self._wait = wait
        self._limit = limit
        self._max_pages = max_pages
        self._min_request_interval = min_request_interval
        self._clock = clock
        self._sleep = sleep

    # ------------------------------------------------------------- consulta

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        """Barras Atlas `{ts, open, high, low, close, volume}` en [start, end].

        Solo velas CERRADAS, alineadas al intervalo y dentro del rango; ordenadas
        y sin duplicados. Respuesta vacia -> lista vacia (fail-safe aguas abajo).
        """
        start = int(start_epoch)
        end = int(end_epoch)
        if end <= start:
            return []
        now = self._clock()
        by_ts: dict[int, dict] = {}
        cursor = start
        last_open: int | None = None
        pages = 0
        while cursor <= end and pages < self._max_pages:
            rows = self._request_page(cursor, end)
            if not rows:
                break
            max_open: int | None = None
            for row in rows:
                open_epoch, bar = self._parse_row(row)
                if max_open is None or open_epoch > max_open:
                    max_open = open_epoch
                if not (start <= open_epoch <= end):
                    continue
                if open_epoch % self.interval_seconds != 0:
                    continue
                if open_epoch + self.interval_seconds > now:
                    continue  # vela aun no cerrada
                by_ts[open_epoch] = bar
            if max_open is None or max_open == last_open:
                break
            last_open = max_open
            if max_open + self.interval_seconds > end:
                break
            cursor = max_open + self.interval_seconds
            pages += 1
            if self._min_request_interval > 0:
                self._sleep(self._min_request_interval)
        return [by_ts[ts] for ts in sorted(by_ts)]

    # ------------------------------------------------------------- interno

    def _request_page(self, start: int, end: int) -> list[Any]:
        params = urllib.parse.urlencode(
            {
                "symbol": self.symbol,
                "interval": self.interval,
                "startTime": start * 1000,
                "endTime": end * 1000,
                "limit": self._limit,
            }
        )
        url = f"{self.base_url}{BINANCE_KLINES_PATH}?{params}"
        raw = self._request(url)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise DataFetchError(
                f"klines REST: respuesta no es JSON valido: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise DataFetchError("klines REST: respuesta no es una lista")
        return data

    def _request(self, url: str) -> bytes:
        """Un request con reintentos acotados; 404 no se reintenta."""
        last: DataFetchError | None = None
        for attempt in range(self._retries):
            try:
                return self._opener(url, self._timeout)
            except NotFoundError:
                raise
            except DataFetchError as exc:
                last = exc
                if attempt < self._retries - 1:
                    self._sleep(self._wait * (attempt + 1))
        assert last is not None
        raise last

    def _parse_row(self, row: Any) -> tuple[int, dict]:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise DataFetchError(f"klines REST: fila invalida {row!r}")
        try:
            open_ms = int(row[0])
            open_ = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError) as exc:
            raise DataFetchError(f"klines REST: fila no numerica {row!r}") from exc
        open_epoch = open_ms // 1000
        return open_epoch, {
            "ts": str(open_epoch),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }


__all__ = [
    "BINANCE_KLINES_PATH",
    "BINANCE_KLINES_URL",
    "BINANCE_REST_BASE",
    "BinanceRestKlinesSource",
]
