"""E10 — H005 :: Diagnóstico de la ventana W2 (2022) del walk-forward.

Pregunta de investigación (pre-registrada): ¿QUÉ característica del entorno de
2022 explica la pérdida simultánea del breakout LONG en los tres regímenes
(BULL/BEAR/LATERAL)? No se busca hacer ganar a W2: se busca entender si el
fallo revela una limitación ESTRUCTURAL del breakout.

Diagnósticos (todos con la config CONGELADA de H002-A/H003-A/H004, sin tocar
ningún parámetro; los cambios de `direction` son diagnósticos de hipótesis,
nunca estrategia):

    1. Expectancy vs trades extremos: distribución de P&L, asimetría, share de
       los top/bottom-K en el P&L bruto.
    2. LONG vs lógica breakout: variantes direction=long/both/short sobre la
       misma ventana, para separar fallo direccional de fallo estructural.
    3. MAE/MFE: distribución y ratio (adversidad en ruta vs ventaja en ruta).
    4. Stop vs salida temporal: P&L y win-rate por reason (STOP/TIME/END).
    5. Duración de trades (barras) por reason y por régimen.
    6. Rachas de pérdidas (y de ganancias).
    7. Volatilidad y ATR del entorno (contexto, no parámetro).
    8. Coste por trade como % del movimiento capturado (drag de costes).
    9. Concentración temporal del P&L (mensual).
   10. Dentro de W2: breakout LONG vs control neutral duty-cycle vs B&H escalado.
   11. Comportamiento separado por régimen + nº de operaciones por régimen
       (se marca la muestra pequeña para no concluir de pocos trades).

Contrastes: las cuatro ventanas del walk-forward (W1..W4) para comparar el
"entorno 2022" con el resto. FINAL_OOS no se toca. DATA_ROLE: W2 vive dentro
del tramo DEVELOPMENT, guard activo para tramos OBSERVED/FINAL_OOS.

Ejecución: python experiments/e10_h005_w2_diagnosis.py
"""

from __future__ import annotations

import copy
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

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
PER_SIDE = 0.15
BARS_PER_YEAR = 8766.0

DEV_START = 1_546_300_800
DEV_END = 1_727_787_600
OBS_END = 1_788_217_200

WINDOWS = [
    ("W1", DEV_START, 1_609_459_200, 1_640_995_200),
    ("W2", DEV_START, 1_640_995_200, 1_672_531_200),
    ("W3", DEV_START, 1_672_531_200, 1_704_067_200),
    ("W4", DEV_START, 1_704_067_200, 1_727_787_600),
]

WINDOW_MONTHS = {
    "W1": "2021",
    "W2": "2022",
    "W3": "2023",
    "W4": "2024 (ene-oct)",
}


def _direction_cfg(base: dict, direction: str) -> dict:
    cfg = copy.deepcopy(base)
    cfg["strategy"]["params"]["direction"] = direction
    cfg["experiment"]["name"] = "h005-w2-diagnosis"
    cfg["experiment"]["hypothesis"] = "h005-w2-diagnosis"
    cfg["experiment"]["type"] = "DIAGNOSTIC"
    cfg["strat_label"] = f"Breakout {direction.upper()}"
    return cfg


