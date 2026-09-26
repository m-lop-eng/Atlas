"""Abstracción de fuentes de datos de mercado (DataSource + DataManifest).

El backtester consume barras Atlas `{ts, open, high, low, close, volume}`
y NO necesita saber de dónde vienen. Cada proveedor declara un
`DataManifest` (snapshot con sha256 + rango) para reproducibilidad.

Proveedores (MVP → evolución):
    KrakenSource        : API pública de Kraken (profundidad ~30 días).
    BinanceVisionSource : dumps mensuales de klines 1h en data.binance.vision
                          (sin clave, sin geobloqueo) → histórico profundo.
    BinanceVisionDaily  : dumps DIARIOS de klines 1h (mismo proveedor); mecanismo
                          de adquisición alternativo al monthly (misma vela en ambos).
    CsvSource           : CSV local normalizado (datasets versionados/tests).
    ParquetSource       : futuro (requiere pyarrow como dependencia opcional).
"""

from __future__ import annotations

import gzip
import io
import json
import socket
import time
import urllib.error
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from data.synthetic import sha256_of_file, write_ohlc_csv

BINANCE_VISION_KLINE_URL = (
    "https://data.binance.vision/data/spot/monthly/klines/"
    "{symbol}/{interval}/{symbol}-{interval}-{year}-{month}.zip"
)

BINANCE_VISION_DAILY_KLINE_URL = (
    "https://data.binance.vision/data/spot/daily/klines/"
    "{symbol}/{interval}/{symbol}-{interval}-{date}.zip"
)


class DataFetchError(RuntimeError):
    """Cualquier fallo de red/servidor al descargar un input (operacional)."""


class NotFoundError(DataFetchError):
    """HTTP 404: el recurso (zip) aún no está publicado (benigno)."""


class DataTimeoutError(DataFetchError):
    """La conexión/lectura superó el timeout: nunca esperar indefinidamente."""


class DataConnectionError(DataFetchError):
    """No se pudo conectar al proveedor (DNS/refused/TLS/5xx)."""


def _sorted_unique_in_range(
    bars: list[dict],
    start_epoch: int,
    end_epoch: int,
) -> list[dict]:
    """Ordena por ts, elimina duplicados exactos y filtra al rango [start, end]."""
    seen: set[str] = set()
    unique: list[dict] = []
    for b in sorted(bars, key=lambda x: int(x["ts"])):
        if b["ts"] not in seen:
            seen.add(b["ts"])
            unique.append(b)
    return [
        b for b in unique if start_epoch <= int(b["ts"]) <= end_epoch
    ]


def _http_get_bytes(
    url: str,
    timeout: int = 60,
    retries: int = 3,
    wait: float = 1.0,
) -> bytes:
    """Descarga bytes con timeout EXPLÍCITO y errores TIPIFICADOS.

    - HTTP 404 -> `NotFoundError` (recurso no publicado; se lanza al instante,
      sin reintentar: un 404 no se convierte en éxito con retries).
    - timeout -> `DataTimeoutError` (la conexión/lectura sobrepasa `timeout`).
    - resto (DNS, refused, TLS, 5xx, corte) -> `DataConnectionError`.
    - Nunca deja el proceso esperando indefinidamente: cada intento está
      acotado por `timeout`, el total por `retries`.
    """
    last: Exception | None = None
    timed_out = False
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - red: clasificar y reintentar
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise NotFoundError(
                    f"No se pudo descargar {url}: HTTP 404 (no publicado)"
                ) from exc
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                timed_out = True
            if attempt < retries - 1:
                time.sleep(wait)
    if timed_out:
        raise DataTimeoutError(
            f"No se pudo descargar {url}: timeout tras {timeout}s x {retries}"
        ) from last
    raise DataConnectionError(f"No se pudo descargar {url}: {last}") from last


@dataclass(frozen=True, slots=True)
class DataManifest:
    """Identidad reproducible de un snapshot de datos (para versionar).

    `sha256` ancla el contenido del CSV real; `params` guarda los parámetros
    de la descarga (aunque la fuente cambie, el manifest fija este snapshot).
    """

    source: str
    pair: str
    interval_seconds: int
    bars: int
    first_ts: str
    last_ts: str
    sha256: str
    params: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "DataManifest":
        return DataManifest(**data)


