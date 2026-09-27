"""Streaming (S1): contrato de datos en vivo por stream, aislado de B1.

Este paquete es un track paralelo a A4 (forward batch) y NO modifica `live/`.
Define el contrato del adapter de streaming, eventos tipados (datos, conexión,
desconexión, heartbeat), errores tipados y un sandbox determinista. En S5 se
puenteará a `LiveDataEngine`; la estrategia sigue recibiendo solo
`ClosedBarEvent`.

Roadmap aislado: S1 contrato (este) -> S2 Binance WebSocket -> S3 reconnect +
heartbeat -> S4 backfill/reconciliación -> S5 integración con LiveDataEngine ->
S6 streaming paper runner.
"""

from __future__ import annotations

from streaming.adapter import StreamConnectionState, StreamingMarketDataAdapter
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

__all__ = [
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
    "ensure_utc",
    "utc_now",
]
