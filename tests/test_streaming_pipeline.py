"""Tests deterministas de la integración Streaming -> B1 (S5), sin red.

Regla verificada: B1 (`LiveDataEngine`) es la autoridad; el streaming no crea
un camino paralelo. Ningún dato llega a `ClosedBarEvent` sin pasar por B1, y
tras una reconexión B1 solo emite barras recuperadas si S4 da HEALTHY.

Batería S5:

  1.  streaming normal -> B1 emite ClosedBarEvent
  2.  vela incompleta -> no se emite
  3.  desconexión -> B1 sin flujo (RECONNECTING/FAILED)
  4.  reconexión -> integridad UNKNOWN
  5.  backfill válido -> S4 HEALTHY -> B1 reconcile -> continúa
  6.  gap no recuperable -> B1 no continúa (DEGRADED)
  7.  duplicado -> no se duplica ClosedBarEvent
  8.  out-of-order -> no contamina B1
  9.  conflicto -> bloqueo, B1 no continúa
 10.  reinicio/replay -> estado consistente (determinista)
 11.  reconcile_open_ended: contiguo/faltante/conflicto/vacío
 12.  sin estrategia/órdenes: bridge no importa brokers/execution/strategies
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from live.adapter import BackfillProvider
from live.engine import EngineState
from live.events import ClosedBarEvent, MarketDataEvent

from streaming import (
    BackoffPolicy,
    IntegrityReconciler,
    IntegrityStatus,
    ReconnectManager,
    ReconnectPolicy,
    ReconnectState,
    StreamData,
    StreamingConnectionError,
    StreamingHealthMonitor,
    StreamingMarketDataAdapter,
    StreamingPipeline,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"


# --------------------------------------------------------------- utilidades


def _market(open_time: int, *, closed: bool = True, c: float = 1.5) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=SYMBOL,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=c,
        volume=1.0,
        is_closed=closed,
    )


def _sdata(open_time: int, *, closed: bool = True) -> StreamData:
    return StreamData(received_at=FIXED_NOW, event=_market(open_time, closed=closed))


class FakeStreamingAdapter(StreamingMarketDataAdapter):
    def __init__(self, script=None) -> None:
        self._script = list(script or [])
        self._connected = False
        self._subscription = None
        self.connect_error: BaseException | None = None

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def state(self):  # type: ignore[override]
        return "CONNECTED" if self._connected else "DISCONNECTED"

    @property
    def subscription(self):
        return self._subscription

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbol, interval_seconds) -> None:
        if not self._connected:
            raise StreamingConnectionError("no conectado")
        self._subscription = (symbol, interval_seconds)

    def receive(self):
        if not self._connected:
            raise StreamingConnectionError("no conectado")
        if not self._script:
            return None
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeHistoricalProvider(BackfillProvider):
    def __init__(self, bars=None) -> None:
        self.bars = list(bars or [])
        self.calls: list[tuple[str, int, int]] = []

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        self.calls.append((symbol, interval_seconds, start_open_time))
        return [b for b in self.bars if b.open_time >= start_open_time]


def _closed(open_time: int, *, seq: int = 1, symbol: str = SYMBOL) -> ClosedBarEvent:
    return ClosedBarEvent(
        symbol=symbol,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1.0,
        interval_seconds=HOUR,
        emission_sequence=seq,
    )


def _pipeline(script, historical, *, observer=None):
    adapter = FakeStreamingAdapter(script)
    manager = ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
        clock=lambda: 0.0,
        wall_clock=lambda: FIXED_NOW,
        sleep=lambda _s: None,
    )
    provider = FakeHistoricalProvider(historical)
    pipeline = StreamingPipeline(
        symbol=SYMBOL,
        interval_seconds=HOUR,
        manager=manager,
        provider=provider,
        observer=observer,
        clock=lambda: 0.0,
        stale_timeout_seconds=1e9,
    )
    return pipeline, adapter, provider, manager


def _open_times(pipeline: StreamingPipeline) -> list[int]:
    return [bar.open_time for bar in pipeline.closed_bars]


# --------------------------------------------------------------- pipeline


def test_streaming_normal_b1_emite() -> None:
    pipeline, _, _, _ = _pipeline([_sdata(BASE), _sdata(BASE + HOUR)], [])
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE, BASE + HOUR]
    assert pipeline.engine.state is EngineState.CONNECTED


def test_vela_incompleta_no_emite() -> None:
    pipeline, _, _, _ = _pipeline([_sdata(BASE, closed=False)], [])
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == []


def test_desconexion_sin_flujo() -> None:
    pipeline, adapter, _, manager = _pipeline(
        [_sdata(BASE), StreamingConnectionError("drop")], []
    )
    pipeline.connect()
    adapter.connect_error = StreamingConnectionError("sigue caído")
    pipeline.poll()
    assert _open_times(pipeline) == [BASE]
    assert pipeline.engine.state is EngineState.RECONNECTING
    assert manager.state is ReconnectState.FAILED


def test_reconexion_integridad_unknown() -> None:
    pipeline, _, _, manager = _pipeline(
        [_sdata(BASE), StreamingConnectionError("drop")], []
    )
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE]
    assert manager.data_integrity_unknown is True
    assert manager.state is ReconnectState.CONNECTED


def test_backfill_valido_b1_continua() -> None:
    historical = [_market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, _, provider, manager = _pipeline(script, historical)
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE, BASE + HOUR, BASE + 2 * HOUR, BASE + 3 * HOUR]
    assert pipeline.engine.state is EngineState.CONNECTED
    assert manager.data_integrity_unknown is False
    assert pipeline.backfill.last_report is not None
    assert pipeline.backfill.last_report.status is IntegrityStatus.HEALTHY
    assert provider.calls == [(SYMBOL, HOUR, BASE + 2 * HOUR)]


def test_gap_no_recuperable_no_continua() -> None:
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, _, _, manager = _pipeline(script, historical=[])
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE, BASE + HOUR]
    assert pipeline.engine.state is EngineState.DEGRADED
    assert manager.data_integrity_unknown is True
    assert pipeline.backfill.last_report.status is IntegrityStatus.UNRECOVERABLE


def test_duplicado_no_duplica_cierre() -> None:
    pipeline, _, _, _ = _pipeline([_sdata(BASE), _sdata(BASE)], [])
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE]


def test_out_of_order_no_contamina_b1() -> None:
    pipeline, _, _, _ = _pipeline(
        [_sdata(BASE), _sdata(BASE + HOUR), _sdata(BASE)], []
    )
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE, BASE + HOUR]


def test_conflicto_bloquea() -> None:
    historical = [_market(BASE + 2 * HOUR, c=1.5), _market(BASE + 2 * HOUR, c=1.6)]
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, _, _, manager = _pipeline(script, historical)
    pipeline.connect()
    pipeline.poll()
    assert _open_times(pipeline) == [BASE, BASE + HOUR]
    assert pipeline.engine.state is EngineState.DEGRADED
    assert manager.data_integrity_unknown is True
    assert pipeline.backfill.last_report.status is IntegrityStatus.BLOCKED


def test_replay_consistente() -> None:
    historical = [_market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    first, _, _, _ = _pipeline(script, historical)
    first.connect()
    first.poll()
    second, _, _, _ = _pipeline(script, historical)
    second.connect()
    second.poll()
    assert _open_times(first) == _open_times(second)
    assert first.engine.state is second.engine.state


# --------------------------------------------------------------- open-ended


def _reconciler(bars):
    return IntegrityReconciler(FakeHistoricalProvider(bars), SYMBOL, HOUR)


def test_open_ended_contiguo_healthy() -> None:
    report = _reconciler([_market(BASE + HOUR), _market(BASE + 2 * HOUR)]).reconcile_open_ended(
        start_open_time=BASE + HOUR
    )
    assert report.status is IntegrityStatus.HEALTHY
    assert [b.open_time for b in report.recovered] == [BASE + HOUR, BASE + 2 * HOUR]
    assert report.ok


def test_open_ended_sin_vela_inicial_unrecoverable() -> None:
    report = _reconciler([_market(BASE + 2 * HOUR)]).reconcile_open_ended(
        start_open_time=BASE + HOUR
    )
    assert report.status is IntegrityStatus.UNRECOVERABLE
    assert not report.ok


def test_open_ended_conflicto_blocked() -> None:
    report = _reconciler(
        [_market(BASE + HOUR, c=1.5), _market(BASE + HOUR, c=1.6)]
    ).reconcile_open_ended(start_open_time=BASE + HOUR)
    assert report.status is IntegrityStatus.BLOCKED


def test_open_ended_vacio_unrecoverable() -> None:
    report = _reconciler([]).reconcile_open_ended(start_open_time=BASE + HOUR)
    assert report.status is IntegrityStatus.UNRECOVERABLE


# --------------------------------------------------------------- aislamiento


def test_bridge_no_importa_ejecucion() -> None:
    source = (ROOT / "streaming" / "bridge.py").read_text(encoding="utf-8")
    forbidden = [
        r"from\s+brokers",
        r"import\s+brokers",
        r"from\s+execution",
        r"import\s+execution",
        r"from\s+strategies",
        r"import\s+strategies",
        r"OrderManager",
        r"PaperBroker",
    ]
    for pattern in forbidden:
        assert re.search(pattern, source) is None, f"referencia prohibida: {pattern}"


# --------------------------------------------------------------- warm_start


def test_warm_start_no_observa_s6() -> None:
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    pipeline, _, provider, manager = _pipeline(
        [_sdata(BASE), _sdata(BASE + HOUR)], [], observer=monitor
    )
    pipeline.connect()
    pipeline.warm_start(_closed(BASE, seq=5))
    assert _open_times(pipeline) == []
    assert provider.calls == []
    assert manager.data_integrity_unknown is False
    snap = monitor.snapshot(pipeline)
    assert snap.last_event_at is None
    assert snap.last_closed_bar is None
    assert snap.reconnections == 0


def test_warm_start_gap_recuperado() -> None:
    historical = [
        _market(BASE + HOUR),
        _market(BASE + 2 * HOUR),
        _market(BASE + 3 * HOUR),
    ]
    script = [_sdata(BASE + 3 * HOUR)]
    pipeline, _, provider, manager = _pipeline(script, historical)
    pipeline.connect()
    pipeline.warm_start(_closed(BASE, seq=5))
    pipeline.poll()
    assert _open_times(pipeline) == [BASE + HOUR, BASE + 2 * HOUR, BASE + 3 * HOUR]
    assert pipeline.engine.state is EngineState.CONNECTED
    assert manager.data_integrity_unknown is False
    assert pipeline.closed_bars[-1].emission_sequence == 8


def test_warm_start_sin_backfill_hasta_poll() -> None:
    pipeline, _, provider, manager = _pipeline([_sdata(BASE + HOUR)], [])
    pipeline.connect()
    pipeline.warm_start(_closed(BASE))
    assert provider.calls == []
    assert manager.data_integrity_unknown is False
    assert pipeline.engine.state is EngineState.CONNECTED
    pipeline.poll()
    assert _open_times(pipeline) == [BASE + HOUR]


def test_warm_start_requiere_connect() -> None:
    pipeline, _, _, _ = _pipeline([], [])
    with pytest.raises(RuntimeError):
        pipeline.warm_start(_closed(BASE))
    assert _open_times(pipeline) == []
