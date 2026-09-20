"""FinalOOSDatasetBuilder: construccion del dataset CONGELADO de datos nuevos.

Infraestructura de datos (sin tocar la semantica estadistica). Toma fuentes de
adquisicion Binance Vision (monthly y/o daily) y produce la unidad relevante
para el FINAL_OOS: un dataset inmutable (CSV + manifest sha256) listo para el
pipeline (research/final_oos_pipeline.py).

Cumple la semantica fijada en README (tests/test_final_oos_semantics.py):
monthly/daily son SOLO mecanismos de adquisicion. La unidad que importa es el
dataset congelado con cobertura [start_epoch, end_epoch).

RESPONSABILIDAD estricta:

    fuentes (monthly/daily)
        -> determinar archivos necesarios
        -> load
        -> normalize
        -> sort
        -> dedupe / rechazo de solapamientos
        -> excluir barra parcial (no incorporarla por accidente)
        -> validacion de calidad (schema OHLC, gaps, intervalo)
        -> validacion de cobertura
        -> CSV
        -> manifest
        -> SHA256
        -> dataset inmutable

Politica de solapamiento (estricta):
    - mismo ts + mismo contenido  -> dedupe determinista registrado
    - mismo ts + contenido distinto -> ERROR (protege frente a revisiones/correcciones)
    - ultima barra parcial         -> excluir y rechazar el build si deja hueco

NUNCA hace: elegir estrategia, calibrar parametros, seleccionar periodos,
tocar evidence/data_roles ni decidir si la estrategia funciona.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.quality import summarize_quality
from data.sources import (
    BINANCE_VISION_DAILY_KLINE_URL,
    BINANCE_VISION_KLINE_URL,
    DataSource,
    write_dataset,
)
from data.synthetic import sha256_of_file

DEFAULT_INTERVAL_SECONDS = 3600


class DatasetBuilderError(ValueError):
    """Cualquier rechazo del builder (calidad, cobertura, solapamiento)."""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SourceInput:
    tag: str
    source: DataSource
    kind: str = "csv"  # 'monthly' | 'daily' | 'csv' (provenance del manifest)


def _content_key(bar: dict) -> tuple[float, float, float, float, float]:
    """Identidad de contenido de una vela (sin el ts). Comparacion estricta.

    Dos descargas que difieren en el valor (p. ej. revision de una vela
    historica) producen claves distintas -> el builder las rechaza.
    """
    return (
        float(bar["open"]),
        float(bar["high"]),
        float(bar["low"]),
        float(bar["close"]),
        float(bar.get("volume", 0.0)),
    )


def binsance_required_files(
    symbol: str,
    start_epoch: int,
    end_epoch: int,
    *,
    interval: str = "1h",
    monthly: bool = True,
    daily: bool = True,
) -> dict[str, list[str]]:
    """Archivos (URLs) que la cobertura [start, end) necesita del proveedor.

    Es informacion de adquisicion, no de evaluacion: se registra en el manifest
    (`params.required_files`) para que el lineage diga exactamente QUE descargas
    forman el dataset, sin necesidad de recurrir a la red para reconstruirlo.

    Semantica de cobertura: un zip MONTHLY cubre un mes completo; los zips DAILY
    cubren un dia. Se piden los monthly de los meses que intersecan el rango y,
    para los dias NO cubiertos por un monthly (por ejemplo si monthly esta
    deshabilitado), los daily correspondientes. Con monthly activo y un rango
    [2026-09-01, 2026-11-01), se obtienen 2 monthly y 0 daily.
    """
    out: dict[str, list[str]] = {"monthly": [], "daily": []}
    if not monthly and not daily:
        return out
    dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    dt_end = datetime.fromtimestamp(end_epoch - 1, tz=timezone.utc)

    covered_days: set[str] = set()

    if monthly:
        m = dt.replace(day=1)
        while m.year * 12 + m.month <= dt_end.year * 12 + dt_end.month:
            out["monthly"].append(
                BINANCE_VISION_KLINE_URL.format(
                    symbol=symbol,
                    interval=interval,
                    year=m.year,
                    month=f"{m.month:02d}",
                )
            )
            _, ndays = calendar.monthrange(m.year, m.month)
            for d in range(1, ndays + 1):
                covered_days.add(f"{m.year:04d}-{m.month:02d}-{d:02d}")
            if m.month == 12:
                m = m.replace(year=m.year + 1, month=1)
            else:
                m = m.replace(month=m.month + 1)

    if daily:
        d = dt
        while d <= dt_end:
            key = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            if key not in covered_days:
                out["daily"].append(
                    BINANCE_VISION_DAILY_KLINE_URL.format(
                        symbol=symbol,
                        interval=interval,
                        date=key,
                    )
                )
            d = d + timedelta(days=1)

    return out


class FinalOosDatasetBuilder:
    """Construye el dataset congelado a partir de fuentes binance/monthly/daily.

    `start_epoch`/`end_epoch` fijan la cobertura objetiva. El builder valida que
    las fuentes la cubran de forma continua, sin duplicados ni conflictos, y
    produce CSV + manifest sha256. No escribe nada mas.

    Args:
        start_epoch: primer epoch del tramo objetivo (semantica [start, end)).
        end_epoch: primer epoch excluido (una vela `ts` pertenece si `ts < end`).
        interval_seconds: intervalo esperado (3600 para BTC 1h).
        symbol: par para el manifest (ej: BTCUSDT).
    """

    def __init__(
        self,
        *,
        start_epoch: int,
        end_epoch: int,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        symbol: str = "BTCUSDT",
    ) -> None:
        if end_epoch <= start_epoch:
            raise DatasetBuilderError(f"end_epoch ({end_epoch}) debe ser > start_epoch ({start_epoch})")
        self.start_epoch = int(start_epoch)
        self.end_epoch = int(end_epoch)
        self.interval_seconds = int(interval_seconds)
        self.symbol = symbol
        self._sources: list[SourceInput] = []

    def add_source(
        self,
        tag: str,
        source: DataSource,
        *,
        kind: str = "csv",
    ) -> "FinalOosDatasetBuilder":
        if not isinstance(source, DataSource):
            raise DatasetBuilderError(f"'{tag}' no es una DataSource")
        if any(s.tag == tag for s in self._sources):
            raise DatasetBuilderError(f"tag de fuente duplicado: '{tag}'")
        self._sources.append(SourceInput(tag=tag, source=source, kind=kind))
        return self

    def _load(self) -> tuple[list[dict], dict[str, int]]:
        """Carga todas las fuentes, normaliza ts a str-epoch y filtra al rango."""
        rows: list[dict] = []
        counts: dict[str, int] = {}
        for s in self._sources:
            bars = s.source.fetch(self.start_epoch, self.end_epoch)
            counts[s.tag] = counts.get(s.tag, 0) + len(bars)
            for b in bars:
                ts = int(b["ts"])
                if self.start_epoch <= ts < self.end_epoch:
                    rows.append(
                        {
                            "ts": str(ts),
                            "open": float(b["open"]),
                            "high": float(b["high"]),
                            "low": float(b["low"]),
                            "close": float(b["close"]),
                            "volume": float(b.get("volume", 0.0)),
                            "_src": s.tag,
                        }
                    )
        return rows, counts

    @staticmethod
    def _dedupe(rows: list[dict]) -> tuple[list[dict], int]:
        """Dedup estricto: idéntico -> dedupe registrado; distinto -> ERROR.

        Returns:
            (bars deduped y ordenadas por ts, conteo de veloces idénticas descartadas).
        """
        rows.sort(key=lambda b: int(b["ts"]))
        merged: list[dict] = []
        seen: dict[int, tuple] = {}
        deduped: int = 0
        for b in rows:
            ts = int(b["ts"])
            key = _content_key(b)
            if ts in seen:
                if seen[ts] != key:
                    raise DatasetBuilderError(
                        f"solapamiento en conflicto en ts={ts} ({_iso(ts)}): "
                        f"misma vela con contenido distinto entre fuentes "
                        f"({b['_src']}). Binance pudo revisar la serie: se rechaza."
                    )
                deduped += 1
                continue
            seen[ts] = key
            merged.append(b)
        return merged, deduped

    @staticmethod
    def _drop_partial(rows: list[dict], interval_seconds: int, end_epoch: int) -> tuple[list[dict], int]:
        """Excluye la barra parcial: ts + interval > end (no ha cerrado dentro del tramo)."""
        kept: list[dict] = []
        dropped = 0
        for b in rows:
            if int(b["ts"]) + interval_seconds > end_epoch:
                dropped += 1
                continue
            kept.append(b)
        return kept, dropped

    def _validate_quality(self, rows: list[dict]) -> dict:
        summary = summarize_quality(
            [{"ts": b["ts"], "open": b["open"], "high": b["high"],
              "low": b["low"], "close": b["close"], "volume": b["volume"]} for b in rows],
            expected_interval_seconds=self.interval_seconds,
        )
        issues = [
            f"fila {i.row}: {i.issue} ({i.detail})" for i in summary.issues
        ]
        if issues:
            raise DatasetBuilderError(
                "[calidad] dataset no valido: " + "; ".join(issues[:8])
            )
        return {"ok": True, "rows": len(rows), "issues": issues}

    def _validate_coverage(self, rows: list[dict]) -> dict:
        if not rows:
            raise DatasetBuilderError(
                "cobertura vacia: ninguna vela en "
                f"[{_iso(self.start_epoch)}, {_iso(self.end_epoch)})"
            )
        first = int(rows[0]["ts"])
        last = int(rows[-1]["ts"])
        if first > self.start_epoch:
            raise DatasetBuilderError(
                f"cobertura al inicio falla: primera vela {first} "
                f"({_iso(first)}) > start_epoch {self.start_epoch}"
            )
        if last + self.interval_seconds < self.end_epoch:
            raise DatasetBuilderError(
                f"cobertura al final falla: ultima vela {last} "
                f"({_iso(last)}) no cubre el fin {self.end_epoch} "
                f"(se necesita >= {self.end_epoch - self.interval_seconds})"
            )
        return {
            "first_ts": first,
            "last_ts": last,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
        }

    def build(
        self,
        csv_path: str | Path,
        manifest_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> dict:
        """Construye y persiste el dataset congelado (CSV + manifest sha256).

        Escribe únicamente `csv_path` y `manifest_path`. Devuelve un dict de
        lineage/provenance. Lanza `DatasetBuilderError` ante cualquier hallazgo.
        """
        csv_path = Path(csv_path)
        if csv_path.exists() and not overwrite:
            raise DatasetBuilderError(
                f"dataset ya existe (inmutable): {csv_path} (usar overwrite=True)"
            )
        if not self._sources:
            raise DatasetBuilderError("sin fuentes: añade al menos una DataSource")

        rows, counts = self._load()
        rows, deduped = self._dedupe(rows)
        rows, partial_dropped = self._drop_partial(rows, self.interval_seconds, self.end_epoch)
        quality = self._validate_quality(rows)
        coverage = self._validate_coverage(rows)

        manifest = write_dataset(
            rows,
            csv_path,
            manifest_path,
            source="binance/vision (builder)",
            pair=self.symbol,
            interval_seconds=self.interval_seconds,
            params={
                "start_epoch": self.start_epoch,
                "end_epoch": self.end_epoch,
                "interval_seconds": self.interval_seconds,
                "sources": [
                    {"tag": s.tag, "kind": s.kind, "class": type(s.source).__name__}
                    for s in self._sources
                ],
                "bars_per_source": counts,
                "deduplicated": deduped,
                "partial_dropped": partial_dropped,
                "required_files": binsance_required_files(
                    self.symbol, self.start_epoch, self.end_epoch
                ),
            },
        )
        return {
            "csv": str(csv_path),
            "manifest": str(manifest_path),
            "sha256": sha256_of_file(csv_path),
            "bars": len(rows),
            "deduplicated": deduped,
            "partial_dropped": partial_dropped,
            "quality": quality,
            "coverage": coverage,
            "manifest_dict": manifest.to_dict(),
        }