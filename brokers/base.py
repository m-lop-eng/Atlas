"""Contrato estable de adaptador de broker (B3).

Todo adapter concreto (paper/sandbox, IBKR, MT5, crypto) implementa esta
interfaz. La lógica específica del broker vive SOLO en el adapter; el
OrderManager es el único cliente autorizado (00_MASTER_SPECIFICATION §29).

Principios que fija este contrato (06_EXECUTION_OPERATION.md):

  * El broker es la FUENTE EXTERNA DE VERDAD para órdenes, posiciones y
    cuenta (`get_order`, `get_open_orders`, `get_positions`,
    `get_account_state` devuelven lo que el broker reporta).
  * `submit_order` NUNCA garantiza un fill: su resultado es una disposición
    (CONFIRMED/REJECTED/UNKNOWN). Si la respuesta no puede determinarse
    (timeout), Atlas puede quedar en UNKNOWN y debe RESOLVERLO consultando
    al broker (`get_order`), no reenviando la orden (06 §8, §67).
  * Fallos de conectividad se propagan como excepciones explícitas
    (`BrokerConnectionError`); nunca se retorna un estado ambiguo como
    si fuera certero.
  * Idempotencia por `client_order_id`: el downstream (OrderManager) la
    exige; el adapter la respeta y no duplica exposiciones (06 §10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from execution.order import Order

from brokers.models import (
    AccountState,
    BrokerOrder,
    OrderSubmission,
    Position,
)


class BrokerAdapter(ABC):
    """Interfaz común a todos los brokers."""

    @abstractmethod
    def connect(self) -> None:
        """Establece la conexión con el broker."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión de forma controlada."""

    @abstractmethod
    def health(self) -> bool:
        """True si el broker responde (conexión viva y autenticada)."""

    @abstractmethod
    def submit_order(self, order: Order) -> OrderSubmission:
        """Envía una orden y devuelve su disposición.

        Returns:
            OrderSubmission: CONFIRMED / REJECTED con la vista broker; o
            UNKNOWN si la respuesta no pudo determinarse (resolver luego).

        Raises:
            BrokerConnectionError: sin conexión con el broker.
            OrderRejectedError: rechazo explícito del broker.
        """

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> BrokerOrder:
        """Cancela una orden pendiente. Raises OrderNotFoundError si no
        existe; OrderRejectedError si ya no se puede cancelar (p. ej. llena).
        """

    @abstractmethod
    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        """Estado actual de una orden según el broker (fuente de verdad).
        None si el broker no conoce esa orden."""

    @abstractmethod
    def get_open_orders(self) -> list[BrokerOrder]:
        """Órdenes abiertas/pendientes según el broker."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Posiciones abiertas según el broker (fuente de verdad)."""

    @abstractmethod
    def get_account_state(self) -> AccountState:
        """Estado de cuenta según el broker."""


__all__ = ["BrokerAdapter"]