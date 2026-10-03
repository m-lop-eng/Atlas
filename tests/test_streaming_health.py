"""Tests deterministas de salud operacional del pipeline (S6).

Batería S6:

  1.  snapshot inicial -> UNKNOWN, contadores a 0
  2.  pipeline normal -> HEALTHY, última vela cerrada, heartbeat OK
  3.  reconexión -> contador de reconexiones
  4.  gap recuperado -> contador de gaps recuperados
  5.  gap no recuperable -> contador + integridad UNKNOWN + nivel UNKNOWN
  6.  errores consecutivos -> se acumulan y se resetean con un evento
  7.  freshness vencida -> heartbeat STALE + nivel DEGRADED
  8.  as_dict estructurado SIN campos de autorización
  9.  el monitor NO actúa (no cambia el estado del pipeline)
  10. polls repetidos del mismo reporte NO inflan los contadores
  11. last_closed_bar refleja SOLO una vela aceptada por B1
  12. el snapshot conserva el evidence() del IntegrityReport (sin duplicar S4)
"""

from __future__ import annotations

from datetime import datetime, timezone

from live.adapter import BackfillProvider
from live.engine import EngineState
from live.events import MarketDataEvent

from streaming import (
    BackoffPolicy,
    HeartbeatStatus,
    ReconnectManager,
    ReconnectPolicy,
    ReconnectState,
    StreamData,
    StreamHeartbeat,
    StreamingConnectionError,
    StreamingHealthLevel,
    StreamingHealthMonitor,
    StreamingMarketDataAdapter,
    StreamingPipeline,
)

FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"


class FakeMono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


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

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        return [b for b in self.bars if b.open_time >= start_open_time]


def _market(open_time: int, *, c: float = 1.5) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=SYMBOL,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=c,
        volume=1.0,
        is_closed=True,
    )


def _sdata(open_time: int) -> StreamData:
    return StreamData(received_at=FIXED_NOW, event=_market(open_time))


def _setup(script, historical, *, mono: FakeMono | None, timeout: float = 60.0):
    mono = mono or FakeMono()
    monitor = StreamingHealthMonitor(
        freshness_timeout_seconds=timeout,
        clock=lambda: FIXED_NOW,
        monotonic=mono,
    )
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
    pipeline = StreamingPipeline(
        symbol=SYMBOL,
        interval_seconds=HOUR,
        manager=manager,
        provider=FakeHistoricalProvider(historical),
        observer=monitor,
        clock=lambda: 0.0,
        stale_timeout_seconds=1e9,
    )
    return pipeline, monitor, manager, mono


# --------------------------------------------------------------- snapshots


def test_snapshot_inicial_unknown() -> None:
    pipeline, monitor, _, _ = _setup([], [], mono=None)
    snap = monitor.snapshot(pipeline)
    assert snap.level is StreamingHealthLevel.UNKNOWN
    assert snap.heartbeat is HeartbeatStatus.UNKNOWN
    assert snap.last_event_at is None
    assert snap.freshness_seconds is None
    assert snap.last_closed_bar is None
    assert (snap.reconnections, snap.gaps_recovered, snap.gaps_unrecoverable) == (0, 0, 0)
    assert snap.consecutive_errors == 0


def test_pipeline_normal_healthy() -> None:
    pipeline, monitor, _, _ = _setup(
        [_sdata(BASE), _sdata(BASE + HOUR)], [], mono=None
    )
    pipeline.connect()
    pipeline.poll()
    snap = monitor.snapshot(pipeline)
    assert snap.level is StreamingHealthLevel.HEALTHY
    assert snap.connection_state == "CONNECTED"
    assert snap.heartbeat is HeartbeatStatus.OK
    assert snap.integrity == "HEALTHY"
    assert snap.last_closed_bar == (SYMBOL, BASE + HOUR)
    assert snap.last_event_at == FIXED_NOW
    assert snap.freshness_seconds == 0.0


def test_reconexion_cuenta() -> None:
    pipeline, monitor, manager, _ = _setup(
        [_sdata(BASE), StreamingConnectionError("drop")], [], mono=None
    )
    pipeline.connect()
    pipeline.poll()
    snap = monitor.snapshot(pipeline)
    assert manager.state is ReconnectState.CONNECTED
    assert snap.reconnections == 1


