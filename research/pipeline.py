"""Pipeline del ciclo completo: datos → estrategia → riesgo → ejecución → reporte.

Orquesta un experimento a partir de configuración declarativa. El resultado
es un reporte de lineage + métricas, determinista dado el mismo dataset y la
misma configuración (reproducibilidad, 05_RESEARCH.md §10).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backtesting.engine import BacktestEngine, ExecutionCosts
from data.loader import read_ohlc_csv
from data.synthetic import sha256_of_file
from execution.order_manager import OrderManager
from reporting.metrics import calculate_metrics
from research.analysis import build_report_sections
from research.experiment import (
    ExperimentRun,
    ExperimentStatus,
    experiment_meta,
    make_config_hash,
)
from risk.engine import RiskEngine
from risk.types import RiskConfig
from strategies.base.strategy import BaseStrategy, StrategyMetadata
from strategies.breakout import BreakoutParams, BreakoutStrategy
from strategies.random_entry import RandomEntryParams, RandomEntryStrategy

STRATEGY_REGISTRY: dict[str, tuple] = {
    "atlas-breakout": (BreakoutParams, BreakoutStrategy),
    "atlas-first-breakout": (BreakoutParams, BreakoutStrategy),
    "atlas-breakout-trend-filter": (BreakoutParams, BreakoutStrategy),
    "atlas-random-entry": (RandomEntryParams, RandomEntryStrategy),
}


def build_strategy(cfg: dict) -> BaseStrategy:
    """Construye la estrategia desde la sección `strategy` de la config."""
    meta = StrategyMetadata(
        strategy_id=cfg["strategy_id"],
        strategy_name=cfg["strategy_name"],
        strategy_family=cfg["strategy_family"],
        market=cfg["market"],
        instrument=cfg["instrument"],
        timeframe=cfg["timeframe"],
        version=cfg["version"],
        parameter_set_version=cfg["parameter_set_version"],
        data_version=cfg["data_version"],
    )
    params_cls, strategy_cls = STRATEGY_REGISTRY[cfg["strategy_id"]]
    return strategy_cls(meta, params_cls(**cfg["params"]))


def build_engine(cfg: dict) -> BacktestEngine:
    """Construye el pipeline (riesgo + ejecución) desde la config."""
    risk_cfg = RiskConfig(**cfg["risk"])
    risk_engine = RiskEngine(config=risk_cfg)
    order_manager = OrderManager()
    costs = ExecutionCosts(**cfg["costs"])
    engine_args = dict(cfg.get("engine", {}))
    return BacktestEngine(
        costs=costs,
        risk_engine=risk_engine,
        order_manager=order_manager,
        **engine_args,
    )


def experiment_id_for(config: dict, dataset_path: Path) -> str:
    """ID determinista del experimento: hash(config + dataset + versión del motor)."""
    payload = {
        "config": config,
        "dataset_sha256": sha256_of_file(dataset_path),
        "engine_version": BacktestEngine.ENGINE_VERSION,
    }
    return f"exp-{make_config_hash(payload)[:12]}"


def run_experiment(
    config: dict,
    data_path: str | Path,
    *,
    out_dir: str | Path | None = None,
) -> dict:
    """Ejecuta el pipeline completo y devuelve/guarda el reporte.

    Returns:
        dict con claves: experiment_id, generated_at, lineage, metrics,
        trades_summary, trades, equity_curve.
    """
    data_path = Path(data_path)
    bars = read_ohlc_csv(data_path)

    strategy = build_strategy(config["strategy"])
    engine = build_engine(config)
    result = engine.run(strategy, bars)
    metrics = calculate_metrics(result)
    sections = build_report_sections(
        result.diagnostics, result.trades, config["costs"], metrics.to_dict()
    )

    experiment_id = experiment_id_for(config, data_path)
    exp_meta = experiment_meta(config)
    lineage = {
        "experiment_id": experiment_id,
        "experiment_type": exp_meta["experiment_type"],
        "parent_experiment_id": exp_meta["parent_experiment_id"],
        "hypothesis": exp_meta["hypothesis"],
        "change": exp_meta["change"],
        "strategy": {
            "strategy_id": strategy.metadata.strategy_id,
            "strategy_version": strategy.metadata.version,
            "parameter_set_version": strategy.metadata.parameter_set_version,
            "data_version": strategy.metadata.data_version,
            "params": config["strategy"]["params"],
            "params_hash": make_config_hash(config["strategy"]["params"]),
        },
        "dataset": {
            "path": str(data_path),
            "sha256": sha256_of_file(data_path),
            "rows": len(bars),
        },
        "risk_config": config["risk"],
        "costs": config["costs"],
        "engine": config.get("engine", {}),
        "engine_version": BacktestEngine.ENGINE_VERSION,
        "environment": config.get("experiment", {"environment": "BACKTEST"}).get(
            "environment", "BACKTEST"
        ),
    }

    report = {
        "experiment_id": experiment_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lineage": lineage,
        "metrics": metrics.to_dict(),
        "trades_summary": {
            "count": len(result.trades),
            "wins": sum(1 for t in result.trades if (t.get("pnl") or 0) > 0),
            "losses": sum(1 for t in result.trades if (t.get("pnl") or 0) < 0),
            "stopped": sum(1 for t in result.trades if t.get("exit_reason") == "STOP"),
            "long": sum(1 for t in result.trades if t.get("side") == "LONG"),
            "short": sum(1 for t in result.trades if t.get("side") == "SHORT"),
            "time_exits": sum(1 for t in result.trades if t.get("exit_reason") == "TIME"),
        },
        "signals": sections["signals"],
        "trades_breakdown": sections["trades_breakdown"],
        "performance": sections["performance"],
        "trade_distribution": sections["trade_distribution"],
        "execution": sections["execution"],
        "sides": sections["sides"],
        "trades": result.trades,
        "equity_curve": result.equity_curve,
        "diagnostics": result.diagnostics,
        "final_equity": result.final_equity,
    }

    if out_dir is not None:
        out = Path(out_dir) / experiment_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )

    return report


def track_experiment(report: dict) -> ExperimentRun:
    """Registra el experimento en el seguimiento de investigación."""
    line = report["lineage"]
    run = ExperimentRun(
        strategy_id=line["strategy"]["strategy_id"],
        config_hash=line["strategy"]["params_hash"],
        dataset_hash=line["dataset"]["sha256"],
    )
    run.transition(ExperimentStatus.RUNNING)
    run.metrics = report["metrics"]
    return run