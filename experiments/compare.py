"""Comparación controlada baseline vs variante (Experimento 1 ↔ 2).

Ejecución:  python experiments/compare.py

Ambos experimentos se ejecutan con el MISMO dataset, capital, RiskEngine,
riesgo, stop, ejecución, costes y metodología. La única diferencia es
`params.trend_filter`. El resultado se guarda en
experiments/outputs/compare/baseline_vs_trend_filter.json.

Advertencia estadística: con N operaciones pequeñas, cualquier diferencia
de rendimiento tiene valor estadístico prácticamente nulo. Esta tabla es
una observación de arquitectura, no una validación de estrategia.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline import run_experiment

EXPERIMENTS = Path(__file__).resolve().parent


def _summary(report: dict) -> dict:
    trades = report["trades"]
    pnls = [t.get("pnl", 0.0) for t in trades]
    metrics = report["metrics"]
    return {
        "experiment_id": report["experiment_id"],
        "trades": len(trades),
        "win_rate": (summary := report["trades_summary"]).get("wins", 0) / len(trades) if trades else 0.0,
        "net_return": metrics["net_return"],
        "max_drawdown": metrics["max_drawdown"],
        "profit_factor": metrics.get("profit_factor"),
        "avg_trade": (sum(pnls) / len(pnls)) if pnls else 0.0,
        "stops": report["trades_summary"].get("stopped", 0),
    }


def main() -> int:
    base_cfg = yaml.safe_load((EXPERIMENTS / "first_experiment" / "config.yaml").read_text(encoding="utf-8"))
    trend_cfg = yaml.safe_load((EXPERIMENTS / "trend_filter" / "config.yaml").read_text(encoding="utf-8"))
    csv_path = ROOT / base_cfg["dataset"]["path"]

    base_report = run_experiment(base_cfg, csv_path)
    trend_report = run_experiment(trend_cfg, csv_path)
    a, b = _summary(base_report), _summary(trend_report)

    header = f"{'Métrica':<14}{'Baseline':>16}{'TrendFilter':>16}"
    rows = [
        ("Trades", str(a["trades"]), str(b["trades"])),
        ("Win rate", f"{a['win_rate'] * 100:.1f}%", f"{b['win_rate'] * 100:.1f}%"),
        ("Net return", f"{a['net_return'] * 100:.2f}%", f"{b['net_return'] * 100:.2f}%"),
        ("Max DD", f"{a['max_drawdown'] * 100:.2f}%", f"{b['max_drawdown'] * 100:.2f}%"),
        ("Profit factor", f"{a['profit_factor']}" if a["profit_factor"] is not None else "n/a",
                           f"{b['profit_factor']}" if b["profit_factor"] is not None else "n/a"),
        ("Avg trade", f"{a['avg_trade']:.2f}", f"{b['avg_trade']:.2f}"),
        ("Stops", str(a["stops"]), str(b["stops"])),
    ]
    print(header)
    for name, left, right in rows:
        print(f"{name:<14}{left:>16}{right:>16}")

    payload = {
        "method": (
            "Control de un solo cambio: mismo dataset (sha256), capital, "
            "RiskEngine, riesgo por operación, stop, ejecución (signal[t] → "
            "open[t+1]), costes y metodología. Solo difiere params.trend_filter."
        ),
        "baseline": base_report,
        "variant": trend_report,
        "summary": {"baseline": a, "variant": b},
    }
    out = EXPERIMENTS / "outputs" / "compare"
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline_vs_trend_filter.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    print(f"guardado en: {out / 'baseline_vs_trend_filter.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())