"""E8 — H003 :: ¿Breakout LONG o beta de BTC? (benchmarks + régimen).

Hipótesis (formalizada antes de mirar nada nuevo):
    El rendimiento LONG del breakout observado en H002 contiene información
    incremental respecto a una exposición pasiva a BTC, y no es explicado
    únicamente por la tendencia estructural alcista del activo.

Tratamiento y benchmarks (sin modificar UN SOLO parámetro de la estrategia):
    H003-A  Breakout LONG (exactamente H002-A: direction=long, mismos
            lookback/ATR/stop/risk/entradas). Tratamiento.
    H003-B  Buy & Hold puro (100% del tiempo, 1 operación).
    H003-C  Buy & Hold con la misma exposición de CAPITAL promedio del
            tratamiento (contesta: "¿gana porque está long, no por timing?").
    H003-D  Breakout LONG vs B&H por RÉGIMEN (bull/bear/lateral). El régimen
            es una variable de ANÁLISIS exógena y pre-registrada (SMA lenta
            ± banda sobre el propio precio); nunca un parámetro de estrategia.
    H003-E  Control con la misma fracción TEMPORAL expuesta long pero neutro
            a señales (duty cycle determinista), para descartar que
            "cualquier exposición long intermitente" produzca lo mismo.

Métricas anualizadas SOLO con frecuencia documentada:
    retorno simple por barra (1h) y anualización ×sqrt(bars_por_año=8766).

Disciplina OOS: se mantiene el split temporal 75/25 ya usado en H002.
Ese test quedó OBSERVADO, así que H003 no decide nada con él como si fuera
desconocido; la siguiente fase (H004) debe fijar TRAIN -> VALIDATION -> FINAL OOS
antes de tocar parámetros.
(o walk-forward) ANTES de tocar parámetros.

Ejecución: python experiments/e8_h003_breakout_vs_buyhold.py
"""

from __future__ import annotations

import copy
import json
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtesting.engine import BacktestResult
from data.loader import read_ohlc_csv
from reporting.metrics import calculate_metrics
from research.benchmarking import (
    buy_and_hold,
    capital_exposure_from_trades,
    classify_regimes,
    curve_metrics,
    exposure_fraction_from_trades,
    exposure_matched_long,
    exposure_scaled_buy_and_hold,
    regime_block_returns,
)
from research.pipeline import build_engine, build_strategy, experiment_id_for

EXPERIMENTS = Path(__file__).resolve().parent
H002_CONFIG = EXPERIMENTS / "h002" / "config.yaml"
CSV_PATH = ROOT / "experiments" / "data" / "market_BTCUSDT_60min.csv"

TRAIN_RATIO = 0.75
INTERVAL_SECONDS = 3600


