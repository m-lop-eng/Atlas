"""Sandbox determinista del contrato de streaming (S1).

`ScriptedStreamingAdapter` implementa `StreamingMarketDataAdapter` sin red:
reproduce una secuencia prefijada de `StreamEvent` y permite inyectar
excepciones tipadas en posiciones concretas (para simular desconexiones,
heartbeats perdidos o mensajes malformados en S3). Es la base de los tests
deterministas de streaming.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from streaming.adapter import StreamConnectionState, StreamingMarketDataAdapter
from streaming.clock import utc_now
from streaming.errors import StreamingConnectionError, StreamingSubscribeError
from streaming.events import StreamConnected, StreamDisconnected, StreamEvent


class ScriptedStreamingAdapter(StreamingMarketDataAdapter):
    """Adapter sandbox que entrega una secuencia de eventos prefijada.

    Attributes:
        emitted: log auditable de todo lo entregado por `receive()`.

    Args:
        events: secuencia a emitir. Un elemento que sea `BaseException` se
            lanza al alcanzarlo (simula fallos tipados).
        auto_connect_event: si True, `connect()` antepone un `StreamConnected`.
        clock: reloj inyectable (UTC-aware) para timestamps deterministas.
    """

    def __init__(
        self,
        provider_name: str = "sandbox",
        events: list[StreamEvent | BaseException] | None = None,
        *,
        auto_connect_event: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._provider_name = provider_name
        self._scripted: list[StreamEvent | BaseException] = list(events or [])
        self._index = 0
        self._state = StreamConnectionState.DISCONNECTED
        self._subscription: tuple[str, int] | None = None
        self._auto_connect_event = auto_connect_event
        self._clock = clock
        self._pending: list[StreamEvent] = []
        self.emitted: list[StreamEvent] = []

    # ---------------------------------------------------------- propiedades

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def state(self) -> StreamConnectionState:
        return self._state

    @property
    def subscription(self) -> tuple[str, int] | None:
        return self._subscription

    # ---------------------------------------------------------- ciclo de vida

    def connect(self) -> None:
        self._state = StreamConnectionState.CONNECTED
        if self._auto_connect_event:
            self._pending.append(
                StreamConnected(received_at=self._clock(), provider=self._provider_name)
            )

    def disconnect(self) -> None:
        self._state = StreamConnectionState.CLOSED
        self._pending.append(
            StreamDisconnected(
                received_at=self._clock(), reason="client", expected=True
            )
        )

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        if self._state is not StreamConnectionState.CONNECTED:
            raise StreamingConnectionError(
                "no se puede subscribe() sin conexión activa "
                f"(estado {self._state.value})"
            )
        if not symbol or interval_seconds <= 0:
            raise StreamingSubscribeError(
                f"suscripción inválida: symbol={symbol!r} interval={interval_seconds}"
            )
        self._subscription = (symbol, interval_seconds)

    # --------------------------------------------------------------- stream

    def receive(self) -> StreamEvent | None:
        if self._pending:
            event = self._pending.pop(0)
            self.emitted.append(event)
            return event
        if self._state is not StreamConnectionState.CONNECTED:
            raise StreamingConnectionError(
                f"receive() sin conexión activa (estado {self._state.value})"
            )
        if self._index >= len(self._scripted):
            return None
        item = self._scripted[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        self.emitted.append(item)
        return item


__all__ = ["ScriptedStreamingAdapter"]
