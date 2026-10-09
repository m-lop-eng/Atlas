"""Replay multiinstrumento determinista (MULTI-ASSET-06).

Driver independiente que reconstruye una secuencia causal global a partir de
UN `bars.jsonl` por instrumento (formato existente de `ForwardRunner`, sin
modificarlo) y la entrega a traves de `MultiAssetIngress` -> `LiveDataEngine`
-> `MultiAssetPaperOrchestrator.apply_bar_group`.

Contratos:
  * Base temporal = `close_time = open_time + interval_seconds`. Es una
    CONVENCION CAUSAL de replay (una vela solo es conocible al cierre); NO
    reconstruye la latencia real del feed (los datos no traen tiempo de
    publicacion).
  * Merge k-way determinista por `(close_time, atlas_id, stream_row_ordinal)`.
    Preserva el orden interno de cada stream; no reordena registros.
  * Grupos por `close_time`: marks comunes de todos los admisibles ANTES de
    evaluar; admision secuencial por `atlas_id`; veto global tras cada worker.
  * Preflight fail-closed: config, legibilidad, tipado, monotonia temporal,
    fingerprint por stream (misma semantica que `ForwardRunner._input_fingerprint`)
    y hash del orden global. Nada se ejecuta si falla la validacion.
  * Salud global del replay: SOLO una observacion explicita del driver, emitida
    tras validar todos los streams. Nunca se deduce de eventos individuales.
  * Sin reloj de pared: se usa un reloj simulado inyectado (el mismo que el
    orquestador/ingress). Sin threads, colas ni async.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from live.events import MarketDataEvent
from monitoring.wiring import observe_data_feed
from execution.order import reset_order_sequence

from papertrading.multi_asset import MultiAssetPaperOrchestrator
from papertrading.multi_asset_ingress import (
    InstrumentStreamConfig,
    MultiAssetIngress,
)

_UTC = timezone.utc


class ReplayIntegrityError(ValueError):
    """Preflight o reanudacion del replay invalida (fail-closed)."""


@dataclass(frozen=True, slots=True)
class ExpectedStreamFingerprint:
    """Huella esperada de un `bars.jsonl` (misma semantica que ForwardRunner).

    `sha256` = sha256 sobre los BYTES crudos del fichero; `bars` = numero de
    lineas no vacias; `first_ts`/`last_ts` = `bar.open_time` de la primera/ultima
    fila. NO es "un hash calculado por el propio driver": es una expectativa
    provista por el llamante y verificada contra el fichero.
    """

    sha256: str
    bars: int
    first_ts: int
    last_ts: int

    @staticmethod
    def from_mapping(data: dict) -> "ExpectedStreamFingerprint":
        try:
            return ExpectedStreamFingerprint(
                sha256=str(data["sha256"]),
                bars=int(data["bars"]),
                first_ts=int(data["first_ts"]),
                last_ts=int(data["last_ts"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayIntegrityError(f"fingerprint invalido: {exc}") from exc

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "bars": self.bars,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


@dataclass(frozen=True, slots=True)
class StreamInput:
    """Entrada de un instrumento: configuracion tipada + fichero + fingerprint."""

    config: InstrumentStreamConfig
    bars_path: str | Path
    fingerprint: ExpectedStreamFingerprint | None = None


@dataclass(frozen=True, slots=True)
class _Row:
    atlas_id: str
    market_data_symbol: str
    interval_seconds: int
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ordinal: int

    @property
    def close_time(self) -> int:
        return self.open_time + self.interval_seconds


@dataclass(frozen=True, slots=True)
class ReplayResult:
    order_hash: str
    fingerprints: dict
    groups: int
    events: int


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    f = float(value)
    return f == f and f not in (float("inf"), float("-inf"))


def compute_stream_fingerprint(
    bars_path: str | Path,
) -> ExpectedStreamFingerprint:
    """Huella de un `bars.jsonl` (identica a `ForwardRunner._input_fingerprint`).

    `sha256` sobre bytes crudos + conteo de lineas no vacias + extremos de
    `bar.open_time`. Un fichero vacio => `ReplayIntegrityError`.
    """
    data = Path(bars_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    bars = 0
    first_ts: int | None = None
    last_ts: int | None = None
    try:
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            ts = int(json.loads(line)["bar"]["open_time"])
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            bars += 1
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ReplayIntegrityError(f"bars.jsonl invalido: {exc}") from exc
    if first_ts is None:
        raise ReplayIntegrityError("bars.jsonl vacio: no admitido")
    return ExpectedStreamFingerprint(sha, bars, first_ts, last_ts)


def compute_order_hash(order: Sequence[_Row]) -> str:
    """Identidad determinista de la secuencia global (independiente de ficheros)."""
    parts = [
        f"{r.atlas_id}\x1f{r.open_time}\x1f{r.interval_seconds}\x1f{r.ordinal}"
        for r in order
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _merge_key(row: _Row):
    return (row.close_time, row.atlas_id, row.ordinal)


class MultiAssetReplay:
    """Driver de replay determinista sobre `MultiAssetIngress`/orquestador."""

    def __init__(
        self,
        orchestrator: MultiAssetPaperOrchestrator,
        ingress: MultiAssetIngress,
        streams: Iterable[StreamInput],
        *,
        clock,
        expected_global_order_hash: str | None = None,
    ) -> None:
        """`clock` debe ser el MISMO reloj simulado (con `.set(datetime)`) que usan
        el orquestador y el ingress (no reloj de pared)."""
        if not hasattr(clock, "set"):
            raise ReplayIntegrityError("se requiere un reloj simulado con .set()")
        self._orchestrator = orchestrator
        self._ingress = ingress
        self._streams = list(streams)
        self._clock = clock
        self._expected_global_order_hash = expected_global_order_hash
        self._ordered: list[_Row] = []
        self._fingerprints: dict = {}
        self._order_hash: str | None = None
        self._preflighted = False
        # Fail-closed: sin validacion previa no hay salud global de replay.
        self._orchestrator.set_global_data_feed(None)

    # ------------------------------------------------------------- preflight

    def preflight(self) -> "MultiAssetReplay":
        """Valida TODOS los streams antes de permitir cualquier efecto de trading."""
        self._orchestrator.set_global_data_feed(None)  # fail-closed
        if not self._streams:
            raise ReplayIntegrityError("replay sin streams")

        seen_atlas: set[str] = set()
        seen_symbol: set[str] = set()
        per_stream: list[list[_Row]] = []
        fingerprints: dict = {}
        for stream in self._streams:
            cfg = stream.config
            if cfg.atlas_id in seen_atlas:
                raise ReplayIntegrityError(f"atlas_id duplicado: {cfg.atlas_id!r}")
            seen_atlas.add(cfg.atlas_id)
            if cfg.market_data_symbol in seen_symbol:
                raise ReplayIntegrityError(
                    f"market_data_symbol duplicado: {cfg.market_data_symbol!r}"
                )
            seen_symbol.add(cfg.market_data_symbol)
            if self._orchestrator.workers.get(cfg.atlas_id) is None:
                raise ReplayIntegrityError(
                    f"atlas_id {cfg.atlas_id!r} no registrado en el orchestrator"
                )
            if self._orchestrator.spec(cfg.atlas_id) != cfg.instrument_spec:
                raise ReplayIntegrityError(
                    f"instrument_spec incoherente para {cfg.atlas_id!r}"
                )
            if stream.fingerprint is None:
                raise ReplayIntegrityError(
                    f"fingerprint obligatorio ausente para {cfg.atlas_id!r}"
                )
            path = Path(stream.bars_path)
            if not path.exists():
                raise ReplayIntegrityError(f"fichero ausente: {path}")
            actual = compute_stream_fingerprint(path)
            if actual != stream.fingerprint:
                raise ReplayIntegrityError(
                    f"fingerprint mismatch para {cfg.atlas_id!r}: "
                    f"esperado {stream.fingerprint.to_dict()} "
                    f"real {actual.to_dict()}"
                )
            rows = self._parse_stream(path, cfg)
            if not rows:
                raise ReplayIntegrityError(f"stream vacio: {cfg.atlas_id!r}")
            per_stream.append(rows)
            fingerprints[cfg.atlas_id] = actual.to_dict()

        # k-way merge determinista; cada stream ya es monotono no-decreciente.
        merged = list(heapq.merge(*per_stream, key=_merge_key))
        order_hash = compute_order_hash(merged)
        if (
            self._expected_global_order_hash is not None
            and order_hash != self._expected_global_order_hash
        ):
            raise ReplayIntegrityError(
                "global order hash mismatch: "
                f"esperado {self._expected_global_order_hash} real {order_hash}"
            )

        self._ordered = merged
        self._fingerprints = fingerprints
        self._order_hash = order_hash
        self._preflighted = True
        # Salud global explicita del entorno de replay: SOLO tras validar todo.
        self._orchestrator.set_global_data_feed(self._healthy_data_feed)
        return self

    def _parse_stream(self, path: Path, cfg: InstrumentStreamConfig) -> list[_Row]:
        rows: list[_Row] = []
        previous_open: int | None = None
        with Path(path).open("r", encoding="utf-8", newline="\n") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(self._parse_row(line, cfg, len(rows), previous_open))
                previous_open = rows[-1].open_time
        return rows

    @staticmethod
    def _parse_row(
        line: str, cfg: InstrumentStreamConfig, ordinal: int, previous_open
    ) -> _Row:
        try:
            payload = json.loads(line)
            bar = payload["bar"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ReplayIntegrityError(f"fila invalida en {cfg.atlas_id!r}: {exc}") from exc
        symbol = bar.get("symbol")
        interval = bar.get("interval_seconds")
        open_time = bar.get("open_time")
        if symbol != cfg.market_data_symbol:
            raise ReplayIntegrityError(
                f"{cfg.atlas_id!r}: symbol {symbol!r} != "
                f"{cfg.market_data_symbol!r}"
            )
        if interval != cfg.interval_seconds:
            raise ReplayIntegrityError(
                f"{cfg.atlas_id!r}: interval {interval!r} != "
                f"{cfg.interval_seconds!r}"
            )
        if not isinstance(open_time, int) or isinstance(open_time, bool) or open_time <= 0:
            raise ReplayIntegrityError(f"{cfg.atlas_id!r}: open_time invalido {open_time!r}")
        if open_time % cfg.interval_seconds != 0:
            raise ReplayIntegrityError(
                f"{cfg.atlas_id!r}: open_time {open_time} no alineado al intervalo"
            )
        if previous_open is not None and open_time < previous_open:
            raise ReplayIntegrityError(
                f"{cfg.atlas_id!r}: secuencia retrocede temporalmente "
                f"({open_time} < {previous_open})"
            )
        ohlc = (bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"))
        volume = bar.get("volume", 0.0)
        if not all(_finite(v) for v in ohlc) or not _finite(volume):
            raise ReplayIntegrityError(f"{cfg.atlas_id!r}: OHLC/volumen invalido")
        o, h, low, c = (float(v) for v in ohlc)
        vol = float(volume)
        if o <= 0 or c <= 0 or vol < 0 or h < max(o, c) or low > min(o, c) or low > h:
            raise ReplayIntegrityError(f"{cfg.atlas_id!r}: OHLC incoherente")
        return _Row(
            atlas_id=cfg.atlas_id,
            market_data_symbol=cfg.market_data_symbol,
            interval_seconds=cfg.interval_seconds,
            open_time=open_time,
            open=o, high=h, low=low, close=c, volume=vol,
            ordinal=ordinal,
        )

    @staticmethod
    def _healthy_data_feed(now: datetime):
        return observe_data_feed("CONNECTED", "HEALTHY", last_event_at=now)

    # ------------------------------------------------------------------ run

    @property
    def order_hash(self) -> str | None:
        return self._order_hash

    @property
    def fingerprints(self) -> dict:
        return dict(self._fingerprints)

    def run(self) -> ReplayResult:
        """Ejecuta el replay completo (preflight previo obligatorio)."""
        if not self._preflighted:
            self.preflight()
        # Determinismo del audit: order_id reproducible entre ejecuciones (como
        # hace `build_forward_runner`).
        reset_order_sequence()
        groups = 0
        events = 0
        for close_time, group_iter in itertools.groupby(
            self._ordered, key=lambda r: r.close_time
        ):
            group = list(group_iter)
            # 1. Reloj simulado a T (nunca reloj de pared).
            self._clock.set(datetime.fromtimestamp(close_time, tz=_UTC))
            # 2-3. Eventos crudos por el ingress (B1 valida/emite, bufferiza).
            raw = [
                MarketDataEvent(
                    symbol=r.market_data_symbol,
                    open_time=r.open_time,
                    open=r.open, high=r.high, low=r.low, close=r.close,
                    volume=r.volume, is_closed=True,
                )
                for r in group
            ]
            emissions = self._ingress.receive_group(raw)
            # 4-7. Barrera de mercado + admision secuencial + veto por worker.
            self._orchestrator.apply_bar_group(emissions)
            groups += 1
            events += len(group)
        return ReplayResult(
            order_hash=self._order_hash or "",
            fingerprints=self.fingerprints,
            groups=groups,
            events=events,
        )


__all__ = [
    "ExpectedStreamFingerprint",
    "MultiAssetReplay",
    "ReplayIntegrityError",
    "ReplayResult",
    "StreamInput",
    "compute_order_hash",
    "compute_stream_fingerprint",
]
