"""Tests e2e del primer experimento Atlas (8 propiedades del pipeline).

Validan las propiedades críticas del pipeline completo: desde la
señal de la estrategia hasta la generación del reporte de lineage.
"""

from __future__ import annotations

import pytest

from backtesting.engine import BacktestEngine, ExecutionCosts
from data.loader import DataLoadError
from data.synthetic import sha256_of_file, write_ohlc_csv
from execution.order import Fill
from execution.order_manager import OrderManager
from research.experiment import make_config_hash
from research.pipeline import build_engine, build_strategy, experiment_id_for, run_experiment
from risk.engine import RiskEngine
from risk.types import RiskConfig
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata


BARS = [
    {"ts": str(i), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
    for i in range(5)
] + [
    {"ts": "5", "open": 100.0, "high": 120.0, "low": 100.0, "close": 110.0},
    {"ts": "6", "open": 101.0, "high": 101.0, "low": 100.5, "close": 101.0},
]


def _config(**overrides) -> dict:
    base = {
        "experiment": {"environment": "BACKTEST"},
        "strategy": {
            "strategy_id": "atlas-breakout",
            "strategy_name": "Pipeline Test",
            "strategy_family": "breakout",
            "market": "cash",
            "instrument": "TST",
            "timeframe": "1h",
            "version": "1.0.0",
            "parameter_set_version": "p1",
            "data_version": "d1",
            "params": {"lookback": 3, "atr_period": 3, "stop_atr_mult": 2.0},
        },
        "risk": {"risk_per_trade": 0.01, "max_open_positions": 3},
        "costs": {
            "spread_ticks": 0,
            "tick_size": 0,
            "slippage_per_side": 0.0,
            "commission_per_unit": 0.0,
            "initial_equity": 100_000.0,
        },
        "engine": {
            "account_id": "TEST",
            "point_value": 1.0,
            "min_size": 0.001,
            "max_bars_in_trade": None,
        },
    }
    base.update(overrides)
    return base


def _csv(tmp_path) -> str:
    path = tmp_path / "data.csv"
    write_ohlc_csv(BARS, path)
    return str(path)


class BrokenNoStopStrategy(BaseStrategy):
    metadata = StrategyMetadata(
        strategy_id="broken",
        strategy_name="Broken",
        strategy_family="test",
        market="cash",
        instrument="TST",
        timeframe="1h",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
    )

    def generate_signal(self, data) -> Signal:
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=Action.LONG,
            reference_price=float(data[-1]["close"]),
            stop_price=None,
            target_quantity=None,
        )


class TestNoLookahead:
    def test_entry_at_next_bar_open(self, tmp_path) -> None:
        """La entrada nunca usa el precio de la barra de la señal."""
        report = run_experiment(_config(), _csv(tmp_path))
        for trade in report["trades"]:
            entry_idx = trade["entry_index"]
            expected_entry = BARS[entry_idx]["open"]
            assert trade["entry_price"] == pytest.approx(expected_entry)
            assert entry_idx >= 1


class TestRiskVeto:
    def test_no_stop_no_order(self, tmp_path) -> None:
        """Sin stop, RiskEngine no aprueba → cero trades y cero órdenes."""
        cfg = _config()
        cfg["strategy"]["strategy_id"] = "broken-nostop"
        engine = build_engine(cfg)
        bars = BARS
        result = engine.run(BrokenNoStopStrategy(), bars)
        assert result.trades == []
        assert not engine.order_manager.orders


class TestSizingMatchesConfig:
    def test_first_trade_uses_initial_equity_cap(self, tmp_path) -> None:
        """qty * distancia * punto = equity * risk_per_trade."""
        cfg = _config()
        bars = BARS
        strategy = build_strategy(cfg["strategy"])
        engine = build_engine(cfg)
        result = engine.run(strategy, bars)
        trade = result.trades[0]
        ref = bars[trade["entry_index"] - 1]["close"]
        distance = abs(ref - trade["stop"])
        expected = cfg["costs"]["initial_equity"] * cfg["risk"]["risk_per_trade"]
        assert abs(trade["quantity"] * distance * cfg["engine"]["point_value"] - expected) < 0.1


class TestIdempotency:
    def test_unique_client_order_ids(self, tmp_path) -> None:
        cfg = _config()
        strategy = build_strategy(cfg["strategy"])
        engine = build_engine(cfg)
        engine.run(strategy, BARS)
        ids = [o.client_order_id for o in engine.order_manager.orders.values()]
        assert len(ids) == len(set(ids))
        assert len(ids) == 1


class TestPartialFillsAveragePrice:
    def test_average_price_weighted(self) -> None:
        from execution.order import Order, OrderSide, OrderStatus, OrderType
        from execution.order_manager import OrderManager

        om = OrderManager()
        order = om.create_order(
            strategy_id="p",
            strategy_version="1",
            account_id="test",
            instrument="TST",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            client_order_id="fill-avg",
        )
        f1 = om.apply_fill(
            order.order_id, Fill(order_id=order.order_id, quantity=4, price=100.0, commission=0)
        )
        f2 = om.apply_fill(
            f1.order_id, Fill(order_id=f1.order_id, quantity=6, price=102.0, commission=0)
        )
        assert f2.status == OrderStatus.FILLED
        assert abs(f2.average_fill_price - 101.2) < 1e-9


class TestCostsAffectResult:
    def test_with_cost_final_lower(self, tmp_path) -> None:
        no_cost = run_experiment(_config(), _csv(tmp_path))
        with_cost = run_experiment(
            _config(costs={"spread_ticks": 1, "tick_size": 0.5, "slippage_per_side": 0.0, "commission_per_unit": 0.0, "initial_equity": 100_000.0}),
            _csv(tmp_path),
        )
        assert with_cost["final_equity"] <= no_cost["final_equity"]


class TestReproducibility:
    def test_same_config_same_csv_same_result(self, tmp_path) -> None:
        cfg = _config()
        csv_path = _csv(tmp_path)
        r1 = run_experiment(cfg, csv_path)
        r2 = run_experiment(cfg, csv_path)
        assert r1["experiment_id"] == r2["experiment_id"]
        assert r1["trades"] == r2["trades"]
        assert r1["equity_curve"] == r2["equity_curve"]
        assert r1["metrics"] == r2["metrics"]


class TestLineage:
    def test_lineage_fields_complete(self, tmp_path) -> None:
        cfg = _config()
        csv_path = _csv(tmp_path)
        report = run_experiment(cfg, csv_path)
        line = report["lineage"]
        assert line["experiment_id"] == report["experiment_id"]
        assert line["strategy"]["strategy_id"] == "atlas-breakout"
        assert line["strategy"]["params_hash"] == make_config_hash(cfg["strategy"]["params"])
        assert line["dataset"]["sha256"] == sha256_of_file(csv_path)
        assert line["risk_config"]["risk_per_trade"] == 0.01
        assert report["trades_summary"]["count"] == len(report["trades"])
        assert "metrics" in report