class DataSource(ABC):
    """Fuente de barras Atlas. `fetch()` devuelve barras ordenadas por ts."""

    @abstractmethod
    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        """Barras `{ts:str, open, high, low, close, volume}` en [start, end]."""


class KrakenSource(DataSource):
    def __init__(
        self,
        pair: str = "XXBTZUSD",
        interval: int = 60,
        timeout: int = 30,
    ) -> None:
        from data.market import fetch_kraken_ohlc

        self._fetch = fetch_kraken_ohlc
        self.pair = pair
        self.interval = interval
        self.timeout = timeout

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        bars = self._fetch(
            pair=self.pair, interval=self.interval, since=start_epoch,
            timeout=self.timeout,
        )
        return [
            b for b in bars if start_epoch <= int(b["ts"]) <= end_epoch
        ]


class BinanceVisionSource(DataSource):
    """Klines spot 1h desde los dumps mensuales (data.binance.vision).

    Descarga por meses, filtra por rango y normaliza a barras Atlas.
    `opener` inyectable permite testear el parseo sin red.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        url_template: str = BINANCE_VISION_KLINE_URL,
        opener: Callable[[str, dict[str, Any]], bytes] | None = None,
    ) -> None:
        self.symbol = symbol
        self.interval = interval
        self.url_template = url_template
        self._opener = (
            opener if opener is not None else lambda url, kw: _http_get_bytes(url, **kw)
        )

    def _month_url(self, year: int, month: int) -> str:
        return self.url_template.format(
            symbol=self.symbol,
            interval=self.interval,
            year=year,
            month=f"{month:02d}",
        )

    @staticmethod
    def _normalize_ts(text: str) -> str:
        """open_time de Binance Vision: pasa a epoch-s.

        El formato cambió en 2025: hasta 2024-12 los dumps usan milisegundos,
        desde 2025-01 microsegundos. Ambos se normalizan a segundos.
        """
        t = text.strip()
        if not t.isdigit():
            return t
        v = int(t)
        if v >= 10**15:  # microsegundos
            v //= 10**6
        elif v >= 10**12:  # milisegundos
            v //= 10**3
        return str(v)

    @staticmethod
    def _parse_zip(raw: bytes) -> list[dict]:
        """Parsea un zip de klines mensuales a barras Atlas."""
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8")
                bars: list[dict] = []
                for line in text:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) < 6:
                        continue
                    bars.append(
                        {
                            "ts": BinanceVisionSource._normalize_ts(parts[0]),
                            "open": float(parts[1]),
                            "high": float(parts[2]),
                            "low": float(parts[3]),
                            "close": float(parts[4]),
                            "volume": float(parts[5]),
                        }
                    )
        return bars

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        """Descarga todos los meses en [start_epoch, end_epoch] y concatena."""
        out: list[dict] = []
        year, month = datetime.fromtimestamp(start_epoch, tz=timezone.utc).year, \
            datetime.fromtimestamp(start_epoch, tz=timezone.utc).month
        while True:
            try:
                raw = self._opener(self._month_url(year, month), {"timeout": 90, "retries": 3})
            except NotFoundError:
                break  # mes en curso: dump aún no publicado
            out.extend(self._parse_zip(raw))
            month += 1
            if month > 12:
                year, month = year + 1, 1
            if year * 12 + month > (
                datetime.fromtimestamp(end_epoch, tz=timezone.utc).year * 12
                + datetime.fromtimestamp(end_epoch, tz=timezone.utc).month
            ):
                break
        seen: set[str] = set()
        unique: list[dict] = []
        for b in sorted(out, key=lambda x: int(x["ts"])):
            if b["ts"] not in seen:
                seen.add(b["ts"])
                unique.append(b)
        return [
            b for b in unique if start_epoch <= int(b["ts"]) <= end_epoch
        ]


class BinanceVisionDailySource(DataSource):
    """Klines spot 1h desde los dumps DIARIOS (data.binance.vision).

    `https://data.binance.vision/data/spot/daily/klines/{symbol}/{interval}/...`
    Descarga un zip por dia (YYYY-MM-DD), filtra por rango y normaliza a barras
    Atlas. Es un mecanismo de adquisicion alternativo al monthly (misma vela
    puede estar en ambos): el merge/dedupe lo resuelve la capa de dataset
    (FinalOOSDatasetBuilder), no la fuente.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        url_template: str = BINANCE_VISION_DAILY_KLINE_URL,
        opener: Callable[[str, dict[str, Any]], bytes] | None = None,
    ) -> None:
        self.symbol = symbol
        self.interval = interval
        self.url_template = url_template
        self._opener = (
            opener if opener is not None else lambda url, kw: _http_get_bytes(url, **kw)
        )

    def _day_url(self, year: int, month: int, day: int) -> str:
        return self.url_template.format(
            symbol=self.symbol,
            interval=self.interval,
            date=f"{year:04d}-{month:02d}-{day:02d}",
        )

    def _days(self, start_epoch: int, end_epoch: int):
        start_dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_epoch, tz=timezone.utc)
        day = start_dt.date()
        last = end_dt.date()
        while day <= last:
            yield day.year, day.month, day.day
            day = day + timedelta(days=1)

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        """Descarga cada dia en [start_epoch, end_epoch] y concatena.

        Un 404 se interpreta como 'dia aún no publicado' y detiene la iteracion
        (igual que monthly); el rango efectivo lo valida la calidad del dataset.
        """
        out: list[dict] = []
        for year, month, day in self._days(start_epoch, end_epoch):
            try:
                raw = self._opener(
                    self._day_url(year, month, day), {"timeout": 90, "retries": 3}
                )
            except NotFoundError:
                break  # dia en curso: dump aún no publicado
            out.extend(BinanceVisionSource._parse_zip(raw))
        return _sorted_unique_in_range(out, start_epoch, end_epoch)


