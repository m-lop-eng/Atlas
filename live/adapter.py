"""Contrato abstracto del adaptador de datos de mercado.

Todo adapter concreto (Binance WebSocket, IBKR, MT5, CME) implementa esta
interfaz. La lógica específica del proveedor vive SOLO en el adapter: el
`LiveDataEngine` es la única capa que entiende la estrategia, y la
estrategia jamás se conecta directamente a un websocket/API.

Propagación de fallos (mismo principio que `brokers/base.py`): los adapters
deben lanzar excepciones explícitas (p. ej. `ConnectionError`) ante fallos
de red/stream. `receive` retorna None cuando no hay eventos pendientes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from live.events import MarketDataEvent


class MarketDataAdapter(ABC):
    """Interfaz común a todos los proveedores de datos en vivo."""

    @abstractmethod
    def connect(self) -> None:
        """Establece la conexión con el proveedor de datos."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión de forma controlada."""

    @abstractmethod
    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        """Suscribe el stream de velas del instrumento."""

    @abstractmethod
    def receive(self) -> MarketDataEvent | None:
        """Devuelve el siguiente evento normalizado (o None si no hay).

        Raises:
            ConnectionError: si el stream está caído / se perdió la conexión.
        """


class BackfillProvider(ABC):
    """Proveedor de datos históricos para reconciliación tras gap/reconnect.

    Interfaz mínima de B1: permite que el `LiveDataEngine` recupere velas
    cerradas que se perdieron y las entregue (una sola vez) a la estrategia
    mediante el mismo contrato `ClosedBarEvent`.
    """

    @abstractmethod
    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        """Devuelve velas cerradas desde `start_open_time` en adelante.

        Raises:
            ConnectionError: si el proveedor de respaldo tampoco responde.
        """