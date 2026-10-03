"""Tests deterministas del ReconnectManager (S3), sin red.

Batería S3:

  1.  backoff determinista y acotado
  2.  connect + re-suscripción + StreamConnected
  3.  sin conexiones duplicadas
  4.  dedup de cierres; los intrabar (updates) sí pasan
  5.  desconexión -> reconecta, re-suscribe, marca integridad, eventos de control
  6.  intentos agotados -> FAILED + StreamingReconnectError
  7.  heartbeat perdido -> reconexión
  8.  heartbeat con pong -> restaura actividad sin reconectar
  9.  integridad NO se limpia sola; solo confirm_data_integrity()
  10. disconnect -> CLOSED
  11. timeout de lectura sostenido -> reconexión
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live.events import MarketDataEvent
from streaming import (
    BackoffPolicy,
    ReconnectManager,
    ReconnectPolicy,
    ReconnectState,
    StreamConnected,
    StreamData,
    StreamDisconnected,
    StreamHeartbeat,
    StreamingConnectionError,
    StreamingMarketDataAdapter,
    StreamingReconnectError,
    StreamingTimeoutError,
)

FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = 1_788_220_800
HOUR = 3_600


# --------------------------------------------------------------- utilidades


def _clock_now() -> datetime:
    return FIXED_NOW


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _sleep_recorder(clock: FakeClock) -> tuple[list[float], object]:
    delays: list[float] = []

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.advance(seconds)

    return delays, sleep


class FakeAdapter(StreamingMarketDataAdapter):
    def __init__(
        self,
        script: list[object] | None = None,
        *,
        name: str = "fake",
    ) -> None:
        self._script = list(script or [])
        self._name = name
        self._connected = False
        self._subscription: tuple[str, int] | None = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.subscribe_calls = 0
        self.heartbeat_calls = 0
        self.connect_error: BaseException | None = None
        self.heartbeat_error: BaseException | None = None

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def state(self) -> str:  # type: ignore[override]
        return "CONNECTED" if self._connected else "DISCONNECTED"

    @property
    def subscription(self) -> tuple[str, int] | None:
        return self._subscription

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self._connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        if not self._connected:
            raise StreamingConnectionError("no conectado")
        self.subscribe_calls += 1
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

    def heartbeat(self) -> None:
        self.heartbeat_calls += 1
        if self.heartbeat_error is not None:
            raise self.heartbeat_error


def _data(open_time: int, *, closed: bool = True, symbol: str = "BTCUSDT") -> StreamData:
    return StreamData(
        received_at=FIXED_NOW,
        event=MarketDataEvent(
            symbol=symbol,
            open_time=open_time,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=1.0,
            is_closed=closed,
        ),
    )


def _hb() -> StreamHeartbeat:
    return StreamHeartbeat(received_at=FIXED_NOW)


def _manager(
    adapter: FakeAdapter,
    clock: FakeClock,
    *,
    policy: ReconnectPolicy | None = None,
    **kwargs,
) -> ReconnectManager:
    return ReconnectManager(
        adapter,
        policy=policy or ReconnectPolicy(),
        clock=clock,
        wall_clock=_clock_now,
        **kwargs,
    )


# --------------------------------------------------------------- backoff


def test_backoff_determinista_y_acotado() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, factor=2.0, max_delay_seconds=5.0)
    assert policy.sequence(5) == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert policy.sequence(0) == []
    with pytest.raises(ValueError):
        policy.delay_for(0)


# --------------------------------------------------------------- conexión


def test_connect_suscribe_y_emite_connected() -> None:
    adapter = FakeAdapter()
    clock = FakeClock()
    delays, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    assert mgr.state is ReconnectState.CONNECTED
    assert adapter.connect_calls == 1
    assert adapter.subscribe_calls == 1
    assert isinstance(mgr.receive(), StreamConnected)


def test_no_conexiones_duplicadas() -> None:
    adapter = FakeAdapter()
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.connect()
    mgr.connect()
    assert adapter.connect_calls == 1


def test_dedup_cierres_intrabar_pasa() -> None:
    adapter = FakeAdapter(
        [_data(BASE), _data(BASE), _data(BASE, closed=False), _data(BASE, closed=False)]
    )
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.connect()
    mgr.receive()  # StreamConnected
    assert isinstance(mgr.receive(), StreamData)  # primer cierre
    assert mgr.receive() is None  # cierre duplicado: no se republica
    assert isinstance(mgr.receive(), StreamData)  # intrabar 1
    assert isinstance(mgr.receive(), StreamData)  # intrabar 2 (update)


# --------------------------------------------------------------- reconexión


def test_desconexion_reconecta_resuscribe_y_marca_integridad() -> None:
    adapter = FakeAdapter([StreamingConnectionError("drop"), _data(BASE)])
    clock = FakeClock()
    delays, sleep = _sleep_recorder(clock)
    policy = ReconnectPolicy(
        max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
    )
    mgr = _manager(adapter, clock, policy=policy, sleep=sleep)
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    assert isinstance(mgr.receive(), StreamConnected)

    out = mgr.receive()  # dispara la recuperación
    assert isinstance(out, StreamDisconnected) and out.expected is False
    assert mgr.state is ReconnectState.CONNECTED
    assert mgr.attempts == 0
    assert adapter.subscribe_calls == 2
    assert delays == [1.0]
    assert mgr.data_integrity_unknown is True

    assert isinstance(mgr.receive(), StreamConnected)  # evento de reconexión
    assert isinstance(mgr.receive(), StreamData)


def test_intentos_agotados_failed() -> None:
    adapter = FakeAdapter([StreamingConnectionError("drop")])
    clock = FakeClock()
    delays, sleep = _sleep_recorder(clock)
    policy = ReconnectPolicy(
        max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
    )
    mgr = _manager(adapter, clock, policy=policy, sleep=sleep)
    mgr.connect()
    mgr.receive()  # StreamConnected
    adapter.connect_error = StreamingConnectionError("sigue caído")

    with pytest.raises(StreamingReconnectError):
        mgr.receive()
    assert mgr.state is ReconnectState.FAILED
    assert mgr.attempts == 3
    assert delays == [1.0, 2.0, 4.0]
    assert mgr.data_integrity_unknown is True


# --------------------------------------------------------------- heartbeat


def test_heartbeat_perdido_reconecta() -> None:
    adapter = FakeAdapter()  # receive devuelve None (sin datos)
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(
        adapter,
        clock,
        sleep=sleep,
        heartbeat_timeout_seconds=10.0,
        pong_timeout_seconds=5.0,
        poll_interval_seconds=1.0,
    )
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    mgr.receive()  # StreamConnected
    clock.advance(10.0)  # vence el heartbeat

    out = mgr.receive()
    assert adapter.heartbeat_calls == 1
    assert isinstance(out, StreamDisconnected)
    assert adapter.connect_calls == 2
    assert mgr.state is ReconnectState.CONNECTED


def test_heartbeat_pong_restaura_sin_reconectar() -> None:
    adapter = FakeAdapter([_hb()])
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(
        adapter,
        clock,
        sleep=sleep,
        heartbeat_timeout_seconds=10.0,
        pong_timeout_seconds=5.0,
        poll_interval_seconds=1.0,
    )
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    mgr.receive()  # StreamConnected
    clock.advance(10.0)

    out = mgr.receive()
    assert isinstance(out, StreamHeartbeat)
    assert adapter.heartbeat_calls == 1
    assert adapter.connect_calls == 1
    assert mgr.state is ReconnectState.CONNECTED
    assert mgr.data_integrity_unknown is False


# --------------------------------------------------------------- integridad


def test_integridad_no_se_limpia_sola() -> None:
    adapter = FakeAdapter([StreamingConnectionError("drop"), _data(BASE)])
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    mgr.receive()  # StreamConnected
    mgr.receive()  # desconexión + reconexión

    assert mgr.data_integrity_unknown is True
    # Más actividad no la limpia (transporte != integridad)
    mgr.receive()  # StreamConnected
    mgr.receive()  # StreamData
    assert mgr.data_integrity_unknown is True

    mgr.confirm_data_integrity()
    assert mgr.data_integrity_unknown is False


# --------------------------------------------------------------- timeout


def test_timeout_de_lectura_reconecta() -> None:
    adapter = FakeAdapter([StreamingTimeoutError("sin datos")])
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.subscribe("BTCUSDT", HOUR)
    mgr.connect()
    mgr.receive()  # StreamConnected

    out = mgr.receive()
    assert isinstance(out, StreamDisconnected)
    assert adapter.connect_calls == 2
    assert mgr.state is ReconnectState.CONNECTED


# --------------------------------------------------------------- cierre


def test_disconnect_cierra() -> None:
    adapter = FakeAdapter()
    clock = FakeClock()
    _, sleep = _sleep_recorder(clock)
    mgr = _manager(adapter, clock, sleep=sleep)
    mgr.connect()
    mgr.disconnect()
    assert mgr.state is ReconnectState.CLOSED
    with pytest.raises(StreamingConnectionError):
        mgr.receive()