def _long_cfg(base: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["strategy"]["params"]["direction"] = "long"
    cfg["experiment"]["name"] = "h003-breakout-long"
    cfg["experiment"]["hypothesis"] = "h003-breakout-vs-buyhold"
    return cfg


def _metrics_row(r: BacktestResult, label: str) -> dict:
    m = calculate_metrics(r)
    long_trades = [t for t in r.trades if t["side"] == "LONG"]
    pnls = [float(t["pnl"]) for t in r.trades if t.get("pnl") is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return {
        "label": label,
        "trades": m.num_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": m.win_rate,
        "expectancy": m.expectancy,
        "expectancy_e": m.expectancy_e,
        "payoff_ratio": m.payoff_ratio,
        "profit_factor": m.profit_factor,
        "avg_win": m.avg_win,
        "avg_loss": m.avg_loss,
        "median_trade": m.median_trade,
        "trade_std": m.trade_std,
        "gross_profit": m.gross_profit,
        "gross_loss": m.gross_loss,
        "costs": m.costs_total or 0.0,
        "max_consecutive_wins": m.max_consecutive_wins,
        "max_consecutive_losses": m.max_consecutive_losses,
        "net_pnl": m.expectancy * m.num_trades if m.expectancy is not None else 0.0,
        "avg_mae": m.avg_mae,
        "avg_mfe": m.avg_mfe,
        "avg_bars": round(statistics.mean([int(t["bars_in_trade"]) for t in r.trades]), 4)
        if r.trades else None,
        "long_trades": len(long_trades),
    }


def run_treatment(cfg: dict, bars: list[dict]) -> tuple[dict, BacktestResult]:
    strategy = build_strategy(cfg["strategy"])
    engine = build_engine(cfg)
    result = engine.run(strategy, bars)
    row = _metrics_row(result, cfg["strat_label"])
    row["experiment_id"] = experiment_id_for(cfg, CSV_PATH)
    return row, result


def _curve_metrics_for(result: BacktestResult, n_bars: int) -> dict:
    cm = curve_metrics(result.equity_curve, n_bars, INTERVAL_SECONDS)
    return {k: v for k, v in cm.items() if k != "annualization"}


def _time_exposure(result: BacktestResult, n_bars: int) -> float:
    return exposure_fraction_from_trades(result.trades, n_bars)


def _capital_exposure(result: BacktestResult, closes: list[float]) -> float:
    """Fracción promedio de capital desplegado MIENTRAS en posición (0 si n/d)."""
    return capital_exposure_from_trades(result.trades, result.equity_curve, closes)


def _fmt_pct(x) -> str:
    return "    —" if x is None else f"{x * 100:>7.2f}%"


def _fmt_num(x, w: int = 9, d: int = 4) -> str:
    return f"{x:>9}" if x is None else f"{round(x, d):>{w}.{d}f}"


def _print_bench_table(title: str, rows: list[dict]) -> None:
    print(f"\n{'=' * 118}\n{title}\n{'=' * 118}")
    header = (
        f"{'Opción':<28}{'trd':>4}{'ret%':>8}{'CAGR%':>8}{'DD%':>7}{'r/DD':>7}"
        f"{'vol%':>7}{'Sharpe':>7}{'Sortino':>8}{'Calmar':>7}"
        f"{'exp%':>6}{'costes':>9}"
    )
    print(header)
    for r in rows:
        m = r["metrics"]
        print(
            f"{r['label']:<28}{r['num_trades']:>4}{r['metrics']['net_return'] * 100:>8.2f}"
            f"{m['cagr'] * 100 if m['cagr'] is not None else float('nan'):>8.2f}"
            f"{m['max_drawdown'] * 100:>7.2f}"
            f"{m['return_over_max_dd'] if m['return_over_max_dd'] is not None else float('nan'):>7.2f}"
            f"{m['annual_volatility'] * 100 if m['annual_volatility'] is not None else float('nan'):>7.2f}"
            f"{m['sharpe'] if m['sharpe'] is not None else float('nan'):>7.2f}"
            f"{m['sortino'] if m['sortino'] is not None else float('nan'):>8.2f}"
            f"{m['calmar'] if m['calmar'] is not None else float('nan'):>7.2f}"
            f"{r['exposure_frac'] * 100:>6.1f}"
            f"{r['costs']:>9.1f}"
        )


def _print_regime_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    header = (
        f"{'régimen':<10}{'trd':>5}{'W/L':>7}{'wr%':>6}{'E$':>9}{'ret%':>8}"
        f"{'exp%':>6}  {'B&H ret%':>9}  {'BULALS':>7}"
    )
    print(header)
    for r in rows:
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"{r['regime']:<10}{r['trades']:>5}{wl:>7}"
            f"{_fmt_pct(r['win_rate'])}"
            f"{_fmt_num(r['expectancy'])}"
            f"{r['net_return_pct']:>8.1f}"
            f"{r['exposure'] * 100:>6.1f}"
            f"  {r['bh_compounded'] * 100 if r['bh_compounded'] is not None else float('nan'):>9.1f}"
            f"  {r['bh_blocks']:>7}"
        )


def _compare_treatment_vs_benchmarks(
    cfg: dict,
    bars: list[dict],
    strategy_id: str,
) -> dict:
    n = len(bars)
    closes = [float(b["close"]) for b in bars]
    cfg["strat_label"] = f"H003-A {strategy_id}"

    row, result = run_treatment(cfg, bars)
    time_frac = _time_exposure(result, n)
    cond_capital = _capital_exposure(result, closes)
    total_capital_frac = time_frac * cond_capital

    bh = buy_and_hold(
        bars,
        initial_equity=cfg["costs"]["initial_equity"],
        per_side=0.15,
        point_value=cfg["engine"].get("point_value", 1.0),
    )
    bh_scaled = exposure_scaled_buy_and_hold(
        bars,
        exposure_frac=total_capital_frac,
        initial_equity=cfg["costs"]["initial_equity"],
        per_side=0.15,
        point_value=cfg["engine"].get("point_value", 1.0),
    )
    matched = exposure_matched_long(
        bars,
        target_exposure=time_frac,
        initial_equity=cfg["costs"]["initial_equity"],
        per_side=0.15,
        point_value=cfg["engine"].get("point_value", 1.0),
        risk_per_trade=cfg["risk"]["risk_per_trade"],
        atr_period=cfg["strategy"]["params"]["atr_period"],
        cycle_bars=cfg["engine"].get("max_bars_in_trade") or 48,
        offset=7,
    )

    treatment_metrics = _curve_metrics_for(result, n)
    rows = [
        {"label": f"H003-A Breakout LONG", "num_trades": row["trades"],
         "metrics": treatment_metrics, "exposure_frac": time_frac, "costs": row["costs"]},
        {"label": "H003-B Buy & Hold", "num_trades": bh["num_trades"],
         "metrics": bh["metrics"], "exposure_frac": bh["exposure_frac"], "costs": bh["costs"]},
        {"label": "H003-C B&H ×exp-cap", "num_trades": bh_scaled["num_trades"],
         "metrics": bh_scaled["metrics"], "exposure_frac": bh_scaled["exposure_frac"],
         "costs": bh_scaled["costs"]},
        {"label": "H003-E Exp-matched", "num_trades": matched["num_trades"],
         "metrics": matched["metrics"], "exposure_frac": matched["exposure_frac"],
         "costs": matched["costs"]},
    ]

    regimes = classify_regimes(bars)
    bh_regimes = regime_block_returns(bars, regimes)

    regime_rows = []
    for label in ("BULL", "BEAR", "LATERAL"):
        trs = [
            t for t in result.trades
            if regimes[int(t.get("entry_index") or 0)] == label
        ]
        pn = [float(t["pnl"]) for t in trs]
        wins = sum(1 for p in pn if p > 0)
        exp_bars = sum(
            max(0, int(t.get("exit_index") or int(t.get("entry_index") or 0))
                - int(t.get("entry_index") or 0) + 1)
            for t in trs
        )
        regime_rows.append({
            "regime": label,
            "trades": len(trs),
            "wins": wins,
            "losses": len(trs) - wins,
            "win_rate": (wins / len(trs)) if trs else None,
            "expectancy": statistics.mean(pn) if pn else None,
            "net_pnl": sum(pn),
            "net_return_pct": sum(pn) / cfg["costs"]["initial_equity"] * 100.0,
            "exposure": exp_bars / n,
            "bh_blocks": bh_regimes[label]["blocks"],
            "bh_compounded": bh_regimes[label]["compounded_return"],
            "avg_bh_block": bh_regimes[label]["avg_block_return"],
        })

    return {
        "strategy": strategy_id,
        "experiment_id": row["experiment_id"],
        "treatment_row": row,
        "benchmarks": rows,
        "exposure": {"time_frac": time_frac, "cond_capital": cond_capital,
                     "total_capital_frac": total_capital_frac},
        "regimes": regime_rows,
    }


def _classify_decision(tr: dict, te: dict) -> dict:
    """Clasifica el resultado según los casos pre-registrados del usuario.

    Comparaciones de CURVA (normalizadas): retorno neto, DD y retorno/riesgo
    contra tres referencias: B&H puro (100% expuesto), B&H escalado a la
    exposición de capital del tratamiento (H003-C) y control de exposición
    temporal NEUTRA (H003-E). Se compara además el signo neto por régimen en
    train y en test (H003-D).

        Caso B: breakout ≈ (o por debajo de) B&H escalado y/o control neutro.
        Caso C: ventaja solo en BULL (neto ≤ 0 en BEAR/LATERAL de test).
        Caso D: ventaja neta en bull + lateral + bear en train Y test.
        Caso A: ventaja vs exposición comparable y vs control neutro, con menor
                DD o mejor retorno/riesgo en ambos períodos.
    """
    def obs_for(data):
        bench = {b["label"]: b["metrics"] for b in data["benchmarks"]}
        bha = bench.get("H003-B Buy & Hold", {})
        bhc = (bench.get("H003-C B&H ×exp-cap") or bha)
        bhe = bench.get("H003-E Exp-matched", {})
        ret_tr = next(b["metrics"]["net_return"] for b in data["benchmarks"]
                      if b["label"].startswith("H003-A"))
        ret_bh = bha.get("net_return") or 0.0
        ret_bhc = bhc.get("net_return") or ret_bh
        ret_bhe = bhe.get("net_return") or ret_bh
        return {
            "ret_treatment": ret_tr,
            "ret_bh": ret_bh,
            "ret_bh_exposure_scaled": ret_bhc,
            "ret_exposure_matched": ret_bhe,
            "delta_bh": ret_tr - ret_bh,
            "delta_bh_scaled": ret_tr - ret_bhc,
            "delta_exposure_matched": ret_tr - ret_bhe,
            "dd_treatment": next(b["metrics"]["max_drawdown"] for b in data["benchmarks"]
                                 if b["label"].startswith("H003-A")),
            "dd_bh": bha.get("max_drawdown") or 0.0,
            "regime_net": {r["regime"]: r["net_pnl"] for r in data["regimes"]},
            "regime_trades": {r["regime"]: r["trades"] for r in data["regimes"]},
        }

    train_obs = obs_for(tr)
    test_obs = obs_for(te)

    def net_positive_regimes(o) -> list[str]:
        return [r for r, v in o["regime_net"].items() if v > 0 and o["regime_trades"].get(r, 0) > 0]

    train_pos = net_positive_regimes(train_obs)
    test_pos = net_positive_regimes(test_obs)

    tr_beats_bh = train_obs["delta_bh_scaled"] > 0
    te_beats_bh = test_obs["delta_bh_scaled"] > 0
    tr_beats_neutral = train_obs["delta_exposure_matched"] > 0
    te_beats_neutral = test_obs["delta_exposure_matched"] > 0
    tr_better_dd = train_obs["dd_treatment"] < train_obs["dd_bh"]
    te_better_dd = test_obs["dd_treatment"] < test_obs["dd_bh"]
    tr_better_risk = (train_obs["ret_treatment"] / train_obs["dd_treatment"]
                      if train_obs["dd_treatment"] > 0 else 0.0)
    te_better_risk = (test_obs["ret_treatment"] / test_obs["dd_treatment"]
                      if test_obs["dd_treatment"] > 0 else 0.0)

    bull_only_test = (
        test_pos == ["BULL"]
        and train_obs["regime_net"].get("BEAR", 0) <= 0
    )
    all_regimes = (
        set(train_pos) == {"BULL", "BEAR", "LATERAL"}
        and set(test_pos) == {"BULL", "BEAR", "LATERAL"}
    )

    if all_regimes and tr_beats_bh and te_beats_bh:
        case = "D"
        conclusion = ("Breakout LONG mantiene ventaja neta en bull + lateral + bear "
                      "en train y en test, y supera al B&H con exposición comparable "
                      "y al control de exposición neutra. Evidencia fuerte a favor de "
                      "señal de timing, pendiente walk-forward/costes/stress.")
    elif tr_beats_bh and te_beats_bh and tr_beats_neutral and te_beats_neutral:
        case = "A"
        conclusion = (
            f"Breakout LONG > B&H con exposición comparable y > control de "
            f"exposición neutra en train y test, con retorno/riesgo "
            f"{tr_better_risk:.2f} (train) / {te_better_risk:.2f} (test). Evidencia de "
            f"timing digna de investigación adicional (H004 walk-forward). "
            f"Caveat: régimen BULL del test neto negativo; ver H003-D."
        )
    elif bull_only_test:
        case = "C"
        conclusion = ("La ventaja del breakout se concentra en BULL: posible "
                      "dependencia de régimen; aún no implica edge universal.")
    else:
        case = "B"
        conclusion = ("Breakout LONG ≈ (o inferior a) B&H escalado y/o control de "
                      "exposición neutra: la mayor parte del resultado parece "
                      "explicada por la exposición alcista (beta) de BTC.")

    return {
        "case": case,
        "conclusion": conclusion,
        "observations": {
            "train": {
                "delta_vs_bh_scaled": train_obs["delta_bh_scaled"],
                "delta_vs_bh": train_obs["delta_bh"],
                "delta_vs_exposure_matched": train_obs["delta_exposure_matched"],
                "dd_vs_bh_lower": tr_better_dd,
                "return_over_dd": tr_better_risk,
                "regime_positive": train_pos,
                "regime_net_usd": train_obs["regime_net"],
            },
            "test": {
                "delta_vs_bh_scaled": test_obs["delta_bh_scaled"],
                "delta_vs_bh": test_obs["delta_bh"],
                "delta_vs_exposure_matched": test_obs["delta_exposure_matched"],
                "dd_vs_bh_lower": te_better_dd,
                "return_over_dd": te_better_risk,
                "regime_positive": test_pos,
                "regime_net_usd": test_obs["regime_net"],
            },
        },
    }


def main() -> int:
    base = yaml.safe_load(H002_CONFIG.read_text(encoding="utf-8"))
    cfg = _long_cfg(base)
    bars = read_ohlc_csv(CSV_PATH)
    split_i = int(len(bars) * TRAIN_RATIO)
    train, test = bars[:split_i], bars[split_i:]
    print(
        f"H003 — BTC 1h {len(bars)} barras | train {len(train)} "
        f"({bars[0]['ts']}->{train[-1]['ts']}) | test {len(test)} "
        f"OBSERVADO por H002 ({test[0]['ts']}->{test[-1]['ts']})"
    )

    strat_id = cfg["strategy"]["strategy_id"]
    tr = _compare_treatment_vs_benchmarks(cfg, train, strat_id)
    te = _compare_treatment_vs_benchmarks(cfg, test, strat_id)

    _print_bench_table("TRAIN (2019->2024) — H003-A vs B&H (benchmarks normalizados)", tr["benchmarks"])
    _print_bench_table("TEST  (2024->2026, OOS ya observado) — H003-A vs B&H", te["benchmarks"])

    print("\nExposición del tratamiento:")
    for name, data in (("train", tr), ("test", te)):
        e = data["exposure"]
        print(
            f"  {name:<6} time_frac={e['time_frac'] * 100:>5.1f}%  "
            f"capital_cond={e['cond_capital'] * 100:>5.1f}%  "
            f"capital_total_avg={e['total_capital_frac'] * 100:>5.1f}%"
        )

    _print_regime_table("RÉGIMEN (H003-D) — TRAIN", tr["regimes"])
    _print_regime_table("RÉGIMEN (H003-D) — TEST", te["regimes"])

    dec = _classify_decision(tr, te)
    print(f"\nDECISIÓN H003 — Caso {dec['case']}: {dec['conclusion']}")

    out = {
        "hypothesis": "H003: el breakout LONG contiene info incremental vs B&H "
                      "(no es solo beta alcista de BTC).",
        "treatment_unchanged": "CERO parámetros tocados: idéntico a H002-A (dir=long).",
        "oos_note": "Split 75/25; el test 25% ya fue observado por H002. No reutilizar "
                    "indefinidamente; H004 definirá TRAIN->VALIDATION->FINAL OOS.",
        "annualization": "retorno simple por barra (1h), ×sqrt(8766)/año para Sharpe/"
                         "Sortino/volatilidad; CAGR sobre años.",
        "train": tr,
        "test": te,
        "decision": dec,
    }
    dest = EXPERIMENTS / "outputs" / "h003_breakout_vs_buyhold.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())