class CsvSource(DataSource):
    """CSV local normalizado `ts,open,high,low,close,volume`."""

    def __init__(self, path: str | Path, skip_rows: int = 1) -> None:
        self.path = Path(path)
        self.skip_rows = skip_rows

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        raw = (
            self.path.read_text(encoding="utf-8").strip().splitlines()
        )[self.skip_rows :]
        bars: list[dict] = []
        for line in raw:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            ts = parts[0]
            if ts.isdigit():
                ets = int(ts)
            else:
                ets = int(
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    .timestamp()
                )
            if start_epoch <= ets <= end_epoch:
                bars.append(
                    {
                        "ts": str(ets),
                        "open": float(parts[1]),
                        "high": float(parts[2]),
                        "low": float(parts[3]),
                        "close": float(parts[4]),
                        "volume": float(parts[5]),
                    }
                )
        return bars


class ParquetSource(DataSource):
    """Futuro proveedor Parquet (dependencia opcional pyarrow)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        try:
            import pyarrow.parquet as pq  # type: ignore[import-untyped]
        except ImportError as exc:
            raise NotImplementedError(
                "ParquetSource requiere pyarrow (dependencia opcional)"
            ) from exc
        table = pq.read_table(self.path)
        rows = table.to_pylist()
        return [
            {
                "ts": str(row["ts"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0)),
            }
            for row in rows
            if start_epoch <= int(row["ts"]) <= end_epoch
        ]


SOURCES = {
    "kraken": KrakenSource,
    "binance": BinanceVisionSource,
    "binance_daily": BinanceVisionDailySource,
    "csv": CsvSource,
    "parquet": ParquetSource,
}


def write_dataset(
    bars: list[dict],
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    source: str,
    pair: str,
    interval_seconds: int,
    params: dict[str, Any] | None = None,
) -> DataManifest:
    """Persiste barras + manifest (CSV gitignored, manifest versionado)."""
    if not bars:
        raise ValueError("no hay barras que persistir")
    csv_path = Path(csv_path)
    write_ohlc_csv(bars, csv_path, include_volume=True)
    manifest = DataManifest(
        source=source,
        pair=pair,
        interval_seconds=interval_seconds,
        bars=len(bars),
        first_ts=bars[0]["ts"],
        last_ts=bars[-1]["ts"],
        sha256=sha256_of_file(csv_path),
        params=dict(params or {}),
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
    )
    return manifest