"""E7 — H002 :: Long/Short Asymmetry (BTC 1h, 2019-2026).

Pregunta: ¿existe una diferencia persistente y explicable entre las
distribuciones LONG y SHORT del breakout en BTC 1h?

Método (evita el sesgo de selección):
    * Hipótesis definida antes de mirar los datos.
    * Split temporal estricto: train = 75% inicial, test = 25% final untouched.
    * Variantes: H002-A (long-only), H002-B (short-only), H002-C (original).
    * No se selecciona la variante con mayor rentabilidad: solo se reporta
      si el contraste LONG vs SHORT persiste en OOS. Cualquier decisión
      direccional se tomará en un paso posterior sobre el test, nunca en train.

Métricas (reporting/metrics): win_rate, expectancy_e (E = P(w)·AvgW − P(l)·AvgL),
payoff_ratio, profit_factor, MAE/MFE + ratio, rachas, gross/net y drawdown.
"Predictive signal != Profitable strategy": se reporta gross (señal bruta)
separado de costes.

Ejecución: python experiments/e7_h002_asymmetry.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtesting.engine import BacktestResult
from data.loader import read_ohlc_csv
from reporting.metrics import calculate_metrics
from research.pipeline import build_engine, build_strategy, experiment_id_for, run_experiment

EXPERIMENTS = Path(__file__).resolve().parent
H002_CONFIG = EXPERIMENTS / "h002" / "config.yaml"
CSV_PATH = ROOT / "experiments" / "data" / "market_BTCUSDT_60min.csv"

TRAIN_RATIO = 0.75


def _apply(cfg: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(cfg)
    for section, values in overrides.items():
        for key, value in values.items():
            merged[section][key] = value
    return merged


def _direction_cfg(base: dict, direction: str) -> dict:
    cfg = copy.deepcopy(base)
    cfg["strategy"]["params"]["direction"] = direction
    return cfg


def run_slice(cfg: dict, bars: list[dict]) -> tuple[dict, dict]:
    """Ejecuta cfg sobre un slice de barras; limpio informe resumen + sides."""
    strategy = build_strategy(cfg["strategy"])
    engine = build_engine(cfg)
    result = engine.run(strategy, bars)
    metrics = calculate_metrics(result)

    long_t = [t for t in result.trades if t["side"] == "LONG"]
    short_t = [t for t in result.trades if t["side"] == "SHORT"]
    sides = {}
    for name, ts in (("LONG", long_t), ("SHORT", short_t)):
        sub = BacktestResult(
            initial_equity=result.initial_equity,
            final_equity=result.final_equity,
            trades=ts,
            equity_curve=result.equity_curve,
            diagnostics=result.diagnostics,
        )
        sides[name] = calculate_metrics(sub).to_dict()

    diag = result.diagnostics
    costs_total = metrics.costs_total or 0.0
    net_pnl = metrics.expectancy * len(result.trades) if metrics.expectancy is not None else 0.0
    row = {
        "trades": metrics.num_trades,
        "wins": sum(1 for t in result.trades if (t.get("pnl") or 0) > 0),
        "losses": sum(1 for t in result.trades if (t.get("pnl") or 0) < 0),
        "stopped": sum(1 for t in result.trades if t.get("exit_reason") == "STOP"),
        "time_exits": sum(1 for t in result.trades if t.get("exit_reason") == "TIME"),
        "win_rate": metrics.win_rate,
        "avg_net": metrics.expectancy,
        "expectancy_e": metrics.expectancy_e,
        "avg_win": metrics.avg_win,
        "avg_loss": metrics.avg_loss,
        "payoff_ratio": metrics.payoff_ratio,
        "profit_factor": metrics.profit_factor,
        "avg_mae": metrics.avg_mae,
        "avg_mfe": metrics.avg_mfe,
        "mfe_mae_ratio": metrics.mfe_mae_ratio,
        "max_consec_wins": metrics.max_consecutive_wins,
        "max_consec_losses": metrics.max_consecutive_losses,
        "gross_profit": metrics.gross_profit,
        "gross_loss": metrics.gross_loss,
        "costs": costs_total,
        "net_pnl": net_pnl,
        "net_return": result.net_return,
        "max_drawdown": metrics.max_drawdown,
        "raw_signals": diag.get("raw_signals", 0),
        "raw_long": diag.get("raw_signals_long", 0),
        "raw_short": diag.get("raw_signals_short", 0),
    }
    return row, sides


def _fmt1(x: float | None, suffix: str = "") -> str:
    return "      —" if x is None else f"{x:>8.1f}{suffix}"


def _pct(x: float | None) -> str:
    return "   —" if x is None else f"{x * 100:>6.1f}"


def _print_block(title: str, rows: list[dict], sides: dict[str, dict]) -> None:
    print(f"\n{'=' * 110}\n{title}\n{'=' * 110}")
    header = (
        f"{'Variante':<10}{'trd':>5}{'W/L':>6}{'wr%':>6}{'E$':>8}{'E_e$':>8}"
        f"{'pay':>6}{'PF':>6}{'MAE':>8}{'MFE':>8}{'r':>5}{'mcl':>4}{'mcg':>4}"
        f"{'gross$':>10}{'costs$':>9}{'net$':>9}{'net%':>7}"
    )
    print(header)
    for r in rows:
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"{r['label']:<10}{r['trades']:>5}{wl:>6}{_pct(r['win_rate']):>6}"
            f"{_fmt1(r['avg_net']):>8}{_fmt1(r['expectancy_e']):>8}{_fmt1(r['payoff_ratio']):>6}"
            f"{_fmt1(r['profit_factor']):>6}{_fmt1(r['avg_mae']):>8}{_fmt1(r['avg_mfe']):>8}"
            f"{_fmt1(r['mfe_mae_ratio']):>5}{r['max_consec_losses'] or 0:>4}"
            f"{r['max_consec_wins'] or 0:>4}"
            f"{r['gross_profit'] if r['gross_profit'] is not None else 0:>10.0f}"
            f"{r['costs']:>9.0f}{r['net_pnl']:>9.0f}{r['net_return'] * 100:>7.2f}"
        )
    if sides:
        print("  Detalle LONG / SHORT (métricas por lado):")
        for key, m in sides.items():
            wins = int(round((m.get("win_rate") or 0) * m["num_trades"]))
            losses = m["num_trades"] - wins
            print(
                f"  {key:<17} trd={m['num_trades']:>4} W/L={wins:>3}/{losses:<3}"
                f" wr={_pct(m.get('win_rate'))}"
                f" E$={_fmt1(m.get('expectancy'))} E_e$={_fmt1(m.get('expectancy_e'))}"
                f" MAE={_fmt1(m.get('avg_mae'))} MFE={_fmt1(m.get('avg_mfe'))}"
                f" mfe/mae={_fmt1(m.get('mfe_mae_ratio'))}"
                f" mcl={m.get('max_consecutive_losses')}"
            )


def main() -> int:
    base = yaml.safe_load(H002_CONFIG.read_text(encoding="utf-8"))
    bars = read_ohlc_csv(CSV_PATH)
    split_i = int(len(bars) * TRAIN_RATIO)
    train, test = bars[:split_i], bars[split_i:]
    print(
        f"H002 — BTC 1h {len(bars)} barras | train {len(train)} "
        f"({bars[0]['ts']}->{train[-1]['ts']}) | test {len(test)} untouched "
        f"({test[0]['ts']}->{test[-1]['ts']})"
    )

    variants = [
        ("H002-A", "long-only", {"long"}),
        ("H002-B", "short-only", {"short"}),
        ("H002-C", "both", {"both"}),
    ]

    block_train: list[dict] = []
    block_test: list[dict] = []
    sides_train: dict[str, dict] = {}
    sides_test: dict[str, dict] = {}

    for label, _name, dirs in variants:
        direction = next(iter(dirs))
        cfg = _direction_cfg(base, direction)
        # id determinista (config + sha del dataset completo); els slices
        # son subrangos deterministas del mismo dataset.
        cfg["experiment"]["name"] = f"h002-{direction}-{label.lower()}"
        rid = experiment_id_for(cfg, CSV_PATH)

        row_tr, side_tr = run_slice(cfg, train)
        row_te, side_te = run_slice(cfg, test)
        row_tr["label"], row_te["label"] = f"{label} (train)", f"{label} (test)"
        row_tr["experiment_id"], row_te["experiment_id"] = rid, rid
        row_tr["raw"], row_te["raw"] = (
            f"{row_tr['raw_signals']}", f"{row_te['raw_signals']}"
        )
        block_train.append(row_tr)
        block_test.append(row_te)
        for name in ("LONG", "SHORT"):
            if side_tr[name]["num_trades"]:
                sides_train[f"{label}-{name}"] = side_tr[name]
            if side_te[name]["num_trades"]:
                sides_test[f"{label}-{name}"] = side_te[name]

    _print_block("TRAIN (75% inicial) — H002-A/B/C", block_train, sides_train)
    _print_block("TEST  (25% final, OOS untouched) — H002-A/B/C", block_test, sides_test)

    # Persistencia del contraste LONG vs SHORT (sin decidir nada).
    print("\nContraste LONG vs SHORT (avg net $ por trade):")
    for label, _name, dirs in variants:
        direction = next(iter(dirs))
        if direction == "both":
            lo_tr = sides_train.get(f"{label}-LONG")
            sh_tr = sides_train.get(f"{label}-SHORT")
            lo_te = sides_test.get(f"{label}-LONG")
            sh_te = sides_test.get(f"{label}-SHORT")
            if lo_tr and sh_tr and lo_te and sh_te:
                d_tr = (lo_tr["expectancy"] or 0) - (sh_tr["expectancy"] or 0)
                d_te = (lo_te["expectancy"] or 0) - (sh_te["expectancy"] or 0)
                pers = "PERSISTE" if (d_tr > 0) == (d_te > 0) and d_te != 0 else "NO PRSISTE"
                print(
                    f"  {label:<8} train LONG-SHORT = {d_tr:>+8.1f} | "
                    f"test LONG-SHORT = {d_te:>+8.1f} | {pers}"
                )

    baseline = run_experiment(_direction_cfg(base, "both"), CSV_PATH,
                              out_dir=EXPERIMENTS / "outputs" / "h002_both")
    print(f"\nbaseline H002-C (dataset completo) guardado: {baseline['experiment_id']}")

    out = EXPERIMENTS / "outputs" / "h002_asymmetry.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"train": block_train, "test": block_test, "sides_train": sides_train,
             "sides_test": sides_test},
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nguardado en: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())