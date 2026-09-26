"""Replay determinista de velas cerradas para el Paper Trading (B7).

`ReplayMarketDataAdapter` implementa el contrato `MarketDataAdapter` (B1)
alimentando a un `LiveDataEngine` con `MarketDataEvent` ya cerrados, predecibles
y alineados al intervalo. Único proveedor de datos del paper engine: nunca
conecta a Binance/IBKR; hace el flujo de datos determinista y repetible.
"""

from __future__ import annotations

from live.adapter import MarketDataAdapter
from live.events import MarketDataEvent


class ReplayMarketDataAdapter(MarketDataAdapter):
    """Adapter en memoria que emite una secuencia prefijada de velas cerradas.

    Attributes:
        events: Lista de `MarketDataEvent` (is_closed=True) a emitir, en orden.
            Se consumen una sola vez; duplicados se insertan manualmente para
            probar la deduplicación del LiveDataEngine.
    """

    def __init__(self, events: list[MarketDataEvent] | None = None) -> None:
        self.events: list[MarketDataEvent] = list(events or [])
        self._connected = False
        self._index = 0

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        self.symbol = symbol
        self.interval_seconds = interval_seconds

    # --------------------------------------------------------------- stream

    def receive(self) -> MarketDataEvent | None:
        if not self._connected:
            raise ConnectionError("ReplayMarketDataAdapter: sin conexión")
        if self._index >= len(self.events):
            return None
        event = self.events[self._index]
        self._index += 1
        return event

    # ------------------------------------------------------------- helpers

    @classmethod
    def bars(
        cls,
        symbol: str,
        interval_seconds: int,
        ohlc: list[tuple[float, float, float, float]],
        *,
        start_open_time: int = 1_700_000_000,
    ) -> "ReplayMarketDataAdapter":
        """Crea un adapter con velas cerradas alineadas al intervalo.

        `ohlc` son tuplas (open, high, low, close); `start_open_time` debe
        estar alineado a `interval_seconds`.
        """
        events: list[MarketDataEvent] = []
        stamp = start_open_time
        for open_, high, low, close in ohlc:
            if stamp % interval_seconds != 0:
                raise ValueError(
                    f"start_open_time {start_open_time} no alineado a "
                    f"interval_seconds {interval_seconds}"
                )
            events.append(
                MarketDataEvent(
                    symbol=symbol,
                    open_time=stamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=1_000.0,
                    is_closed=True,
                )
            )
            stamp += interval_seconds
        return cls(events)


__all__ = ["ReplayMarketDataAdapter"]