def test_gap_recuperado_cuenta() -> None:
    historical = [_market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, monitor, _, _ = _setup(script, historical, mono=None)
    pipeline.connect()
    pipeline.poll()
    snap = monitor.snapshot(pipeline)
    assert snap.gaps_recovered == 1
    assert snap.gaps_unrecoverable == 0
    assert snap.integrity == "HEALTHY"
    assert snap.level is StreamingHealthLevel.HEALTHY


def test_gap_no_recuperable_cuenta() -> None:
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, monitor, manager, _ = _setup(script, [], mono=None)
    pipeline.connect()
    pipeline.poll()
    snap = monitor.snapshot(pipeline)
    assert snap.gaps_unrecoverable == 1
    assert snap.gaps_recovered == 0
    assert manager.data_integrity_unknown is True
    assert snap.integrity == "UNKNOWN"
    assert snap.level is StreamingHealthLevel.UNKNOWN


def test_poll_repetido_no_infla_gaps_unrecoverable() -> None:
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, monitor, manager, _ = _setup(script, [], mono=None)
    pipeline.connect()
    for _ in range(5):
        pipeline.poll()
        snap = monitor.snapshot(pipeline)
        assert snap.gaps_unrecoverable == 1
        assert snap.gaps_recovered == 0
        assert manager.data_integrity_unknown is True
        assert snap.integrity == "UNKNOWN"
        assert snap.level is StreamingHealthLevel.UNKNOWN


def test_last_closed_bar_solo_aceptadas_por_b1() -> None:
    script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, monitor, _, _ = _setup(script, [], mono=None)
    pipeline.connect()
    pipeline.poll()
    accepted = pipeline.closed_bars
    assert [(b.symbol, b.open_time) for b in accepted] == [
        (SYMBOL, BASE),
        (SYMBOL, BASE + HOUR),
    ]
    snap = monitor.snapshot(pipeline)
    assert snap.last_closed_bar == (accepted[-1].symbol, accepted[-1].open_time)
    assert snap.last_closed_bar != (SYMBOL, BASE + 3 * HOUR)


def test_snapshot_incluye_evidence_de_integridad() -> None:
    unrecoverable_script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    pipeline, monitor, _, _ = _setup(unrecoverable_script, [], mono=None)
    pipeline.connect()
    pipeline.poll()
    snap = monitor.snapshot(pipeline)
    assert snap.integrity == "UNKNOWN"
    assert snap.last_integrity_evidence is not None
    assert snap.last_integrity_evidence["status"] == "UNRECOVERABLE"
    assert snap.last_integrity_evidence["recovered"] == 0
    assert snap.last_integrity_evidence["reason"]

    recovered_script = [
        _sdata(BASE),
        _sdata(BASE + HOUR),
        StreamingConnectionError("drop"),
        _sdata(BASE + 3 * HOUR),
    ]
    historical = [_market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    ok_pipeline, ok_monitor, _, _ = _setup(recovered_script, historical, mono=None)
    ok_pipeline.connect()
    ok_pipeline.poll()
    ok = ok_monitor.snapshot(ok_pipeline)
    assert ok.integrity == "HEALTHY"
    assert ok.last_integrity_evidence is not None
    assert ok.last_integrity_evidence["status"] == "HEALTHY"
    assert ok.last_integrity_evidence["recovered"] > 0


def test_errores_consecutivos_reset() -> None:
    pipeline, monitor, _, _ = _setup([], [], mono=None)
    monitor.on_stream_error(StreamingConnectionError("e1"))
    monitor.on_stream_error(StreamingConnectionError("e2"))
    assert monitor.snapshot(pipeline).consecutive_errors == 2
    monitor.on_stream_event(StreamHeartbeat(received_at=FIXED_NOW))
    snap = monitor.snapshot(pipeline)
    assert snap.consecutive_errors == 0
    assert snap.last_error is not None


def test_freshness_vencida_degraded() -> None:
    mono = FakeMono()
    pipeline, monitor, _, _ = _setup(
        [_sdata(BASE)], [], mono=mono, timeout=10.0
    )
    pipeline.connect()
    pipeline.poll()
    assert monitor.snapshot(pipeline).heartbeat is HeartbeatStatus.OK
    mono.advance(11.0)
    snap = monitor.snapshot(pipeline)
    assert snap.heartbeat is HeartbeatStatus.STALE
    assert snap.freshness_seconds == 11.0
    assert snap.level is StreamingHealthLevel.DEGRADED


def test_as_dict_sin_autorizacion() -> None:
    pipeline, monitor, _, _ = _setup([_sdata(BASE)], [], mono=None)
    pipeline.connect()
    pipeline.poll()
    data = monitor.snapshot(pipeline).as_dict()
    for key in (
        "level",
        "checked_at",
        "connection_state",
        "heartbeat",
        "integrity",
        "pipeline_state",
        "last_event_at",
        "freshness_seconds",
        "last_closed_bar",
        "reconnections",
        "gaps_recovered",
        "gaps_unrecoverable",
        "consecutive_errors",
        "last_error",
        "last_integrity_evidence",
    ):
        assert key in data
    for forbidden in ("can_trade", "authorized", "authorization", "allow", "permission"):
        assert forbidden not in data


def test_monitor_no_actua() -> None:
    pipeline, monitor, manager, _ = _setup(
        [_sdata(BASE), StreamingConnectionError("drop")], [], mono=None
    )
    pipeline.connect()
    pipeline.poll()
    before_state = pipeline.engine.state
    before_integrity = manager.data_integrity_unknown
    monitor.snapshot(pipeline)
    monitor.snapshot(pipeline)
    assert pipeline.engine.state is before_state
    assert manager.data_integrity_unknown is before_integrity
    assert pipeline.engine.state is EngineState.CONNECTED
