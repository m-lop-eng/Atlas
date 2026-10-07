"""Adaptadores de broker (00_MASTER_SPECIFICATION.md §29).

La estrategia nunca llama directamente a estos adapters.
El OrderManager es el único cliente autorizado.
El broker es la fuente externa de verdad (06_EXECUTION_OPERATION.md §8).
"""

from .base import BrokerAdapter
from .models import (
    AccountState,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderState,
    BrokerConnectionError,
    OrderNotFoundError,
    OrderRejectedError,
    OrderSubmission,
    Position,
    SubmitStatus,
)
from .paper import PaperBrokerAdapter
from .reconcile import is_uncertain, resolve_order
from .mt5 import (
    MT5BrokerAdapter,
    MT5Config,
    MT5ConfigError,
    MT5Credentials,
    MT5Environment,
    MT5SymbolSpec,
)

__all__ = [
    "AccountState",
    "BrokerAdapter",
    "BrokerError",
    "BrokerFill",
    "BrokerOrder",
    "BrokerOrderState",
    "BrokerConnectionError",
    "MT5BrokerAdapter",
    "MT5Config",
    "MT5ConfigError",
    "MT5Credentials",
    "MT5Environment",
    "MT5SymbolSpec",
    "OrderNotFoundError",
    "OrderRejectedError",
    "OrderSubmission",
    "PaperBrokerAdapter",
    "Position",
    "SubmitStatus",
    "is_uncertain",
    "resolve_order",
]