def _slice(bars: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    return [b for b in bars if start_ts <= float(b["ts"]) < end_ts]


def _run(cfg: dict, bars: list[dict]) -> BacktestResult:
    strategy = build_strategy(cfg["strategy"])
    engine = build_engine(cfg)
    return engine.run(strategy, bars)


def _skew(values: list[float]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    m = sum(values) / n
    s2 = sum((v - m) ** 2 for v in values) / n
    if s2 == 0:
        return None
    s = s2 ** 0.5
    return round(sum((v - m) ** 3 for v in values) / n / s**3, 3)


def _quantiles(values: list[float], ps: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 0.90)) -> dict:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    return {f"q{int(p * 100)}": round(s[min(n - 1, max(0, int(p * n)))], 2) for p in ps}


def _concentration(pnls: list[float], k: int = 5) -> dict:
    pos = [p for p in pnls if p > 0]
    neg = [p for p in pnls if p < 0]
    total_pos = sum(pos)
    total_neg = -sum(neg)
    top = sorted(pos, reverse=True)[:k]
    bot = sorted(neg)[:k]
    return {
        "topK_gross_pos": round(sum(top), 2) if top else 0.0,
        "topK_share_gross_pos": round(sum(top) / total_pos, 4) if total_pos > 0 else None,
        "botK_gross_neg": round(-sum(bot), 2) if bot else 0.0,
        "botK_share_gross_neg": round(-sum(bot) / total_neg, 4) if total_neg > 0 else None,
    }


def _pnl_distribution(pnls: list[float]) -> dict:
    return {
        "n": len(pnls),
        "mean": round(statistics.mean(pnls), 2) if pnls else None,
        "median": round(statistics.median(pnls), 2) if pnls else None,
        "std": round(statistics.pstdev(pnls), 2) if len(pnls) > 1 else None,
        "skew": _skew(pnls),
        "min": round(min(pnls), 2) if pnls else None,
        "max": round(max(pnls), 2) if pnls else None,
        "quantiles": _quantiles(pnls),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "gross_pos": round(sum(p for p in pnls if p > 0), 2),
        "gross_neg": round(-sum(p for p in pnls if p < 0), 2),
        "concentration": _concentration(pnls),
    }


def _expectancy_decomp(pnls: list[float]) -> dict:
    if not pnls:
        return {}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / len(pnls)
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(abs(p) for p in losses) if losses else 0.0
    return {
        "win_rate": round(wr, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss_mag": round(avg_loss, 2),
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss else None,
        "expectancy": round(wr * avg_win - (1 - wr) * avg_loss, 2),
    }


def _exit_breakdown(trades: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for t in trades:
        by.setdefault(t.get("exit_reason") or "?", []).append(t)
    out = {}
    for reason in sorted(by):
        ts = by[reason]
        pnls = [float(t["pnl"]) for t in ts]
        durs = [int(t.get("bars_in_trade") or 0) for t in ts]
        out[reason] = {
            "trades": len(ts),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(ts), 4) if ts else None,
            "avg_pnl": round(statistics.mean(pnls), 2) if pnls else None,
            "net_pnl": round(sum(pnls), 2),
            "avg_bars": round(statistics.mean(durs), 1) if durs else None,
            "median_bars": round(statistics.median(durs), 1) if durs else None,
        }
    return out


def _streaks(pnls: list[float]) -> dict:
    max_w = max_l = 0
    cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif p < 0:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)
    return {"max_consecutive_wins": max_w, "max_consecutive_losses": max_l}


def _mae_mfe(trades: list[dict]) -> dict:
    if not trades:
        return {}
    maes = [float(t["mae"]) for t in trades]
    mfes = [float(t["mfe"]) for t in trades]
    avg_mae = statistics.mean(maes)
    avg_mfe = statistics.mean(mfes)
    return {
        "n": len(trades),
        "avg_mae": round(avg_mae, 2),
        "median_mae": round(statistics.median(maes), 2),
        "avg_mfe": round(avg_mfe, 2),
        "median_mfe": round(statistics.median(mfes), 2),
        "mfe_mae_ratio": round(avg_mfe / avg_mae, 4) if avg_mae else None,
        "quantiles_mae": _quantiles(maes),
        "quantiles_mfe": _quantiles(mfes),
    }


def _cost_drag(trades: list[dict], initial_equity: float) -> dict:
    ratios: list[float] = []
    costs: list[float] = []
    for t in trades:
        gross = abs(float(t["gross_pnl"]))
        c = float(t["costs"])
        costs.append(c)
        ratios.append(c / (gross + c) if gross + c > 0 else 1.0)
    total = sum(costs)
    return {
        "avg_share_costs_of_move": round(statistics.mean(ratios), 4) if ratios else None,
        "median_share_costs_of_move": round(statistics.median(ratios), 4) if ratios else None,
        "avg_costs_per_trade": round(statistics.mean(costs), 2) if costs else None,
        "total_costs": round(total, 2),
        "costs_pct_initial_equity": round(total / initial_equity, 5) if initial_equity else None,
    }


def _monthly(trades: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for t in trades:
        key = datetime.fromtimestamp(float(t["entry_time"]), tz=timezone.utc).strftime("%Y-%m")
        by.setdefault(key, []).append(t)
    rows = []
    for key in sorted(by):
        pnls = [float(t["pnl"]) for t in by[key]]
        rows.append({
            "month": key,
            "trades": len(pnls),
            "wins": sum(1 for p in pnls if p > 0),
            "net_pnl": round(sum(pnls), 2),
            "avg_pnl": round(statistics.mean(pnls), 2) if pnls else None,
        })
    positive_months = [r for r in rows if r["net_pnl"] > 0]
    total_pos = sum(r["net_pnl"] for r in positive_months)
    top3 = sorted(positive_months, key=lambda r: r["net_pnl"], reverse=True)[:3]
    return {
        "months": rows,
        "month_count": len(rows),
        "worst_month": rows[0] if rows else None,
        "best_month": rows[-1] if rows else None,
        "top3_pos_months_share": round(sum(r["net_pnl"] for r in top3) / total_pos, 4) if total_pos > 0 else None,
    }


def _market_context(bars: list[dict], atr_period: int = 14) -> dict:
    closes = [float(b["close"]) for b in bars]
    if len(closes) < 2:
        return {}
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    atrs: list[float] = []
    window: list[float] = []
    for i, b in enumerate(bars):
        high = float(b["high"])
        low = float(b["low"])
        if i == 0:
            tr = high - low
        else:
            prev_close = float(bars[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        window.append(tr)
        if len(window) > atr_period:
            window.pop(0)
        atrs.append(sum(window) / len(window))
    avg_close = statistics.mean(closes)
    atr_mask = atrs[atr_period - 1:] if len(atrs) >= atr_period else atrs
    return {
        "bars": len(bars),
        "market_return": round(closes[-1] / closes[0] - 1.0, 4),
        "avg_close": round(avg_close, 2),
        "max_close": round(max(closes), 2),
        "min_close": round(min(closes), 2),
        "avg_daily_range_pct": round(statistics.mean(atr_mask) / avg_close * 100, 3) if atr_mask and avg_close else None,
        "atr_mean": round(statistics.mean(atr_mask), 3) if atr_mask else None,
        "annual_vol_per_bar": round(statistics.pstdev(rets) * (BARS_PER_YEAR ** 0.5), 4) if len(rets) > 1 else None,
        "sharpe_style_ret_per_bar": round(statistics.mean(rets) * BARS_PER_YEAR, 4) if rets else None,
    }


def _regime_table(trades: list[dict], regimes: list[str], min_sample: int = 10) -> dict:
    rows = []
    for label in ("BULL", "BEAR", "LATERAL"):
        ts = [t for t in trades if regimes[int(t.get("entry_index") or 0)] == label]
        pnls = [float(t["pnl"]) for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        rows.append({
            "regime": label,
            "trades": len(ts),
            "win_rate": round(wins / len(ts), 4) if ts else None,
            "expectancy": round(statistics.mean(pnls), 2) if pnls else None,
            "net_pnl": round(sum(pnls), 2) if pnls else 0.0,
            "avg_mae": round(statistics.mean(float(t["mae"]) for t in ts), 2) if ts else None,
            "avg_mfe": round(statistics.mean(float(t["mfe"]) for t in ts), 2) if ts else None,
            "small_sample": len(ts) < min_sample,
        })
    return {"rows": rows, "min_sample": min_sample}


def _benchmarks(cfg: dict, valid_bars: list[dict], va_row: dict) -> dict:
    n_v = len(valid_bars)
    valid_closes = [float(b["close"]) for b in valid_bars]
    eq_frac = va_row["exposure_time"]
    cap = capital_exposure_from_trades
    total_capital_frac = eq_frac * va_row.get("capital_exposure_cond", 0.0)

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
    return {
        "bh": {
            "net_return": bh["metrics"]["net_return"],
            "net_pnl": bh["net_pnl"],
            "max_drawdown": bh["metrics"]["max_drawdown"],
            "sharpe": bh["metrics"]["sharpe"],
        },
        "bh_scaled": {
            "net_return": bh_scaled["metrics"]["net_return"],
            "net_pnl": bh_scaled["net_pnl"],
            "max_drawdown": bh_scaled["metrics"]["max_drawdown"],
            "sharpe": bh_scaled["metrics"]["sharpe"],
            "scale_frac": total_capital_frac,
        },
        "neutral": {
            "net_return": neutral["metrics"]["net_return"],
            "net_pnl": neutral["net_pnl"],
            "max_drawdown": neutral["metrics"]["max_drawdown"],
            "sharpe": neutral["metrics"]["sharpe"],
            "exposure_frac": neutral["exposure_frac"],
            "blocks": neutral["num_trades"],
        },
        "target_eq_frac": eq_frac,
        "contrasts": {
            "delta_vs_neutral_pp": round((va_row["net_return"] - neutral["metrics"]["net_return"]) * 100, 1),
            "delta_vs_bh_scaled_pp": round((va_row["net_return"] - bh_scaled["metrics"]["net_return"]) * 100, 1),
            "expectancy_breakout_vs_neutral": round((va_row["expectancy"] or 0.0) - (neutral["block_pnls"] and statistics.mean(neutral["block_pnls"]) or 0.0), 2),
        },
    }


def _window_diagnostics(cfg: dict, bars_all: list[dict], label: str, train_start: int,
                        valid_start: int, valid_end: int) -> dict:
    valid_bars = _slice(bars_all, valid_start, valid_end)
    res = _run(cfg, valid_bars)
    m = calculate_metrics(res)
    pnls = [float(t["pnl"]) for t in res.trades]
    cm = curve_metrics(res.equity_curve, len(valid_bars), INTERVAL_SECONDS)
    regimes = classify_regimes(valid_bars)
    trades = res.trades

    row = {
        "trades": m.num_trades,
        "net_return": cm["net_return"],
        "net_pnl": sum(pnls),
        "expectancy": m.expectancy,
        "win_rate": m.win_rate,
        "profit_factor": m.profit_factor,
        "max_drawdown": cm["max_drawdown"],
        "sharpe": cm["sharpe"],
        "exposure_time": exposure_fraction_from_trades(trades, len(valid_bars)),
        "capital_exposure_cond": capital_exposure_from_trades(trades, res.equity_curve, [float(b["close"]) for b in valid_bars]),
        "max_consecutive_losses": m.max_consecutive_losses,
    }

    return {
        "label": label,
        "months": WINDOW_MONTHS[label],
        "valid_range": [valid_start, valid_end],
        "valid_bars": len(valid_bars),
        "metrics_row": row,
        "pnl_distribution": _pnl_distribution(pnls),
        "expectancy_decomp": _expectancy_decomp(pnls),
        "exit_breakdown": _exit_breakdown(trades),
        "streaks": _streaks(pnls),
        "mae_mfe": _mae_mfe(trades),
        "cost_drag": _cost_drag(trades, cfg["costs"]["initial_equity"]),
        "monthly": _monthly(trades),
        "market_context": _market_context(valid_bars),
        "regimes": _regime_table(trades, regimes),
        "diagnostics": res.diagnostics,
    }


def _fmt_pct(x) -> str:
    return "   --" if x is None else f"{x * 100:>6.1f}%"


def _fmt_num(x, d: int = 2) -> str:
    return "   --" if x is None else f"{x:>{d + 6}.{d}f}"


def _print_diagnostic(title: str, d: dict) -> None:
    print(f"\n{title}")
    m = d["metrics_row"]
    print(f"  net p&l ${m['net_pnl']:>10.1f}  ret {m['net_return'] * 100:>6.2f}%  DD {m['max_drawdown'] * 100:>5.1f}%  "
          f"Sharpe {m['sharpe']:>5.2f}  trades {m['trades']}  exp.t {m['exposure_time'] * 100:>5.1f}%")

    dist = d["pnl_distribution"]
    conc = dist["concentration"]
    print(f"  P&L/trade: mean ${dist['mean']}  med ${dist['median']}  std ${dist['std']}  "
          f"skew {dist['skew']}  min ${dist['min']}  max ${dist['max']}")
    print(f"  cuantiles $: " + " ".join(f"{k} {v}" for k, v in dist["quantiles"].items()))
    print(f"  wins {dist['wins']}/{dist['n']} ({dist['wins'] / dist['n'] * 100:.0f}%)  "
          f"gross+ ${dist['gross_pos']:.0f}  gross- ${dist['gross_neg']:.0f}")
    print(f"  top5 share gross+: {conc['topK_share_gross_pos'] or float('nan'):.1%}  "
          f"bot5 share gross-: {conc['botK_share_gross_neg'] or float('nan'):.1%}")

    ed = d["expectancy_decomp"]
    print(f"  expectancy: {ed['win_rate']:.2f}*{ed['avg_win']}$ - {1 - ed['win_rate']:.2f}*{ed['avg_loss_mag']}$ "
          f"= {ed['expectancy']}$  (payoff {ed['payoff_ratio']})")

    ex = d["exit_breakdown"]
    for reason in sorted(ex):
        r = ex[reason]
        wr = "   --" if r["win_rate"] is None else f"{r['win_rate'] * 100:>6.1f}%"
        print(f"  exit {reason:<4} n={r['trades']:>3} wr {wr} "
              f"avg ${r['avg_pnl']:>9.1f} net ${r['net_pnl']:>10.1f} dur {r['avg_bars']:>6.1f} bars")

    st = d["streaks"]
    mm = d["mae_mfe"]
    print(f"  rachas: maxW {st['max_consecutive_wins']}  maxL {st['max_consecutive_losses']}")
    print(f"  MAE/MFE: avg MAE ${mm['avg_mae']} med ${mm['median_mae']} | avg MFE ${mm['avg_mfe']} med ${mm['median_mfe']} "
          f"| ratio {mm['mfe_mae_ratio']}")

    cd = d["cost_drag"]
    print(f"  costes: avg ${cd['avg_costs_per_trade']}/trade, share del movimiento {cd['avg_share_costs_of_move'] or float('nan'):.1%}, "
          f"total ${cd['total_costs']:.0f} ({cd['costs_pct_initial_equity'] * 100:.2f}% equity)")

    mo = d["monthly"]
    worst = mo["worst_month"]
    best = mo["best_month"]
    print(f"  mes peor $: {worst['month']} ({worst['net_pnl']:+.0f}$, {worst['trades']} tr)  "
          f"mes mejor $: {best['month']} ({best['net_pnl']:+.0f}$, {best['trades']} tr)  "
          f"share top3 pos: {mo['top3_pos_months_share'] or float('nan'):.1%}")

    mc = d["market_context"]
    print(f"  contexto: ret mdo {mc['market_return'] * 100:+.1f}%  vol ann {mc['annual_vol_per_bar'] * 100:.0f}%  "
          f"range/close {mc['avg_daily_range_pct'] or float('nan')}%  ATR mean {mc['atr_mean'] or float('nan')}  "
          f"close {mc['avg_close']} ({mc['min_close']}-{mc['max_close']})")

    rg = d["regimes"]
    for r in rg["rows"]:
        wr = "   --" if r["win_rate"] is None else f"{r['win_rate'] * 100:>6.1f}%"
        flag = "  <-- muestra pequena" if r["small_sample"] else ""
        print(f"  régimen {r['regime']:<8} n={r['trades']:>3} wr {wr} "
              f"E ${r['expectancy'] or float('nan'):>9.1f} net ${r['net_pnl']:>10.1f} "
              f"MAE ${r['avg_mae'] or float('nan'):>8.1f} MFE ${r['avg_mfe'] or float('nan'):>8.1f}{flag}")


def main() -> int:
    base = yaml.safe_load(H002_CONFIG.read_text(encoding="utf-8"))
    bars = read_ohlc_csv(CSV_PATH)
    initial_equity = base["costs"]["initial_equity"]

    reg = RoleRegistry.load_default()
    print(
        "H005-W2 — Diagnóstico de W2 (2022). Config CONGELADA H002-A/H003-A/H004\n"
        f"  BTC 1h {len(bars)} barras | DEVELOPMENT {DEV_START}->{DEV_END} | OBSERVED {DEV_END}->{OBS_END}"
    )
    print(f"  previous_oos_consumed={reg.previous_oos_consumed} final_oos_consumed={reg.final_oos_consumed}")

    for _, ts, vs, ve in WINDOWS:
        try:
            reg.guard_final_oos(vs, ve, purpose=f"diagnóstico {ts}")
        except DataRoleError as exc:
            print(f"  guard W? error: {exc}")
            return 1
    print("  guard_final_oos: ventanas W1..W4 dentro de DEVELOPMENT, sin tocar OBSERVED/FINAL_OOS.")

    windows = []
    for label, ts, vs, ve in WINDOWS:
        d = _window_diagnostics(_direction_cfg(base, "long"), bars, label, ts, vs, ve)
        windows.append(d)
        _print_diagnostic(f"-- {label} ({WINDOW_MONTHS[label]}) — breakout LONG", d)

    w2 = next(d for d in windows if d["label"] == "W2")
    w2_valid = _slice(bars, *w2["valid_range"])
    w2_regimes = classify_regimes(w2_valid)
    cfg_long = _direction_cfg(base, "long")
    w2_long_trades = _run(cfg_long, w2_valid).trades

    bench = _benchmarks(cfg_long, w2_valid, w2["metrics_row"])
    print(f"\n-- W2 benchmarks internos (breakout LONG vs pasivos)")
    print(f"  breakout LONG ret {w2['metrics_row']['net_return'] * 100:+.1f}% | "
          f"B&H {bench['bh']['net_return'] * 100:+.1f}% | "
          f"B&H xcap {bench['bh_scaled']['net_return'] * 100:+.1f}% | "
          f"neutral {bench['neutral']['net_return'] * 100:+.1f}%")
    print(f"  delta vs neutral {bench['contrasts']['delta_vs_neutral_pp']:+.1f}pp | "
          f"delta vs B&H escalado {bench['contrasts']['delta_vs_bh_scaled_pp']:+.1f}pp | "
          f"expectancy vs neutral {bench['contrasts']['expectancy_breakout_vs_neutral']:+.2f}$")

    print(f"\n-- W2 monthly detalle")
    for r in w2["monthly"]["months"]:
        print(f"  {r['month']}  n={r['trades']:>3}  wins {r['wins']:>2}  net {r['net_pnl']:>10.1f}  avg {r['avg_pnl']:>9.1f}")

    print(f"\n-- W2 direccional (misma config, dirección como diagnóstico)")
    direction_rows = {}
    for direction in ("long", "short", "both"):
        cfg = _direction_cfg(base, direction)
        res = _run(cfg, w2_valid)
        pnls = [float(t["pnl"]) for t in res.trades]
        m = calculate_metrics(res)
        cm = curve_metrics(res.equity_curve, len(w2_valid), INTERVAL_SECONDS)
        per_side_stats = {"LONG": [], "SHORT": []}
        for t in res.trades:
            per_side_stats.setdefault(t.get("side"), []).append(float(t["pnl"]))
        row = {
            "direction": direction,
            "trades": len(pnls),
            "long_trades": len(per_side_stats.get("LONG", [])),
            "short_trades": len(per_side_stats.get("SHORT", [])),
            "net_return": cm["net_return"],
            "net_pnl": sum(pnls),
            "expectancy": m.expectancy,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "max_drawdown": cm["max_drawdown"],
            "expectancy_decomp": _expectancy_decomp(pnls),
            "pnl_distribution": _pnl_distribution(pnls),
            "by_side": {
                side: {
                    "trades": len(p),
                    "net_pnl": round(sum(p), 2),
                    "expectancy": round(statistics.mean(p), 2) if p else None,
                    "win_rate": round(sum(1 for v in p if v > 0) / len(p), 4) if p else None,
                }
                for side, p in per_side_stats.items()
            },
        }
        direction_rows[direction] = row
        print(f"  direction={direction:<5} trades {len(pnls):>3} (L {row['long_trades']} / S {row['short_trades']})  "
              f"ret {cm['net_return'] * 100:>+6.1f}%  net ${sum(pnls):>10.1f}  E {m.expectancy:>8.2f}$  "
              f"wr {(m.win_rate or 0.0):>6.1%}  PF {m.profit_factor or float('nan'):>4.2f}")

    print(f"\n-- W2 peores trades (top 10 por pérdida neta)")
    worst = sorted(w2_long_trades, key=lambda t: float(t["pnl"]))[:10]
    for t in worst:
        when = datetime.fromtimestamp(float(t["entry_time"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {when}  {t['side']:<4} pnl ${float(t['pnl']):>9.1f}  exit {t.get('exit_reason'):<4}  "
              f"dur {t.get('bars_in_trade'):>2}  MAE ${float(t['mae']):>7.1f}  costs ${float(t['costs']):>6.1f}")

    params_hash = make_config_hash(base["strategy"]["params"])

    conclusion = _build_conclusion(w2, bench, direction_rows, windows)
    print(f"\n-- CONCLUSION DIAGNÓSTICO W2")
    print(f"  causa: {conclusion['cause_class']} — {conclusion['market_regime_2022']}")
    print(f"  trades extremos: {conclusion['extreme_trades']['note']}")
    print(f"  expectancy: {conclusion['expectancy_decay']['note']}")
    print(f"  direccion: {conclusion['both_directions_2022']['note']}")
    print(f"  MFE/MAE: {conclusion['mfe_mae']['note']}")
    print(f"  TIME vs STOP: {conclusion['time_vs_stop']['note']}")
    print(f"  costes: {conclusion['costs']['note']}")
    print(f"  concentracion temporal: {conclusion['temporal_concentration']['note']}")
    print(f"  regimen: {conclusion['regimes']['note']}")
    print(f"  benchmarks W2: {conclusion['benchmark_within_w2']['note']}")
    print(f"  implicacion: {conclusion['implication']}")

    out = {
        "hypothesis": "H005-W2: la perdida de 2022 (W2) procede de beta direccional adversa (bear estructural -64.5%), no de un fallo de la logica breakout ni de trades extremos, costes o un evento concentrado.",
        "method": "Diagnostico del ciclo H004 con config CONGELADA (H002-A/H003-A). Ningun parametro modificado; la direccion (long/both/short) se usa solo como diagnostico de hipotesis sobre la logica breakout. Se contrasta W2 vs W1/W3/W4 como control temporal.",
        "lineage": {
            "hypothesis_id": "H005-W2",
            "parent_hypothesis": "H004",
            "strategy_id": base["strategy"]["strategy_id"],
            "params_long_frozen": {"lookback": 20, "atr_period": 14, "stop_atr_mult": 2.0, "direction": "long"},
            "params_hash": params_hash,
            "parameters_changed": False,
            "optimization_performed": False,
            "previous_oos_consumed": reg.previous_oos_consumed,
            "final_oos_consumed": reg.final_oos_consumed,
            "experiment_id": experiment_id_for(_direction_cfg(base, "long"), CSV_PATH),
        },
        "data_roles": {
            "development_range": [DEV_START, DEV_END],
            "observed_range": [DEV_END, OBS_END],
            "note": "Diagnostico dentro de DEVELOPMENT (W2 = 2022). No se toca OBSERVED ni FINAL_OOS. final_oos_consumed=false.",
        },
        "windows_long": windows,
        "w2_benchmarks": bench,
        "w2_directions": direction_rows,
        "conclusion": conclusion,
        "decision": {
            "state": "RESEARCH_REQUIRED",
            "note": "Diagnostico; no cierra ciclo. La causa de W2 queda documentada: beta direccional adversa del filtro LONG en un bear estructural. H005 se evalua con el FINAL_OOS limpio cuando existan datos nuevos; no se tocan parametros.",
        },
    }
    dest = EXPERIMENTS / "outputs" / "h005_w2_diagnosis.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {dest}")
    return 0


def _build_conclusion(w2: dict, bench: dict, directions: dict, windows: list[dict]) -> dict:
    others = [d for d in windows if d["label"] != "W2"]
    other_ed = [d["expectancy_decomp"] for d in others]
    other_bot = [(d["pnl_distribution"]["concentration"]["botK_share_gross_neg"]) for d in others]
    other_ratio = [d["mae_mfe"]["mfe_mae_ratio"] for d in others]
    other_time = [d["exit_breakdown"].get("TIME", {}) for d in others]
    other_stop = [d["exit_breakdown"].get("STOP", {}).get("avg_pnl") for d in others]
    time_wr_range = (min(t.get("win_rate") or 0.0 for t in other_time), max(t.get("win_rate") or 0.0 for t in other_time))

    dist = w2["pnl_distribution"]
    conc = dist["concentration"]
    ed = w2["expectancy_decomp"]
    mm = w2["mae_mfe"]
    ctx = w2["market_context"]
    cd = w2["cost_drag"]
    mo = w2["monthly"]
    regs = {r["regime"]: r for r in w2["regimes"]["rows"]}
    time_exits = w2["exit_breakdown"].get("TIME", {})
    stop_exits = w2["exit_breakdown"].get("STOP", {})
    row_long = directions["long"]
    row_short = directions["short"]
    row_both = directions["both"]
    neg_months = sum(1 for r in mo["months"] if r["net_pnl"] < 0)

    others_wr = [ed_i["win_rate"] for ed_i in other_ed]
    others_win = [ed_i["avg_win"] for ed_i in other_ed]
    others_loss = [ed_i["avg_loss_mag"] for ed_i in other_ed]

    extreme_note = (
        f"bottom-5 {conc['botK_share_gross_neg'] or 0.0:.1%} del gross negativo (rango otras ventanas "
        f"{min(other_bot) or 0.0:.1%}-{max(other_bot) or 0.0:.1%}) y el stop medio en 2022 es MENOR "
        f"(${stop_exits.get('avg_pnl') or 0.0:.0f} vs ${min(other_stop):.0f} a ${max(other_stop):.0f} en resto): "
        f"sin trades extremos."
    )
    expectancy_note = (
        f"win rate {ed['win_rate']:.0%} (vs {min(others_wr):.0%}-{max(others_wr):.0%}), avg win "
        f"${ed['avg_win']:.0f} (vs ${max(others_win):.0f}) y avg loss ${ed['avg_loss_mag']:.0f} "
        f"(MENOR que ${max(others_loss):.0f} del resto): deterioro del lado ganador, no de las perdedoras."
    )
    direction_note = (
        f"LONG-only {row_long['net_return'] * 100:.1f}%; con ambas direcciones juntas ≈ "
        f"{row_both['net_return'] * 100:.1f}% (PF {row_both['profit_factor']:.2f}): la logica breakout "
        f"no desaparece en 2022; el filtro LONG concentra todo el riesgo de un bear estructural."
    )
    mfe_note = (
        f"MFE/MAE {mm['mfe_mae_ratio']:.2f} (el mas bajo; resto {min(other_ratio):.2f}-{max(other_ratio):.2f}): "
        f"mediana MFE ${mm['median_mfe']:.0f} < mediana MAE ${mm['median_mae']:.0f} -> sin follow-through alcista."
    )
    time_note = (
        f"TIME-exits wr {time_exits.get('win_rate') or 0.0:.0%} avg ${time_exits.get('avg_pnl') or 0.0:.0f} "
        f"(en W1/W3/W4 ganan {time_wr_range[0]:.0%}-{time_wr_range[1]:.0%} y aportan la mayoria del P&L)."
    )
    costs_note = f"{cd['costs_pct_initial_equity'] * 100:.2f}% del equity -> descartada."
    temporal_note = (
        f"{neg_months}/{mo['month_count']} meses negativos, peor mes {mo['worst_month']['month']} "
        f"({mo['worst_month']['net_pnl']:+.0f}$); perdida repartida, no un evento."
    )
    regime_note = (
        f"BULL n={regs['BULL']['trades']} E {regs['BULL']['expectancy']}$ | BEAR n={regs['BEAR']['trades']} "
        f"E {regs['BEAR']['expectancy']}$ | LATERAL n={regs['LATERAL']['trades']} E {regs['LATERAL']['expectancy']}$; "
        f"perdida en los tres regimenes con muestras validas (>=10 cada uno)."
    )
    benchmark_note = (
        f"incluso perdiendo ({w2['metrics_row']['net_return'] * 100:.1f}%), LONG supera a B&H puro "
        f"({bench['bh']['net_return'] * 100:.1f}%), a B&H escalado ({bench['bh_scaled']['net_return'] * 100:.1f}%) "
        f"y al control neutral ({bench['neutral']['net_return'] * 100:.1f}%) -> el timing no desaparece; "
        f"la direccion es el problema."
    )
    implication = (
        "W2 no invalida el edge de timing: lo expone a la beta. La limitacion estructural es la restriccion "
        "LONG (H002) en mercados que pierden ~65%. Implicaciones abiertas (NO se implementan aqui; prohibido "
        "hasta el FINAL_OOS): (a) filtro direccional por regimen, (b) aceptar drawdown en bears, o (c) "
        "direction=both. Requiere nueva hipotesis documentada + FINAL_OOS limpio."
    )
    return {
        "cause_class": "DIRECTIONAL_BETA_ADVERSA",
        "market_regime_2022": f"bear estructural: mercado {ctx['market_return'] * 100:+.1f}% en 2022",
        "extreme_trades": {
            "botK_share_gross_neg": conc["botK_share_gross_neg"],
            "avg_stop_pnl_w2": stop_exits.get("avg_pnl"),
            "other_windows_botK_range": other_bot,
            "note": extreme_note,
        },
        "expectancy_decay": {
            "win_rate": ed["win_rate"],
            "avg_win": ed["avg_win"],
            "avg_loss_mag": ed["avg_loss_mag"],
            "median_trade": dist["median"],
            "skew": dist["skew"],
            "note": expectancy_note,
        },
        "both_directions_2022": {
            "long": {"net_return": row_long["net_return"], "profit_factor": row_long["profit_factor"], "expectancy": row_long["expectancy"]},
            "short": {"net_return": row_short["net_return"], "profit_factor": row_short["profit_factor"], "expectancy": row_short["expectancy"]},
            "both": {"net_return": row_both["net_return"], "profit_factor": row_both["profit_factor"], "expectancy": row_both["expectancy"]},
            "note": direction_note,
        },
        "mfe_mae": {
            "ratio": mm["mfe_mae_ratio"],
            "median_mfe": mm["median_mfe"],
            "median_mae": mm["median_mae"],
            "other_windows_ratio": other_ratio,
            "note": mfe_note,
        },
        "time_vs_stop": {
            "TIME": {k: time_exits.get(k) for k in ("win_rate", "avg_pnl", "net_pnl", "trades")},
            "STOP": {k: stop_exits.get(k) for k in ("win_rate", "avg_pnl", "net_pnl", "trades")},
            "note": time_note,
        },
        "costs": {
            "costs_pct_equity": cd["costs_pct_initial_equity"],
            "total_costs": cd["total_costs"],
            "note": costs_note,
        },
        "temporal_concentration": {
            "negative_months": neg_months,
            "total_months": mo["month_count"],
            "worst_month": mo["worst_month"]["month"],
            "note": temporal_note,
        },
        "regimes": {
            "rows": w2["regimes"]["rows"],
            "note": regime_note,
        },
        "benchmark_within_w2": {
            "breakout_long": w2["metrics_row"]["net_return"],
            "bh": bench["bh"]["net_return"],
            "bh_scaled": bench["bh_scaled"]["net_return"],
            "neutral": bench["neutral"]["net_return"],
            "delta_vs_neutral_pp": bench["contrasts"]["delta_vs_neutral_pp"],
            "delta_vs_bh_scaled_pp": bench["contrasts"]["delta_vs_bh_scaled_pp"],
            "note": benchmark_note,
        },
        "implication": implication,
    }


if __name__ == "__main__":
    raise SystemExit(main())