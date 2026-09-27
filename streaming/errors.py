"""Errores tipados del subsistema de streaming (S1).

Aislado de B1: este paquete NO modifica `live/`. Cada fallo del stream es
explícito y tipado (mismo principio que `brokers/models.py`): nunca se retorna
un estado ambiguo ni se silencia una desconexión. Los adapters concretos
(S2: Binance WebSocket) traducen su error nativo a uno de estos tipos.
"""

from __future__ import annotations


class StreamingError(Exception):
    """Error base del dominio de streaming."""


class StreamingConnectionError(StreamingError):
    """No se pudo establecer o mantener la conexión con el proveedor."""


class StreamingSubscribeError(StreamingError):
    """La suscripción al stream solicitado fue rechazada o es inválida."""


class StreamingProtocolError(StreamingError):
    """Mensaje del proveedor malformado o fuera del protocolo esperado."""


class StreamingTimeoutError(StreamingError):
    """No se recibieron datos/heartbeat dentro del plazo configurado."""


class StreamingHeartbeatError(StreamingError):
    """El heartbeat del proveedor se perdió o está fuera de tolerancia."""


class StreamingReconnectError(StreamingError):
    """Un intento de reconexión falló (reservado para S3)."""


__all__ = [
    "StreamingConnectionError",
    "StreamingError",
    "StreamingHeartbeatError",
    "StreamingProtocolError",
    "StreamingReconnectError",
    "StreamingSubscribeError",
    "StreamingTimeoutError",
]
