"""Generación de datos sintéticos deterministas (sin pandas).

Serie GBM discreta: suficiente para probar el ciclo completo Atlas.
Con el mismo `seed`, `n_bars` e `interval_seconds` la salida es idéntica
(reproducibilidad del experimento).
"""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path


def generate_ohlc_bars(
    *,
    n_bars: int = 720,
    start_price: float = 100.0,
    start_epoch: int = 1_700_000_000,
    interval_seconds: int = 3_600,
    seed: int = 42,
    drift: float = 0.00005,
    volatility: float = 0.002,
    min_price: float = 1.0,
) -> list[dict]:
    """Genera barras OHLC deterministas.

    Modelo: chaque barra abre = cierre anterior; rango intrabar
    proporcional; cierre = open * (1 + drift + vol * z).

    Raises:
        ValueError: si la configuración no permite generar barras (n=0, vol ruido).
    """
    if n_bars < 1:
        raise ValueError("n_bars debe ser >= 1")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds debe ser positivo")
    if min_price <= 0 or start_price <= 0:
        raise ValueError("precios deben ser positivos")

    rng = random.Random(seed)
    bars: list[dict] = []
    price = start_price

    for i in range(n_bars):
        open_price = price
        z = rng.gauss(0.0, 1.0)
        close = max(min_price, open_price * (1.0 + drift + volatility * z))

        spread = abs(close - open_price) * (0.2 + 0.3 * rng.random())
        wick_up = spread * (0.5 + 0.5 * rng.random())
        wick_down = spread * (0.5 + 0.5 * rng.random())

        high = max(open_price, close) + wick_up
        low = max(min_price, min(open_price, close) - wick_down)

        bars.append(
            {
                "ts": str(start_epoch + i * interval_seconds),
                "open": round(open_price, 6),
                "high": round(high, 6),
                "low": round(low, 6),
                "close": round(close, 6),
            }
        )
        price = close

    return bars


def write_ohlc_csv(
    bars: list[dict],
    path: str | Path,
    *,
    include_volume: bool = False,
) -> Path:
    """Escribe barras a CSV con la cabecera estándar de Atlas."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["ts", "open", "high", "low", "close"] + (
        ["volume"] if include_volume else []
    )
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for bar in bars:
            row = {c: bar.get(c) for c in columns}
            writer.writerow(row)
    return path


def sha256_of_file(path: str | Path) -> str:
    """Hash del dataset para el lineage (reproducibilidad)."""
    import hashlib

    path = Path(path)
    digest = hashlib.sha256(path.read_bytes())
    return digest.hexdigest()