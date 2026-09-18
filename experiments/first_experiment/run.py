"""Primer experimento Atlas: breakout simple sobre datos sintéticos.

Ejecución:  python experiments/first_experiment/run.py

Pipeline validado: CSV → loader → BreakoutStrategy → Signal → RiskEngine
(sizing fixed-fractional) → OrderManager → fills open[t+1] → stops →
métricas → reporte de lineage JSON en outputs/<experiment_id>/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from data.quality import summarize_quality
from data.synthetic import generate_ohlc_bars, write_ohlc_csv
from research.pipeline import run_experiment, track_experiment

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def regenerate_dataset(cfg: dict) -> Path:
    """Genera el dataset sintético determinista (mismo seed → mismo CSV)."""
    ds = cfg["dataset"]
    path = ROOT / ds["path"]
    if path.exists() and not ds.get("regenerate", False):
        return path
    bars = generate_ohlc_bars(
        n_bars=ds["n_bars"],
        start_price=ds["start_price"],
        start_epoch=ds["start_epoch"],
        interval_seconds=ds["interval_seconds"],
        seed=ds["seed"],
        drift=ds["drift"],
        volatility=ds["volatility"],
    )
    return write_ohlc_csv(bars, path)


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"Configuración no encontrada: {CONFIG_PATH}")
        return 1

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    csv_path = regenerate_dataset(config)

    bars = None
    from data.loader import read_ohlc_csv

    bars = read_ohlc_csv(csv_path)
    quality = summarize_quality(bars)
    if not quality.ok:
        print(f"Dataset con problemas ({len(quality.issues)}):")
        for issue in quality.issues[:5]:
            print(f"  - {issue.issue}: {issue.detail}")

    out_dir = Path(__file__).resolve().parent / "outputs"
    report = run_experiment(config, csv_path, out_dir=out_dir)
    tracked = track_experiment(report)

    print(f"experiment_id : {report['experiment_id']}")
    print(f"datos         : {csv_path} ({len(bars)} barras)")
    print(f"trades        : {report['trades_summary']['count']}")
    print(f"  wins/losses : {report['trades_summary']['wins']}/{report['trades_summary']['losses']}")
    print(f"  stopped     : {report['trades_summary']['stopped']}")
    print(f"final_equity  : {report['final_equity']:.2f}")
    print(f"net_return    : {report['metrics']['net_return']}")
    print(f"max_drawdown  : {report['metrics']['max_drawdown']}")
    print(f"registro      : {tracked.status.value} ({report['experiment_id']})")
    print(f"reporte       : {out_dir / report['experiment_id'] / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())