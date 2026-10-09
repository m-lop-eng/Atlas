"""Multi-asset ingress (MULTI-ASSET-05): raw feed -> B1 -> dispatcher -> worker.

VÃ­a de ingreso multiinstrumento que REUTILIZA `LiveDataEngine` (B1) como
autoridad de normalizaciÃ³n/validaciÃ³n POR INSTRUMENTO. No reimplementa en
paralelo las reglas de LIVE-03 (dedup, orden, alineaciÃ³n, gaps): cada flujo
tiene su propio `LiveDataEngine`.

    MarketDataEvent(raw, symbol=market_data_symbol)
        -> LiveDataEngine del flujo (autoridad: validacion/dedup/orden/gap)
        -> ClosedBarEvent validado (symbol=market_data_symbol)
        -> dispatcher (normaliza identidad market_data_symbol -> atlas_id)
        -> MultiAssetPaperOrchestrator.on_closed_bar  -> worker(atlas_id)

Notas de contrato:
  * El worker recibe SOLO el `ClosedBarEvent` emitido por su motor (vÃ­a normal).
  * `MultiAssetPaperOrchestrator.on_closed_bar` sigue existiendo como entrada de
    eventos de DOMINIO **ya normalizados** (symbol == atlas_id); NO es la entrada
    para datos crudos de un adaptador. Los datos crudos entran por aquÃ­.
  * Un sÃ­mbolo de datos desconocido NO se descarta en silencio: se cuenta y se
    registra como rechazo observable (en memoria; ver lÃ­mite de persistencia).
  * Secuencial y determinista; sin threads/colas/async.
  * Los workers siguen con `enable_monitoring=False`; solo el orquestador escribe
    fuentes globales. Este ingress no toca el kill switch ni monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from execution.instrument import InstrumentSpec
from live.adapter import MarketDataAdapter
from live.engine import DataQuality, EngineState, LiveDataEngine
from live.events import ClosedBarEvent, MarketDataEvent

from papertrading.multi_asset import MultiAssetPaperOrchestrator


class IngressConfigError(ValueError):
    """Configuracion de flujo invalida/ambigua (fail-closed)."""


def _require_nonempty(value: object, *, name: str) -> str:
    if value is None or not str(value).strip():
        raise IngressConfigError(f"{name} no puede estar vacio")
    return str(value)


@dataclass(frozen=True, slots=True)
class InstrumentStreamConfig:
    """Configuracion de UN flujo de datos de mercado (identidad + intervalo).

    Separa explicitamente:
      * `atlas_id`: identificador canonico de Atlas (clave del worker).
      * `market_data_symbol`: simbolo del proveedor de datos (feed).
      * `broker_symbol`: simbolo del broker (opcional; para ejecucion futura).
      * `interval_seconds`: intervalo de mercado (NO es propiedad economica).
      * `instrument_spec`: contrato economico (MULTI-ASSET-03).
    """

    atlas_id: str
    market_data_symbol: str
    interval_seconds: int
    instrument_spec: InstrumentSpec
    broker_symbol: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.atlas_id, name="atlas_id")
        _require_nonempty(self.market_data_symbol, name="market_data_symbol")
        if isinstance(self.interval_seconds, bool) or not isinstance(
            self.interval_seconds, int
        ):
            raise IngressConfigError("interval_seconds debe ser entero")
        if self.interval_seconds <= 0:
            raise IngressConfigError("interval_seconds debe ser > 0")
        if not isinstance(self.instrument_spec, InstrumentSpec):
            raise IngressConfigError("instrument_spec debe ser InstrumentSpec")
        if self.instrument_spec.atlas_instrument_id != self.atlas_id:
            raise IngressConfigError(
                "instrument_spec.atlas_instrument_id "
                f"{self.instrument_spec.atlas_instrument_id!r} != atlas_id "
                f"{self.atlas_id!r}"
            )
        if self.broker_symbol is not None:
            _require_nonempty(self.broker_symbol, name="broker_symbol")


@dataclass
class StreamMetrics:
    """Contadores en MEMORIA por flujo (no durables; ver limite de persistencia).

    Fronteras medidas (explicitas):
      * `received`: eventos CRUDOS recibidos por `receive()` para este flujo.
      * `emitted`: velas CERRADAS emitidas por `LiveDataEngine` (B1) y despachadas
        al orquestador. NO implica aceptacion operativa del worker (que puede
        rechazarlas despues, p.ej. freshness/future); por eso NO se llama
        `accepted`.
      * `rejected`: eventos crudos descartados por B1 por invalidez
        (INVALID_TIMESTAMP/INVALID_OHLC/STALE).
      * `duplicates` / `late` / `gaps`: incidencias detectadas por B1.
    """

    received: int = 0
    emitted: int = 0
    rejected: int = 0
    duplicates: int = 0
    late: int = 0
    gaps: int = 0
    last_open_time_emitted: int | None = None
    last_reject_reason: str | None = None

    def snapshot(self) -> dict:
        return {
            "received": self.received,
            "emitted": self.emitted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "late": self.late,
            "gaps": self.gaps,
            "last_open_time_emitted": self.last_open_time_emitted,
            "last_reject_reason": self.last_reject_reason,
        }


@dataclass(frozen=True, slots=True)
class IngressEmission:
    """Emision NORMALIZADA de B1 lista para despacho (identidad = atlas_id)."""

    closed: ClosedBarEvent
    data_state: str
    data_quality: str


class MultiAssetIngress:
    """Dispatcher secuencial: raw MarketDataEvent -> LiveDataEngine -> worker."""

    def __init__(
        self,
        orchestrator: MultiAssetPaperOrchestrator,
        streams: Iterable[tuple[InstrumentStreamConfig, MarketDataAdapter]],
        *,
        stale_timeout_seconds: float = 60.0,
        engine_clock: Callable[[], float] | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._by_market_symbol: dict[str, InstrumentStreamConfig] = {}
        self._engines: dict[str, LiveDataEngine] = {}
        self.metrics: dict[str, StreamMetrics] = {}
        self.unknown_symbol_rejections: list[str] = []
        self._unknown_symbols = 0
        # Buffer de emisiones para procesamiento por GRUPO temporal (aditivo;
        # `receive()` conserva el despacho inmediato).
        self._buffering = False
        self._buffer: list[IngressEmission] | None = None

        seen_atlas: set[str] = set()
        for config, adapter in streams:
            if config.atlas_id in seen_atlas:
                raise IngressConfigError(
                    f"atlas_id duplicado en el ingress: {config.atlas_id!r}"
                )
            seen_atlas.add(config.atlas_id)
            if config.market_data_symbol in self._by_market_symbol:
                raise IngressConfigError(
                    "market_data_symbol ambiguo/duplicado: "
                    f"{config.market_data_symbol!r}"
                )
            worker = orchestrator.workers.get(config.atlas_id)
            if worker is None:
                raise IngressConfigError(
                    f"atlas_id {config.atlas_id!r} no registrado en el "
                    "orchestrator (workers)"
                )
            registered_spec = orchestrator.spec(config.atlas_id)
            if registered_spec != config.instrument_spec:
                raise IngressConfigError(
                    f"instrument_spec del flujo {config.atlas_id!r} no coincide "
                    "con el registrado en el orchestrator"
                )
            self._by_market_symbol[config.market_data_symbol] = config
            self.metrics[config.atlas_id] = StreamMetrics()
            self._engines[config.atlas_id] = LiveDataEngine(
                config.market_data_symbol,
                config.interval_seconds,
                adapter,
                stale_timeout_seconds=stale_timeout_seconds,
                on_closed_bar=self._on_engine_closed,
                **({} if engine_clock is None else {"clock": engine_clock}),
            )

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "MultiAssetIngress":
        """CONNECTING->CONNECTED de cada motor (adapter.connect/subscribe)."""
        for engine in self._engines.values():
            engine.start()
        return self

    def check_stale_all(self) -> dict[str, EngineState]:
        """Evalua `check_stale()` en cada motor (API real de B1) sin duplicar LIVE-03.

        Delega en `LiveDataEngine.check_stale()`; NO toca fuentes globales ni el
        kill switch. Devuelve el estado de cada flujo por `atlas_id`. Un flujo
        STALE es local y no debe propagarse automaticamente a los demas.
        """
        states: dict[str, EngineState] = {}
        for atlas_id, engine in self._engines.items():
            engine.check_stale()
            states[atlas_id] = engine.state
        return states
    # --------------------------------------------------------------- consulta

    def engine_for(self, atlas_id: str) -> LiveDataEngine:
        try:
            return self._engines[atlas_id]
        except KeyError:
            raise KeyError(f"flujo no registrado: {atlas_id!r}") from None

    @property
    def unknown_symbol_count(self) -> int:
        return self._unknown_symbols

    def snapshot(self) -> dict:
        return {
            "streams": {a: m.snapshot() for a, m in self.metrics.items()},
            "unknown_symbol_rejections": list(self.unknown_symbol_rejections),
        }

    # ---------------------------------------------------------------- ingreso

    def receive(self, event: MarketDataEvent) -> ClosedBarEvent | None:
        """Enruta un evento crudo al `LiveDataEngine` de su flujo.

        El dispatcher NO aplica politica propia de LIVE-03: solo enruta y
        clasifica el resultado/issue del motor (autoridad). Un simbolo de datos
        desconocido produce un rechazo observable y no afecta a los workers.
        """
        config = self._by_market_symbol.get(event.symbol)
        if config is None:
            self._unknown_symbols += 1
            self.unknown_symbol_rejections.append(event.symbol)
            return None
        engine = self._engines[config.atlas_id]
        metrics = self.metrics[config.atlas_id]
        metrics.received += 1

        before = list(engine.issues)
        emitted = engine.process(event)
        self._classify_new_issues(config.atlas_id, before, emitted)
        return emitted

    def receive_group(
        self, events: Iterable[MarketDataEvent]
    ) -> list[IngressEmission]:
        """Ingress de un GRUPO temporal sin despachar hasta completarlo.

        Aditivo (MULTI-ASSET-06): procesa los eventos crudos por sus motores B1
        (validacion/dedup/orden/gap) y ACUMULA las emisiones normalizadas en
        lugar de enviarlas al orquestador una a una. `receive()` conserva el
        despacho inmediato. No es reentrante.
        """
        if self._buffering:
            raise RuntimeError("receive_group no es reentrante")
        self._buffering = True
        self._buffer = []
        try:
            for event in events:
                self.receive(event)
            return list(self._buffer)
        finally:
            self._buffer = None
            self._buffering = False

    def _on_engine_closed(self, closed: ClosedBarEvent) -> None:
        """Callback de B1: normaliza identidad y despacha (o bufferiza) al worker."""
        config = self._by_market_symbol.get(closed.symbol)
        if config is None:  # pragma: no cover - engine solo emite su simbolo
            self._unknown_symbols += 1
            self.unknown_symbol_rejections.append(closed.symbol)
            return
        normalized = ClosedBarEvent(
            symbol=config.atlas_id,
            open_time=closed.open_time,
            open=closed.open,
            high=closed.high,
            low=closed.low,
            close=closed.close,
            volume=closed.volume,
            interval_seconds=closed.interval_seconds,
            emission_sequence=closed.emission_sequence,
        )
        engine = self._engines[config.atlas_id]
        metrics = self.metrics[config.atlas_id]
        metrics.emitted += 1
        metrics.last_open_time_emitted = closed.open_time
        emission = IngressEmission(
            closed=normalized,
            data_state=engine.state.value,
            data_quality=engine.quality.value,
        )
        if self._buffering:
            assert self._buffer is not None
            self._buffer.append(emission)
            return
        self._orchestrator.on_closed_bar(
            emission.closed,
            data_state=emission.data_state,
            data_quality=emission.data_quality,
        )

    # ------------------------------------------------------------ clasificacion

    def _classify_new_issues(
        self, atlas_id: str, before: list, emitted: ClosedBarEvent | None
    ) -> None:
        metrics = self.metrics[atlas_id]
        engine = self._engines[atlas_id]
        if emitted is not None:
            return  # emitido por B1 (contabilizado en el callback)
        new = self._new_issues(engine.issues, before)
        if not new:
            # No emitio y no hay issue nuevo (p.ej. DEGRADED/RECONNECTING ya
            # registrado): no fabricamos razon; se conserva el ultimo motivo.
            if engine.state in (EngineState.DEGRADED, EngineState.RECONNECTING):
                metrics.gaps += 1
                metrics.last_reject_reason = engine.quality.value
            return
        quality, detail = new[-1]
        metrics.last_reject_reason = f"{quality.value}: {detail}"
        if quality is DataQuality.DUPLICATE:
            metrics.duplicates += 1
        elif quality is DataQuality.OUT_OF_ORDER:
            metrics.late += 1
        elif quality in (DataQuality.GAP_DETECTED, DataQuality.INCOMPLETE_BAR):
            metrics.gaps += 1
        else:  # INVALID_TIMESTAMP / INVALID_OHLC / STALE
            metrics.rejected += 1

    @staticmethod
    def _new_issues(after: list, before: list) -> list:
        """Diferencia robusta ante la ventana rodante `issues[-100:]` de B1."""
        if len(after) >= len(before) and after[: len(before)] == before:
            return after[len(before):]
        return after[-1:] if after else []


__all__ = [
    "IngressConfigError",
    "IngressEmission",
    "InstrumentStreamConfig",
    "MultiAssetIngress",
    "StreamMetrics",
]
