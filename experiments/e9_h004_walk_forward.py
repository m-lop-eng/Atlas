"""E9 — H004 :: Walk-Forward de parámetros FIJOS (estabilidad temporal).

Pregunta: ¿el edge observado en H003 (timing LONG del breakout) aparece
REPETIDAMENTE a través del tiempo, sin recalibración y sin selección de
ventanas? ¿Es estable frente a cambios de régimen, o depende de unas pocas
ventanas históricas favorables?

Disciplina (pre-registrada, NADA se optimiza aquí):
    * Estrategia CONGELADA = H002-A (direction=long) y H003-A: los parámetros
      lookback/ATR/stop/riesgo/costes/ejecución NO cambian. No hay tuning ni
      selección por ventana: cada ventana se evalúa con la misma config.
    * Walk-forward puramente temporal sobre el tramo DEVELOPMENT (2019-2024).
      Las validaciones W1..W4 son ventanas de ~1 año consecutivas, todas
      ANTES del tramo OBSERVADO (2024-10::2026-08) consumido por H002/H003.
    * FINAL OOS: NO SE TOCA aquí. previous_oos_consumed=true,
      final_oos_consumed=false. El framework DATA_ROLE (research/data_role.py)
      impide presentar el tramo OBSERVED como OOS desconocido (guard).
    * Régimen BULL adverso de H003: no se arregla, se registra por ventana.

Contrastes por ventana (reutilizando benchmarks de H003, sin nuevos controles
optimizados): Breakout LONG, B&H, B&H escalado a la misma exposición de
capital, control de exposición temporal neutra (duty-cycle). Contraste
central: expectancy del breakout vs expectancy del control neutro.

Ejecución: python experiments/e9_h004_walk_forward.py
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
)
from research.data_role import DataRoleError, RoleRegistry
from research.experiment import make_config_hash
from research.pipeline import build_engine, build_strategy, experiment_id_for

EXPERIMENTS = Path(__file__).resolve().parent
H002_CONFIG = EXPERIMENTS / "h002" / "config.yaml"
CSV_PATH = ROOT / "experiments" / "data" / "market_BTCUSDT_60min.csv"

INTERVAL_SECONDS = 3600
PER_SIDE = 0.15  # slippage 0.05 + comisión 0.10 por unidad (igual que H003)

DEV_START = 1_546_300_800  # 2019-01-01 (inicio del dataset)
DEV_END = 1_727_787_600  # 2024-10-01 (fin del tramo DEVELOPMENT)
OBS_END = 1_788_217_200  # 2026-08-31 (fin del dataset, tramo OBSERVED)

# Validaciones consecutivas ~1 año, todas dentro de DEVELOPMENT.
WINDOWS = [
    ("W1", DEV_START, 1_609_459_200, 1_640_995_200),  # valid 2021-01->2022-01
    ("W2", DEV_START, 1_640_995_200, 1_672_531_200),  # valid 2022-01->2023-01
    ("W3", DEV_START, 1_672_531_200, 1_704_067_200),  # valid 2023-01->2024-01
    ("W4", DEV_START, 1_704_067_200, 1_727_787_600),  # valid 2024-01->2024-10
]


def _long_cfg(base: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["strategy"]["params"]["direction"] = "long"
    cfg["experiment"]["name"] = "h004-walk-forward"
    cfg["experiment"]["hypothesis"] = "h004-temporal-robustness"
    cfg["experiment"]["type"] = "WALK_FORWARD"
    cfg["strat_label"] = "Breakout LONG"
    return cfg


def _slice(bars: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    return [b for b in bars if start_ts <= float(b["ts"]) < end_ts]


def _run(cfg: dict, bars: list[dict]) -> BacktestResult:
    strategy = build_strategy(cfg["strategy"])
    engine = build_engine(cfg)
    return engine.run(strategy, bars)


def _row_metrics(result: BacktestResult, n_bars: int, closes: list[float]) -> dict:
    m = calculate_metrics(result)
    cm = curve_metrics(result.equity_curve, n_bars, INTERVAL_SECONDS)
    pnls = [float(t["pnl"]) for t in result.trades if t.get("pnl") is not None]
    gross = sum(p for p in pnls if p > 0) - abs(sum(p for p in pnls if p < 0))
    net = sum(pnls)
    costs = m.costs_total or 0.0
    return {
        "trades": m.num_trades,
        "win_rate": m.win_rate,
        "expectancy": m.expectancy,
        "expectancy_r": (m.expectancy / m.avg_loss) if m.expectancy is not None and m.avg_loss else None,
        "profit_factor": m.profit_factor,
        "avg_win": m.avg_win,
        "avg_loss": m.avg_loss,
        "median_trade": m.median_trade,
        "trade_std": m.trade_std,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": net,
        "net_return": cm["net_return"],
        "cagr": cm["cagr"],
        "max_drawdown": cm["max_drawdown"],
        "sharpe": cm["sharpe"],
        "sortino": cm["sortino"],
        "calmar": cm["calmar"],
        "avg_mae": m.avg_mae,
        "avg_mfe": m.avg_mfe,
        "mfe_mae_ratio": m.mfe_mae_ratio,
        "consecutive_losses": m.max_consecutive_losses,
        "exposure_time": exposure_fraction_from_trades(result.trades, n_bars),
        "capital_exposure_cond": capital_exposure_from_trades(result.trades, result.equity_curve, closes),
    }


def _regime_row(result: BacktestResult, regimes: list[str], n: int, equity_base: float) -> dict:
    rows = []
    for label in ("BULL", "BEAR", "LATERAL"):
        trs = [t for t in result.trades if regimes[int(t.get("entry_index") or 0)] == label]
        pn = [float(t["pnl"]) for t in trs]
        wins = sum(1 for p in pn if p > 0)
        losses = len(pn) - wins
        gross_wins = sum(p for p in pn if p > 0)
        gross_losses = abs(sum(p for p in pn if p < 0))
        acc = 0.0
        peak = 0.0
        dd_frac = 0.0
        for p in pn:
            acc += p
            peak = max(peak, acc)
            dd_frac = max(dd_frac, (peak - acc) / equity_base)
        rows.append({
            "regime": label,
            "trades": len(trs),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(trs)) if trs else None,
            "expectancy": statistics.mean(pn) if pn else None,
            "profit_factor": (gross_wins / gross_losses) if gross_losses else None,
            "net_pnl": sum(pn),
            "dd_approx_pnl_init": dd_frac,
            "exposure_frac": (
                sum(max(0, int(t.get("exit_index") or int(t.get("entry_index") or 0))
                        - int(t.get("entry_index") or 0) + 1) for t in trs) / n
                if trs else 0.0
            ),
        })
    return {"rows": rows, "n_bars": n}


def _fmt_pct(x) -> str:
    return "   --" if x is None else f"{x * 100:>6.1f}%"


def _fmt_num(x, d: int = 4) -> str:
    return "    --" if x is None else f"{x:>{d + 5}.{d}f}"


def _print_metrics_table(title: str, rows: list[tuple[str, dict]]) -> None:
    print(f"\n{title}")
    header = (
        f"{'segmento':<14}{'trd':>4}{'wr%':>6}{'E$':>9}{'E/R':>6}{'PF':>6}"
        f"{'net$':>11}{'ret%':>8}{'DD%':>7}{'Sharpe':>7}{'Sortino':>8}{'Calmar':>7}"
        f"{'MAE$':>9}{'MFE$':>9}{'MF/MA':>6}{'exp%':>6}{'cap.%':>6}"
    )
    print(header)
    for label, r in rows:
        cap = r["capital_exposure_cond"]
        cap_str = f"    --" if cap is None else f"{cap * 100:>6.1f}"
        print(
            f"{label:<14}{r['trades']:>4}{_fmt_pct(r['win_rate'])}"
            f"{_fmt_num(r['expectancy'], 2)}"
            f"{_fmt_num(r['expectancy_r'], 2)}"
            f"{_fmt_num(r['profit_factor'], 2)}"
            f"{r['net_pnl']:>11.1f}"
            f"{r['net_return'] * 100:>8.2f}"
            f"{r['max_drawdown'] * 100:>7.1f}"
            f"{r['sharpe'] if r['sharpe'] is not None else float('nan'):>7.2f}"
            f"{r['sortino'] if r['sortino'] is not None else float('nan'):>8.2f}"
            f"{r['calmar'] if r['calmar'] is not None else float('nan'):>7.2f}"
            f"{r['avg_mae'] if r['avg_mae'] is not None else float('nan'):>9.1f}"
            f"{r['avg_mfe'] if r['avg_mfe'] is not None else float('nan'):>9.1f}"
            f"{r['mfe_mae_ratio'] if r['mfe_mae_ratio'] is not None else float('nan'):>6.2f}"
            f"{r['exposure_time'] * 100:>6.1f}"
            f"{cap_str}"
        )


def _print_regime_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    print(f"{'régimen':<10}{'trd':>5}{'W/L':>7}{'wr%':>6}{'E$':>9}{'PF':>6}"
          f"{'net$':>11}{'DD~%init':>10}{'exp%':>6}")
    for r in rows:
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"{r['regime']:<10}{r['trades']:>5}{wl:>7}{_fmt_pct(r['win_rate'])}"
            f"{_fmt_num(r['expectancy'], 2)}"
            f"{_fmt_num(r['profit_factor'], 2)}"
            f"{r['net_pnl']:>11.1f}"
            f"{r['dd_approx_pnl_init'] * 100:>10.1f}"
            f"{r['exposure_frac'] * 100:>6.1f}"
        )


def _eval_window(cfg: dict, bars_all: list[dict], label: str, train_start: int, valid_start: int, valid_end: int) -> dict:
    train_bars = _slice(bars_all, train_start, valid_start)
    valid_bars = _slice(bars_all, valid_start, valid_end)
    n_v = len(valid_bars)
    valid_closes = [float(b["close"]) for b in valid_bars]

    tr_res = _run(cfg, train_bars)
    va_res = _run(cfg, valid_bars)

    tr_row = _row_metrics(tr_res, len(train_bars), [float(b["close"]) for b in train_bars])
    va_row = _row_metrics(va_res, n_v, valid_closes)

    total_capital_frac = va_row["exposure_time"] * va_row["capital_exposure_cond"]
    eq_frac = va_row["exposure_time"]

    bh = buy_and_hold(valid_bars, initial_equity=cfg["costs"]["initial_equity"],
                      per_side=PER_SIDE, point_value=cfg["engine"].get("point_value", 1.0))
    bh_scaled = exposure_scaled_buy_and_hold(valid_bars, exposure_frac=total_capital_frac,
                                             initial_equity=cfg["costs"]["initial_equity"],
                                             per_side=PER_SIDE, point_value=cfg["engine"].get("point_value", 1.0))
    neutral = exposure_matched_long(valid_bars, target_exposure=eq_frac,
                                    initial_equity=cfg["costs"]["initial_equity"],
                                    per_side=PER_SIDE, point_value=cfg["engine"].get("point_value", 1.0),
                                    risk_per_trade=cfg["risk"]["risk_per_trade"],
                                    atr_period=cfg["strategy"]["params"]["atr_period"],
                                    cycle_bars=cfg["engine"].get("max_bars_in_trade") or 48,
                                    offset=7)

    neutral_row = {
        "trades": neutral["num_trades"],
        "win_rate": None, "expectancy": (statistics.mean(neutral["block_pnls"])
                                         if neutral["block_pnls"] else None),
        "expectancy_r": None, "profit_factor": None,
        "avg_win": None, "avg_loss": None, "median_trade": None, "trade_std": None,
        "gross_pnl": None, "costs": neutral["costs"],
        "net_pnl": neutral["net_pnl"],
        "net_return": neutral["metrics"]["net_return"],
        "cagr": neutral["metrics"]["cagr"],
        "max_drawdown": neutral["metrics"]["max_drawdown"],
        "sharpe": neutral["metrics"]["sharpe"],
        "sortino": neutral["metrics"]["sortino"],
        "calmar": neutral["metrics"]["calmar"],
        "avg_mae": None, "avg_mfe": None, "mfe_mae_ratio": None,
        "consecutive_losses": None,
        "exposure_time": neutral["exposure_frac"],
        "capital_exposure_cond": None,
    }
    bh_row = {
        "trades": bh["num_trades"], "win_rate": None,
        "expectancy": None, "expectancy_r": None, "profit_factor": None,
        "avg_win": None, "avg_loss": None, "median_trade": None, "trade_std": None,
        "gross_pnl": None, "costs": bh["costs"], "net_pnl": bh["net_pnl"],
        "net_return": bh["metrics"]["net_return"], "cagr": bh["metrics"]["cagr"],
        "max_drawdown": bh["metrics"]["max_drawdown"],
        "sharpe": bh["metrics"]["sharpe"], "sortino": bh["metrics"]["sortino"],
        "calmar": bh["metrics"]["calmar"],
        "avg_mae": None, "avg_mfe": None, "mfe_mae_ratio": None,
        "consecutive_losses": None,
        "exposure_time": bh["exposure_frac"], "capital_exposure_cond": None,
    }
    bh_scaled_row = dict(bh_row)
    bh_scaled_row.update({
        "trades": bh_scaled["num_trades"], "costs": bh_scaled["costs"],
        "net_pnl": bh_scaled["net_pnl"], "net_return": bh_scaled["metrics"]["net_return"],
        "max_drawdown": bh_scaled["metrics"]["max_drawdown"],
        "sharpe": bh_scaled["metrics"]["sharpe"], "sortino": bh_scaled["metrics"]["sortino"],
        "calmar": bh_scaled["metrics"]["calmar"],
    })

    regimes = classify_regimes(valid_bars)
    reg = _regime_row(va_res, regimes, n_v, cfg["costs"]["initial_equity"])

    return {
        "window": label,
        "valid_range": [valid_start, valid_end],
        "valid_bars": n_v,
        "train_bars": len(train_bars),
        "train": tr_row,
        "validation": va_row,
        "benchmarks": {
            "bh": bh_row,
            "bh_scaled": bh_scaled_row,
            "neutral": neutral_row,
            "bh_scaled_frac": total_capital_frac,
            "neutral_target_frac": eq_frac,
        },
        "regimes": reg["rows"],
        "contrasts": {
            "delta_breakout_vs_neutral_ret": va_row["net_return"] - neutral["metrics"]["net_return"],
            "delta_breakout_vs_bh_scaled_ret": va_row["net_return"] - bh_scaled["metrics"]["net_return"],
            "expectancy_breakout_vs_neutral": va_row["expectancy"] - neutral_row["expectancy"],
        },
    }


def _classify_decision(windows: list[dict]) -> dict:
    n = len(windows)
    majority = n // 2 + 1
    be_neutral = sum(1 for w in windows
                     if w["contrasts"]["delta_breakout_vs_neutral_ret"] > 0)
    be_scaled = sum(1 for w in windows
                    if w["contrasts"]["delta_breakout_vs_bh_scaled_ret"] > 0)
    net_pos_win = sum(1 for w in windows if w["validation"]["net_return"] > 0)
    bull_neg_win = sum(1 for w in windows
                       if next(r["net_pnl"] for r in w["regimes"] if r["regime"] == "BULL") < 0)

    pooled = {"BULL": 0.0, "BEAR": 0.0, "LATERAL": 0.0}
    pooled_tr = {"BULL": 0, "BEAR": 0, "LATERAL": 0}
    for w in windows:
        for r in w["regimes"]:
            pooled[r["regime"]] += r["net_pnl"]
            pooled_tr[r["regime"]] += r["trades"]
    regime_pos = [r for r in ("BULL", "BEAR", "LATERAL")
                  if pooled[r] > 0 and pooled_tr[r] >= 10]
    non_bull_pos = pooled["BEAR"] > 0 or pooled["LATERAL"] > 0

    total_pos = sum(max(w["validation"]["net_pnl"], 0.0) for w in windows)
    largest = max(max(w["validation"]["net_pnl"], 0.0) for w in windows)
    largest_share = (largest / total_pos) if total_pos > 0 else 0.0

    robust = (
        be_neutral >= majority
        and be_scaled >= majority
        and net_pos_win >= majority
        and bull_neg_win <= (n - majority)
        and len(regime_pos) >= 2
        and non_bull_pos
        and largest_share <= 0.6
    )
    rejected = (
        be_neutral < majority
        and net_pos_win < majority
        and bull_neg_win >= majority
    )
    if robust:
        state = "ROBUSTNESS_SUPPORTED"
        conclusion = (
            "La ventaja del breakout LONG aparece repetidamente en las ventanas "
            "de validación, frente al control neutral y al B&H escalado, sin "
            "depender de una única ventana y con contribución neta en mas de un "
            "régimen. Estabilidad temporal preliminar (falta FINAL OOS limpio)."
        )
    elif rejected:
        state = "REJECTED"
        conclusion = (
            "El efecto desaparece consistentemente fuera de las condiciones que "
            "generaron la hipótesis: la mayoría de ventanas no supera al control "
            "neutro ni al B&H escalado, o son netamente negativas."
        )
    else:
        state = "RESEARCH_REQUIRED"
        conclusion = (
            "Existe ventaja pero hay inestabilidad importante entre ventanas o "
            "dependencia de régimen: se mantiene en investigación, sin evidencia "
            "suficiente de robustez temporal ni de rechazo."
        )

    return {
        "state": state,
        "conclusion": conclusion,
        "windows_n": n,
        "majority_threshold": majority,
        "beats_neutral_windows": be_neutral,
        "beats_bh_scaled_windows": be_scaled,
        "positive_net_windows": net_pos_win,
        "bull_negative_windows": bull_neg_win,
        "pooled_regime_net": pooled,
        "pooled_regime_trades": pooled_tr,
        "regimes_positive": regime_pos,
        "non_bull_positive": non_bull_pos,
        "largest_window_share": largest_share,
        "criteria": {
            "robustness": "majority ven ventajas vs neutral y B&H escalado, neto>0,"
                          " BULL negativo no recurrente, >=2 regímenes positivos con"
                          " al menos uno no-BULL, ninguna ventana >60% del total.",
            "status": "heuristic, no umbral de rentabilidad (05_RESEARCH disciplina).",
        },
    }


def main() -> int:
    base = yaml.safe_load(H002_CONFIG.read_text(encoding="utf-8"))
    cfg = _long_cfg(base)
    bars = read_ohlc_csv(CSV_PATH)

    reg = RoleRegistry.load_default()
    print(
        f"H004 — BTC 1h {len(bars)} barras | DEVELOPMENT {DEV_START}->{DEV_END} "
        f"| OBSERVED {DEV_END}->{OBS_END} (consumido por H002/H003, no reutilizable)"
    )
    print(f"previous_oos_consumed={reg.previous_oos_consumed} "
          f"final_oos_consumed={reg.final_oos_consumed}")

    for label, ts, vs, ve in WINDOWS:
        reg.guard_final_oos(vs, ve, purpose=f"validación {label}")
    try:
        reg.guard_final_oos(DEV_END, OBS_END, purpose="intento de reutilizar el OOS viejo")
        print("ERROR: el guard no bloqueo el tramo OBSERVED")
        return 1
    except DataRoleError as exc:
        print(f"    guard OK: {exc}")

    print("\nVentanas de validación (train expandido desde 2019):")
    for label, ts, vs, ve in WINDOWS:
        from datetime import datetime, timezone
        d = lambda e: datetime.fromtimestamp(e, timezone.utc).date().isoformat()
        print(f"  {label}: validación {d(vs)} -> {d(ve)}")

    windows = []
    for label, ts, vs, ve in WINDOWS:
        print(f"\nWINDOW {label}")
        w = _eval_window(cfg, bars, label, ts, vs, ve)
        windows.append(w)
        _print_metrics_table(
            f"  {label} — métricas (estrategia CONGELADA, sin recalibración)",
            [
                ("TRAIN breakout", w["train"]),
                ("VALID breakout", w["validation"]),
                ("VALID B&H", w["benchmarks"]["bh"]),
                ("VALID B&H xcap", w["benchmarks"]["bh_scaled"]),
                ("VALID neutral", w["benchmarks"]["neutral"]),
            ],
        )
        _print_regime_table(f"  {label} — régimen (validación)", w["regimes"])
        c = w["contrasts"]
        print(f"  {label} contraste: delta vs neutral={c['delta_breakout_vs_neutral_ret'] * 100:+.1f}pp "
              f"| delta vs B&H escalado={c['delta_breakout_vs_bh_scaled_ret'] * 100:+.1f}pp "
              f"| expectancy breakout vs neutral={c['expectancy_breakout_vs_neutral']:+.2f}$")

    rets = [w["validation"]["net_return"] for w in windows]
    print("\nDistribución de resultados de validación (breakout):")
    print(f"  net returns %: " + ", ".join(f"{r * 100:+.1f}" for r in rets))
    print(f"  mean={statistics.mean(rets) * 100:+.2f}%  median={statistics.median(rets) * 100:+.2f}%  "
          f"min={min(rets) * 100:+.2f}%  max={max(rets) * 100:+.2f}%  "
          f"std={statistics.pstdev(rets) * 100:+.2f}pp")
    print(f"  positivas: {sum(1 for r in rets if r > 0)}/{len(rets)}")

    dec = _classify_decision(windows)
    print(f"\nDECISIÓN H004 — estado {dec['state']}")
    print(f"  {dec['conclusion']}")

    params_hash = make_config_hash(cfg["strategy"]["params"])
    out = {
        "hypothesis": "H004: el edge de timing LONG (H003-A) es estable temporalmente"
                      " y resistente a cambios de régimen, sin optimización.",
        "method": "Fixed-parameter walk-forward diagnostic (sin recalibración, sin"
                  " selección, sin tuning por ventana).",
        "lineage": {
            "hypothesis_id": "H004",
            "parent_hypothesis": "H003",
            "strategy_id": cfg["strategy"]["strategy_id"],
            "params": cfg["strategy"]["params"],
            "strategy_config_hash": params_hash,
            "parameters_changed": False,
            "optimization_performed": False,
            "previous_oos_consumed": reg.previous_oos_consumed,
            "final_oos_consumed": reg.final_oos_consumed,
            "experiment_id": experiment_id_for(cfg, CSV_PATH),
        },
        "data_roles": {
            "development_range": [DEV_START, DEV_END],
            "observed_range": [DEV_END, OBS_END],
            "note": "Ningún FINAL_OOS limpio disponible: 2024-10->2026-08 es OBSERVED."
                    " Se exige datos nuevos antes de declarar FINAL_OOS.",
        },
        "annualization": "retorno simple por barra (1h), sqrt(8766)/año.",
        "windows": windows,
        "distribution": {
            "validation_net_returns": [r for r in rets],
            "mean": statistics.mean(rets),
            "median": statistics.median(rets),
            "std": statistics.pstdev(rets),
            "positive": sum(1 for r in rets if r > 0),
            "count": len(rets),
        },
        "decision": dec,
    }
    dest = EXPERIMENTS / "outputs" / "h004_walk_forward.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())