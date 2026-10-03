"""S7 — Streaming Paper/Forward Integration (WebSocket -> B1 -> ForwardRunner).

Camino UNICO de datos, sin segundo camino ni segunda autoridad de barras:

    Binance WebSocket -> S2 -> S3 ReconnectManager -> S4 integridad
        -> B1 LiveDataEngine (AUTORIDAD) -> ClosedBarEvent
            -> StreamingForwardRunner (callback) -> ForwardRunner.on_closed_bar
                -> PaperTradingEngine (B7) -> PaperBrokerAdapter (paper)

Reglas de S7:
  * La fuente de datos y la ejecucion estan desacopladas: el pipeline no conoce
    al broker y el runner no conoce el transporte. El unico punto de union es
    `ClosedBarEvent` emitido por B1.
  * Los eventos crudos del stream NUNCA llegan al `ForwardRunner`; este modulo
    no importa ni referencia los tipos de evento de streaming.
  * S6 (`StreamingHealthMonitor`) es EXCLUSIVAMENTE observacional: se persiste y
    no bloquea/autoriza/modifica B5.
  * Solo `PaperBrokerAdapter`; no hay broker real ni claves API.
  * Warm-start silencioso via `StreamingPipeline.warm_start` (nunca
    `engine.process`), conservando `emission_sequence`.
  * `DataFetchError` del backfill se traduce a `ConnectionError` para que S4
    responda BLOCKED/unrecoverable (fail-safe) y NO se absorba en silencio.
  * Sin scheduler/deployment: solo CLI `--once/--loop/--status`.

Este modulo NO modifica `streaming/`, `live/`, `papertrading/engine.py`,
`papertrading/forward_runner.py` ni A4/FINAL_OOS.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from data.sources import DataFetchError
from live.adapter import BackfillProvider
from live.events import ClosedBarEvent, MarketDataEvent
from papertrading.forward_runner import (
    FROZEN_CONFIG_HASH,
    FROZEN_STRATEGY_HASH,
    ForwardRunner,
)
from streaming import (
    ReconnectManager,
    StreamingHealthMonitor,
    StreamingPipeline,
    interval_to_binance,
)

ROOT = Path(__file__).resolve().parents[1]

S7_RUN_ID = "FORWARD-STREAM-B1"
S7_OUT_ROOT = ROOT / "experiments" / "streaming" / "outputs"
S7_LOCK = ".streaming.lock"
S7_PROVENANCE = "streaming_manifest.json"
S7_HEALTH_LOG = "streaming_health.ndjson"
S7_OPS_LOG = "ops_log.ndjson"
S7_INCIDENTS = "incidents.ndjson"
S7_REPORT = "report_stream.json"

DEFAULT_POLL_MAX_EVENTS = 100
DEFAULT_MAX_POLLS = 240
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_STALE_TIMEOUT_SECONDS = 60.0

_UTC = timezone.utc


class SeedError(ValueError):
    """La ultima fila de `bars.jsonl` no permite reconstruir el seed."""


def _close_dt(closed: ClosedBarEvent) -> datetime:
    return datetime.fromtimestamp(closed.close_time, tz=_UTC)


class StreamingBackfillProvider(BackfillProvider):
    """Adapter S7 de backfill para S4 sobre `BinanceVisionDailySource`.

    Traduce explicitamente `DataFetchError` -> `ConnectionError` para que
    `IntegrityReconciler.reconcile_open_ended` lo marque como `BLOCKED` (fail-
    safe) y NUNCA quede absorbido por el `except RuntimeError` de
    `StreamingPipeline._maybe_reconcile`.

    No recuperar un gap reciente (aun no publicado por batch) produce el
    comportamiento fail-safe ya definido: sin barras -> B1 permanece DEGRADED y
    no se emiten barras falsas.
    """

    def __init__(self, source: object) -> None:
        self._source = source

    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        end = int(time.time()) + 2 * interval_seconds
        try:
            bars = self._source.fetch(start_open_time, end)  # type: ignore[attr-defined]
        except DataFetchError as exc:
            # Fallo de adquisicion -> ConnectionError: S4 lo marca BLOCKED y
            # NUNCA se absorbe en silencio por `_maybe_reconcile`.
            raise ConnectionError(str(exc)) from exc
        out: list[MarketDataEvent] = []
        for bar in bars:
            ts = int(bar["ts"])
            if ts < start_open_time or ts % interval_seconds != 0:
                continue
            out.append(
                MarketDataEvent(
                    symbol=symbol,
                    open_time=ts,
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar.get("volume", 0.0)),
                    is_closed=True,
                )
            )
        return out


def load_seed_bar(bars_path: str | Path) -> ClosedBarEvent | None:
    """Reconstruye la ultima `ClosedBarEvent` persistida en `bars.jsonl`.

    Campo por campo, incluido `emission_sequence` (continuidad). Devuelve None
    si el archivo no existe o esta vacio. Una fila malformada lanza `SeedError`
    (fail-safe: no se ignora en silencio).
    """
    path = Path(bars_path)
    if not path.exists():
        return None
    last: str | None = None
    with path.open("r", encoding="utf-8", newline="\n") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return None
    try:
        row = json.loads(last)
        bar = row["bar"]
        return ClosedBarEvent(
            symbol=str(bar["symbol"]),
            open_time=int(bar["open_time"]),
            open=float(bar["open"]),
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            volume=float(bar["volume"]),
            interval_seconds=int(bar["interval_seconds"]),
            emission_sequence=int(bar["emission_sequence"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SeedError(f"bars.jsonl: ultima fila invalida: {exc}") from exc


def _append_ndjson(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        fh.flush()


class StreamingForwardRunner:
    """Orquesta `StreamingPipeline` (fuente) + `ForwardRunner` (paper).

    Args:
        forward_runner: runner forward ya construido (sin `start()`).
        symbol / interval_seconds: instrumento y temporalidad (deben coincidir
            con el instrumento congelado del runner).
        manager: `ReconnectManager` (S3) ya envuelto sobre el adapter S2.
        backfill_provider: `BackfillProvider` para S4 (sin atajos).
        monitor: observador S6 opcional (solo observa; nunca decide).
        poll_max_events: tope de eventos por `poll_once`.
        stale_timeout_seconds / clock: se pasan a B1.
    """

    def __init__(
        self,
        *,
        forward_runner: ForwardRunner,
        symbol: str,
        interval_seconds: int,
        manager: ReconnectManager,
        backfill_provider: BackfillProvider,
        monitor: StreamingHealthMonitor | None = None,
        poll_max_events: int = DEFAULT_POLL_MAX_EVENTS,
        stale_timeout_seconds: float = DEFAULT_STALE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if forward_runner.instrument != symbol:
            raise ValueError(
                f"S7: instrumento del runner {forward_runner.instrument!r} != "
                f"symbol {symbol!r}"
            )
        if interval_seconds <= 0:
            raise ValueError("S7: interval_seconds debe ser > 0")
        self._forward = forward_runner
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self._manager = manager
        self._backfill = backfill_provider
        self._monitor = monitor
        self._poll_max_events = poll_max_events
        self._stale_timeout = stale_timeout_seconds
        self._clock = clock
        self.pipeline: StreamingPipeline | None = None
        self._started = False
        self._seed: ClosedBarEvent | None = None
        self._emitted = 0
        self._deduped = 0

    # -------------------------------------------------------------- estado

    @property
    def forward_runner(self) -> ForwardRunner:
        return self._forward

    @property
    def output_dir(self) -> Path:
        return self._forward.output_dir

    @property
    def seed(self) -> ClosedBarEvent | None:
        return self._seed

    @property
    def emitted(self) -> int:
        return self._emitted

    @property
    def deduped(self) -> int:
        return self._deduped

    # ------------------------------------------------------------ ciclo de vida

    def start(self) -> "StreamingForwardRunner":
        """runner.start() -> seed -> pipeline.connect() -> warm_start(seed).

        Hardening de arranque: si CUALQUIER paso desde la construccion del
        pipeline en adelante falla (construir pipeline, `connect()`,
        `warm_start()` o provenance), se garantiza el cleanup
        (`disconnect()` best-effort + `checkpoint()` aplicable) y el error se
        re-propaga SIN ocultarse. El lock de instancia lo libera el driver.
        """
        if self._started:
            return self
        # 1) El runner reproduce bars.jsonl (idempotente) y fija su ancla.
        self._forward.start()
        # 2) Seed DESPUES de start (nunca antes de cualquier callback).
        self._seed = load_seed_bar(self._forward.output_dir / "bars.jsonl")
        try:
            # 3) Pipeline (B1 es la autoridad); unico callback hacia el runner.
            self.pipeline = StreamingPipeline(
                symbol=self.symbol,
                interval_seconds=self.interval_seconds,
                manager=self._manager,
                provider=self._backfill,
                on_closed_bar=self._on_closed_bar,
                observer=self._monitor,
                stale_timeout_seconds=self._stale_timeout,
                clock=self._clock,
            )
            self.pipeline.connect()
            # 4) Warm-start SILENCIOSO (no emite, no observa S6, conserva seq).
            if self._seed is not None:
                self.pipeline.warm_start(self._seed)
            self._write_provenance()
        except BaseException:
            # Cleanup garantizado; nunca enmascara el error original.
            try:
                self.close()
            except Exception:  # noqa: BLE001 - cleanup best-effort
                pass
            raise
        self._started = True
        return self

    def _on_closed_bar(self, closed: ClosedBarEvent) -> None:
        """UNICO punto de llegada al runner: ClosedBarEvent de B1."""
        record = self._forward.on_closed_bar(
            closed, data_quality="HEALTHY", at=_close_dt(closed)
        )
        if record is None:
            self._deduped += 1
        else:
            self._emitted += 1

    # ------------------------------------------------------------------ poll

    def poll_once(self, max_events: int | None = None) -> int:
        if not self._started or self.pipeline is None:
            raise RuntimeError("S7: start() antes de poll_once()")
        n = self.pipeline.poll(
            self._poll_max_events if max_events is None else max_events
        )
        self._write_health()
        return n

    def run_bounded(self, *, max_events: int, max_polls: int) -> dict:
        """Sesion acotada: poll hasta `max_events` o `max_polls` (determinista)."""
        if not self._started or self.pipeline is None:
            raise RuntimeError("S7: start() antes de run_bounded()")
        processed = 0
        polls = 0
        while polls < max_polls and processed < max_events:
            processed += self.poll_once()
            polls += 1
        self.checkpoint()
        last_report = self.pipeline.backfill.last_report
        return {
            "processed": processed,
            "polls": polls,
            "emitted": self._emitted,
            "deduped": self._deduped,
            "closed_bars": len(self.pipeline.closed_bars),
            "engine_state": self.pipeline.engine.state.value,
            "engine_quality": self.pipeline.engine.quality.value,
            "backfill_status": last_report.status.value if last_report else None,
            "restart": self._seed is not None,
        }

    def checkpoint(self) -> None:
        self._forward.checkpoint()

    def close(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.disconnect()
            except Exception:  # noqa: BLE001 - shutdown best-effort
                pass
        self._forward.checkpoint()

    # -------------------------------------------------------------- persist

    def _write_health(self) -> None:
        if self._monitor is None or self.pipeline is None:
            return
        _append_ndjson(
            self.output_dir / S7_HEALTH_LOG,
            self._monitor.snapshot(self.pipeline).as_dict(),
        )

    def _write_provenance(self) -> None:
        payload = {
            "run_id": self._forward.run_id,
            "created_at": datetime.now(_UTC).isoformat(),
            "data_source": "binance-websocket",
            "streaming_adapter": "BinanceStreamingAdapter",
            "provider_name": self._manager.provider_name,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "binance_interval": interval_to_binance(self.interval_seconds),
            "backfill": {
                "source": "BinanceVisionDailySource",
                "policy": "reconcile_open_ended / IntegrityBackfillProvider",
                "error_translation": "DataFetchError -> ConnectionError",
                "on_unrecoverable": "BLOCKED/UNRECOVERABLE -> B1 DEGRADED, sin emision",
            },
            "integrity": {
                "authority": "LiveDataEngine (B1)",
                "clears_on": "IntegrityReport.ok (HEALTHY)",
                "warm_start": "silencio (no emite, no observa S6)",
            },
            "namespace": {
                "out_dir": str(self.output_dir),
                "run_id": self._forward.run_id,
                "artifacts": [
                    "bars.jsonl",
                    "records.ndjson",
                    "audit.jsonl",
                    "snapshot.json",
                    "manifest.json",
                    S7_PROVENANCE,
                    S7_HEALTH_LOG,
                    S7_OPS_LOG,
                    S7_INCIDENTS,
                    S7_REPORT,
                ],
            },
            "strategy": {
                "hash": FROZEN_STRATEGY_HASH,
                "config_hash": FROZEN_CONFIG_HASH,
            },
            "seed": {
                "present": self._seed is not None,
                "open_time": self._seed.open_time if self._seed else None,
                "emission_sequence": (
                    self._seed.emission_sequence if self._seed else None
                ),
            },
        }
        path = self.output_dir / S7_PROVENANCE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


__all__ = [
    "DEFAULT_MAX_POLLS",
    "DEFAULT_POLL_MAX_EVENTS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_STALE_TIMEOUT_SECONDS",
    "S7_HEALTH_LOG",
    "S7_INCIDENTS",
    "S7_LOCK",
    "S7_OPS_LOG",
    "S7_OUT_ROOT",
    "S7_PROVENANCE",
    "S7_REPORT",
    "S7_RUN_ID",
    "SeedError",
    "StreamingBackfillProvider",
    "StreamingForwardRunner",
    "load_seed_bar",
]
