"""Eventos del stream (S1): datos, conexión/desconexión y heartbeat.

Aislado de B1: NO modifica `live/`, pero reutiliza `MarketDataEvent` (el modelo
canónico de datos, de solo lectura) para que en S5 se pueda puentear el stream
a un `LiveDataEngine` sin acoplar la estrategia al proveedor.

Contrato de S1: un `StreamingMarketDataAdapter` entrega una secuencia de
`StreamEvent` tipados:

    StreamData       -> tick/kline normalizado (envuelve MarketDataEvent)
    StreamHeartbeat  -> latido del proveedor (liveness, sin datos)
    StreamConnected  -> se estableció conexión (o reconexión) con el proveedor
    StreamDisconnected -> se perdió/cerró la conexión (con motivo)

Regla temporal: `received_at` (reloj local) y `server_time` (si el proveedor lo
manda) son `datetime` UTC-aware. Los timestamps de datos (`open_time`) son
epoch UTC en segundos. Ningún `StreamEvent` se construye con un timestamp
naive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from live.events import MarketDataEvent

from streaming.clock import ensure_utc


class StreamEventKind(str, Enum):
    """Discriminante estable de los eventos del stream."""

    DATA = "DATA"
    HEARTBEAT = "HEARTBEAT"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """Base inmutable de todo evento del stream.

    Attributes:
        received_at: instante UTC-aware en que el adapter recibió el evento.
    """

    received_at: datetime
    kind: ClassVar[StreamEventKind]

    def __post_init__(self) -> None:
        ensure_utc(self.received_at)


@dataclass(frozen=True, slots=True)
class StreamData(StreamEvent):
    """Dato de mercado normalizado al modelo canónico `MarketDataEvent`."""

    event: MarketDataEvent
    kind: ClassVar[StreamEventKind] = StreamEventKind.DATA


@dataclass(frozen=True, slots=True)
class StreamHeartbeat(StreamEvent):
    """Latido del proveedor: confirma liveness sin entregar datos.

    `server_time` es opcional (no todos los proveedores lo exponen); si está
    presente debe ser UTC-aware.
    """

    server_time: datetime | None = None
    kind: ClassVar[StreamEventKind] = StreamEventKind.HEARTBEAT

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.server_time is not None:
            ensure_utc(self.server_time)


@dataclass(frozen=True, slots=True)
class StreamConnected(StreamEvent):
    """Se estableció (o restableció) la conexión con el proveedor."""

    provider: str = ""
    kind: ClassVar[StreamEventKind] = StreamEventKind.CONNECTED


@dataclass(frozen=True, slots=True)
class StreamDisconnected(StreamEvent):
    """Se perdió o se cerró la conexión con el proveedor.

    Attributes:
        reason: descripción legible del motivo.
        expected: True si fue un cierre solicitado por el cliente (disconnect),
            False si fue una pérdida inesperada del stream.
    """

    reason: str = ""
    expected: bool = False
    kind: ClassVar[StreamEventKind] = StreamEventKind.DISCONNECTED


__all__ = [
    "StreamConnected",
    "StreamData",
    "StreamDisconnected",
    "StreamEvent",
    "StreamEventKind",
    "StreamHeartbeat",
]
