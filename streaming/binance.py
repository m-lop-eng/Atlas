"""Adaptador Binance WebSocket (S2), SOLO LECTURA.

Flujo:

    Binance WebSocket  ->  BinanceStreamingAdapter  ->  StreamEvent
                                                     ->  MarketDataEvent

Alcance S2 (aislado de B1 y de A4): conexión WebSocket real, suscripción a un
símbolo/intervalo, recepción y validación básica del protocolo público de
klines, normalización a `MarketDataEvent`, timestamps UTC-aware y distinción
vela cerrada / intrabar, `connect`/`disconnect`/`subscribe`/`receive`, estados
de `StreamingMarketDataAdapter`, errores tipados de S1 y desconexión limpia.

README DE SOLO LECTURA: solo market data público (klines). NO hay claves API,
ni endpoints de órdenes, ni ninguna vía de escritura. No existe camino desde
este módulo hacia APIs de trading.

Fuera de S2 (deliberadamente NO implementado): reconexión/backoff (S3),
heartbeat propio (S3), backfill (S4), integración con `LiveDataEngine`,
PaperRunner, OrderManager/brokers ni lógica de estrategia.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from live.events import MarketDataEvent

from streaming.adapter import StreamConnectionState, StreamingMarketDataAdapter
from streaming.clock import utc_now
from streaming.errors import (
    StreamingConnectionError,
    StreamingProtocolError,
    StreamingSubscribeError,
)
from streaming.events import StreamData, StreamHeartbeat
from streaming.transport import WebSocketTransport, WebsocketClientTransport

# Endpoint público de market data (sin autenticación).
BINANCE_WS_BASE = "wss://stream.binance.com:9443"

# intervalos Binance soportados: segundos -> nombre del stream.
BINANCE_INTERVALS: dict[int, str] = {
    1: "1s",
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    7200: "2h",
    14400: "4h",
    21600: "6h",
    28800: "8h",
    43200: "12h",
    86400: "1d",
    259200: "3d",
    604800: "1w",
}


def interval_to_binance(interval_seconds: int) -> str:
    """Traduce segundos al nombre de intervalo de Binance.

    Raises:
        StreamingSubscribeError: si el intervalo no está soportado.
    """
    try:
        return BINANCE_INTERVALS[interval_seconds]
    except KeyError as exc:
        raise StreamingSubscribeError(
            f"intervalo no soportado por Binance: {interval_seconds}s"
        ) from exc


def parse_binance_message(
    raw: str,
    *,
    symbol: str,
    interval_seconds: int,
    received_at: datetime,
) -> StreamData | StreamHeartbeat | None:
    """Normaliza un frame de Binance a `StreamData`/`StreamHeartbeat` (o None).

    Acepta tanto el stream crudo (`{"e": "kline", ...}`) como el combinado
    (`{"stream": ..., "data": {...}}`). El ack de suscripción se ignora
    (None); el pong se emite como `StreamHeartbeat` (liveness, S3). Los frames
    de error o malformados lanzan `StreamingProtocolError`.

    `open_time` se deriva de `k.t` (ms) a epoch UTC en segundos y debe estar
    alineado al intervalo. `is_closed` refleja `k.x` (vela cerrada vs intrabar).
    """
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise StreamingProtocolError(f"frame no es JSON válido: {exc}") from exc

    if not isinstance(message, dict):
        raise StreamingProtocolError("frame JSON no es un objeto")

    # Stream combinado: desenvolver.
    if "data" in message and "stream" in message:
        message = message["data"]
        if not isinstance(message, dict):
            raise StreamingProtocolError("data del stream combinado no es objeto")

    if "error" in message:
        raise StreamingProtocolError(f"error del proveedor: {message['error']}")
    if "result" in message and "id" in message:
        return None  # ack de suscripción
    if "pong" in message:
        return StreamHeartbeat(received_at=received_at)  # latido (S3)

    if message.get("e") != "kline":
        return None  # evento no soportado en S2 (se ignora explícitamente)

    kline = message.get("k")
    if not isinstance(kline, dict):
        raise StreamingProtocolError("frame kline sin objeto 'k'")

    provider_symbol = kline.get("s", message.get("s"))
    if not isinstance(provider_symbol, str) or provider_symbol.upper() != symbol.upper():
        raise StreamingProtocolError(
            f"símbolo inesperado {provider_symbol!r} (esperado {symbol!r})"
        )

    try:
        open_time = int(kline["t"]) // 1000
        open_ = float(kline["o"])
        high = float(kline["h"])
        low = float(kline["l"])
        close = float(kline["c"])
        volume = float(kline["v"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StreamingProtocolError(f"frame kline incompleto/inválido: {exc}") from exc

    if open_time <= 0 or open_time % interval_seconds != 0:
        raise StreamingProtocolError(
            f"open_time {open_time} no alineado al intervalo {interval_seconds}s"
        )

    is_closed = bool(kline.get("x", False))
    event = MarketDataEvent(
        symbol=provider_symbol,
        open_time=open_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
    )
    return StreamData(received_at=received_at, event=event)


class BinanceStreamingAdapter(StreamingMarketDataAdapter):
    """Adaptador read-only del stream público de klines de Binance.

    Args:
        transport: transporte WebSocket (por defecto, `websocket-client`).
            Inyectable para que los tests no dependan de Internet.
        clock: reloj inyectable UTC-aware.
        base_url: endpoint base (permite apuntar a un entorno de test).
        connect_timeout_seconds / recv_timeout_seconds: plazos de conexión y de
            lectura (un timeout de lectura => `receive()` devuelve None).
        max_control_frames: máximo de frames de control consecutivos que
            `receive()` salta antes de devolver None.
    """

    def __init__(
        self,
        *,
        transport: WebSocketTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        base_url: str = BINANCE_WS_BASE,
        connect_timeout_seconds: float = 10.0,
        recv_timeout_seconds: float = 5.0,
        max_control_frames: int = 10,
    ) -> None:
        if recv_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("los timeouts deben ser > 0")
        self._transport: WebSocketTransport = (
            transport if transport is not None else WebsocketClientTransport()
        )
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout_seconds
        self._recv_timeout = recv_timeout_seconds
        self._max_control_frames = max_control_frames
        self._state = StreamConnectionState.DISCONNECTED
        self._subscription: tuple[str, int] | None = None
        self._request_id = 0
        self._ping_id = 0
        self.last_error: str | None = None

    # ---------------------------------------------------------- propiedades

    @property
    def provider_name(self) -> str:
        return "binance-websocket"

    @property
    def state(self) -> StreamConnectionState:
        return self._state

    @property
    def subscription(self) -> tuple[str, int] | None:
        return self._subscription

    # ---------------------------------------------------------- ciclo de vida

    def connect(self) -> None:
        if self._state in (StreamConnectionState.CONNECTED, StreamConnectionState.CONNECTING):
            return
        self._state = StreamConnectionState.CONNECTING
        try:
            self._transport.open(f"{self._base_url}/ws", timeout=self._connect_timeout)
        except StreamingConnectionError as exc:
            self._state = StreamConnectionState.DISCONNECTED
            self.last_error = str(exc)
            raise
        self._state = StreamConnectionState.CONNECTED

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        if self._state is not StreamConnectionState.CONNECTED:
            raise StreamingConnectionError(
                f"subscribe() sin conexión activa (estado {self._state.value})"
            )
        if not symbol or interval_seconds <= 0:
            raise StreamingSubscribeError(
                f"suscripción inválida: symbol={symbol!r} interval={interval_seconds}"
            )
        interval_name = interval_to_binance(interval_seconds)
        stream = f"{symbol.lower()}@kline_{interval_name}"
        self._request_id += 1
        frame = json.dumps(
            {"method": "SUBSCRIBE", "params": [stream], "id": self._request_id}
        )
        try:
            self._transport.send(frame)
        except StreamingConnectionError as exc:
            self._state = StreamConnectionState.DISCONNECTED
            self.last_error = str(exc)
            raise
        self._subscription = (symbol, interval_seconds)

    def receive(self) -> StreamData | None:
        if self._state is not StreamConnectionState.CONNECTED:
            raise StreamingConnectionError(
                f"receive() sin conexión activa (estado {self._state.value})"
            )
        if self._subscription is None:
            raise StreamingSubscribeError("receive() sin suscripción activa")
        symbol, interval_seconds = self._subscription
        for _ in range(self._max_control_frames + 1):
            try:
                raw = self._transport.recv(timeout=self._recv_timeout)
            except StreamingConnectionError as exc:
                self._state = StreamConnectionState.DISCONNECTED
                self.last_error = str(exc)
                raise
            if raw is None:
                return None
            data = parse_binance_message(
                raw,
                symbol=symbol,
                interval_seconds=interval_seconds,
                received_at=self._clock(),
            )
            if data is not None:
                return data
        return None

    def disconnect(self) -> None:
        try:
            self._transport.close()
        finally:
            self._state = StreamConnectionState.CLOSED
            self._subscription = None

    def heartbeat(self) -> None:
        if self._state is not StreamConnectionState.CONNECTED:
            raise StreamingConnectionError(
                f"heartbeat() sin conexión activa (estado {self._state.value})"
            )
        self._ping_id += 1
        try:
            self._transport.send(json.dumps({"ping": self._ping_id}))
        except StreamingConnectionError as exc:
            self._state = StreamConnectionState.DISCONNECTED
            self.last_error = str(exc)
            raise


__all__ = [
    "BINANCE_INTERVALS",
    "BINANCE_WS_BASE",
    "BinanceStreamingAdapter",
    "interval_to_binance",
    "parse_binance_message",
]
