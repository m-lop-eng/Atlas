"""Streaming (S1–S2): datos en vivo por stream, aislados de B1.

Este paquete es un track paralelo a A4 (forward batch) y NO modifica `live/`.
Define el contrato del adapter (S1), eventos tipados (datos, conexión,
desconexión, heartbeat), errores tipados, un sandbox determinista y el adaptador
read-only de Binance WebSocket (S2). En S5 se puenteará a `LiveDataEngine`; la
estrategia sigue recibiendo solo `ClosedBarEvent`.

Roadmap aislado: S1 contrato (este) -> S2 Binance WebSocket -> S3 reconnect +
heartbeat -> S4 backfill/reconciliación -> S5 integración con LiveDataEngine ->
S6 streaming paper runner.
"""

from __future__ import annotations

from streaming.adapter import StreamConnectionState, StreamingMarketDataAdapter
from streaming.backoff import BackoffPolicy
from streaming.binance import (
    BINANCE_INTERVALS,
    BINANCE_WS_BASE,
    BinanceStreamingAdapter,
    interval_to_binance,
    parse_binance_message,
)
from streaming.clock import ensure_utc, utc_now
from streaming.errors import (
    StreamingConnectionError,
    StreamingError,
    StreamingHeartbeatError,
    StreamingProtocolError,
    StreamingReconnectError,
    StreamingSubscribeError,
    StreamingTimeoutError,
)
from streaming.events import (
    StreamConnected,
    StreamData,
    StreamDisconnected,
    StreamEvent,
    StreamEventKind,
    StreamHeartbeat,
)
from streaming.sandbox import ScriptedStreamingAdapter
from streaming.reconnect import ReconnectManager, ReconnectPolicy, ReconnectState
from streaming.transport import WebSocketTransport, WebsocketClientTransport

__all__ = [
    "BINANCE_INTERVALS",
    "BINANCE_WS_BASE",
    "BackoffPolicy",
    "BinanceStreamingAdapter",
    "ReconnectManager",
    "ReconnectPolicy",
    "ReconnectState",
    "ScriptedStreamingAdapter",
    "StreamConnected",
    "StreamConnectionState",
    "StreamData",
    "StreamDisconnected",
    "StreamEvent",
    "StreamEventKind",
    "StreamHeartbeat",
    "StreamingConnectionError",
    "StreamingError",
    "StreamingHeartbeatError",
    "StreamingMarketDataAdapter",
    "StreamingProtocolError",
    "StreamingReconnectError",
    "StreamingSubscribeError",
    "StreamingTimeoutError",
    "WebSocketTransport",
    "WebsocketClientTransport",
    "ensure_utc",
    "interval_to_binance",
    "parse_binance_message",
    "utc_now",
]
