"""Tests del diagnóstico de señales E4: registro por-trade y agregaciones.

Valida el contrato del `BacktestResult` enriquecido:
    * por-trade: trade_id, signal_time, gross/costs/net (consistencia con
      pnl), MAE/MFE, bars_in_trade y razones de salida.
    * agregado  : secciones del reporte SIGNALS/TRADES/PERFORMANCE/
      TRADE_DISTRIBUTION/EXECUTION + tablas LONG/SHORT.

Todos los casos son deterministas (sin look-ahead; solo barras cerradas).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtesting.engine import BacktestEngine, ExecutionCosts
from execution.order import Fill
from execution.order_manager import OrderManager
from research.analysis import duration_bucket, trade_distribution
from research.pipeline import build_engine, build_strategy, run_experiment
from risk.engine import RiskEngine
from risk.types import RiskConfig
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata
from strategies.breakout import BreakoutParams, BreakoutStrategy
from strategies.random_entry import RandomEntryParams, RandomEntryStrategy


def make_bars():
    return [
        {"ts": "1000", "open": 100.0, "high": 105.0, "low": 99.0, "close": 101.0},
        {"ts": "2000", "open": 102.0, "high": 110.0, "low": 101.0, "close": 108.0},
        {"ts": "3000", "open": 107.0, "high": 112.0, "low": 103.0, "close": 105.0},
        {"ts": "4000", "open": 104.0, "high": 104.5, "low": 90.0, "close": 92.0},
    ]


def make_engine(
    *,
    spread_ticks: float = 1,
    tick_size: float = 0.5,
    slippage_per_side: float = 0.0,
    commission_per_unit: float = 0.0,
    max_bars_in_trade: int | None = None,
) -> BacktestEngine:
    costs = ExecutionCosts(
        spread_ticks=spread_ticks,
        tick_size=tick_size,
        slippage_per_side=slippage_per_side,
        commission_per_unit=commission_per_unit,
        initial_equity=100_000.0,
    )
    risk = RiskEngine(config=RiskConfig(risk_per_trade=0.01, max_daily_loss=0.9999))
    return BacktestEngine(
        costs=costs,
        risk_engine=risk,
        order_manager=OrderManager(),
        point_value=1.0,
        min_size=0.01,
        max_bars_in_trade=max_bars_in_trade,
    )


class AlwaysLong(BaseStrategy):
    metadata = StrategyMetadata(
        strategy_id="rec", strategy_name="Record", strategy_family="demo",
        market="cash", instrument="TST", timeframe="1h", version="1.0.0",
        parameter_set_version="p1", data_version="d1",
    )

    def __init__(self, stop_dist: float = 2.0):
        self._stop_dist = stop_dist

    def generate_signal(self, data) -> Signal:
        close = float(data[-1]["close"])
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=close,
            stop_price=close - self._stop_dist,
            target_quantity=None,
        )


class TestTradeRecord:
    def test_required_fields_present(self) -> None:
        result = make_engine().run(AlwaysLong(), make_bars())
        assert len(result.trades) == 1
        t = result.trades[0]
        for key in (
            "trade_id", "signal_time", "entry_time", "exit_time", "side",
            "entry_price", "exit_price", "stop_price", "bars_in_trade",
            "exit_reason", "gross_pnl", "costs", "net_pnl", "pnl",
            "mae", "mfe",
        ):
            assert key in t, f"falta la clave {key}"

    def test_consistency_gross_costs_net(self) -> None:
        result = make_engine().run(AlwaysLong(), make_bars())
        t = result.trades[0]
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["costs"])
        assert t["pnl"] == pytest.approx(t["net_pnl"])
        # coste = cantidad * 2 * per_side (0.5) → qty * 1.0
        assert t["costs"] == pytest.approx(t["quantity"] * 1.0)

    def test_signal_time_before_entry_time(self) -> None:
        result = make_engine().run(AlwaysLong(), make_bars())
        t = result.trades[0]
        assert int(t["signal_time"]) <= int(t["entry_time"])

    def test_mae_mfe_non_negative_and_reasonable(self) -> None:
        result = make_engine().run(AlwaysLong(), make_bars())
        t = result.trades[0]
        assert t["mae"] >= 0.0
        assert t["mfe"] >= 0.0
        assert t["mae_units"] >= 0.0
        assert t["mfe_units"] >= 0.0
        assert t["bars_in_trade"] >= 1

    def test_exit_reason_stop_mfe_captured(self) -> None:
        """STOP en barra 3: el MFE debe reflejar el high intrabar (110→112)."""
        result = make_engine().run(AlwaysLong(), make_bars())
        t = result.trades[0]
        assert t["exit_reason"] == "STOP"
        assert t["entry_raw"] == pytest.approx(102.0)
        assert t["exit_raw"] == pytest.approx(99.0)
        # LONG: mejor punto = max(high) - entrada (112 - 102 = 10)
        assert t["mfe_units"] == pytest.approx(10.0)

    def test_trade_id_deterministic(self) -> None:
        r1 = make_engine().run(AlwaysLong(), make_bars())
        r2 = make_engine().run(AlwaysLong(), make_bars())
        assert r1.trades[0]["trade_id"] == r2.trades[0]["trade_id"]

    def test_time_exit_bars_in_trade(self) -> None:
        eng = make_engine(max_bars_in_trade=1)
        result = eng.run(AlwaysLong(), make_bars())
        assert result.trades[0]["exit_reason"] == "TIME"
        assert result.trades[0]["bars_in_trade"] == 2


class TestSideAggregation:
    def _run_random(self) -> dict:
        cfg = {
            "experiment": {"environment": "BACKTEST"},
            "strategy": {
                "strategy_id": "atlas-random-entry",
                "strategy_name": "Random",
                "strategy_family": "control",
                "market": "cash",
                "instrument": "SAMPLE",
                "timeframe": "1h",
                "version": "1.0.0",
                "parameter_set_version": "n1",
                "data_version": "v1",
                "params": {"atr_period": 3, "stop_atr_mult": 2.0, "seed": 7},
            },
            "risk": {"risk_per_trade": 0.01, "max_daily_loss": 0.9999},
            "costs": {
                "spread_ticks": 0, "tick_size": 0,
                "slippage_per_side": 0.05, "commission_per_unit": 0.1,
                "initial_equity": 100_000.0,
            },
            "engine": {
                "account_id": "T", "point_value": 1.0,
                "min_size": 0.001, "max_bars_in_trade": 3,
            },
            "dataset": {"path": "unused", "n_bars": 5, "seed": 7},
        }
        csv = Path(__file__).resolve().parents[1] / "experiments" / "data" / "sample_1h.csv"
        return run_experiment(cfg, csv)

    def test_sides_long_short(self) -> None:
        report = self._run_random()
        sides = report["sides"]
        lo = sides["LONG"]
        sh = sides["SHORT"]
        assert isinstance(lo["trades"], int) and isinstance(sh["trades"], int)
        assert lo["trades"] + sh["trades"] == report["trades_breakdown"]["total"]

        sig = report["signals"]
        assert sig["long"] + sig["short"] == sig["raw"]
        # aceptadas + rechazadas + en posición = total de señales crudas
        inpos = report["diagnostics"]["signals_while_in_position"]
        assert sig["accepted"] + sig["rejected"] + inpos == sig["raw"]

    def test_random_strategy_deterministic(self) -> None:
        from data.synthetic import generate_ohlc_bars

        bars = generate_ohlc_bars(n_bars=40, start_price=100.0, start_epoch=1_700_000_000,
                                  interval_seconds=3600, seed=42, drift=0.0, volatility=0.01)
        meta = StrategyMetadata(
            strategy_id="atlas-random-entry", strategy_name="R", strategy_family="control",
            market="cash", instrument="S", timeframe="1h", version="1.0.0",
            parameter_set_version="n1", data_version="v1",
        )
        s1 = RandomEntryStrategy(meta, RandomEntryParams(seed=9))
        s2 = RandomEntryStrategy(meta, RandomEntryParams(seed=9))
        sigs1 = [s1.generate_signal(bars[: i + 1]) for i in range(len(bars))]
        sigs2 = [s2.generate_signal(bars[: i + 1]) for i in range(len(bars))]
        assert [(s.action, s.stop_price) for s in sigs1] == [(s.action, s.stop_price) for s in sigs2]
        assert sum(1 for s in sigs1 if s.action is Action.LONG) > 0
        assert sum(1 for s in sigs1 if s.action is Action.SHORT) > 0


class TestInvertedBreakout:
    def test_invert_flips_action(self) -> None:
        bars = [
            {"ts": "1", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"ts": "2", "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0},
            {"ts": "3", "open": 102.0, "high": 103.0, "low": 101.0, "close": 102.0},
            {"ts": "4", "open": 102.0, "high": 103.0, "low": 101.0, "close": 102.0},
            {"ts": "5", "open": 102.0, "high": 105.0, "low": 102.0, "close": 105.0},
        ]
        meta = StrategyMetadata(
            strategy_id="atlas-first-breakout", strategy_name="B", strategy_family="breakout",
            market="cash", instrument="S", timeframe="1h", version="1.0.0",
            parameter_set_version="p1", data_version="v1",
        )
        normal = BreakoutStrategy(meta, BreakoutParams(lookback=3, atr_period=3, stop_atr_mult=2.0))
        inverted = BreakoutStrategy(meta, BreakoutParams(lookback=3, atr_period=3, stop_atr_mult=2.0, invert=True))
        s_n = normal.generate_signal(bars)
        s_i = inverted.generate_signal(bars)
        assert s_n.action is Action.LONG
        assert s_i.action is Action.SHORT
        assert s_i.stop_price > s_i.reference_price


class TestDistributionAndReport:
    def test_duration_buckets(self) -> None:
        assert duration_bucket(3) == "0-5"
        assert duration_bucket(5) == "0-5"
        assert duration_bucket(6) == "6-10"
        assert duration_bucket(11) == "11-20"
        assert duration_bucket(21) == "21-40"
        assert duration_bucket(41) == "40+"
        assert duration_bucket(200) == "40+"

    def test_report_sections_and_crosstab(self) -> None:
        bars = make_bars() * 12
        result = make_engine(max_bars_in_trade=2).run(AlwaysLong(), bars)
        dist = trade_distribution(result.trades)
        assert set(dist["duration_by_exit"]) <= {"0-5", "6-10", "11-20", "21-40", "40+"}
        reasons = {r for cell in dist["duration_by_exit"].values() for r in cell}
        assert reasons <= {"STOP", "TIME", "END"}
        total = sum(
            cell["count"]
            for cells in dist["duration_by_exit"].values()
            for cell in cells.values()
        )
        assert total == len(result.trades)

    def test_performance_sections_coherent(self, tmp_path) -> None:
        from data.synthetic import generate_ohlc_bars, write_ohlc_csv

        csv = tmp_path / "x.csv"
        write_ohlc_csv(
            generate_ohlc_bars(n_bars=200, start_price=100.0, start_epoch=1_700_000_000,
                               interval_seconds=3600, seed=42, drift=0.0, volatility=0.01),
            csv,
        )
        cfg = {
            "experiment": {"environment": "BACKTEST"},
            "strategy": {
                "strategy_id": "atlas-breakout",
                "strategy_name": "B", "strategy_family": "breakout",
                "market": "cash", "instrument": "S", "timeframe": "1h",
                "version": "1.0.0", "parameter_set_version": "p1", "data_version": "v1",
                "params": {"lookback": 20, "atr_period": 14, "stop_atr_mult": 2.0},
            },
            "risk": {"risk_per_trade": 0.01, "max_daily_loss": 0.9999},
            "costs": {
                "spread_ticks": 0, "tick_size": 0,
                "slippage_per_side": 0.05, "commission_per_unit": 0.1,
                "initial_equity": 100_000.0,
            },
            "engine": {"account_id": "T", "point_value": 1.0, "min_size": 0.001,
                       "max_bars_in_trade": 48},
        }
        report = run_experiment(cfg, csv)
        perf = report["performance"]
        assert perf["net_pnl"] == pytest.approx(perf["gross_pnl"] - perf["costs"])
        assert perf["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in report["trades"]))
        exe = report["execution"]
        assert exe["total_costs"] == pytest.approx(perf["costs"])
        assert exe["average_cost"] is not None
        tb = report["trades_breakdown"]
        assert tb["long"] + tb["short"] == tb["total"]
        assert tb["wins"] + tb["losses"] == tb["total"]