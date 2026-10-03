"""Provider de backfill S8 (`BackfillProvider`) sobre la fuente REST read-only.

Se inyecta en `StreamingForwardRunner` (S7, sin modificarlo) y alimenta
EXCLUSIVAMENTE a `IntegrityReconciler.reconcile_open_ended` (S4). Nunca crea ni
emite `ClosedBarEvent`: B1 (`LiveDataEngine`) sigue siendo la unica autoridad.

Contrato (identico al resto de `BackfillProvider`):
    fetch_closed_bars(symbol, interval_seconds, start_open_time) -> list[MarketDataEvent]

Fail-safe: cualquier `DataFetchError` de la fuente se traduce a `ConnectionError`
(que S4 marca como BLOCKED); respuesta vacia/insuficiente se propaga como lista
vacia (S4 -> UNRECOVERABLE). Jamas se fabrican barras.
"""

from __future__ import annotations

import time
from typing import Callable

from data.sources import DataFetchError
from live.adapter import BackfillProvider
from live.events import MarketDataEvent


class RestStreamingBackfillProvider(BackfillProvider):
    """Backfill S8: barras REST -> `MarketDataEvent` para S4 (sin atajos)."""

    def __init__(
        self,
        source: object,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._clock = clock

    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        end = int(self._clock()) + 2 * interval_seconds
        try:
            bars = self._source.fetch(start_open_time, end)  # type: ignore[attr-defined]
        except DataFetchError as exc:
            # Fallo/timeout/rate-limit -> ConnectionError -> S4 BLOCKED (fail-safe).
            raise ConnectionError(str(exc)) from exc
        out: list[MarketDataEvent] = []
        for bar in bars:
            ts = int(bar["ts"])
            if ts < start_open_time or ts % interval_seconds != 0:
                continue
            out.append(
                MarketDataEvent(
                    symbol=symbol,
                    open_time=ts,
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar.get("volume", 0.0)),
                    is_closed=True,
                )
            )
        return out


__all__ = ["RestStreamingBackfillProvider"]
