"""Adquisicion real del dataset FINAL_OOS: monthly preferred / daily fallback, OFFLINE-FIRST.

Flujo (commit 3, sin tocar el protocolo estadistico ni consumir el FINAL_OOS)::

    Binance Vision
        |-- monthly (dumps mensuales)
        `-- daily   (dumps diarios)
              |
              v
    planificacion de archivos necesarios      (monthly preferred; daily fallback)
              |
              v
    adquirir -> data/raw/ (cache de inputs INMUTABLES, sha256 por archivo)
              |
              v
    manifest de inputs                        (reproducible: mismo raw -> mismo dataset)
              |
              v
    FinalOosDatasetBuilder                     (OFFLINE: lee SOLO de data/raw/)
              |
              v
    quality + coverage + lineage  ->  Frozen dataset (CSV + manifest sha256)
              |
              v
    estado: BLOCKED_BY_DATA_AVAILABILITY | ACQUISITION_ERROR | ELIGIBLE_PARA_DRY_RUN

PRINCIPIOS (identicos a tests/test_final_oos_semantics.py):

  * adquisicion != autorizacion: este modulo JAMAS consume el FINAL_OOS,
    no ejecuta --authorize, no llama a run_final_oos y no muta
    evidence.json / data_roles.json / el preregistro / la config.
  * dataset disponible != FINAL_OOS consumido: ELIGIBLE_PARA_DRY_RUN significa
    solo que la cobertura [start_epoch, end_epoch) esta completa y puede
    pasarse a e12 --dry-run (que opera sobre COPIES temporales).
  * Los zips de binance.vision son inputs inmutables y versionados (sha256):
    una vez en data/raw/, el dataset se reconstruye SIN volver a internet
    (dentro de un ano se puede reproducir el mismo CSV).
  * No se "rellena" silenciosamente: cualquier hueco (archivo esperado que
    no existe ni 404 produce datos) deja el estado BLOCKED_BY_DATA_AVAILABILITY.
  * Adquisicion operacional: cada request tiene timeout EXPLICITO
    (PROBE_TIMEOUT_S) y errores TIPIFICADOS. Un proveedor caido aborta en el
    PRIMER request con ACQUISITION_ERROR: nunca se espera indefinidamente ni se
    itera ~340 dias futuros; el scan daily se corta en el primer dia sin
    publicar (publicacion monotona por dia).
  * Causas distinguibles: 404 (no publicado, benigno -> cobertura) frente a
    timeout / sin conexion / zip corrupto (operacional -> ACQUISITION_ERROR con `cause`).

Este modulo no depende de la estrategia, no calibra parametros y no abre H006.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from data.loader import read_ohlc_csv
from data.quality import (
    check_ohlc_integrity,
    check_price_volume_integrity,
    check_sorted_unique,
    check_timestamp_alignment,
)
from data.sources import (
    BINANCE_VISION_DAILY_KLINE_URL,
    BINANCE_VISION_KLINE_URL,
    BinanceVisionSource,
    DataConnectionError,
    DataFetchError,
    DataSource,
    DataTimeoutError,
    NotFoundError,
    _http_get_bytes,
    _sorted_unique_in_range,
)
from data.synthetic import sha256_of_file
from research.final_oos_dataset import (
    DatasetBuilderError,
    FinalOosDatasetBuilder,
    binsance_required_files,
)

# Timeout EXPLICITO por request de red: nunca esperar indefinidamente. Un
# request colgado se aborta a los PROBE_TIMEOUT_S; un proveedor caido aborta
# toda la adquisicion en el PRIMER request (sin iterar meses/dias futuros).
PROBE_TIMEOUT_S = 20
PROBE_RETRIES = 2


class CorruptDownloadError(DataFetchError):
    """Un zip ya adquirido no se puede parsear (descarga corrupta/incompleta)."""


class ExistingDatasetIntegrityError(RuntimeError):
    """El dataset congelado ya presente no supera las verificaciones de integridad.

    Se distingue de "no existe": el artefacto ESTA pero es corrupto, truncado o
    no corresponde a su manifest. NUNCA se reconstruye en silencio sobre el.
    """


class AcquisitionStatus(str, Enum):
    """Estado de la adquisicion respecto al tramo objetivo.

    - BLOCKED_BY_DATA_AVAILABILITY: la cobertura [start, end) esta incompleta
      (mes/dia sin publicar, hueco real, o span menor que el minimo preregistrado).
    - ACQUISITION_ERROR: fallo OPERACIONAL de red/adquisicion (timeout, sin
      conexion, zip corrupto). Estado DETERMINISTA y terminal: no se deja el
      proceso esperando, no se construye dataset y JAMAS se consume/decide.
    - ELIGIBLE_PARA_DRY_RUN: el dataset congelado cubre [start, end) con calidad
      y puede someterse al protocolo de PREPARACION (e12 --dry-run, sobre copias).
    """

    BLOCKED_BY_DATA_AVAILABILITY = "BLOCKED_BY_DATA_AVAILABILITY"
    ACQUISITION_ERROR = "ACQUISITION_ERROR"
    ELIGIBLE_PARA_DRY_RUN = "ELIGIBLE_PARA_DRY_RUN"


@dataclass(frozen=True, slots=True)
class RawInputRecord:
    """Un input inmutable de la adquisicion (un zip de binance.vision).

    `sha256` ancla el contenido: dos descargas del mismo archivo deben dar el
    mismo hash; el dataset se reconstruye SOLO a partir de estos archivos.
    """

    kind: str  # 'monthly' | 'daily'
    date_key: str  # '2026-09' | '2026-09-01'
    url: str
    local_path: str
    sha256: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "date_key": self.date_key,
            "url": self.url,
            "sha256": self.sha256,
        }


class RawZipSource(DataSource):
    """Fuente OFFLINE: lee zips locales (data/raw/) y los parsea a barras Atlas.

    Igual parseo que BinanceVisionSource (klines binance.vision), pero sin red:
    el orquestador garantiza que estos archivos ya existen y estan versionados.
    """

    def __init__(self, paths: list[str | Path]) -> None:
        self.paths = [Path(p) for p in paths]

    def fetch(self, start_epoch: int, end_epoch: int) -> list[dict]:
        rows: list[dict] = []
        for p in sorted(self.paths):
            try:
                rows.extend(BinanceVisionSource._parse_zip(p.read_bytes()))
            except Exception as exc:  # noqa: BLE001 - zip corrupto/truncado
                raise CorruptDownloadError(f"zip corrupto: {p}: {exc}") from exc
        return [
            b for b in _sorted_unique_in_range(rows, start_epoch, end_epoch)
        ]


class FinalOosAcquisition:
    """Orquesta la adquisicion del dataset congelado (commit 3).

    - Planifica los archivos necesarios: monthly preferred; cuando un mes NO
      tiene dump mensual (aun no publicado, 404), cae a los daily de ese mes.
    - Offline-first: si el input ya esta en `raw_dir`, no vuelve a la red
      (reproducibilidad); si falta, lo descarga y lo versiona (sha256).
    - Construye el dataset con FinalOosDatasetBuilder leyendo SOLO `raw_dir`.
    - Devuelve un dict de estado (BLOCKED | ELIGIBLE) + lineage de inputs.
    - NUNCA toca los registros reales ni el protocolo.
    """

    def __init__(
        self,
        *,
        start_epoch: int,
        end_epoch: int,
        min_span_seconds: int,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        interval_seconds: int = 3600,
        raw_dir: str | Path = "data/raw",
        out_dir: str | Path = "experiments/data/final_oos",
        opener: Callable[[str, dict[str, Any]], bytes] | None = None,
    ) -> None:
        self.start_epoch = int(start_epoch)
        self.end_epoch = int(end_epoch)
        self.min_span_seconds = int(min_span_seconds)
        self.symbol = symbol
        self.interval = interval
        self.interval_seconds = int(interval_seconds)
        self.raw_dir = Path(raw_dir)
        self.out_dir = Path(out_dir)
        self._opener = (
            opener if opener is not None else lambda url, kw: _http_get_bytes(url, **kw)
        )
        self._monthly_dir = self.raw_dir / "monthly"
        self._daily_dir = self.raw_dir / "daily"

    # ------------------------------------------------------------------
    # Planificacion
    # ------------------------------------------------------------------

    def _month_url(self, year: int, month: int) -> str:
        return BINANCE_VISION_KLINE_URL.format(
            symbol=self.symbol, interval=self.interval, year=year, month=f"{month:02d}"
        )

    def _day_url(self, year: int, month: int, day: int) -> str:
        return BINANCE_VISION_DAILY_KLINE_URL.format(
            symbol=self.symbol,
            interval=self.interval,
            date=f"{year:04d}-{month:02d}-{day:02d}",
        )

    def _months_in_range(self) -> list[tuple[int, int]]:
        start = datetime.fromtimestamp(self.start_epoch, tz=timezone.utc)
        end = datetime.fromtimestamp(self.end_epoch - 1, tz=timezone.utc)
        months: list[tuple[int, int]] = []
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return months

    def _days_in_range(self) -> list[tuple[int, int, int]]:
        start = datetime.fromtimestamp(self.start_epoch, tz=timezone.utc)
        end = datetime.fromtimestamp(self.end_epoch - 1, tz=timezone.utc)
        days: list[tuple[int, int, int]] = []
        cur = start
        while cur.date() <= end.date():
            days.append((cur.year, cur.month, cur.day))
            cur = cur + timedelta(days=1)
        return days

    def required_files(self) -> dict[str, list[str]]:
        """URLs ideales del tramo (monthly preferred). Lineage del plan."""
        return binsance_required_files(
            self.symbol, self.start_epoch, self.end_epoch, interval=self.interval
        )

    # ------------------------------------------------------------------
    # Adquisicion (offline-first)
    # ------------------------------------------------------------------

    def _local_path(self, kind: str, url: str) -> Path:
        name = url.rsplit("/", 1)[-1]
        return (self._monthly_dir if kind == "monthly" else self._daily_dir) / name

    @staticmethod
    def _atomic_write_bytes(dest: Path, raw: bytes) -> None:
        """Escritura atomica: tmp en el mismo dir -> fsync -> os.replace.

        Una descarga/comprobacion interrumpida NUNCA deja el archivo final a
        medias: o no existe, o esta completo (y validado antes de escribir).
        """
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            with tmp.open("wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _validate_raw_bars(self, bars: list[dict], *, where: str) -> None:
        """Valida filas de un zip crudo: ts unicos/ordenados/alineados, OHLC y volumen."""
        issues = []
        issues.extend(check_sorted_unique(bars))
        issues.extend(check_timestamp_alignment(bars, self.interval_seconds))
        issues.extend(check_ohlc_integrity(bars))
        issues.extend(check_price_volume_integrity(bars))
        if issues:
            sample = "; ".join(f"fila {i.row}: {i.issue}" for i in issues[:5])
            raise CorruptDownloadError(
                f"zip corrupto: {where}: contenido invalido "
                f"({len(issues)} issues) -> {sample}"
            )

    def _parse_and_validate_zip(self, raw: bytes, *, where: str) -> list[dict]:
        """Parsea el zip y valida sus filas ANTES de aceptarlo/cachearlo."""
        try:
            bars = BinanceVisionSource._parse_zip(raw)
        except Exception as exc:  # noqa: BLE001 - zip corrupto/truncado/no-zip
            raise CorruptDownloadError(f"zip corrupto: {where}: {exc}") from exc
        self._validate_raw_bars(bars, where=where)
        return bars

    def _obtain(self, kind: str, url: str) -> RawInputRecord | None:
        """Offline-first: devuelve None si el archivo es 404 (no publicado).

        Cache: si el zip ya esta en `data/raw/` se reutiliza SIN red, pero se
        revalida (un raw corrupto jamas se presenta como valido). Descarga:
        se valida el contenido y se escribe de forma atomica (tmp + replace),
        de modo que una descarga interrumpida no deja archivo final.
        """
        local = self._local_path(kind, url)
        if local.exists() and local.stat().st_size > 0:
            raw = local.read_bytes()
            self._parse_and_validate_zip(raw, where=str(local))
            return RawInputRecord(
                kind=kind,
                date_key=self._date_key(kind, url),
                url=url,
                local_path=str(local),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        try:
            raw = self._opener(url, {"timeout": PROBE_TIMEOUT_S, "retries": PROBE_RETRIES})
        except NotFoundError:
            return None  # no publicado aun -> fallback/cobertura incompleta
        # DataTimeoutError/DataConnectionError/DataFetchError: propagan; acquire()
        # termina en ACQUISITION_ERROR determinista (no cuelga ni itera el resto).
        self._parse_and_validate_zip(raw, where=url)
        local.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_bytes(local, raw)
        return RawInputRecord(
            kind=kind,
            date_key=self._date_key(kind, url),
            url=url,
            local_path=str(local),
            sha256=sha256_of_file(local),
        )

    @staticmethod
    def _date_key(kind: str, url: str) -> str:
        name = url.rsplit("/", 1)[-1]  # BTCUSDT-1h-2026-09.zip | ...-2026-09-01.zip
        body = name.removesuffix(".zip")
        parts = body.split("-")
        if kind == "monthly":
            y, m = parts[-2], parts[-1]
            return f"{y}-{m}"
        return "-".join(parts[-3:])

    def _acquire_inputs(self) -> dict[str, Any]:
        """Planifica y obtiene inputs. Returns las dos listas de zips locales."""
        monthly: list[RawInputRecord] = []
        daily: list[RawInputRecord] = []
        months = self._months_in_range()

        # monthly preferred: el primer mes sin dump mensual corta el monthly
        monthly_tail_start: tuple[int, int] | None = None
        for y, m in months:
            rec = self._obtain("monthly", self._month_url(y, m))
            if rec is None:
                monthly_tail_start = (y, m)
                break
            monthly.append(rec)

        # daily fallback: los dias de meses SIN monthly (cola no publicada)
        if monthly_tail_start is not None:
            for y, m, d in self._days_in_range():
                if (y, m) < monthly_tail_start:
                    continue  # ya cubierto por monthly
                rec = self._obtain("daily", self._day_url(y, m, d))
                if rec is None:
                    # primer dia no publicado: la ventana queda incompleta
                    # (BLOCKED). Corte AQUI: la publicacion es monotona por dia
                    # (los dias posteriores serian 404) -> se acota la ejecucion
                    # a dias realmente publicados + 1, en vez de ~340 request.
                    break
                daily.append(rec)

        return {
            "monthly": monthly,
            "daily": daily,
            "monthly_tail_start": monthly_tail_start,
        }

    # ------------------------------------------------------------------
    # Build (offline) + estado
    # ------------------------------------------------------------------

    def _build_dataset(
        self, monthly: list[RawInputRecord], daily: list[RawInputRecord]
    ) -> dict[str, Any]:
        out_files = {
            "csv": self.out_dir / f"{self.symbol}_final_oos.csv",
            "manifest": self.out_dir / f"{self.symbol}_final_oos.manifest.json",
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)

        builder = FinalOosDatasetBuilder(
            start_epoch=self.start_epoch,
            end_epoch=self.end_epoch,
            interval_seconds=self.interval_seconds,
            symbol=self.symbol,
        )
        if monthly:
            builder.add_source(
                "monthly", RawZipSource([r.local_path for r in monthly]), kind="monthly"
            )
        if daily:
            builder.add_source(
                "daily", RawZipSource([r.local_path for r in daily]), kind="daily"
            )
        out = builder.build(out_files["csv"], out_files["manifest"])
        return out

    # ------------------------------------------------------------------
    # Ejecucion
    # ------------------------------------------------------------------

    def acquired_inputs(self) -> list[RawInputRecord]:
        return self.acquire()["inputs"]

    def _acquisition_error(
        self,
        *,
        cause: str,
        detail: str,
        plan: dict[str, list[str]],
        inputs: list[RawInputRecord] | None = None,
        manifest_name: str = "",
        build_error: str | None = None,
    ) -> dict[str, Any]:
        """Estado terminal DETERMINISTA ante un fallo operacional de adquisicion.

        No se construye dataset, no se consuma nada y la causa queda tipificada
        en `cause` ('timeout' | 'connection_error' | 'corrupt_file' | 'network_error').
        """
        return {
            "status": AcquisitionStatus.ACQUISITION_ERROR,
            "cause": cause,
            "reason": detail,
            "inputs": list(inputs or []),
            "manifest_inputs": manifest_name,
            "dataset_sha256": None,
            "dataset_bars": None,
            "coverage": None,
            "eligible": False,
            "required_files": plan,
            "build_error": build_error or detail,
        }

    def acquire(self) -> dict[str, Any]:
        """Ejecuta la adquisicion completa y devuelve el estado.

        Returns (dict):
            status            AcquisitionStatus (BLOCKED | ACQUISITION_ERROR | ELIGIBLE)
            cause             str | None: 'coverage' (huecos/span) o codigo
                              operacional: 'timeout' | 'connection_error' |
                              'corrupt_file' | 'network_error'
            reason            str (detal para BLOCKED / ACQUISITION_ERROR)
            inputs            list[RawInputRecord] (todos los zips usados)
            manifest_inputs   basename del manifest de inputs escrito ('' si fallo antes)
            dataset_sha256    hash del CSV del dataset (si se pudo construir)
            dataset_bars      numero de barras
            coverage          dict del builder (first/last/start/end)
            eligible          bool: cubre [start, end) y span >= min_span
            required_files    dict del plan ideal (lineage del tramo)
            build_error       str | None
        """
        plan = self.required_files()
        try:
            acquired = self._acquire_inputs()
        except CorruptDownloadError as exc:
            return self._acquisition_error(cause="corrupt_file", detail=str(exc), plan=plan)
        except DataTimeoutError as exc:
            return self._acquisition_error(cause="timeout", detail=str(exc), plan=plan)
        except DataConnectionError as exc:
            return self._acquisition_error(
                cause="connection_error", detail=str(exc), plan=plan
            )
        except DataFetchError as exc:
            return self._acquisition_error(cause="network_error", detail=str(exc), plan=plan)
        monthly, daily = acquired["monthly"], acquired["daily"]
        inputs = monthly + daily

        # 1) manifest de inputs (lineage reproducible; sin timestamps volatiles)
        inputs_manifest = {
            "symbol": self.symbol,
            "interval": self.interval,
            "range": {
                "start_epoch": self.start_epoch,
                "end_epoch": self.end_epoch,
            },
            "inputs": sorted(
                (r.to_dict() for r in inputs), key=lambda r: (r["kind"], r["date_key"])
            ),
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.out_dir / "inputs.manifest.json"
        manifest_path.write_text(
            json.dumps(inputs_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # 2) build offline del dataset congelado (inmutable: si ya existe, no
        #    se reconstruye; se devuelve el mismo artefacto: reproducibilidad)
        try:
            existing = self._existing_dataset()
        except ExistingDatasetIntegrityError as exc:
            return self._acquisition_error(
                cause="dataset_integrity",
                detail=str(exc),
                plan=plan,
                inputs=inputs,
                manifest_name=manifest_path.name,
                build_error=str(exc),
            )
        if existing is not None:
            if not existing["covers_requested"]:
                return {
                    "status": AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY,
                    "cause": "coverage_incomplete",
                    "reason": (
                        "dataset existente no cubre [start, end): "
                        f"first={existing['coverage']['first_ts']}, "
                        f"last={existing['coverage']['last_ts']}, "
                        f"start={self.start_epoch}, end={self.end_epoch}"
                    ),
                    "inputs": inputs,
                    "manifest_inputs": manifest_path.name,
                    "dataset_sha256": existing["sha256"],
                    "dataset_bars": existing["bars"],
                    "coverage": existing["coverage"],
                    "eligible": False,
                    "required_files": plan,
                    "build_error": None,
                }
            span_ok = (self.end_epoch - self.start_epoch) >= self.min_span_seconds
            status = (
                AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
                if span_ok
                else AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
            )
            return {
                "status": status,
                "cause": None,
                "reason": "dataset inmutable ya construido (reproducibilidad: se reutiliza)",
                "inputs": inputs,
                "manifest_inputs": manifest_path.name,
                "dataset_sha256": existing["sha256"],
                "dataset_bars": existing["bars"],
                "coverage": existing["coverage"],
                "eligible": span_ok,
                "required_files": plan,
                "build_error": None,
            }

        try:
            build = self._build_dataset(monthly, daily)
        except CorruptDownloadError as exc:
            return self._acquisition_error(
                cause="corrupt_file",
                detail=str(exc),
                plan=plan,
                inputs=inputs,
                manifest_name=manifest_path.name,
                build_error=str(exc),
            )
        except DatasetBuilderError as exc:
            return {
                "status": AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY,
                "cause": "coverage_incomplete",
                "reason": f"cobertura incompleta: {exc}",
                "inputs": inputs,
                "manifest_inputs": manifest_path.name,
                "dataset_sha256": None,
                "dataset_bars": None,
                "coverage": None,
                "eligible": False,
                "required_files": plan,
                "build_error": str(exc),
            }

        span_ok = (self.end_epoch - self.start_epoch) >= self.min_span_seconds
        status = (
            AcquisitionStatus.ELIGIBLE_PARA_DRY_RUN
            if span_ok
            else AcquisitionStatus.BLOCKED_BY_DATA_AVAILABILITY
        )
        reason = (
            ""
            if span_ok
            else f"span {self.end_epoch - self.start_epoch}s < min_span {self.min_span_seconds}s"
        )

        return {
            "status": status,
            "cause": None,
            "reason": reason,
            "inputs": inputs,
            "manifest_inputs": manifest_path.name,
            "dataset_sha256": build["sha256"],
            "dataset_bars": build["bars"],
            "coverage": build["coverage"],
            "eligible": span_ok,
            "required_files": plan,
            "build_error": None,
        }

    def _existing_dataset(self) -> dict[str, Any] | None:
        """Reutiliza un dataset congelado ya presente SOLO si es integro.

        Devuelve None si no hay artefacto (hay que construir). Si el artefacto
        existe pero es corrupto, truncado o no corresponde a su manifest, lanza
        `ExistingDatasetIntegrityError`: NUNCA se reconstruye en silencio sobre
        el.
        """
        csv_path = self.out_dir / f"{self.symbol}_final_oos.csv"
        manifest_path = self.out_dir / f"{self.symbol}_final_oos.manifest.json"
        csv_exists = csv_path.exists()
        man_exists = manifest_path.exists()
        if not csv_exists and not man_exists:
            return None
        if csv_exists != man_exists:
            raise ExistingDatasetIntegrityError(
                "dataset parcial: CSV y manifest no coexisten "
                f"(csv={csv_exists}, manifest={man_exists})"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExistingDatasetIntegrityError(f"manifest ilegible: {exc}") from exc
        required = ("sha256", "bars", "first_ts", "last_ts", "interval_seconds")
        missing = [k for k in required if k not in manifest]
        if missing:
            raise ExistingDatasetIntegrityError(
                f"manifest incompleto: faltan {missing}"
            )
        if int(manifest["interval_seconds"]) != self.interval_seconds:
            raise ExistingDatasetIntegrityError(
                f"intervalo del manifest {manifest['interval_seconds']} != "
                f"{self.interval_seconds}"
            )
        real_sha = sha256_of_file(csv_path)
        if real_sha != manifest["sha256"]:
            raise ExistingDatasetIntegrityError(
                "sha256 del CSV no coincide con el manifest "
                "(corrupto/alterado/truncado)"
            )
        bars = read_ohlc_csv(csv_path)
        if len(bars) != int(manifest["bars"]):
            raise ExistingDatasetIntegrityError(
                f"conteo de barras {len(bars)} != manifest {manifest['bars']} (truncado)"
            )
        first = int(bars[0]["ts"])
        last = int(bars[-1]["ts"])
        if first != int(manifest["first_ts"]) or last != int(manifest["last_ts"]):
            raise ExistingDatasetIntegrityError(
                "extremos del CSV no corresponden al manifest "
                f"(first={first}/{manifest['first_ts']}, "
                f"last={last}/{manifest['last_ts']})"
            )
        covers = first <= self.start_epoch and (
            last + self.interval_seconds >= self.end_epoch
        )
        return {
            "sha256": real_sha,
            "bars": len(bars),
            "coverage": {
                "first_ts": first,
                "last_ts": last,
                "start_epoch": self.start_epoch,
                "end_epoch": self.end_epoch,
            },
            "covers_requested": covers,
        }


def main(argv: list[str] | None = None) -> int:
    """CLI de lectura: adquiere (o reusa cache) y reporta el estado.

    NUNCA autoriza ni consume. Requiere el entorno normal de FINAL_OOS: los
    anclas del tramo salen del preregistro congelado (solo lectura).
    """
    import argparse

    ROOT = Path(__file__).resolve().parents[1]
    plan_path = ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    esquema = plan["esquema"]

    ap = argparse.ArgumentParser(description="E12 adquisicion: dataset FINAL_OOS (offline-first)")
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out-dir", default=str(ROOT / "experiments" / "data" / "final_oos"))
    args = ap.parse_args(argv)

    acq = FinalOosAcquisition(
        start_epoch=int(esquema["start_epoch"]),
        end_epoch=int(esquema["start_epoch"]) + int(esquema["min_span_seconds"]),
        min_span_seconds=int(esquema["min_span_seconds"]),
        interval_seconds=int(esquema["interval_seconds"]),
        raw_dir=args.raw_dir,
        out_dir=args.out_dir,
    )
    res = acq.acquire()

    print(f"FINAL_OOS acquisition ........ {res['status'].value}")
    if res["cause"]:
        print(f"causa ......................... {res['cause']}")
    if res["reason"]:
        print(f"reason ....................... {res['reason']}")
    print(f"inputs obtenidos ............. {len(res['inputs'])} ({res['manifest_inputs']})")
    for r in sorted(res["inputs"], key=lambda x: (x.kind, x.date_key)):
        print(f"  [{r.kind:7s}] {r.date_key}  {r.sha256[:12]}...")
    if res["dataset_sha256"]:
        print(f"dataset sha256 ............... {res['dataset_sha256'][:16]}... ({res['dataset_bars']} barras)")
    print(f"elegible para dry-run ........ {'SI' if res['eligible'] else 'NO'}")
    if res["status"] is AcquisitionStatus.ACQUISITION_ERROR:
        print(
            "adquisicion ABORTADA: fallo operacional (no se escribio dataset, "
            "no se autorizo ni consumio nada)",
            file=sys.stderr,
        )
        return 1
    print("NOTA: esto NO consume el FINAL_OOS ni autoriza nada (verificar e12 --status)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())