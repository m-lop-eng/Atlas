"""Contrato abstracto de adaptador de broker.

Todo adapter concreto (MT5, futuros, IBKR, crypto) debe implementar
esta interfaz. La lógica específica del broker vive SOLO en el adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from execution.order import Order


class BrokerAdapter(ABC):
    """Interfaz común a todos los brokers.

    Propagación de fallos: los adapters deben lanzar excepciones explícitas
    (p. ej. ConnectionError, OrderRejectedError) y nunca retornar estados
    ambiguos. Ante incertidumbre (timeout) la orden queda en estado UNKNOWN
    y el OrderManager debe reconciliar antes de reintentar (06 §8, §31).
    """

    @abstractmethod
    def connect(self) -> None:
        """Establece la conexión con el broker."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión de forma controlada."""

    @abstractmethod
    def get_account(self) -> dict:
        """Devuelve el estado de cuenta (balance, equity, margin...)."""

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """Devuelve las posiciones abiertas actuales (fuente externa de verdad)."""

    @abstractmethod
    def get_orders(self) -> list[dict]:
        """Devuelve las órdenes abiertas/desconocidas en el broker."""

    @abstractmethod
    def submit_order(self, order: Order) -> dict:
        """Envía una orden. Estado devuelto: ack/timeout/rejection."""

    @abstractmethod
    def cancel_order(self, order: Order) -> dict:
        """Cancela una orden."""

    @abstractmethod
    def modify_order(self, order: Order) -> dict:
        """Modifica precio/cantidad de una orden pendiente."""

    @abstractmethod
    def get_market_data(self, instrument: str, timeframe: str) -> object:
        """Devuelve datos de mercado actuales del instrumento."""