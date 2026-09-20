"""Capa de datos en vivo (B1): solo market data, sin órdenes.

La estrategia recibe exclusivamente `ClosedBarEvent` vía el `LiveDataEngine`;
jamás se conecta directamente a un websocket/API ni conoce al proveedor.
"""

from __future__ import annotations

from live.adapter import BackfillProvider, MarketDataAdapter
from live.engine import DataQuality, EngineState, LiveDataEngine
from live.events import ClosedBarEvent, MarketDataEvent

__all__ = [
    "BackfillProvider",
    "ClosedBarEvent",
    "DataQuality",
    "EngineState",
    "LiveDataEngine",
    "MarketDataAdapter",
    "MarketDataEvent",
]