"""Integración Streaming -> B1 (S5), sin segundo camino.

    Binance WebSocket
        -> S2 adapter -> S3 ReconnectManager -> (S4 integridad)
            -> B1StreamingAdapter (contrato MarketDataAdapter de B1)
                -> LiveDataEngine (AUTORIDAD única)
                    -> ClosedBarEvent

REGLA FUNDAMENTAL: B1 sigue siendo la autoridad sobre qué barra llega a
estrategia. El streaming NO crea un camino paralelo: todos los datos pasan por
`LiveDataEngine`, que deduplica, detecta gaps y decide qué vela se emite.

Tras una desconexión:

    WebSocket reconnect -> integrity UNKNOWN -> backfill (S4)
        -> S4 HEALTHY -> B1 reconcile -> velas contiguas válidas -> ClosedBarEvent

Si S4 no llega a HEALTHY, el proveedor de backfill devuelve [] y B1 NO emite
barras recuperadas como si fueran normales.

S5 NO autoriza trading: solo demuestra que el camino
stream -> recuperación -> B1 es coherente. Sin estrategia, órdenes ni broker.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from live.adapter import BackfillProvider, MarketDataAdapter
from live.engine import EngineState, LiveDataEngine
from live.events import ClosedBarEvent, MarketDataEvent

from streaming.errors import StreamingError
from streaming.events import StreamData, StreamEvent
from streaming.integrity import IntegrityReconciler, IntegrityReport
from streaming.reconnect import ReconnectManager, ReconnectState


class _StreamingSource:
    """Protocolo estructural de la fuente streaming (S3 ReconnectManager)."""

    provider_name: str
    state: object

    def connect(self) -> None:  # pragma: no cover - estructural
        ...

    def disconnect(self) -> None:  # pragma: no cover - estructural
        ...

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        ...  # pragma: no cover - estructural

    def receive(self) -> StreamEvent | None:  # pragma: no cover - estructural
        ...


class StreamingPipelineObserver(Protocol):
    """Observador opcional del pipeline (S6). Solo lectura; no decide nada."""

    def on_stream_event(self, event: StreamEvent) -> None:
        ...  # pragma: no cover - estructural

    def on_stream_error(self, error: StreamingError) -> None:
        ...  # pragma: no cover - estructural

    def on_integrity_report(self, report: IntegrityReport) -> None:
        ...  # pragma: no cover - estructural


class B1StreamingAdapter(MarketDataAdapter):
    """Adapta una fuente streaming al contrato `MarketDataAdapter` de B1.

    Traduce `StreamData` a `MarketDataEvent` y descarta los eventos de control
    (heartbeat, conexión/desconexión): B1 solo consume market data. Los fallos
    de streaming se traducen a `ConnectionError` para que `LiveDataEngine`
    entre en RECONNECTING y deje de emitir.
    """

    def __init__(
        self,
        source: _StreamingSource,
        *,
        observer: StreamingPipelineObserver | None = None,
    ) -> None:
        self._source = source
        self._observer = observer

    def connect(self) -> None:
        try:
            self._source.connect()  # type: ignore[attr-defined]
        except StreamingError as exc:
            raise ConnectionError(str(exc)) from exc

    def disconnect(self) -> None:
        try:
            self._source.disconnect()  # type: ignore[attr-defined]
        except StreamingError:
            pass

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        try:
            self._source.subscribe(symbol, interval_seconds)  # type: ignore[attr-defined]
        except StreamingError as exc:
            raise ConnectionError(str(exc)) from exc

    def receive(self) -> MarketDataEvent | None:
        while True:
            try:
                event = self._source.receive()  # type: ignore[attr-defined]
            except StreamingError as exc:
                if self._observer is not None:
                    self._observer.on_stream_error(exc)
                raise ConnectionError(str(exc)) from exc
            if event is None:
                return None
            if self._observer is not None:
                self._observer.on_stream_event(event)
            if isinstance(event, StreamData):
                return event.event
            # heartbeat / conexión: no es market data, seguir drenando
            continue


class IntegrityBackfillProvider(BackfillProvider):
    """`BackfillProvider` de B1 respaldado por S4 (sin atajos).

    Solo devuelve velas (y confirma integridad) cuando S4 emite un reporte
    HEALTHY. En caso contrario devuelve [] y B1 permanece DEGRADED.
    """

    def __init__(
        self,
        provider: BackfillProvider,
        reconciler: IntegrityReconciler,
        manager: ReconnectManager,
    ) -> None:
        self._provider = provider
        self._reconciler = reconciler
        self._manager = manager
        self.last_report: IntegrityReport | None = None

    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        report = self._reconciler.reconcile_open_ended(start_open_time=start_open_time)
        self.last_report = report
        if not report.ok:
            return []
        self._manager.confirm_data_integrity()
        return list(report.recovered)


class StreamingPipeline:
    """Orquesta stream -> S3 -> S4 -> B1 (`LiveDataEngine`).

    Args:
        symbol / interval_seconds: instrumento y temporalidad.
        manager: `ReconnectManager` (S3) ya envuelto sobre el adapter S2.
        provider: `BackfillProvider` histórico para S4/B1.
        on_closed_bar: callback invocado por B1 por cada `ClosedBarEvent`.
        observer: observador opcional (S6) de eventos/errores/reportes.
        stale_timeout_seconds / clock: se pasan a B1.
    """

    def __init__(
        self,
        *,
        symbol: str,
        interval_seconds: int,
        manager: ReconnectManager,
        provider: BackfillProvider,
        on_closed_bar: Callable[[ClosedBarEvent], None] | None = None,
        observer: StreamingPipelineObserver | None = None,
        stale_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.manager = manager
        self._observer = observer
        self._last_notified_signature: tuple | None = None
        self.reconciler = IntegrityReconciler(provider, symbol, interval_seconds)
        self.backfill = IntegrityBackfillProvider(provider, self.reconciler, manager)
        self.adapter = B1StreamingAdapter(manager, observer=observer)
        self.engine = LiveDataEngine(
            symbol,
            interval_seconds,
            self.adapter,
            stale_timeout_seconds=stale_timeout_seconds,
            backfill_provider=self.backfill,
            on_closed_bar=on_closed_bar,
            clock=clock,
        )

    @property
    def closed_bars(self) -> list[ClosedBarEvent]:
        return self.engine.closed_bars

    def connect(self) -> None:
        self.engine.start()

    def disconnect(self) -> None:
        self.engine.stop()

    def warm_start(self, closed: ClosedBarEvent) -> None:
        """Siembra el ancla de B1 del pipeline (delegado, sin observar en S6).

        Delega en `LiveDataEngine.warm_start`: no emite `ClosedBarEvent`, no
        invoca el observer S6 (no pasa por `B1StreamingAdapter.receive`) y no
        reconcilia. Debe invocarse tras `connect()`.
        """
        self.engine.warm_start(closed)

    def poll(self, max_events: int = 100) -> int:
        """Drena el stream hacia B1 y reconcilia si B1 quedó DEGRADED."""
        processed = self.engine.poll(max_events)
        self._maybe_reconcile()
        return processed

    def _maybe_reconcile(self) -> None:
        if self.manager.state is not ReconnectState.CONNECTED:
            return  # sin transporte no se declara recuperación
        if self.engine.state in (EngineState.DEGRADED, EngineState.RECONNECTING):
            try:
                self.engine.reconcile()
            except RuntimeError:
                return
            report = self.backfill.last_report
            if report is not None:
                signature = (
                    report.status,
                    report.expected_start,
                    report.resume_open_time,
                    report.recovered,
                    report.missing,
                    report.conflicts,
                    report.duplicates,
                )
                if signature != self._last_notified_signature:
                    self._last_notified_signature = signature
                    if self._observer is not None:
                        self._observer.on_integrity_report(report)


__all__ = [
    "B1StreamingAdapter",
    "IntegrityBackfillProvider",
    "StreamingPipeline",
    "StreamingPipelineObserver",
]
