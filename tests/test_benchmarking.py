"""Tests de benchmarks de exposición pasiva (H003): B&H, exposure-matched,
B&H escalado por exposición, régimen y métricas anualizadas."""

from __future__ import annotations

import pytest

from research.benchmarking import (
    buy_and_hold,
    capital_exposure_from_trades,
    classify_regimes,
    curve_metrics,
    exposure_fraction_from_trades,
    exposure_matched_long,
    exposure_scaled_buy_and_hold,
    max_drawdown,
    regime_block_returns,
)

PER_SIDE = 0.0
EQUITY = 100_000.0


def _bars(prices: list[float], high_mult: float = 1.05, low_mult: float = 0.95) -> list[dict]:
    ts = 1_700_000_000
    out = []
    for i, close in enumerate(prices):
        out.append({
            "ts": str(ts + i * 3600),
            "open": close,
            "high": close * high_mult,
            "low": close * low_mult,
            "close": close,
        })
    return out


def _rising(n: int = 100) -> list[dict]:
    return _bars([100.0 * (1.0 + 0.01 * i) for i in range(n)])


def test_max_drawdown():
    curve = [100.0, 110.0, 90.0, 95.0, 80.0]
    assert abs(max_drawdown(curve) - (110.0 - 80.0) / 110.0) < 1e-9


def test_buy_and_hold_rising_market():
    bars = _rising(100)
    bh = buy_and_hold(bars, initial_equity=EQUITY, per_side=PER_SIDE)
    expected_return = bars[-1]["close"] / bars[0]["close"] - 1.0
    assert bh["num_trades"] == 1
    assert bh["exposure_frac"] == 1.0
    assert abs(bh["metrics"]["net_return"] - expected_return) < 1e-9
    assert bh["costs"] == 0.0
    assert len(bh["curve"]) == len(bars) + 1


def test_buy_and_hold_with_costs():
    bars = _rising(100)
    bh = buy_and_hold(bars, initial_equity=EQUITY, per_side=0.5, point_value=10.0)
    qty = EQUITY / bars[0]["close"]
    expected_costs = qty * 2 * 0.5 * 10.0
    assert abs(bh["costs"] - expected_costs) < 1e-6


def test_exposure_matched_reaches_approx_target():
    bars = _rising(500)
    target = 0.4
    em = exposure_matched_long(bars, target_exposure=target, initial_equity=EQUITY,
                               per_side=PER_SIDE, cycle_bars=50, offset=3)
    assert abs(em["exposure_frac"] - target) < 0.02
    assert em["num_trades"] > 1


def test_exposure_matched_no_lookahead_shape():
    bars = _rising(200)
    em = exposure_matched_long(bars, target_exposure=0.25, initial_equity=EQUITY,
                               per_side=PER_SIDE, cycle_bars=40)
    assert len(em["curve"]) == len(bars) + 1
    assert em["metrics"]["net_return"] > 0.0


def test_exposure_scaled_buy_and_hold_scales_return():
    bars = _rising(100)
    full = buy_and_hold(bars, initial_equity=EQUITY, per_side=PER_SIDE)
    scaled = exposure_scaled_buy_and_hold(bars, exposure_frac=0.5,
                                          initial_equity=EQUITY, per_side=PER_SIDE)
    ratio = full["metrics"]["net_return"] / scaled["metrics"]["net_return"]
    assert abs(ratio - 2.0) < 1e-6


def test_classify_regimes_labels_only():
    bars = _rising(900)
    regimes = classify_regimes(bars, sma_period=60, band=0.01)
    assert len(regimes) == len(bars)
    assert set(regimes) <= {"BULL", "BEAR", "LATERAL"}


def test_regime_block_returns_structure():
    bars = _rising(300)
    regimes = classify_regimes(bars, sma_period=40, band=0.02)
    out = regime_block_returns(bars, regimes)
    for label in ("BULL", "BEAR", "LATERAL"):
        assert set(out[label]) == {
            "blocks", "bars", "returns", "compounded_return", "avg_block_return"
        }


def test_curve_metrics_annualization_positive_vol():
    bars = _rising(300)
    bh = buy_and_hold(bars, initial_equity=EQUITY, per_side=PER_SIDE)
    m = bh["metrics"]
    assert m["net_return"] > 0
    assert m["cagr"] is not None and m["cagr"] > 0
    assert m["annual_volatility"] is not None and m["annual_volatility"] > 0
    assert "annualization" in m


def test_exposure_fraction_from_trades():
    trades = [
        {"entry_index": 0, "exit_index": 9},
        {"entry_index": 20, "exit_index": 29},
    ]
    assert abs(exposure_fraction_from_trades(trades, 100) - 0.20) < 1e-9
    assert exposure_fraction_from_trades([], 100) == 0.0


def test_capital_exposure_proportional_to_quantity():
    trades = [{"entry_index": 0, "exit_index": 9, "quantity": q} for q in (1.0, 2.0)]
    closes = [100.0] * 10
    curve = [100_000.0] * 11
    base = capital_exposure_from_trades([trades[0]], curve, closes)
    doubled = capital_exposure_from_trades(trades, curve, closes)
    assert doubled == pytest.approx(base * 1.5)  # coinciden en las mismas 10 barras
    assert base == pytest.approx(100.0 / 100_000.0)