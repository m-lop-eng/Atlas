"""Estrategia de entrada aleatoria determinista (hipótesis nula E5-B).

No contiene ninguna predicción: cada barra plana genera una señal LONG o
SHORT según un generador pseudoaleatorio sembrado por (seed, timestamp).
Las salidas (stop ATR + regla temporal) y el risk/sizing/frecuencia de
llamada son idénticos a los del resto de experimentos: esto convierte a la
estrategia en un benchmark nulo para "una regla arbitraria de entrada".

Determinismo: la misma seed + las mismas barras producen exactamente el
mismo libro de señales (CRITICAL: reproducible, no usa os.urandom/numpy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zlib import crc32

from .base.signal import Action, Signal
from .base.strategy import BaseStrategy, StrategyMetadata
from .breakout import _atr


@dataclass(frozen=True, slots=True)
class RandomEntryParams:
    """Parámetros de la estrategia nula (versionados, parte del lineage).

    Attributes:
        atr_period: Periodo de la media de True Range (misma medida que el
            breakout para el stop).
        stop_atr_mult: Múltiplo de ATR usado como distancia de stop.
        seed: Semilla del generador pseudoaleatorio (parte de la config).
    """

    atr_period: int = 14
    stop_atr_mult: float = 2.0
    seed: int = 0

    def __post_init__(self) -> None:
        if self.atr_period < 1:
            raise ValueError("atr_period debe ser >= 1")
        if self.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult debe ser positivo")


class RandomEntryStrategy(BaseStrategy):
    """Entrada LONG/SHORT pseudoaleatoria, salidas idénticas a las demás."""

    def __init__(self, metadata: StrategyMetadata, params: RandomEntryParams) -> None:
        self.metadata = metadata
        self.params = params

    def generate_signal(self, data: Any) -> Signal:
        bars = list(data)
        p = self.params
        if len(bars) < 2:
            return self._no_trade()

        atr = _atr(bars, p.atr_period)
        if atr <= 0:
            return self._no_trade()

        last = bars[-1]
        close = float(last["close"])
        draw = crc32(f"{p.seed}:{last['ts']}".encode("utf-8")) & 0xFFFFFFFF
        going_long = (draw & 1) == 0

        if going_long:
            return Signal(
                strategy_id=self.metadata.strategy_id,
                strategy_version=self.metadata.version,
                instrument=self.metadata.instrument,
                action=Action.LONG,
                reference_price=close,
                stop_price=close - p.stop_atr_mult * atr,
                target_quantity=None,
            )
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.SHORT,
            reference_price=close,
            stop_price=close + p.stop_atr_mult * atr,
            target_quantity=None,
        )

    def _no_trade(self) -> Signal:
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.NO_TRADE,
            reference_price=None,
            stop_price=None,
            target_quantity=None,
        )