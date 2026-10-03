"""Adquisición de datos de mercado real (DataSource + DataManifest).

CLI:
    python data/market.py kraken  [pair] [interval] [since_iso]
    python data/market.py binance [symbol] [start_iso] [end_iso]

Compatibilidad: `fetch_kraken_ohlc`, `epoch_of` y `write_market_dataset` se
mantienen como API estable (proveedor Kraken) para tests/CLI legados; toda
la lógica de proveedores vive en `data/sources.py`.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from data.sources import (
    BinanceVisionSource,
    KrakenSource,
    write_dataset,
)
from data.synthetic import sha256_of_file, write_ohlc_csv

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"


def fetch_kraken_ohlc(
    pair: str = "XXBTZUSD",
    interval: int = 60,
    since: int | None = None,
    *,
    timeout: int = 30,
) -> list[dict]:
    """Descarga OHLCV de Kraken y devuelve barras Atlas (ts en string epoch).

    Raises:
        RuntimeError: si la API responde con error o sin datos.
    """
    params: dict[str, Any] = {"pair": pair, "interval": interval}
    if since is not None:
        params["since"] = since
    url = f"{KRAKEN_OHLC_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)

    errors = payload.get("error", [])
    if errors:
        raise RuntimeError(f"Kraken OHLC error: {errors}")
    result = payload.get("result", {})
    keys = [k for k in result if k != "last"]
    if not keys:
        raise RuntimeError("Kraken OHLC sin datos en la respuesta")
    rows = result[keys[0]]

    bars: list[dict] = []
    for row in rows:
        bars.append(
            {
                "ts": str(int(row[0])),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[6]),
            }
        )
    bars.sort(key=lambda b: int(b["ts"]))
    return bars


def epoch_of(iso: str) -> int:
    """Convierte 'YYYY-MM-DD[ HH:MM:SS]' (UTC) a epoch."""
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def write_market_dataset(
    bars: list[dict],
    csv_path: str | Path,
    manifest_path: str | Path,
    *,
    pair: str,
    interval: int,
    since: int,
    source: str = "kraken/public/ohlc",
) -> None:
    """Persiste el CSV real y un manifest con integridad (sha256)."""
    csv_path = Path(csv_path)
    write_ohlc_csv(bars, csv_path, include_volume=True)
    manifest = {
        "source": source,
        "pair": pair,
        "interval_seconds": interval,
        "since": since,
        "until": int(bars[-1]["ts"]),
        "bars": len(bars),
        "csv_path": str(csv_path),
        "sha256": sha256_of_file(csv_path),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "binance":
        return _binance_main(argv[1:])
    return _kraken_main(argv)


def _kraken_main(argv: list[str]) -> int:
    pair = argv[0] if argv else "XXBTZUSD"
    interval = int(argv[1]) if len(argv) > 1 else 60
    since_iso = argv[2] if len(argv) > 2 else "2026-08-01T00:00:00"
    since = epoch_of(since_iso)

    root = Path(__file__).resolve().parents[1]
    bars = fetch_kraken_ohlc(pair=pair, interval=interval, since=since)
    out_csv = root / "experiments" / "data" / f"market_{pair}_{interval}min.csv"
    manifest = root / "experiments" / "data" / f"market_{pair}_{interval}min.manifest.json"
    write_market_dataset(
        bars, out_csv, manifest, pair=pair, interval=interval, since=since
    )
    print(f"descargadas {len(bars)} barras de {pair} ({interval} min)")
    print(f"rango       : {bars[0]['ts']} -> {bars[-1]['ts']}")
    print(f"csv         : {out_csv}")
    print(f"manifest    : {manifest}")
    return 0


def _binance_main(argv: list[str]) -> int:
    """`python data/market.py binance [symbol] [start_iso] [end_iso]`."""
    symbol = argv[0] if argv else "BTCUSDT"
    start_iso = argv[1] if len(argv) > 1 else "2019-01-01"
    end_iso = argv[2] if len(argv) > 2 else datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    start = epoch_of(start_iso)
    end = epoch_of(end_iso)
    if end <= start:
        raise ValueError(f"end ({end}) debe ser > start ({start})")

    root = Path(__file__).resolve().parents[1]
    bars = BinanceVisionSource(symbol=symbol).fetch(start, end)
    out_csv = root / "experiments" / "data" / f"market_{symbol}_{int(3600 / 60)}min.csv"
    manifest = root / "experiments" / "data" / (
        f"market_{symbol}_{int(3600 / 60)}min.manifest.json"
    )
    write_dataset(
        bars,
        out_csv,
        manifest,
        source="binance/vision/klines-1h",
        pair=symbol,
        interval_seconds=3600,
        params={"symbol": symbol, "interval": "1h", "start": start_iso, "end": end_iso},
    )
    print(f"descargadas {len(bars)} barras de {symbol} 1h ({start_iso} -> {end_iso})")
    print(f"rango       : {bars[0]['ts']} -> {bars[-1]['ts']}")
    print(f"csv         : {out_csv}")
    print(f"manifest    : {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())