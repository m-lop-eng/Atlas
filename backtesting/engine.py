"""Motor de backtest iterativo sin look-ahead (04_BACKTEST_VALIDATION.md §8-9).

Pipeline completo por barra:
    Strategy → Signal → RiskEngine (veto + sizing) → OrderManager → fill en open[i+1]

Disciplina de ejecución:
    * Orden temporal estricto, barra a barra.
    * La estrategia SOLO recibe barras cerradas hasta el instante t.
    * La señal de la barra t se ejecuta en el open de la barra t+1.
    * Stops y salidas temporales intrabar (low/high) sin reordenar.
    * P&L valorado close-to-close mientras la posición está abierta.

Costes: el precio de entrada incorpora per_side para entrar y el de salida
per_side para salir (comisión+slippage+spread del lado correspondiente).

Limitaciones MVP (Milestone 2): una posición a la vez, sin partial fills
simulados, sin gaps, sin rollover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from execution.order import Fill, OrderSide, OrderStatus, OrderType
from execution.order_manager import OrderManager
from risk.engine import RiskEngine
from risk.types import AccountRiskState
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy

Bar = dict[str, Any]

TradeRecord = dict[str, Any]


def _day_key(ts) -> Any:
    """Día de trading (UTC) de un timestamp de barra.

    Soporta epoch numérico/string y fechas ISO. Devuelve None si no se
    puede interpretar (entonces no hay reset diario).
    """
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts)).date()
    if isinstance(ts, str):
        if ts.isdigit():
            return datetime.fromtimestamp(float(ts)).date()
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


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
    """Resultado agregado + registro completo de operaciones.

    Attributes:
        diagnostics: Contadores diagnósticos EXACTOS del recorrido (raw
            signals, bloqueadas por posición, rechazadas por riesgo, etc.).
    """

    initial_equity: float
    final_equity: float
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def net_return(self) -> float:
        return self.final_equity / self.initial_equity - 1.0 if self.initial_equity else 0.0


@dataclass(slots=True)
class BacktestEngine:
    """Simula una estrategia sobre datos históricos a través del pipeline real.

    Versión 2 (ENGINE_VERSION): el veto de pérdida diaria
    (`max_daily_loss`) usa la pérdida REALIZADA del día de trading
    (calendar-day UTC), no el P&L acumulado del backtest. Esto refleja la
    semántica realista de "pérdida diaria" sin modificar el RiskEngine.

    Monetización en divisa de cuenta: P&L, costes, MAE y MFE se expresan en
    $ multiplicando por `point_value` (dólares por unidad de precio). Para
    point_value = 1.0 el resultado es idéntico al histórico del grafo H001
    (por eso se mantiene el mismo ENGINE_VERSION y los experiment_id
    permanecen estables); con point_value > 1 (p.ej. NQ=50) el risk in $ y
    el P&L in $ quedan perfectamente alineados (auditoría de unidades).

    Attributes:
        costs: Supuestos de costes (documentar siempre).
        risk_engine: Autoridad de veto y sizing de cada intención.
        order_manager: Registra y controla el ciclo de vida de las órdenes.
        account_id: Cuenta representada en el backtest.
        point_value: P&L monetario por unidad de precio (sizing fixed-fractional).
        min_size: Cantidad mínima operada; por debajo no se crea orden.
        max_bars_in_trade: Regla temporal de salida (None = solo stop/END).
    """

    ENGINE_VERSION = 2  # semántica del recorrido (véase docstring de la clase)

    costs: ExecutionCosts
    risk_engine: RiskEngine
    order_manager: OrderManager
    account_id: str = "BACKTEST-01"
    point_value: float = 1.0
    min_size: float = 0.0
    max_bars_in_trade: int | None = None
    seed: int | None = None

    def run(self, strategy: BaseStrategy, bars: list[Bar]) -> BacktestResult:
        """Ejecuta el backtest. `bars` debe estar cronológicamente ordenado."""
        if len(bars) < 2:
            raise ValueError("Se necesitan al menos 2 barras para ejecutar")

        realized = 0.0
        day_pnl = 0.0
        current_day = _day_key(bars[0]["ts"])
        curve: list[float] = [self.costs.initial_equity]
        trades: list[TradeRecord] = []
        diag: dict[str, int | dict[str, int]] = {
            "raw_signals": 0,
            "raw_signals_long": 0,
            "raw_signals_short": 0,
            "signals_while_flat": 0,
            "signals_while_in_position": 0,
            "risk_rejected_signals": 0,
            "orders_created": 0,
            "risk_rejection_by_reason": {},
        }

        quantity = 0.0
        entry: float | None = None
        entry_raw: float | None = None
        side: str | None = None
        stop: float | None = None
        entry_idx: int | None = None
        mae_units = 0.0
        mfe_units = 0.0
        pending = None
        pending_ts: str | None = None
        per_side = self.costs.per_side_cost

        for i, bar in enumerate(bars):
            new_day = _day_key(bar["ts"])
            if new_day is not None and new_day != current_day:
                current_day = new_day
                day_pnl = 0.0

            if pending is not None:
                quantity, entry, entry_raw, side, stop, entry_idx = self._fill(
                    pending, float(bar["open"]), per_side, i
                )
                trades.append(
                    {
                        "trade_id": pending.client_order_id,
                        "strategy_id": pending.strategy_id,
                        "strategy_version": pending.strategy_version,
                        "instrument": pending.instrument,
                        "signal_time": pending_ts,
                        "entry_time": bar["ts"],
                        "entry_index": i,
                        "side": side,
                        "quantity": quantity,
                        "entry_price": entry,
                        "entry_raw": entry_raw,
                        "stop_price": stop,
                        "stop": stop,
                        "mae": 0.0,
                        "mfe": 0.0,
                        "mae_units": 0.0,
                        "mfe_units": 0.0,
                        "gross_pnl": 0.0,
                        "costs": 0.0,
                        "net_pnl": 0.0,
                        "pnl": 0.0,
                        "bars_in_trade": 0,
                    }
                )
                mae_units = 0.0
                mfe_units = 0.0
                pending = None
                pending_ts = None

            if quantity != 0.0:
                low = float(bar["low"])
                high = float(bar["high"])
                if side == "LONG":
                    adverse = entry_raw - low
                    favorable = high - entry_raw
                else:
                    adverse = high - entry_raw
                    favorable = entry_raw - low
                mae_units = max(mae_units, max(adverse, 0.0))
                mfe_units = max(mfe_units, max(favorable, 0.0))

                exit_plan = self._evaluate_exit(
                    bar, side, stop, i, entry_idx
                )
                if exit_plan is not None:
                    exit_raw, reason = exit_plan
                    exit_price = self._exit_price(side, exit_raw, per_side)
                    sign = 1 if side == "LONG" else -1
                    pv = self.point_value
                    costs = quantity * 2 * per_side * pv
                    gross = (exit_raw - entry_raw) * sign * quantity * pv
                    net = gross - costs
                    realized += net
                    day_pnl += net
                    trades[-1].update(
                        exit_price=exit_price,
                        exit_raw=exit_raw,
                        exit_time=bar.get("ts"),
                        exit_index=i,
                        exit_reason=reason,
                        gross_pnl=gross,
                        costs=costs,
                        net_pnl=net,
                        pnl=net,
                        mae=round(mae_units * quantity * pv, 8),
                        mfe=round(mfe_units * quantity * pv, 8),
                        mae_units=round(mae_units, 8),
                        mfe_units=round(mfe_units, 8),
                        bars_in_trade=i - entry_idx + 1,
                    )
                    quantity = 0.0
                    entry = None
                    entry_raw = None
                    side = None
                    stop = None
                    entry_idx = None
                    mae_units = 0.0
                    mfe_units = 0.0

            if quantity != 0.0:
                close = float(bar["close"])
                unrealized = (
                    (close - entry) * quantity * (1 if side == "LONG" else -1)
                    * self.point_value
                )
                curve.append(self.costs.initial_equity + realized + unrealized)
            else:
                curve.append(self.costs.initial_equity + realized)

            if i < len(bars) - 1:
                signal = strategy.generate_signal(bars[: i + 1])
                if signal.action is not Action.NO_TRADE:
                    diag["raw_signals"] += 1
                    if signal.action is Action.LONG:
                        diag["raw_signals_long"] += 1
                    else:
                        diag["raw_signals_short"] += 1
                    if quantity != 0.0:
                        diag["signals_while_in_position"] += 1
                    else:
                        diag["signals_while_flat"] += 1
                        order = self._decide_order(
                            signal, curve[-1], day_pnl, bar["ts"], diag
                        )
                        if order is not None:
                            pending = order
                            pending_ts = bar["ts"]

        if quantity != 0.0:
            bar = bars[-1]
            close = float(bar["close"])
            exit_raw = close
            exit_price = self._exit_price(side, exit_raw, per_side)
            sign = 1 if side == "LONG" else -1
            pv = self.point_value
            costs = quantity * 2 * per_side * pv
            gross = (exit_raw - entry_raw) * sign * quantity * pv
            net = gross - costs
            realized += net
            day_pnl += net
            trades[-1].update(
                exit_price=exit_price,
                exit_raw=exit_raw,
                exit_time=bar.get("ts"),
                exit_index=len(bars) - 1,
                exit_reason="END",
                gross_pnl=gross,
                costs=costs,
                net_pnl=net,
                pnl=net,
                mae=round(mae_units * quantity * pv, 8),
                mfe=round(mfe_units * quantity * pv, 8),
                mae_units=round(mae_units, 8),
                mfe_units=round(mfe_units, 8),
                bars_in_trade=len(bars) - 1 - entry_idx + 1,
            )

        curve[-1] = self.costs.initial_equity + realized
        diag["trades_completed"] = len(trades)
        return BacktestResult(
            initial_equity=self.costs.initial_equity,
            final_equity=self.costs.initial_equity + realized,
            trades=trades,
            equity_curve=curve,
            diagnostics=diag,
        )

    def _fill(
        self,
        order,
        open_price: float,
        per_side: float,
        index: int,
    ) -> tuple[float, float, float, str, float | None, int]:
        side = "LONG" if order.side is OrderSide.BUY else "SHORT"
        fill_raw = open_price
        fill_price = self._entry_price(side, fill_raw, per_side)
        filled = self.order_manager.apply_fill(
            order.order_id,
            Fill(
                order_id=order.order_id,
                quantity=order.quantity,
                price=fill_price,
                commission=0.0,
                slippage=per_side if per_side else None,
            ),
        )
        return (
            filled.fill_quantity,
            fill_price,
            fill_raw,
            side,
            order.stop_price,
            index,
        )

    def _evaluate_exit(
        self,
        bar: Bar,
        side: str | None,
        stop: float | None,
        index: int,
        entry_idx: int | None,
    ) -> tuple[float, str] | None:
        """Devuelve el PRECIO BRUTO de salida (sin costes) y la razón.

        Los costes se aplican después (`_exit_price`), por lo que el precio
        registrado distingue `exit_raw` (bruto) de `exit_price` (neto).
        """
        low = float(bar["low"])
        high = float(bar["high"])

        if side == "LONG" and stop is not None and low <= stop:
            return stop, "STOP"
        if side == "SHORT" and stop is not None and high >= stop:
            return stop, "STOP"
        snapped = index - entry_idx if entry_idx is not None else 0
        if (
            self.max_bars_in_trade is not None
            and snapped >= self.max_bars_in_trade
        ):
            close = float(bar["close"])
            return close, "TIME"
        return None

    def _decide_order(
        self,
        signal: Signal,
        equity: float,
        day_pnl: float,
        bar_ts: str,
        diag: dict[str, Any],
    ):
        """Valida la intención, crea/registra la orden si se aprueba.

        Solo se llama desde `run()` cuando la señal es de entrada (no
        NO_TRADE) y la posición está plana. Los rechazos se contabilizan
        en `diag` (diagnóstico de capacidad de señales).
        """
        state = AccountRiskState(
            equity=equity,
            balance=self.costs.initial_equity,
            open_positions=0,
            current_daily_pnl=day_pnl,
            current_drawdown=0.0,
        )
        decision = self.risk_engine.validate(
            signal, state, point_value=self.point_value
        )
        if not decision.approved:
            diag["risk_rejected_signals"] += 1
            for name, ok in decision.checks.items():
                if not ok:
                    bucket = diag["risk_rejection_by_reason"]
                    bucket[name] = bucket.get(name, 0) + 1
            return None

        if decision.approved_quantity < self.min_size:
            diag["risk_rejected_signals"] += 1
            bucket = diag["risk_rejection_by_reason"]
            bucket["min_size"] = bucket.get("min_size", 0) + 1
            return None

        side = OrderSide.BUY if signal.action is Action.LONG else OrderSide.SELL
        client_order_id = f"{signal.strategy_id}:{signal.instrument}:{bar_ts}"
        order = self.order_manager.create_order(
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            account_id=self.account_id,
            instrument=signal.instrument,
            side=side,
            quantity=decision.approved_quantity,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
            stop_price=signal.stop_price,
        )
        self.order_manager.approve(order.order_id)
        self.order_manager.submit(order.order_id)
        diag["orders_created"] += 1
        return order

    def _entry_price(self, side: str | None, price: float, per_side: float) -> float:
        return price + per_side if side == "LONG" else price - per_side

    def _exit_price(self, side: str | None, price: float, per_side: float) -> float:
        return price - per_side if side == "LONG" else price + per_side