"""Streaming (S1–S5): datos en vivo por stream, aislados de B1.

Este paquete es un track paralelo a A4 (forward batch) y NO modifica `live/`.
Define el contrato del adapter (S1), eventos tipados, errores tipados, sandbox
determinista, el adaptador read-only de Binance WebSocket (S2), reconexión
(S3), reconciliación de integridad con backfill (S4) y el puente a
`LiveDataEngine` (S5). La estrategia sigue recibiendo solo `ClosedBarEvent`.

Roadmap aislado: S1 contrato -> S2 Binance WebSocket -> S3 reconnect + heartbeat
-> S4 backfill/reconciliación -> S5 integración con LiveDataEngine ->
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
from streaming.bridge import (
    B1StreamingAdapter,
    IntegrityBackfillProvider,
    StreamingPipeline,
    StreamingPipelineObserver,
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
from streaming.integrity import (
    IntegrityCoordinator,
    IntegrityReconciler,
    IntegrityReport,
    IntegrityStatus,
)
from streaming.health import (
    HeartbeatStatus,
    StreamingHealthLevel,
    StreamingHealthMonitor,
    StreamingHealthSnapshot,
)
from streaming.sandbox import ScriptedStreamingAdapter
from streaming.reconnect import ReconnectManager, ReconnectPolicy, ReconnectState
from streaming.transport import WebSocketTransport, WebsocketClientTransport

__all__ = [
    "B1StreamingAdapter",
    "BINANCE_INTERVALS",
    "BINANCE_WS_BASE",
    "BackoffPolicy",
    "BinanceStreamingAdapter",
    "HeartbeatStatus",
    "IntegrityBackfillProvider",
    "IntegrityCoordinator",
    "IntegrityReconciler",
    "IntegrityReport",
    "IntegrityStatus",
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
    "StreamingHealthLevel",
    "StreamingHealthMonitor",
    "StreamingHealthSnapshot",
    "StreamingHeartbeatError",
    "StreamingMarketDataAdapter",
    "StreamingPipeline",
    "StreamingPipelineObserver",
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
