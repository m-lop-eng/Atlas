"""Contrato abstracto del adaptador de streaming (S1).

Aislado de B1: NO modifica `live/`. Es la interfaz que todo proveedor de
streaming real (S2: Binance WebSocket; luego IBKR/MT5/CME) implementará. En S5
se puenteará a `LiveDataEngine`, que sigue siendo la única puerta hacia la
estrategia.

Semántica del contrato (la lógica específica del proveedor vive SOLO en el
adapter; la estrategia jamás ve un websocket):

    connect()                     -> estado CONNECTED
    subscribe(symbol, interval)   -> guarda la suscripción (idempotente)
    receive()                     -> siguiente StreamEvent | None
    disconnect()                  -> estado CLOSED (cierre controlado)

Propagación de fallos (mismo principio que `live/adapter.py` y
`brokers/base.py`): el adapter lanza excepciones tipadas de `streaming.errors`
ante fallos de red/stream; NUNCA retorna un estado ambiguo. `receive()` retorna
None cuando no hay eventos pendientes en este instante.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from streaming.events import StreamEvent


class StreamConnectionState(str, Enum):
    """Estado de conexión del adapter de streaming."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    CLOSED = "CLOSED"


class StreamingMarketDataAdapter(ABC):
    """Interfaz común a todos los proveedores de datos por stream."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identificador del proveedor (p. ej. "binance-websocket")."""

    @property
    @abstractmethod
    def state(self) -> StreamConnectionState:
        """Estado de conexión actual."""

    @property
    @abstractmethod
    def subscription(self) -> tuple[str, int] | None:
        """Suscripción activa `(symbol, interval_seconds)` o None."""

    @abstractmethod
    def connect(self) -> None:
        """Establece la conexión con el proveedor de datos.

        Raises:
            StreamingConnectionError: si no se puede conectar.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión de forma controlada (estado CLOSED)."""

    @abstractmethod
    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        """Suscribe el stream de velas del instrumento.

        Raises:
            StreamingConnectionError: si se invoca sin conexión activa.
            StreamingSubscribeError: si la suscripción es inválida/rechazada.
        """

    @abstractmethod
    def receive(self) -> StreamEvent | None:
        """Devuelve el siguiente evento normalizado, o None si no hay.

        Raises:
            StreamingConnectionError: si el stream está caído.
            StreamingTimeoutError: si el proveedor no responde a tiempo.
            StreamingHeartbeatError: si se perdió el heartbeat.
            StreamingProtocolError: si llegó un mensaje malformado.
        """

    def heartbeat(self) -> None:
        """Solicita un latido de aplicación al proveedor (S3).

        Proveedores que soportan ping/pong (p. ej. Binance) lo sobrescriben y
        responden emitiendo un `StreamHeartbeat`. Por defecto es un no-op:
        el `ReconnectManager` mide la liveness por actividad reciente y, si no
        hay soporte de latido, recurre a la propia actividad del stream.

        Raises:
            StreamingConnectionError: si no se puede enviar el latido.
        """
        return None


__all__ = ["StreamConnectionState", "StreamingMarketDataAdapter"]
