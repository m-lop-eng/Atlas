"""Eventos de datos de mercado en vivo (protocolo paper-safe).

Contrato clave de B1: la estrategia SOLO recibe `ClosedBarEvent` (velas
cerradas). Nunca recibe ticks intrabar, ni llamadas a websockets, ni datos
de un broker concreto. El `LiveDataEngine` es la única puerta de entrada
hacia la estrategia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    """Tick/kline normalizado emitido por un `MarketDataAdapter`.

    Es la unidad de entrada al `LiveDataEngine`. Un adapter concreto
    (Binance, IBKR, MT5, CME) traduce su formato nativo a este modelo;
    la lógica Atlas nunca ve el formato del proveedor.

    Attributes:
        symbol: Instrumento (p. ej. "BTCUSDT", "NQ").
        open_time: Tiempo de apertura de la vela en epoch UTC (segundos).
        open/high/low/close/volume: OHLCV.
        is_closed: True si este tick finaliza la vela (barra cerrada).
            Un flujo real suele entregar ticks de la misma vela abierta
            (is_closed=False) y, al cierre, el tick definitivo (is_closed=True).
    """

    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    is_closed: bool = False


@dataclass(frozen=True, slots=True)
class ClosedBarEvent:
    """Única entrada de datos que recibe la estrategia.

    Representa una vela 100% cerrada. El `emission_sequence` es monótono
    y sirve para garantizar que cada vela se entrega exactamente una vez
    (idempotencia ante duplicados del proveedor).

    `atlas_bar` expone el mismo contrato de datos que las barras históricas
    (`{"ts": "<epoch>", "open", "high", "low", "close", "volume"}`):
    backtest y live usan la MISMA representación (diferente fuente de
    tiempo), tal como exige el modo paper-safe.
    """

    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval_seconds: int
    emission_sequence: int

    @property
    def close_time(self) -> int:
        """Tiempo de cierre de la vela (exclusivo)."""
        return self.open_time + self.interval_seconds

    @property
    def atlas_bar(self) -> dict[str, Any]:
        """Contrato Atlas estándar (mismo que backtest/CSV)."""
        return {
            "ts": str(self.open_time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }