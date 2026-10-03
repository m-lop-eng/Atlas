"""Transporte WebSocket inyectable (S2).

El `BinanceStreamingAdapter` no habla directamente con ninguna librería de
websocket: depende de este `Protocol`. Los tests deterministas inyectan un
transporte falso (sin red); el smoke de integración usa
`WebsocketClientTransport`, respaldado por `websocket-client`.

README DE SOLO LECTURA: este módulo (y `binance.py`) NO exponen ninguna vía
hacia APIs de escritura/órdenes. La librería de websocket se importa de forma
perezosa para que el resto del subsistema y la suite determinista no dependan
de ella.
"""

from __future__ import annotations

from typing import Protocol

from streaming.errors import StreamingConnectionError


class WebSocketTransport(Protocol):
    """Contrato mínimo de transporte de texto sobre WebSocket."""

    def open(self, url: str, *, timeout: float) -> None:
        """Abre la conexión a `url`.

        Raises:
            StreamingConnectionError: si no se puede conectar.
        """

    def send(self, payload: str) -> None:
        """Envía un frame de texto.

        Raises:
            StreamingConnectionError: si la conexión no está activa / falla.
        """

    def recv(self, *, timeout: float) -> str | None:
        """Devuelve el siguiente frame de texto, o None si expira `timeout`.

        Raises:
            StreamingConnectionError: si se pierde la conexión.
        """

    def close(self) -> None:
        """Cierra la conexión de forma controlada e idempotente."""


class WebsocketClientTransport:
    """Transporte real sobre `websocket-client` (import perezoso).

    La dependencia es opcional: ``pip install "atlas[streaming]"``. Por defecto
    solo se usa en el smoke de integración; los tests inyectan un transporte
    falso.
    """

    def __init__(self) -> None:
        self._ws = None
        self._lib = None

    def open(self, url: str, *, timeout: float) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise StreamingConnectionError(
                "falta la dependencia 'websocket-client'; instala atlas[streaming]"
            ) from exc
        self._lib = websocket
        try:
            self._ws = websocket.create_connection(url, timeout=timeout)
        except Exception as exc:
            raise StreamingConnectionError(f"no se pudo conectar a {url}: {exc}") from exc

    def send(self, payload: str) -> None:
        if self._ws is None:
            raise StreamingConnectionError("transporte no conectado")
        try:
            self._ws.send(payload)
        except Exception as exc:
            raise StreamingConnectionError(f"fallo al enviar frame: {exc}") from exc

    def recv(self, *, timeout: float) -> str | None:
        if self._ws is None:
            raise StreamingConnectionError("transporte no conectado")
        try:
            self._ws.settimeout(timeout)
            return self._ws.recv()
        except self._lib.WebSocketTimeoutException:
            return None
        except Exception as exc:
            raise StreamingConnectionError(f"conexión perdida: {exc}") from exc

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pragma: no cover - cierre best-effort
                pass


__all__ = ["WebSocketTransport", "WebsocketClientTransport"]
