"""Tests deterministas del adaptador Binance WebSocket (S2), sin red.

Batería S2:

  1.  mapeo de intervalos            -> segundos <-> nombre; no soportado falla
  2.  parse kline crudo (cerrada)    -> StreamData + MarketDataEvent
  3.  parse kline intrabar           -> is_closed False
  4.  stream combinado               -> desenvolver "data"
  5.  ack / pong / evento no-kline   -> ignorados (None), sin lanzar
  6.  JSON malformado / no objeto    -> StreamingProtocolError
  7.  símbolo inesperado             -> StreamingProtocolError
  8.  kline incompleto               -> StreamingProtocolError
  9.  open_time desalineado          -> StreamingProtocolError
  10. frame de error del proveedor   -> StreamingProtocolError
  11. connect/subscribe/receive      -> url /ws + frame SUBSCRIBE correcto
  12. receive salta control y datos  -> devuelve el StreamData
  13. receive timeout                -> None
  14. pérdida de conexión            -> StreamingConnectionError + estado
  15. subscribe sin conexión         -> StreamingConnectionError
  16. receive sin suscripción        -> StreamingSubscribeError
  17. connect fallido                -> estado DISCONNECTED y propaga
  18. disconnect limpio e idempotente
  19. solo lectura                   -> fuentes sin vías de órdenes/escritura
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from streaming import (
    BinanceStreamingAdapter,
    StreamConnectionState,
    StreamData,
    StreamHeartbeat,
    StreamingConnectionError,
    StreamingProtocolError,
    StreamingSubscribeError,
    interval_to_binance,
    parse_binance_message,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = 1_788_220_800  # 2026-09-01T00:00Z, alineado a 1h
HOUR = 3_600


def _clock() -> datetime:
    return FIXED_NOW


def _frame(
    open_time: int,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    o: str = "100.0",
    h: str = "102.0",
    l: str = "99.0",
    c: str = "101.0",
    v: str = "5.5",
    closed: bool = True,
) -> str:
    return json.dumps(
        {
            "e": "kline",
            "E": open_time * 1000,
            "s": symbol,
            "k": {
                "t": open_time * 1000,
                "T": (open_time + HOUR) * 1000 - 1,
                "s": symbol,
                "i": interval,
                "o": o,
                "c": c,
                "h": h,
                "l": l,
                "v": v,
                "x": closed,
            },
        }
    )


def _parse(raw: str, *, symbol: str = "BTCUSDT") -> StreamData | None:
    return parse_binance_message(
        raw, symbol=symbol, interval_seconds=HOUR, received_at=FIXED_NOW
    )


class FakeWebSocketTransport:
    """Transporte sandbox: frames prefijados (str), None (timeout) o excepción."""

    def __init__(
        self,
        frames: list[object] | None = None,
        *,
        open_error: BaseException | None = None,
    ) -> None:
        self.frames = list(frames or [])
        self.opens: list[tuple[str, float]] = []
        self.sent: list[str] = []
        self.close_calls = 0
        self._connected = False
        self._open_error = open_error

    def open(self, url: str, *, timeout: float) -> None:
        if self._open_error is not None:
            raise self._open_error
        self.opens.append((url, timeout))
        self._connected = True

    def send(self, payload: str) -> None:
        if not self._connected:
            raise StreamingConnectionError("transporte no conectado")
        self.sent.append(payload)

    def recv(self, *, timeout: float) -> str | None:
        if not self._connected:
            raise StreamingConnectionError("transporte no conectado")
        if not self.frames:
            return None
        item = self.frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]

    def close(self) -> None:
        self.close_calls += 1
        self._connected = False


def _adapter(transport: FakeWebSocketTransport) -> BinanceStreamingAdapter:
    return BinanceStreamingAdapter(transport=transport, clock=_clock)


# --------------------------------------------------------------- intervalos


def test_interval_to_binance() -> None:
    assert interval_to_binance(60) == "1m"
    assert interval_to_binance(3600) == "1h"
    with pytest.raises(StreamingSubscribeError):
        interval_to_binance(123)


# ----------------------------------------------------------------- parsing


def test_parse_kline_cerrada() -> None:
    data = _parse(_frame(BASE, o="100.0", h="102.0", l="99.0", c="101.0", v="5.5"))
    assert isinstance(data, StreamData)
    ev = data.event
    assert ev.symbol == "BTCUSDT"
    assert ev.open_time == BASE and ev.open_time % HOUR == 0
    assert (ev.open, ev.high, ev.low, ev.close, ev.volume) == (100.0, 102.0, 99.0, 101.0, 5.5)
    assert ev.is_closed is True
    assert data.received_at == FIXED_NOW


def test_parse_kline_intrabar() -> None:
    data = _parse(_frame(BASE, closed=False))
    assert data is not None and data.event.is_closed is False


def test_parse_stream_combinado() -> None:
    raw = json.dumps({"stream": "btcusdt@kline_1h", "data": json.loads(_frame(BASE))})
    data = _parse(raw)
    assert data is not None and data.event.open_time == BASE


def test_parse_frames_de_control_ignorados() -> None:
    assert _parse(json.dumps({"result": None, "id": 1})) is None
    pong = _parse(json.dumps({"pong": 1788220800000}))
    assert isinstance(pong, StreamHeartbeat)  # latido (S3)
    assert _parse(json.dumps({"e": "depthUpdate", "s": "BTCUSDT"})) is None


def test_parse_json_malformado() -> None:
    with pytest.raises(StreamingProtocolError):
        _parse("{no-json")
    with pytest.raises(StreamingProtocolError):
        _parse("[1, 2, 3]")


def test_parse_simbolo_inesperado() -> None:
    with pytest.raises(StreamingProtocolError):
        _parse(_frame(BASE, symbol="ETHUSDT"), symbol="BTCUSDT")


def test_parse_kline_incompleto() -> None:
    bad = json.dumps({"e": "kline", "s": "BTCUSDT", "k": {"t": BASE * 1000}})
    with pytest.raises(StreamingProtocolError):
        _parse(bad)


def test_parse_open_time_desalineado() -> None:
    with pytest.raises(StreamingProtocolError):
        _parse(_frame(BASE + 1))


def test_parse_error_del_proveedor() -> None:
    with pytest.raises(StreamingProtocolError):
        _parse(json.dumps({"error": {"code": 1, "msg": "bad"}}))


# ------------------------------------------------------------------ adapter


def test_connect_subscribe_envia_frame_correcto() -> None:
    transport = FakeWebSocketTransport()
    adapter = _adapter(transport)
    adapter.connect()
    assert adapter.state is StreamConnectionState.CONNECTED
    assert transport.opens == [("wss://stream.binance.com:9443/ws", 10.0)]

    adapter.subscribe("BTCUSDT", HOUR)
    assert adapter.subscription == ("BTCUSDT", HOUR)
    assert len(transport.sent) == 1
    frame = json.loads(transport.sent[0])
    assert frame["method"] == "SUBSCRIBE"
    assert frame["params"] == ["btcusdt@kline_1h"]


def test_receive_salta_control_y_devuelve_datos() -> None:
    transport = FakeWebSocketTransport(
        [json.dumps({"result": None, "id": 1}), _frame(BASE)]
    )
    adapter = _adapter(transport)
    adapter.connect()
    adapter.subscribe("BTCUSDT", HOUR)
    data = adapter.receive()
    assert isinstance(data, StreamData) and data.event.open_time == BASE


def test_receive_timeout_devuelve_none() -> None:
    transport = FakeWebSocketTransport([])
    adapter = _adapter(transport)
    adapter.connect()
    adapter.subscribe("BTCUSDT", HOUR)
    assert adapter.receive() is None


def test_receive_perdida_de_conexion() -> None:
    transport = FakeWebSocketTransport([StreamingConnectionError("drop")])
    adapter = _adapter(transport)
    adapter.connect()
    adapter.subscribe("BTCUSDT", HOUR)
    with pytest.raises(StreamingConnectionError):
        adapter.receive()
    assert adapter.state is StreamConnectionState.DISCONNECTED
    assert adapter.last_error is not None


def test_heartbeat_envia_ping_y_pong_emite_heartbeat() -> None:
    transport = FakeWebSocketTransport([json.dumps({"pong": 1})])
    adapter = _adapter(transport)
    adapter.connect()
    adapter.subscribe("BTCUSDT", HOUR)
    adapter.heartbeat()
    assert json.loads(transport.sent[-1]) == {"ping": 1}
    assert isinstance(adapter.receive(), StreamHeartbeat)


def test_subscribe_sin_conexion() -> None:
    adapter = _adapter(FakeWebSocketTransport())
    with pytest.raises(StreamingConnectionError):
        adapter.subscribe("BTCUSDT", HOUR)


def test_receive_sin_suscripcion() -> None:
    adapter = _adapter(FakeWebSocketTransport())
    adapter.connect()
    with pytest.raises(StreamingSubscribeError):
        adapter.receive()


def test_connect_fallido_deja_estado_disconnected() -> None:
    transport = FakeWebSocketTransport(open_error=StreamingConnectionError("no dns"))
    adapter = _adapter(transport)
    with pytest.raises(StreamingConnectionError):
        adapter.connect()
    assert adapter.state is StreamConnectionState.DISCONNECTED


def test_disconnect_limpio_e_idempotente() -> None:
    transport = FakeWebSocketTransport()
    adapter = _adapter(transport)
    adapter.connect()
    adapter.subscribe("BTCUSDT", HOUR)
    adapter.disconnect()
    assert adapter.state is StreamConnectionState.CLOSED
    assert adapter.subscription is None
    adapter.disconnect()
    assert transport.close_calls == 2
    assert adapter.state is StreamConnectionState.CLOSED


# --------------------------------------------------------------- solo lectura


def test_adaptador_solo_lectura() -> None:
    forbidden = [
        "/api/",
        "listenKey",
        "apiKey",
        "api_key",
        "X-MBX",
        "SIGNED",
        "secret",
        "order",
        "ORDER",
    ]
    source = (ROOT / "streaming" / "binance.py").read_text(
        encoding="utf-8"
    ) + (ROOT / "streaming" / "transport.py").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in source, f"token de escritura/órdenes encontrado: {token}"
