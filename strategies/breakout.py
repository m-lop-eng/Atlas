"""Primera estrategia Atlas: breakout de N barras (MVP deliberadamente simple).

Reglas:
    * LONG si close > max(high) de las N barras anteriores (excluye la actual).
    * SHORT si close < min(low) de las N barras anteriores.
    * Stop basado en ATR (periodo corto), conocida en el momento de la señal.
    * Sin indicadores complejos, sin ML, sin Look-ahead (solo barras visibles).

Sizing y límites NO son responsabilidad de la estrategia: el Signal solo
propone dirección + stop; cantidad la decide el RiskEngine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base.signal import Action, Signal
from .base.strategy import BaseStrategy, StrategyMetadata


@dataclass(frozen=True, slots=True)
class BreakoutParams:
    """Parámetros de la estrategia (versionados, parte del lineage).

    Attributes:
        lookback: Barras anteriores para el máximo/mínimo de ruptura.
        atr_period: Periodo de la media de True Range para el stop.
        stop_atr_mult: Múltiplo de ATR usado como distancia de stop.
        trend_filter: Periodo del filtro de tendencia `close vs SMA(close)`.
            None (default) = sin filtro. Si el SMA no es computable (pocas
            barras) el filtro no se aplica: la variante es un superset puro
            del baseline (únicamente elimina señales).
        invert: Invierte la señal breakout (control negativo E5-C): cuando el
            baseline sería LONG contra el máximo, la variante entra SHORT
            (y viceversa). El stop se coloca en el lado opuesto. Deliberado
            para experimentos de hipótesis nula, no como estrategia.
        direction: Restricción direccional de H002. 'both' (default) opera
            los dos lados; 'long' opera solo LONG (las señales SHORT se
            descartan); 'short' opera solo SHORT (las LONG se descartan).
    """

    lookback: int = 20
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    trend_filter: int | None = None
    invert: bool = False
    direction: str = "both"

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError("lookback debe ser >= 1")
        if self.atr_period < 1:
            raise ValueError("atr_period debe ser >= 1")
        if self.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult debe ser positivo")
        if self.trend_filter is not None and self.trend_filter < 1:
            raise ValueError("trend_filter debe ser >= 1")
        if self.direction not in ("both", "long", "short"):
            raise ValueError("direction debe ser one of 'both', 'long', 'short'")


def _atr(bars: list[dict], period: int) -> float:
    """ATR simple (media de True Range) sobre las barras dadas.

    Implementación de referencia (sin caché) para el cálculo inicial y para
    ventanas arbitrarias: idéntica a la lógica incremental de la clase (que
    solo acelera las llamadas secuenciales del motor barra a barra).
    """
    if len(bars) < 2:
        return 0.0
    trues: list[float] = []
    for i in range(len(bars)):
        bar = bars[i]
        high = float(bar["high"])
        low = float(bar["low"])
        if i == 0:
            tr = high - low
        else:
            prev_close = float(bars[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trues.append(tr)
    window = trues[-period:]
    return sum(window) / len(window)


def _sma(bars: list[dict], period: int) -> float | None:
    """Media aritmética de los cierres de las últimas `period` barras.

    Returns:
        None si no hay suficientes barras para computar el SMA.
    """
    if len(bars) < period:
        return None
    closes = [float(b["close"]) for b in bars[-period:]]
    return sum(closes) / period


class BreakoutStrategy(BaseStrategy):
    """Breakout de N barras con salida por stop ATR.

    Example:
        >>> meta = StrategyMetadata(strategy_id="exp-001-breakout", ...)
        >>> strat = BreakoutStrategy(meta, BreakoutParams(lookback=20))
        >>> sig = strat.generate_signal(bars[:25])
    """

    def __init__(self, metadata: StrategyMetadata, params: BreakoutParams) -> None:
        self.metadata = metadata
        self.params = params
        self._tr_hist: list[float] | None = None
        self._tr_sum = 0.0
        self._tr_period = 0
        self._tr_last_len = 0
        self._tr_last_prev: Any = None

    def _true_range(self, bar: dict, prev: dict) -> float:
        high = float(bar["high"])
        low = float(bar["low"])
        prev_close = float(prev["close"])
        return max(high - low, abs(high - prev_close), abs(low - prev_close))

    def _atr_cached(self, bars: list[dict], period: int) -> float:
        """ATR idéntico a `_atr` pero amortizado para ventanas secuenciales.

        Solo usa la caché cuando la ventana creció en exactamente una barra y
        la barra previa es el MISMO objeto (llamada secuencial del motor).
        Cualquier otra llamada (tests, ventanas arbitrarias) recalcula igual
        que el baseline, por lo que la semántica numérica no cambia.
        """
        n = len(bars)
        if n < 2:
            return 0.0
        prev_bar = bars[n - 2]
        if (
            self._tr_hist is not None
            and self._tr_period == period
            and self._tr_last_len == n - 1
            and prev_bar is self._tr_last_prev
        ):
            tr = self._true_range(bars[n - 1], prev_bar)
            hist = self._tr_hist
            if len(hist) >= period:
                self._tr_sum -= hist[0]
                hist.pop(0)
            self._tr_sum += tr
            hist.append(tr)
            self._tr_last_len = n
            self._tr_last_prev = bars[n - 1]
            return self._tr_sum / len(hist)
        return self._rebuild_atr(bars, period)

    def _rebuild_atr(self, bars: list[dict], period: int) -> float:
        n = len(bars)
        if n <= period:
            window = self._full_trues(bars)
        else:
            start = n - period
            window = [
                self._true_range(bars[i], bars[i - 1]) for i in range(start, n)
            ]
        self._tr_hist = window
        self._tr_sum = sum(window)
        self._tr_period = period
        self._tr_last_len = n
        self._tr_last_prev = bars[n - 1]
        return self._tr_sum / len(window) if window else 0.0

    def _full_trues(self, bars: list[dict]) -> list[float]:
        trues: list[float] = []
        for i in range(len(bars)):
            bar = bars[i]
            if i == 0:
                tr = float(bar["high"]) - float(bar["low"])
            else:
                tr = self._true_range(bar, bars[i - 1])
            trues.append(tr)
        return trues

    def generate_signal(self, data: Any) -> Signal:
        bars = list(data)
        p = self.params
        if len(bars) <= p.lookback:
            return self._signal(Action.NO_TRADE, reference=None, stop=None)

        last = bars[-1]
        close = float(last["close"])
        window = bars[-p.lookback : -1]
        prev_high = max(float(b["high"]) for b in window)
        prev_low = min(float(b["low"]) for b in window)

        atr = self._atr_cached(bars, p.atr_period)
        if atr <= 0 or p.stop_atr_mult * atr <= 0:
            return self._signal(Action.NO_TRADE, reference=None, stop=None)

        sma = _sma(bars, p.trend_filter) if p.trend_filter is not None else None

        if close > prev_high:
            if sma is not None and close <= sma:
                return self._signal(Action.NO_TRADE, reference=None, stop=None)
            action = Action.SHORT if p.invert else Action.LONG
            if not self._direction_allowed(action):
                return self._signal(Action.NO_TRADE, reference=None, stop=None)
            stop = close - p.stop_atr_mult * atr
            return self._signal(
                action,
                reference=close,
                stop=close + p.stop_atr_mult * atr if p.invert else stop,
            )

        if close < prev_low:
            if sma is not None and close >= sma:
                return self._signal(Action.NO_TRADE, reference=None, stop=None)
            action = Action.LONG if p.invert else Action.SHORT
            if not self._direction_allowed(action):
                return self._signal(Action.NO_TRADE, reference=None, stop=None)
            stop = close + p.stop_atr_mult * atr
            return self._signal(
                action,
                reference=close,
                stop=close - p.stop_atr_mult * atr if p.invert else stop,
            )

        return self._signal(Action.NO_TRADE, reference=None, stop=None)

    def _direction_allowed(self, action: Action) -> bool:
        if self.params.direction == "both":
            return True
        if self.params.direction == "long" and action is Action.LONG:
            return True
        if self.params.direction == "short" and action is Action.SHORT:
            return True
        return False

    def _signal(
        self,
        action: Action,
        reference: float | None,
        stop: float | None,
    ) -> Signal:
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=action,
            reference_price=reference,
            stop_price=stop,
            target_quantity=None,
        )