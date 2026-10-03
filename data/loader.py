"""Carga de datos OHLC desde CSV (00_MASTER_SPECIFICATION.md §10).

El estándar de entrada para investigación es CSV plano:
    ts,open,high,low,close[,volume]

- Timestamps normalizados a string ISO/epoch; el orden cronológico es
  obligatorio (el motor de backtest asume barras ordenadas).
- Errores de formato son fatales: nunca se silencian filas corruptas.
"""

from __future__ import annotations

import csv
from pathlib import Path

REQUIRED_COLUMNS = ("ts", "open", "high", "low", "close")


class DataLoadError(ValueError):
    pass


def read_ohlc_csv(
    path: str | Path,
    *,
    ts_col: str = "ts",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    volume_col: str | None = None,
) -> list[dict]:
    """Lee un CSV OHLC conservando el ORDEN del archivo.

    El estándar Atlas es `ts,open,high,low,close[,volume]`: el volumen es
    opcional por defecto. Pasar `volume_col="volume"` lo exige explícitamente.
    El orden cronológico es OBLIGATORIO en la entrada; el loader NO reordena
    (la detección de desorden vive en `data.quality.check_sorted_unique`).

    Raises:
        DataLoadError: archivo inexistente, columnas faltantes o filas corruptas.
    """
    path = Path(path)
    if not path.exists():
        raise DataLoadError(f"Dataset no encontrado: {path}")

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise DataLoadError(f"CSV vacío o sin cabecera: {path}")

            need = {ts_col, open_col, high_col, low_col, close_col}
            if volume_col:
                need.add(volume_col)
            missing = need - {c for c in reader.fieldnames}
            if missing:
                raise DataLoadError(
                    f"Faltan columnas en {path}: {', '.join(sorted(missing))}"
                )

            bars: list[dict] = []
            for row_no, row in enumerate(reader, start=2):
                try:
                    bar = {
                        "ts": row[ts_col].strip(),
                        "open": float(row[open_col]),
                        "high": float(row[high_col]),
                        "low": float(row[low_col]),
                        "close": float(row[close_col]),
                    }
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataLoadError(
                        f"Fila {row_no} corrupta en {path}: {exc}"
                    ) from exc
                if volume_col and row.get(volume_col) not in (None, ""):
                    try:
                        bar["volume"] = float(row[volume_col])
                    except ValueError as exc:
                        raise DataLoadError(
                            f"Fila {row_no}: volume inválido en {path}"
                        ) from exc
                bars.append(bar)
    except OSError as exc:
        raise DataLoadError(f"No se pudo leer {path}: {exc}") from exc

    # El ORDEN del archivo se conserva tal cual: el contrato exige que la
    # entrada este cronologicamente ordenada (docstring; 04 §7 "Timestamp
    # order"). Reordenar en silencio ocultaba un dataset desordenado y hacia
    # pasar artificialmente `check_sorted_unique`. La deteccion corresponde a
    # `data.quality.check_sorted_unique`/`summarize_quality`, no al loader.
    return bars