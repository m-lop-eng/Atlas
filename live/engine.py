"""Motor de datos en vivo (B1): solo market data, sin órdenes.

Arquitectura:

    Adapter de mercado (Binance/IBKR/MT5/CME)
        ->  MarketDataAdapter.receive() -> MarketDataEvent
            ->  LiveDataEngine (normaliza, valida, detecta gaps/duplicados)
                ->  ClosedBarEvent (SOLO velas cerradas) -> estrategia

Reglas críticas del contrato paper-safe:
    1. La estrategia solo recibe velas cerradas (nunca intrabar).
    2. Cada vela se emite exactamente una vez (dedupe por open_time).
    3. Un gap/out-of-order/timestamp inválido detiene la evaluación de la
       estrategia hasta reconciliación con un BackfillProvider.
    4. El engine no calcula señales, no conoce brokers, no fabrica velas.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable

from live.adapter import BackfillProvider, MarketDataAdapter
from live.events import ClosedBarEvent, MarketDataEvent


class EngineState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    HALTED = "HALTED"


class DataQuality(str, Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    GAP_DETECTED = "GAP_DETECTED"
    DUPLICATE = "DUPLICATE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    INVALID_OHLC = "INVALID_OHLC"
    INCOMPLETE_BAR = "INCOMPLETE_BAR"


class WarmStartError(ValueError):
    """La vela de siembra de `warm_start` es invalida o inconsistente."""


class LiveDataEngine:
    """Consume MarketDataEvents del adapter y emite ClosedBarEvents.

    El motor es determinista respecto a la secuencia de eventos que recibe:
    con la misma secuencia produce exactamente las mismas emisiones.
    Reloj inyectable (`clock`) para tests deterministas de staleness.
    """

    def __init__(
        self,
        symbol: str,
        interval_seconds: int,
        adapter: MarketDataAdapter,
        *,
        stale_timeout_seconds: float = 60.0,
        backfill_provider: BackfillProvider | None = None,
        on_closed_bar: Callable[[ClosedBarEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds debe ser > 0, recibido {interval_seconds}"
            )
        if stale_timeout_seconds <= 0:
            raise ValueError(
                f"stale_timeout_seconds debe ser > 0, recibido {stale_timeout_seconds}"
            )
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.adapter = adapter
        self.stale_timeout_seconds = stale_timeout_seconds
        self.backfill_provider = backfill_provider
        self._on_closed_bar = on_closed_bar
        self._clock = clock

        self.state: EngineState = EngineState.DISCONNECTED
        self.quality: DataQuality = DataQuality.HEALTHY
        self.last_error: str | None = None
        self.closed_bars: list[ClosedBarEvent] = []
        self.issues: list[tuple[DataQuality, str]] = []
        self._in_progress: MarketDataEvent | None = None
        self._last_emitted_open_time: int | None = None
        self._emission_seq = 0
        self._last_data_at: float | None = None

    # ----------------------------------------------------------- ciclo de vida

    def start(self) -> None:
        """DISCONNECTED -> CONNECTING -> CONNECTED."""
        if self.state in (EngineState.CONNECTING, EngineState.CONNECTED):
            return
        if self.state in (EngineState.RECONNECTING, EngineState.DEGRADED):
            raise RuntimeError(
                f"no se puede start() desde {self.state.value}; use reconnect()/reconcile()"
            )
        self.state = EngineState.CONNECTING
        try:
            self.adapter.connect()
            self.adapter.subscribe(self.symbol, self.interval_seconds)
        except ConnectionError as exc:
            self.state = EngineState.DISCONNECTED
            self.last_error = str(exc)
            raise
        self.state = EngineState.CONNECTED

    def stop(self) -> None:
        """Cualquier estado activo -> DISCONNECTED (parada controlada)."""
        try:
            self.adapter.disconnect()
        except ConnectionError as exc:
            self.last_error = str(exc)
        self.state = EngineState.DISCONNECTED
        self.quality = DataQuality.HEALTHY
        # La vela abierta no se conserva entre sesiones: jamás se fabrica un
        # cierre falso para una barra que nunca cerró.
        self._in_progress = None
        self._last_data_at = None

    def warm_start(self, closed: ClosedBarEvent) -> None:
        """Fija el ancla de reanudacion de B1 SIN emitir ni llamar callbacks.

        Siembra el estado con la ultima vela persistida de una sesion previa
        para que B1 detecte huecos entre reinicios: la proxima vela real en
        `open_time + interval` se acepta; un salto mayor dispara GAP_DEGRADED.

        Garantias:
          * no produce `ClosedBarEvent` ni invoca `on_closed_bar`;
          * no modifica `closed_bars`, `issues`, `state`, `quality` ni
            `_last_data_at`;
          * no ejecuta `reconcile()` ni consulta el `BackfillProvider`;
          * conserva exactamente `closed.emission_sequence` (continuidad);
          * valida todo atomicamente antes de tocar estado.

        Args:
            closed: ultima vela cerrada persistida (con su `emission_sequence`).

        Raises:
            WarmStartError: dato invalido (tipo/simbolo/intervalo/timestamp/
                OHLC/emission_sequence).
            RuntimeError: estado no CONNECTED, vela en curso, o ancla ya fijada
                en un `open_time` distinto.
        """
        if not isinstance(closed, ClosedBarEvent):
            raise WarmStartError("warm_start exige un ClosedBarEvent")
        if self.state is not EngineState.CONNECTED:
            raise RuntimeError(
                f"warm_start solo desde CONNECTED, estado actual {self.state.value}"
            )
        if self._in_progress is not None:
            raise RuntimeError("warm_start no permitido con una vela en curso")
        if closed.symbol != self.symbol:
            raise WarmStartError(
                f"warm_start: simbolo {closed.symbol!r} != {self.symbol!r}"
            )
        if closed.interval_seconds != self.interval_seconds:
            raise WarmStartError(
                f"warm_start: intervalo {closed.interval_seconds} != "
                f"{self.interval_seconds}"
            )
        if not self._is_valid_timestamp(closed.open_time):
            raise WarmStartError(
                f"warm_start: open_time {closed.open_time} no alineado al "
                f"intervalo {self.interval_seconds}"
            )
        if not self._is_valid_ohlc(closed):
            raise WarmStartError(
                f"warm_start: OHLC invalido en open_time {closed.open_time}"
            )
        seq = closed.emission_sequence
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise WarmStartError(
                f"warm_start: emission_sequence invalido {seq!r}"
            )

        if self._last_emitted_open_time is not None:
            if closed.open_time == self._last_emitted_open_time:
                return  # no-op idempotente: mismo ancla
            raise RuntimeError(
                f"warm_start: ancla ya fijada en {self._last_emitted_open_time}; "
                f"no se puede reemplazar por {closed.open_time}"
            )

        self._last_emitted_open_time = closed.open_time
        self._emission_seq = seq
        self._in_progress = None

    def poll(self, max_events: int = 100) -> int:
        """Drena hasta `max_events` del adapter procesándolos.

        Devuelve el número de eventos procesados. Si el adapter lanza
        ConnectionError, pasa a RECONNECTING y corta el drenado.
        """
        processed = 0
        while processed < max_events:
            try:
                event = self.adapter.receive()
            except ConnectionError as exc:
                self._on_connection_lost(str(exc))
                break
            if event is None:
                break
            self.process(event)
            processed += 1
        self.check_stale()
        return processed

    # --------------------------------------------------------- núcleo determinista

    def process(self, event: MarketDataEvent) -> ClosedBarEvent | None:
        """Procesa un evento. Devuelve la vela cerrada emitida (o None).

        Es determinista: la secuencia exacta de eventos produce el mismo
        resultado en cada ejecución.
        """
        if event.symbol != self.symbol:
            self._record_issue(
                DataQuality.INVALID_TIMESTAMP, f"símbolo inesperado {event.symbol}"
            )
            return None

        if not self._is_valid_timestamp(event.open_time):
            self._record_issue(
                DataQuality.INVALID_TIMESTAMP,
                f"open_time {event.open_time} no alineado al intervalo {self.interval_seconds}",
            )
            return None

        if not self._is_valid_ohlc(event):
            self._record_issue(
                DataQuality.INVALID_OHLC, f"OHLC inválido en open_time {event.open_time}"
            )
            return None

        self._last_data_at = self._clock()

        # Datos frescos recuperan un estado STALE de forma natural.
        if self.state == EngineState.STALE:
            self.state = EngineState.CONNECTED
            self.quality = DataQuality.HEALTHY

        # Tras un gap, solo la reconciliación explícita restaura el flujo:
        # no se fabrica la barra perdida ni se evalúan datos no contiguos.
        if self.state == EngineState.DEGRADED:
            return None

        # Mientras se reconecta no se evaluan datos: primero hay que
        # reconectar y reconciliar (no se reanuda sin comprobar).
        if self.state == EngineState.RECONNECTING:
            return None

        in_progress = self._in_progress
        if in_progress is None:
            return self._tick_without_in_progress(event)
        return self._tick_with_in_progress(event, in_progress)

    # ------------------------------------------------------------------ ticks

    def _tick_without_in_progress(self, event: MarketDataEvent) -> ClosedBarEvent | None:
        expected = self._next_open_time()
        if self._last_emitted_open_time is None:
            # Primera vela: se acepta la primera barra alineada.
            if event.is_closed:
                return self._emit(event)
            self._in_progress = event
            return None
        if event.open_time == expected:
            if event.is_closed:
                return self._emit(event)
            self._in_progress = event
            return None
        if event.open_time == self._last_emitted_open_time:
            self._record_issue(
                DataQuality.DUPLICATE,
                f"cierre duplicado para open_time {event.open_time}",
            )
            return None
        if event.open_time < expected:
            self._record_issue(
                DataQuality.OUT_OF_ORDER,
                f"vela {event.open_time} anterior a la esperada {expected}",
            )
            return None
        return self._gap(event)

    def _tick_with_in_progress(
        self, event: MarketDataEvent, in_progress: MarketDataEvent
    ) -> ClosedBarEvent | None:
        if event.open_time == in_progress.open_time:
            # Mismos barra: actualiza el agregado en curso.
            self._merge_in_progress(event)
            if event.is_closed:
                return self._emit(self._in_progress)
            return None
        if event.open_time < in_progress.open_time:
            self._record_issue(
                DataQuality.OUT_OF_ORDER,
                f"vela {event.open_time} anterior a la en curso {in_progress.open_time}",
            )
            return None
        # Barra más nueva sin cerrar la anterior: la anterior quedó
        # incompleta, no podemos emitirla. Es un gap de integridad.
        return self._gap(event)

    # ------------------------------------------------------------- calidad/estado

    def check_stale(self) -> None:
        """CONNECTED sin datos durante `stale_timeout_seconds` -> STALE."""
        if self.state != EngineState.CONNECTED:
            return
        if self._last_data_at is None:
            return
        elapsed = self._clock() - self._last_data_at
        if elapsed > self.stale_timeout_seconds:
            self.state = EngineState.STALE
            self.quality = DataQuality.STALE

    def _gap(self, event: MarketDataEvent) -> None:
        """Gap/barra faltante. Detiene el flujo hasta reconciliar."""
        self._record_issue(
            DataQuality.GAP_DETECTED,
            f"gap: se esperaba {self._next_open_time()}, llegó {event.open_time}",
        )
        self.state = EngineState.DEGRADED
        self.quality = DataQuality.GAP_DETECTED
        self._in_progress = None
        return None

    def reconcile(self) -> list[ClosedBarEvent]:
        """Recupera velas perdidas desde el BackfillProvider (contrato B1).

        El backfill es una frontera de confianza igual que el stream: se
        normaliza/valida antes de emitir. Orden: simbolo -> timestamp/intervalo
        -> OHLC -> vela cerrada -> orden temporal -> duplicados -> contigüidad.
        Se emiten SOLO las velas validas contiguas desde `_next_open_time()`.

        Ante cualquier dato invalido NO se marca HEALTHY: se registra el issue,
        el motor permanece DEGRADED (con la calidad de la violacion) y se
        devuelve el prefijo valido ya emitido. Sin velas recuperadas tampoco hay
        evidencia de cierre del gap -> sigue DEGRADED. Solo una secuencia
        valida que cierra el gap devuelve el motor a CONNECTED/HEALTHY.
        """
        if self.backfill_provider is None:
            raise RuntimeError("no hay BackfillProvider configurado")
        if self.state not in (EngineState.DEGRADED, EngineState.RECONNECTING):
            raise RuntimeError(
                f"reconcile() solo desde DEGRADED/RECONNECTING, estado actual {self.state.value}"
            )
        start_from = self._next_open_time()
        bars = self.backfill_provider.fetch_closed_bars(
            self.symbol, self.interval_seconds, start_from
        )
        replayed: list[ClosedBarEvent] = []
        expected = start_from
        violation: DataQuality | None = None
        detail = ""
        for event in bars:
            if event.symbol != self.symbol:
                violation = DataQuality.INVALID_TIMESTAMP
                detail = f"backfill: simbolo inesperado {event.symbol}"
            elif not self._is_valid_timestamp(event.open_time):
                violation = DataQuality.INVALID_TIMESTAMP
                detail = (
                    f"backfill: open_time {event.open_time} no alineado al "
                    f"intervalo {self.interval_seconds}"
                )
            elif not self._is_valid_ohlc(event):
                violation = DataQuality.INVALID_OHLC
                detail = f"backfill: OHLC invalido en open_time {event.open_time}"
            elif not event.is_closed:
                violation = DataQuality.INCOMPLETE_BAR
                detail = f"backfill: vela no cerrada en open_time {event.open_time}"
            elif event.open_time < expected:
                violation = (
                    DataQuality.DUPLICATE
                    if event.open_time == self._last_emitted_open_time
                    else DataQuality.OUT_OF_ORDER
                )
                detail = (
                    f"backfill fuera de orden: {event.open_time} < esperado {expected}"
                )
            elif event.open_time > expected:
                violation = DataQuality.GAP_DETECTED
                detail = (
                    f"backfill con gap interno: se esperaba {expected}, "
                    f"llego {event.open_time}"
                )
            if violation is not None:
                break
            closed = self._emit(event)
            if closed is None:
                violation = DataQuality.DUPLICATE
                detail = f"backfill: cierre duplicado {event.open_time}"
                break
            replayed.append(closed)
            expected += self.interval_seconds

        self._in_progress = None
        if violation is not None:
            self._record_issue(violation, detail)
            self.state = EngineState.DEGRADED
            self.quality = violation
            return replayed
        if not replayed:
            self._record_issue(
                DataQuality.GAP_DETECTED,
                f"backfill sin velas desde {start_from}; gap sin evidencia de cierre",
            )
            self.state = EngineState.DEGRADED
            self.quality = DataQuality.GAP_DETECTED
            return replayed
        self.state = EngineState.CONNECTED
        self.quality = DataQuality.HEALTHY
        return replayed

    def reconnect(self) -> None:
        """Reintenta la conexión del adapter. RECONNECTING -> CONNECTED."""
        self.state = EngineState.RECONNECTING
        try:
            self.adapter.connect()
            self.adapter.subscribe(self.symbol, self.interval_seconds)
        except ConnectionError as exc:
            self.last_error = str(exc)
            raise
        if self.backfill_provider is not None:
            self.reconcile()
        else:
            self.state = EngineState.CONNECTED
            self.quality = DataQuality.HEALTHY

    def _on_connection_lost(self, error: str) -> None:
        self.last_error = error
        self.state = EngineState.RECONNECTING
        self.quality = DataQuality.STALE

    # -------------------------------------------------------------- emisión/mix

    def _emit(self, event: MarketDataEvent) -> ClosedBarEvent | None:
        """Crea, registra y entrega una ClosedBarEvent (una vez por vela)."""
        if self._last_emitted_open_time == event.open_time:
            self._record_issue(
                DataQuality.DUPLICATE,
                f"cierre duplicado para open_time {event.open_time}",
            )
            return None
        self._emission_seq += 1
        closed = ClosedBarEvent(
            symbol=self.symbol,
            open_time=event.open_time,
            open=event.open,
            high=event.high,
            low=event.low,
            close=event.close,
            volume=event.volume,
            interval_seconds=self.interval_seconds,
            emission_sequence=self._emission_seq,
        )
        self.closed_bars.append(closed)
        self._last_emitted_open_time = event.open_time
        self._in_progress = None
        self.quality = DataQuality.HEALTHY
        if self._on_closed_bar is not None:
            self._on_closed_bar(closed)
        return closed

    def _merge_in_progress(self, event: MarketDataEvent) -> None:
        merged = self._in_progress
        self._in_progress = MarketDataEvent(
            symbol=self.symbol,
            open_time=event.open_time,
            open=merged.open,
            high=max(merged.high, event.high),
            low=min(merged.low, event.low),
            close=event.close,
            volume=event.volume,
            is_closed=event.is_closed,
        )
        if event.is_closed:
            self._in_progress = event

    # ----------------------------------------------------------------- helpers

    def _next_open_time(self) -> int:
        if self._in_progress is not None:
            return self._in_progress.open_time
        if self._last_emitted_open_time is None:
            return 0
        return self._last_emitted_open_time + self.interval_seconds

    def _is_valid_timestamp(self, open_time: int) -> bool:
        return open_time > 0 and open_time % self.interval_seconds == 0

    def _is_valid_ohlc(self, event: MarketDataEvent) -> bool:
        vals = (event.open, event.high, event.low, event.close, event.volume)
        if any(v is None or v != v or abs(v) == float("inf") for v in vals):
            return False
        if event.open <= 0 or event.close <= 0 or event.volume < 0:
            return False
        return event.high >= max(event.open, event.close) and event.low <= min(
            event.open, event.close
        ) and event.low <= event.high

    def _record_issue(self, quality: DataQuality, detail: str) -> None:
        self.issues.append((quality, detail))
        self.issues = self.issues[-100:]