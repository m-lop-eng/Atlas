"""Runner compartido de experimentos Atlas.

Uso: cada directorio experiments/<nombre> contiene run.py + config.yaml;
run.py delega aquí. Garantiza que TODOS los experimentos usan la misma
mecánica (dataset → pipeline → métricas → lineage), de modo que las
diferencias entre variantes sean atribuibles solo al cambio controlado.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.loader import read_ohlc_csv
from data.quality import summarize_quality
from data.synthetic import generate_ohlc_bars, write_ohlc_csv
from research.pipeline import run_experiment, track_experiment


def regenerate_dataset(cfg: dict) -> Path:
    """Genera el dataset sintético determinista (mismo seed → mismo CSV).

    El CSV nunca se commitea (política de datos): la reproducibilidad del
    dataset vive en `config.yaml::dataset` (seed, parámetros del generador).
    """
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


def run_experiment_dir(config_path: str | Path) -> int:
    """Ejecuta el experimento descrito por `config.yaml` y muestra el resumen."""
    config_path = Path(config_path)
    if not config_path.exists():
        print(f"Configuración no encontrada: {config_path}")
        return 1

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    csv_path = regenerate_dataset(config)
    bars = read_ohlc_csv(csv_path)

    quality = summarize_quality(bars)
    if not quality.ok:
        print(f"Dataset con problemas ({len(quality.issues)}):")
        for issue in quality.issues[:5]:
            print(f"  - {issue.issue}: {issue.detail}")

    out_dir = config_path.parent / "outputs"
    report = run_experiment(config, csv_path, out_dir=out_dir)
    tracked = track_experiment(report)

    print(f"experiment_id : {report['experiment_id']}")
    print(f"datos         : {csv_path} ({len(bars)} barras)")
    print(f"trades        : {report['trades_summary']['count']}")
    print(
        f"  wins/losses : {report['trades_summary']['wins']}/{report['trades_summary']['losses']}"
    )
    print(f"  stopped     : {report['trades_summary']['stopped']}")
    print(f"final_equity  : {report['final_equity']:.2f}")
    print(f"net_return    : {report['metrics']['net_return']}")
    print(f"max_drawdown  : {report['metrics']['max_drawdown']}")
    diag = report.get("diagnostics", {})
    if diag:
        print(f"raw/flat/en-pos/rechazadas/órdenes/trades : "
              f"{diag['raw_signals']}/{diag['signals_while_flat']}/"
              f"{diag['signals_while_in_position']}/{diag['risk_rejected_signals']}/"
              f"{diag['orders_created']}/{diag['trades_completed']}")
    print(f"registro      : {tracked.status.value} ({report['experiment_id']})")
    print(f"reporte       : {out_dir / report['experiment_id'] / 'report.json'}")
    return 0