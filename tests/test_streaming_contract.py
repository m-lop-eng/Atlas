"""Tests del contrato de streaming (S1).

Batería S1 (contrato aislado, sin red):

  1. contrato abstracto        -> no instanciable
  2. aislamiento de B1         -> contrato propio, no hereda MarketDataAdapter
  3. timestamps UTC            -> naive/offset != 0 rechazado; epoch intacto
  4. discriminante de eventos  -> kind estable por tipo
  5. inmutabilidad             -> StreamEvent frozen
  6. sandbox: ciclo de vida    -> connect/subscribe/receive/disconnect + orden
  7. sandbox: guards           -> subscribe sin conexión / intervalo inválido
  8. sandbox: errores tipados  -> excepción scriptada propagada tal cual
  9. taxonomía de errores      -> todos heredan de StreamingError
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live.adapter import MarketDataAdapter
from live.events import MarketDataEvent
from streaming import (
    ScriptedStreamingAdapter,
    StreamConnected,
    StreamConnectionState,
    StreamData,
    StreamDisconnected,
    StreamEvent,
    StreamEventKind,
    StreamHeartbeat,
    StreamingConnectionError,
    StreamingError,
    StreamingHeartbeatError,
    StreamingMarketDataAdapter,
    StreamingProtocolError,
    StreamingReconnectError,
    StreamingSubscribeError,
    StreamingTimeoutError,
    ensure_utc,
    utc_now,
)

BASE = 1_788_220_800  # 2026-09-01T00:00Z, alineado a 1h
HOUR = 3_600


def _data(open_time: int = BASE, *, close: float = 100.0) -> StreamData:
    return StreamData(
        received_at=utc_now(),
        event=MarketDataEvent(
            symbol="BTCUSDT",
            open_time=open_time,
            open=100.0,
            high=101.0,
            low=99.0,
            close=close,
            volume=5.0,
            is_closed=True,
        ),
    )


# --------------------------------------------------------------- contrato


def test_contrato_es_abstracto() -> None:
    with pytest.raises(TypeError):
        StreamingMarketDataAdapter()  # type: ignore[abstract]


def test_contrato_no_hereda_de_b1() -> None:
    assert not issubclass(StreamingMarketDataAdapter, MarketDataAdapter)


# --------------------------------------------------------------- timestamps


def test_received_at_debe_ser_utc_aware() -> None:
    naive = datetime(2026, 9, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        StreamConnected(received_at=naive)
    offset = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(ValueError):
        StreamConnected(received_at=offset)


def test_server_time_si_presente_debe_ser_utc() -> None:
    with pytest.raises(ValueError):
        StreamHeartbeat(received_at=utc_now(), server_time=datetime(2026, 9, 1))
    hb = StreamHeartbeat(received_at=utc_now(), server_time=None)
    assert hb.server_time is None


def test_ensure_utc_devuelve_el_mismo_instante() -> None:
    now = utc_now()
    assert ensure_utc(now) is now


def test_epoch_de_datos_intacto() -> None:
    data = _data(BASE)
    assert data.event.open_time == BASE
    assert data.event.open_time % HOUR == 0


# --------------------------------------------------------------- eventos


def test_kind_por_tipo() -> None:
    assert _data().kind is StreamEventKind.DATA
    assert StreamHeartbeat(received_at=utc_now()).kind is StreamEventKind.HEARTBEAT
    assert StreamConnected(received_at=utc_now()).kind is StreamEventKind.CONNECTED
    assert (
        StreamDisconnected(received_at=utc_now()).kind is StreamEventKind.DISCONNECTED
    )


def test_stream_event_es_inmutable() -> None:
    event = StreamConnected(received_at=utc_now())
    with pytest.raises(Exception):
        event.received_at = utc_now()  # type: ignore[misc]
    assert isinstance(event, StreamEvent)


def test_disconnected_expected_flag() -> None:
    lost = StreamDisconnected(received_at=utc_now(), reason="dropped", expected=False)
    closed = StreamDisconnected(received_at=utc_now(), reason="client", expected=True)
    assert lost.expected is False
    assert closed.expected is True


# --------------------------------------------------------------- sandbox


def test_sandbox_ciclo_de_vida_y_orden() -> None:
    clock = iter(
        [
            datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc),
        ]
    )
    adapter = ScriptedStreamingAdapter(
        provider_name="sandbox",
        events=[_data(BASE), StreamHeartbeat(received_at=utc_now())],
        clock=lambda: next(clock),
    )
    assert adapter.state is StreamConnectionState.DISCONNECTED

    adapter.connect()
    assert adapter.state is StreamConnectionState.CONNECTED
    adapter.subscribe("BTCUSDT", HOUR)
    assert adapter.subscription == ("BTCUSDT", HOUR)

    first = adapter.receive()
    assert isinstance(first, StreamConnected)
    assert first.provider == "sandbox"
    assert isinstance(adapter.receive(), StreamData)
    assert isinstance(adapter.receive(), StreamHeartbeat)
    assert adapter.receive() is None

    adapter.disconnect()
    assert adapter.state is StreamConnectionState.CLOSED
    tail = adapter.receive()
    assert isinstance(tail, StreamDisconnected) and tail.expected is True


def test_sandbox_subscribe_sin_conexion_falla() -> None:
    adapter = ScriptedStreamingAdapter()
    with pytest.raises(StreamingConnectionError):
        adapter.subscribe("BTCUSDT", HOUR)


def test_sandbox_subscribe_invalido_falla() -> None:
    adapter = ScriptedStreamingAdapter()
    adapter.connect()
    adapter.receive()  # drena el StreamConnected
    with pytest.raises(StreamingSubscribeError):
        adapter.subscribe("BTCUSDT", 0)


def test_sandbox_receive_propaga_error_tipado() -> None:
    adapter = ScriptedStreamingAdapter(
        events=[StreamingProtocolError("frame malformado")]
    )
    adapter.connect()
    adapter.receive()  # drena el StreamConnected
    with pytest.raises(StreamingProtocolError):
        adapter.receive()


# --------------------------------------------------------------- errores


def test_taxonomia_de_errores() -> None:
    for exc in (
        StreamingConnectionError,
        StreamingSubscribeError,
        StreamingProtocolError,
        StreamingTimeoutError,
        StreamingHeartbeatError,
        StreamingReconnectError,
    ):
        assert issubclass(exc, StreamingError)
