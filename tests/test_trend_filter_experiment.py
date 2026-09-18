"""Tests de la variante controlada E1→E2 (breakout + filtro SMA50).

Garantizan que el experimento 2 es reproducible, que SOLO cambia
`params.trend_filter` respecto al baseline, y que el baseline sigue
generando exactamente exp-c494bacc5643 tras la modificación de la
estrategia (el parámetro nuevo es un superset opcional).
"""

from __future__ import annotations

import yaml
import pytest
from pathlib import Path

from data.synthetic import generate_ohlc_bars, write_ohlc_csv
from research.pipeline import build_strategy, run_experiment

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
BASELINE_ID = "exp-bb2cb3867462"


@pytest.fixture()
def configs(tmp_path):
    """Configs de ambos experimentos + dataset idéntico (mismo seed)."""
    base = yaml.safe_load(
        (EXPERIMENTS / "first_experiment" / "config.yaml").read_text(encoding="utf-8")
    )
    variant = yaml.safe_load(
        (EXPERIMENTS / "trend_filter" / "config.yaml").read_text(encoding="utf-8")
    )
    csv = tmp_path / "data.csv"
    ds = base["dataset"]
    bars = generate_ohlc_bars(
        n_bars=ds["n_bars"],
        start_price=ds["start_price"],
        start_epoch=ds["start_epoch"],
        interval_seconds=ds["interval_seconds"],
        seed=ds["seed"],
        drift=ds["drift"],
        volatility=ds["volatility"],
    )
    write_ohlc_csv(bars, csv)
    return base, variant, csv


class TestControlledComparison:
    def test_baseline_unchanged(self, configs) -> None:
        """El nuevo parámetro (None por defecto) no altera el baseline."""
        base, _, csv = configs
        report = run_experiment(base, csv)
        assert report["experiment_id"] == BASELINE_ID

    def test_only_trend_filter_differs(self, configs) -> None:
        base, variant, _ = configs
        assert variant["strategy"]["params"]["trend_filter"] == 50
        assert "trend_filter" not in base["strategy"]["params"]
        base_params = dict(base["strategy"]["params"])
        variant_params = dict(variant["strategy"]["params"])
        variant_params.pop("trend_filter")
        assert base_params == variant_params
        for section in ("risk", "costs", "engine", "dataset"):
            assert base[section] == variant[section]
        assert base["experiment"]["environment"] == variant["experiment"]["environment"]

    def test_experiment_type_and_parent_lineage(self, configs) -> None:
        """E2 es ABLATION con parent = id del baseline; E1 es BASELINE."""
        base, variant, csv = configs
        base_report = run_experiment(base, csv)
        variant_report = run_experiment(variant, csv)
        assert base_report["lineage"]["experiment_type"] == "BASELINE"
        assert base_report["lineage"]["parent_experiment_id"] is None
        assert variant_report["lineage"]["experiment_type"] == "ABLATION"
        assert variant_report["lineage"]["change"] == "sma50_filter"
        assert variant_report["lineage"]["parent_experiment_id"] == base_report["experiment_id"]
        assert variant_report["lineage"]["hypothesis"] == "h001-breakout"

    def test_experiment_ids_differ(self, configs) -> None:
        base, variant, csv = configs
        assert run_experiment(base, csv)["experiment_id"] != run_experiment(variant, csv)["experiment_id"]

    def test_variant_reproducible(self, configs) -> None:
        _, variant, csv = configs
        r1 = run_experiment(variant, csv)
        r2 = run_experiment(variant, csv)
        assert r1["experiment_id"] == r2["experiment_id"]
        assert r1["trades"] == r2["trades"]
        assert r1["equity_curve"] == r2["equity_curve"]
        assert r1["metrics"] == r2["metrics"]

    def test_variant_lineage_records_filter(self, configs) -> None:
        _, variant, csv = configs
        report = run_experiment(variant, csv)
        params = report["lineage"]["strategy"]["params"]
        assert params["trend_filter"] == 50

    def test_variant_no_lookahead(self, configs) -> None:
        _, variant, csv = configs
        from data.loader import read_ohlc_csv
        from research.pipeline import build_engine

        engine = build_engine(variant)
        result = engine.run(build_strategy(variant["strategy"]), read_ohlc_csv(csv))
        bars = read_ohlc_csv(csv)
        costs = variant["costs"]
        per_side = (
            costs["spread_ticks"] * costs["tick_size"]
            + costs["slippage_per_side"]
            + costs["commission_per_unit"]
        )
        for trade in result.trades:
            sign = -1.0 if trade["side"] == "SHORT" else 1.0
            expected = bars[trade["entry_index"]]["open"] + sign * per_side
            assert trade["entry_price"] == pytest.approx(expected)