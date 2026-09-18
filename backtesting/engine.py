"""Motor de backtest iterativo sin look-ahead (04_BACKTEST_VALIDATION.md §8-9).

Disciplina de ejecución:
    * Se procesa barra a barra en orden temporal estricto.
    * La estrategia SOLO recibe barras cerradas hasta el instante t.
    * La señal de la barra t se ejecuta en el open de la barra t+1,
      incorporando costes (spread + slippage + comisión).
    * P&L valorado close-to-close mientras la posición está abierta.

Limitaciones del MVP (deben resolverse en Milestone 2):
    * Sin stops/take-profits intrabar, sin partial fills, sin gaps,
      sin rollover. La posición se cierra solo por señal contraria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy

Bar = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionCosts:
    """Supuestos de ejecución del backtest (RULE-019)."""

    spread_ticks: float = 0.0
    tick_size: float = 0.0
    slippage_per_side: float = 0.0
    commission_per_unit: float = 0.0
    initial_equity: float = 100_000.0

    @property
    def per_side_cost(self) -> float:
        spread_cost = self.spread_ticks * self.tick_size
        return spread_cost + self.slippage_per_side + self.commission_per_unit


@dataclass(slots=True)
class BacktestResult:
    """Resultado agregado + registro completo de operaciones."""

    initial_equity: float
    final_equity: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def net_return(self) -> float:
        return self.final_equity / self.initial_equity - 1.0 if self.initial_equity else 0.0


@dataclass(slots=True)
class BacktestEngine:
    """Simula una estrategia sobre datos históricos.

    Args:
        costs: Supuestos de costes (documentar siempre).
        slippage_random: reservado para modelos estocásticos en Milestone 2.
    """

    costs: ExecutionCosts
    seed: int | None = None

    def run(self, strategy: BaseStrategy, bars: list[Bar]) -> BacktestResult:
        """Ejecuta el backtest. `bars` debe estar cronológicamente ordenado."""
        if len(bars) < 2:
            raise ValueError("Se necesitan al menos 2 barras para ejecutar")

        equity = self.costs.initial_equity
        curve: list[float] = [equity]
        trades: list[dict[str, Any]] = []

        position_qty: float = 0.0
        position_entry: float | None = None
        position_side: str | None = None
        turnover_counter = 0

        for i in range(len(bars) - 1):
            current = bars[i]
            nxt = bars[i + 1]

            visible = bars[: i + 1]
            signal = strategy.generate_signal(visible)

            fill_price = float(nxt["open"])
            per_side = self.costs.per_side_cost

            if signal.action is not Action.NO_TRADE and position_qty == 0:
                side = "LONG" if signal.action is Action.LONG else "SHORT"
                size = _sanitize_quantity(signal.target_quantity, equity, fill_price)
                exec_price = fill_price + per_side if side == "LONG" else fill_price - per_side
                position_qty = size
                position_entry = exec_price
                position_side = side
                equity -= size * per_side
                turnover_counter += 1
                trades.append({"signal": signal, "entry_price": exec_price, "side": side})

            elif signal.action is not Action.NO_TRADE and position_qty != 0:
                close_price = (
                    fill_price - per_side if position_side == "LONG" else fill_price + per_side
                )
                pnl = (close_price - position_entry) * position_qty if position_side == "LONG" \
                    else (position_entry - close_price) * position_qty
                equity += pnl - abs(position_qty) * per_side
                trades[-1]["exit_price"] = close_price
                trades[-1]["pnl"] = pnl
                trades[-1]["exit_time"] = nxt.get("ts")

                side = "LONG" if signal.action is Action.LONG else "SHORT"
                size = _sanitize_quantity(signal.target_quantity, equity, fill_price)
                exec_price = fill_price + per_side if side == "LONG" else fill_price - per_side
                position_qty = size
                position_entry = exec_price
                position_side = side
                equity -= size * per_side
                turnover_counter += 1
                trades.append({"signal": signal, "entry_price": exec_price, "side": side})

            if position_qty and position_side:
                close = float(nxt["close"])
                unrealized = (close - position_entry) * position_qty if position_side == "LONG" \
                    else (position_entry - close) * position_qty
                curve.append(equity + unrealized)
            else:
                curve.append(equity)

        if position_qty and position_side:
            last = bars[-1]["close"]
            per_side = self.costs.per_side_cost
            exit_price = last - per_side if position_side == "LONG" else last + per_side
            pnl = (exit_price - position_entry) * position_qty if position_side == "LONG" \
                else (position_entry - exit_price) * position_qty
            equity += pnl - abs(position_qty) * per_side
            trades[-1]["exit_price"] = exit_price
            trades[-1]["pnl"] = pnl
            trades[-1]["exit_time"] = bars[-1].get("ts")

        curve[-1] = equity
        return BacktestResult(
            initial_equity=self.costs.initial_equity,
            final_equity=equity,
            trades=trades,
            equity_curve=curve,
        )


def _sanitize_quantity(target_quantity, equity: float, price: float) -> float:
    """Cantidad con las que operar en ausencia de RiskEngine.

    IMPORTANTE: en producción la cantidad SIEMPRE la determina el RiskEngine.
    Este valor (1 unidad) es un placeholder del simulador para el MVP.
    """
    if target_quantity is None or target_quantity <= 0:
        return 1.0
    return float(target_quantity)