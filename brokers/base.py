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
from typing import Any

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
    def submit_order(
        self, order: Order, *, authorization: Any = None
    ) -> OrderSubmission:
        """Envía una orden y devuelve su disposición.

        `authorization` es opcional en el contrato genérico. Los adapters que
        exigen una autorización de ejecución de un solo uso (MT5) la validan y
        consumen de forma atómica antes del envío. Un adapter que no la soporta
        debe rechazarla explícitamente, nunca ignorarla en silencio.

        Contrato de fase (ORDER-07B): una implementación que ya invocó
        `order_send`/el transporte de envío NUNCA debe propagar una excepción
        como si no hubiera enviado. Un fallo posterior de interpretación debe
        devolver `UNKNOWN` (o una excepción tipada que declare inequívocamente
        `send_attempted=True`). Las excepciones pre-send demostradas pueden
        propagarse con los tipos existentes.

        Returns:
            OrderSubmission: CONFIRMED / REJECTED con la vista broker; o
            UNKNOWN si la respuesta no pudo determinarse (resolver luego).

        Raises:
            BrokerConnectionError: sin conexión con el broker (pre-send).
            OrderRejectedError: rechazo explícito/validación pre-send.
        """

    @abstractmethod
    def cancel_order(
        self,
        client_order_id: str,
        *,
        operation_id: str | None = None,
        instrument: str | None = None,
        account_id: str | None = None,
        authorization: Any = None,
    ) -> BrokerOrder:
        """Cancela una orden pendiente. Raises OrderNotFoundError si no
        existe; OrderRejectedError si ya no se puede cancelar (p. ej. llena).

        ORDER-07C: el retorno debe ser el estado OBSERVADO del broker. Un
        retcode de aceptacion no demuestra cancelacion: no se infiere
        `CANCELLED`; si el estado sigue activo/ambiguo, el coordinador debe
        tratarlo como no confirmado.
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