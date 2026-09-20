"""Pipeline FINAL_OOS: protocolo mecanico preparado antes de que lleguen los datos.

Cuando data.binance.vision publique el dump mensual de 2026-09, los datos
NUEVOS arrancan en `start_epoch = 1788220800` (2026-09-01T00:00Z). Ese tramo
es el unico que puede formar un FINAL_OOS limpio (evidence.json:
`minimum_start=1788220800`, estado `BLOCKED_BY_DATA_AVAILABILITY`).

Este modulo deja DECIDIDO y verificado por tests el protocolo de evaluacion:
lectura del dataset → manifest (sha256) → validacion de calidad (schema OHLC,
integridad, gaps, cobertura) → guards (DATA_ROLE + EVIDENCE + hash de
configuracion congelada + no doble ejecucion) → declaracion → evaluacion con
la estrategia congelada → benchmarks → reporte preregistrado → consumo
irreversible del FINAL_OOS → EvidenceRegistry.

Principio rector (coincide con H004-B): nada de lo que este modulo hace puede
depender de los numeros del OOS. Metricas, benchmarks, rango y criterios de
lectura estan congelados en `research/decisions/FINAL_OOS_PRE_REGISTRATION.json`
y verificados por `tests/test_final_oos_preregistration.py`.

Los tests de integracion (`tests/test_final_oos_pipeline.py`) ejercitan todo el
protocolo con un dataset sintetico que imita la estructura del futuro OOS, sin
tocar los registros reales (evidence.json / data_roles.json).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from data.loader import read_ohlc_csv
from data.quality import summarize_quality
from data.synthetic import sha256_of_file
from research.benchmarking import (
    buy_and_hold,
    capital_exposure_from_trades,
    curve_metrics,
    exposure_fraction_from_trades,
    exposure_matched_long,
    exposure_scaled_buy_and_hold,
)
from research.data_role import RoleRegistry
from research.evidence import EvidenceRegistry, FinalOosStatus
from research.experiment import make_config_hash
from research.pipeline import build_engine, build_strategy, experiment_id_for
from reporting.metrics import calculate_metrics

DEFAULT_PLAN_PATH = (
    Path(__file__).resolve().parent / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
)
DEFAULT_EVIDENCE_PATH = Path(__file__).resolve().parent / "evidence.json"
DEFAULT_ROLES_PATH = Path(__file__).resolve().parent / "data_roles.json"


class FinalOosError(ValueError):
    """Cualquier guard del protocolo FINAL_OOS que rechaza la operacion."""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _git_commit() -> str | None:
    """Commit de codigo usado en la evaluacion (lineage reproducible)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 - diagnostico, nunca fatal
        return None
    return None


def load_plan(path: str | Path | None = None) -> dict:
    """Carga el preregistro congelado (FINAL_OOS_PRE_REGISTRATION.json)."""
    path = Path(path) if path is not None else DEFAULT_PLAN_PATH
    if not path.exists():
        raise FinalOosError(f"Falta el preregistro del FINAL_OOS: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(manifest_path: str | Path) -> dict:
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise FinalOosError(f"Falta el manifest del dataset: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def start_epoch_of(plan: dict) -> int:
    return int(plan["esquema"]["start_epoch"])


def interval_of(plan: dict) -> int:
    return int(plan["esquema"]["interval_seconds"])


def min_span_of(plan: dict) -> int:
    return int(plan["esquema"]["min_span_seconds"])


def derive_end(scheme: dict, manifest: dict) -> int:
    """Regla de fin preregistrada: ultima hora COMPLETA de datos nuevos."""
    interval = int(scheme["interval_seconds"])
    last = int(manifest["last_ts"])
    return (last // interval + 1) * interval


def validate_quality(
    bars: list[dict],
    *,
    start_epoch: int,
    end_epoch: int,
    interval_seconds: int,
) -> dict:
    """Schema + integridad OHLC + duplicados + gaps + cobertura del rango.

    Returns:
        dict con `ok`, `rows` e `issues` (lista de str). El llamador usa
        `require_valid_quality` para transformarlo en excepcion si no `ok`.
    """
    summary = summarize_quality(bars, expected_interval_seconds=interval_seconds)
    issues = [
        f"fila {issue.row}: {issue.issue} ({issue.detail})" for issue in summary.issues
    ]
    if not bars:
        issues.append("dataset vacio")
    else:
        first = int(bars[0]["ts"])
        last = int(bars[-1]["ts"])
        if first > start_epoch:
            issues.append(
                f"primera barra {first} ({_iso(first)}) posterior a start_epoch "
                f"{start_epoch}: el rango no queda cubierto al inicio"
            )
        if last + interval_seconds < end_epoch:
            issues.append(
                f"ultima barra {last} ({_iso(last)}): no cubre el final del rango "
                f"({end_epoch}, {_iso(end_epoch)})"
            )
    return {
        "ok": not issues,
        "rows": len(bars),
        "issues": issues,
        "coverage": {
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "interval_seconds": interval_seconds,
            "first_ts": int(bars[0]["ts"]) if bars else None,
            "last_ts": int(bars[-1]["ts"]) if bars else None,
        },
    }


def require_valid_quality(summary: dict) -> None:
    if not summary["ok"]:
        raise FinalOosError(
            "[calidad] dataset OOS no valido: " + "; ".join(summary["issues"])
        )


def validate_manifest(
    csv_path: str | Path,
    manifest: dict,
    *,
    start_epoch: int,
    end_epoch: int,
    interval_seconds: int,
    bars: list[dict],
) -> dict:
    """Integridad del par CSV+manifest y cobertura sobre [start, end)."""
    checks = {
        "sha256_matches_csv": sha256_of_file(csv_path) == manifest.get("sha256"),
        "bars_match": int(manifest.get("bars", -1)) == len(bars),
        "coverage_start": int(manifest["first_ts"]) <= start_epoch,
        "coverage_end": int(manifest["last_ts"]) >= end_epoch - interval_seconds,
        "interval_seconds": int(manifest.get("interval_seconds", -1)) == interval_seconds,
    }
    return {"ok": all(checks.values()), "checks": checks}


def require_valid_manifest(integrity: dict) -> None:
    if not integrity["ok"]:
        failed = [k for k, v in integrity["checks"].items() if not v]
        raise FinalOosError(f"[manifest] integridad invalida: {', '.join(failed)}")


def frozen_strategy_matches(cfg: dict, plan: dict) -> None:
    """Guards de configuracion congelada: hash + params + parametros efectivos.

    Impide recalibracion o cualquier desviacion (params, risk, costs, engine)
    respecto al preregistro. Lanza `FinalOosError` si algo cambio.
    """
    p = plan["estrategia_congelada"]
    if make_config_hash(cfg["strategy"]["params"]) != p["strategy_config_hash"]:
        raise FinalOosError(
            "strategy_config_hash NO coincide con el preregistro (se recalibro la estrategia)"
        )
    if make_config_hash(cfg) != p["config_hash"]:
        raise FinalOosError(
            "config_hash NO coincide con el preregistro: cambio la configuracion completa"
        )
    if cfg["strategy"]["params"] != p["params"]:
        raise FinalOosError("params NO coinciden con los congelados en el preregistro")
    eff = p["parametros_efectivos"]
    derived = {
        "risk_per_trade": float(cfg["risk"]["risk_per_trade"]),
        "initial_equity": float(cfg["costs"]["initial_equity"]),
        "max_bars_in_trade": float(cfg["engine"].get("max_bars_in_trade")),
        "point_value": float(cfg["engine"].get("point_value", 1.0)),
        "interval_seconds": float(cfg["dataset"].get("interval_seconds", 3600)),
    }
    for key, value in derived.items():
        if float(eff[key]) != value:
            raise FinalOosError(
                f"parametro efectivo '{key}' cambiado: {value} != {eff[key]} (preregistro). "
                "Sin recalibracion para el FINAL_OOS."
            )


def preflight(
    plan: dict,
    evidence: EvidenceRegistry,
    roles: RoleRegistry,
    cfg: dict,
    *,
    csv_path: str | Path,
    manifest: dict,
    start_epoch: int,
    end_epoch: int,
    bars: list[dict],
) -> None:
    """Guard stack pre-ejecucion (no muta registros). Lanza si algo procede mal.

    No llama a `evidence.guard` sobre el propio tramo mientras esta BLOCKED: la
    declaracion es justamente el acto de desbloquear. La proteccion contra
    OBSERVED la aporta `roles.guard_final_oos` (mecanico e independiente).
    """
    if evidence.final_oos_status is FinalOosStatus.CONSUMED:
        raise FinalOosError("FINAL_OOS ya CONSUMIDO: no se evalua dos veces")
    if roles.final_oos_consumed:
        raise FinalOosError("RoleRegistry marca final_oos_consumed: irreversible")

    minimum_start = evidence.dataset.final_oos.minimum_start
    if minimum_start is None or start_epoch < minimum_start:
        raise FinalOosError(
            f"start_epoch {start_epoch} < minimum_start {minimum_start}: "
            "solo datos nuevos (> tramo OBSERVED) pueden formar el FINAL_OOS"
        )
    if end_epoch - start_epoch < min_span_of(plan):
        raise FinalOosError(
            f"cobertura {end_epoch - start_epoch}s < min_span {min_span_of(plan)}s "
            "(365 dias): el FINAL_OOS sigue BLOCKED_BY_DATA_AVAILABILITY por calendario"
        )

    roles.guard_final_oos(start_epoch, end_epoch, purpose="FINAL_OOS preflight")
    frozen_strategy_matches(cfg, plan)
    require_valid_manifest(
        validate_manifest(
            csv_path,
            manifest,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            interval_seconds=interval_of(plan),
            bars=bars,
        )
    )


def declare_final_oos(
    plan: dict,
    evidence: EvidenceRegistry,
    roles: RoleRegistry,
    *,
    start_epoch: int,
    end_epoch: int,
    label: str = "final_oos_v1",
    note: str | None = None,
):
    """Declara (desbloquea) el FINAL_OOS limpio.

    Idempotente si ya esta DECLARED exactamente con el mismo tramo; rechaza
    cualquier segundo FINAL_OOS o tramo distinto.
    """
    if evidence.final_oos_status is FinalOosStatus.DECLARED:
        rng = roles.final_oos
        if rng is None or rng.start_epoch != start_epoch or rng.end_epoch != end_epoch:
            raise FinalOosError(
                "FINAL_OOS ya declarado con otro tramo; no se declara un segundo"
            )
        return rng
    if evidence.final_oos_status is not FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY:
        raise FinalOosError(
            f"estado no declarable ({evidence.final_oos_status.value}): se exige "
            "BLOCKED_BY_DATA_AVAILABILITY o DECLARED con el mismo tramo"
        )
    return evidence.unblock_final_oos(
        roles,
        label=label,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        note=note or f"FINAL_OOS limpio [{_iso(start_epoch)}, {_iso(end_epoch)})",
    )


def _bench_row(entry: dict) -> dict:
    metrics = entry["metrics"]
    return {
        "label": entry.get("label"),
        "net_return": metrics["net_return"],
        "net_pnl": entry.get("net_pnl"),
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "sortino": metrics["sortino"],
        "calmar": metrics["calmar"],
        "exposure_frac": entry.get("exposure_frac"),
        "num_trades": entry.get("num_trades"),
        "costs": entry.get("costs"),
    }


def _compute_benchmarks(cfg: dict, bars: list[dict], plan: dict, exposure_time: float, cap_exp: float) -> dict:
    """Benchmarks preregistrados sobre el mismo tramo (identicos a H003/H004/H004-B)."""
    bench_cfg = plan["benchmarks"]
    initial_equity = float(cfg["costs"]["initial_equity"])
    point_value = float(cfg["engine"].get("point_value", 1.0))
    per_side = float(bench_cfg["per_side"])
    interval = interval_of(plan)

    bh = buy_and_hold(
        bars,
        initial_equity=initial_equity,
        per_side=per_side,
        point_value=point_value,
    )
    scaled_frac = max(exposure_time * cap_exp, 1e-6)
    bh_scaled = exposure_scaled_buy_and_hold(
        bars,
        exposure_frac=scaled_frac,
        initial_equity=initial_equity,
        per_side=per_side,
        point_value=point_value,
    )
    neutral = exposure_matched_long(
        bars,
        target_exposure=max(exposure_time, 1e-6),
        initial_equity=initial_equity,
        per_side=per_side,
        point_value=point_value,
        risk_per_trade=cfg["risk"]["risk_per_trade"],
        atr_period=cfg["strategy"]["params"]["atr_period"],
        cycle_bars=cfg["engine"].get("max_bars_in_trade") or 48,
        offset=bench_cfg["neutral_offset"],
    )
    return {
        "bh": _bench_row(bh),
        "bh_scaled": _bench_row(bh_scaled),
        "neutral": _bench_row(neutral),
        "neutral_exposure_frac": neutral["exposure_frac"],
        "neutral_block_trades": neutral["num_trades"],
    }


def evaluate(
    plan: dict,
    cfg: dict,
    *,
    bars_window: list[dict],
) -> dict:
    """Ejecuta la estrategia CONGELADA sobre el tramo final y calcula metricas.

    Returns:
        dict con metrics, curve_metrics, exposure, benchmarks, contrasts,
        reading y trades/equity_curve (brutos para el reporte).
    """
    strategy = build_strategy(cfg["strategy"])
    engine = build_engine(cfg)
    result = engine.run(strategy, bars_window)

    metrics = calculate_metrics(result)
    cm = curve_metrics(result.equity_curve, len(bars_window), interval_of(plan))
    closes = [float(b["close"]) for b in bars_window]
    n = len(bars_window)
    exposure_time = exposure_fraction_from_trades(result.trades, n)
    cap_exp = capital_exposure_from_trades(result.trades, result.equity_curve, closes)

    bench = _compute_benchmarks(cfg, bars_window, plan, exposure_time, cap_exp)

    neutral_exp = bench["neutral"]["net_return"]
    contrasts = {
        "delta_vs_neutral_ret": round(metrics.net_return - neutral_exp, 8),
        "delta_vs_bh_scaled_ret": round(
            metrics.net_return - bench["bh_scaled"]["net_return"], 8
        ),
        "expectancy_breakout_vs_neutral": round(
            metrics.expectancy - (bench["neutral"]["net_pnl"] / bench["neutral_block_trades"])
            if metrics.expectancy is not None and bench["neutral_block_trades"]
            else None,
            8,
        ),
    }

    effective = {
        "risk_per_trade": cfg["risk"]["risk_per_trade"],
        "initial_equity": cfg["costs"]["initial_equity"],
        "max_bars_in_trade": cfg["engine"].get("max_bars_in_trade"),
        "point_value": cfg["engine"].get("point_value", 1.0),
        "interval_seconds": interval_of(plan),
    }

    return {
        "metrics": metrics.to_dict(),
        "curve_metrics": cm,
        "exposure": {"exposure_time": exposure_time, "capital_exposure_cond": cap_exp},
        "benchmarks": bench,
        "contrasts": contrasts,
        "effective_config": effective,
        "trades": result.trades,
        "equity_curve": result.equity_curve,
        "diagnostics": result.diagnostics,
    }


def apply_reading(plan: dict, report: dict) -> dict:
    """Criterios de lectura preregistrados: flags primarios + conclusion.

    Criterios NULOS de falsacion (no peor que no hacer nada con la misma
    exposicion), tal como estan congelados en el preregistro.
    """
    m = report["metrics"]
    primarios = {
        "net_return_gt_zero": float(m["net_return"]) > 0.0,
        "profit_factor_ge_one": m["profit_factor"] is None or float(m["profit_factor"]) >= 1.0,
        "expectancy_gt_zero": m["expectancy"] is None or float(m["expectancy"]) > 0.0,
        "beats_neutral_control": float(report["contrasts"]["delta_vs_neutral_ret"]) > 0.0,
    }
    notas = {
        "profit_factor_ge_one": "None si no hay perdidas (se trata como satisfecho)",
        "expectancy_gt_zero": "None si no hay trades (se reporta literal)",
    }
    conclusion = "NO_FALSADO" if all(primarios.values()) else "FALSADO"
    return {
        "primarios": primarios,
        "notas_de_tipos_None": notas,
        "regla": "NO_FALSADO si los 4 primarios cumplen; FALSADO si alguno no "
                 "cumple (falsacion estricta). La transicion de evidencia de la "
                 "hipotesis madre sigue ALLOWED_TRANSITIONS de evidence.py.",
        "conclusion": conclusion,
    }


def build_oos_report(
    plan: dict,
    cfg: dict,
    *,
    ev: dict,
    integrity: dict,
    quality: dict,
    manifest: dict,
    csv_path: str | Path,
    rng,
    roles: RoleRegistry,
    evidence_status_before: str,
) -> dict:
    """Ensambla el reporte con TODAS las secciones preregistradas."""
    required = plan["secciones_del_reporte"]
    report = {
        "hypothesis": plan["pregunta"],
        "method": (
            "Evaluacion unica del FINAL_OOS limpio (datos nuevos >= 2026-09-01) "
            "con la estrategia congelada, sin recalibracion ni seleccion. Esquema, "
            "metricas y criterios en FINAL_OOS_PRE_REGISTRATION.json."
        ),
        "lineage": {
            "final_oos": {
                "label": rng.label,
                "start_epoch": rng.start_epoch,
                "end_epoch": rng.end_epoch,
                "iso_start": _iso(rng.start_epoch),
                "iso_end": _iso(rng.end_epoch),
            },
            "strategy": {
                "strategy_id": cfg["strategy"]["strategy_id"],
                "params": cfg["strategy"]["params"],
                "strategy_config_hash": plan["estrategia_congelada"]["strategy_config_hash"],
                "config_hash": plan["estrategia_congelada"]["config_hash"],
                "parameters_changed": False,
                "optimization_performed": False,
            },
            "dataset": {
                "csv_path": str(csv_path),
                "csv_sha256": sha256_of_file(csv_path),
                "manifest_sha256": manifest.get("sha256"),
                "manifest_bars": manifest.get("bars"),
                "manifest_window": [manifest.get("first_ts"), manifest.get("last_ts")],
            },
            "engine": {
                "engine_version": 2,
                "experiment_id": experiment_id_for(cfg, csv_path),
            },
            "code_used": {
                "git_commit": _git_commit(),
                "runner": "experiments/e12_final_oos.py",
                "module": "research/final_oos_pipeline.py",
            },
            "within_study_guards": {
                "previous_oos_consumed": roles.previous_oos_consumed,
                "final_oos_consumed": integrity.get("final_oos_consumed", roles.final_oos_consumed),
                "final_oos_status": integrity.get("final_oos_status"),
            },
        },
        "dataset_integrity": {
            "quality": quality,
            "manifest": integrity["manifest"],
            "coverage": quality["coverage"],
        },
        "metrics": ev["metrics"],
        "curve_metrics": ev["curve_metrics"],
        "exposure": ev["exposure"],
        "benchmarks": ev["benchmarks"],
        "contrasts": ev["contrasts"],
        "reading": ev["reading"],
        "evidence": {
            "status_before": evidence_status_before,
            "status_after": integrity.get("final_oos_status"),
            "minimum_start": plan["esquema"]["start_epoch"],
        },
        "trades_summary": {
            "count": len(ev["trades"]),
            "wins": sum(1 for t in ev["trades"] if float(t.get("pnl") or 0) > 0),
            "losses": sum(1 for t in ev["trades"] if float(t.get("pnl") or 0) < 0),
            "stopped": sum(1 for t in ev["trades"] if t.get("exit_reason") == "STOP"),
            "time_exits": sum(1 for t in ev["trades"] if t.get("exit_reason") == "TIME"),
            "long": sum(1 for t in ev["trades"] if t.get("side") == "LONG"),
        },
        "trades": ev["trades"],
        "equity_curve": ev["equity_curve"],
    }
    missing = [s for s in required if s not in report]
    if missing:
        raise FinalOosError(
            f"Reporte incompleto frente al preregistro; faltan: {', '.join(missing)}"
        )
    return report


def run_final_oos(
    plan: dict,
    cfg: dict,
    *,
    csv_path: str | Path,
    manifest_path: str | Path,
    evidence: EvidenceRegistry | None = None,
    roles: RoleRegistry | None = None,
    evidence_path: str | Path = DEFAULT_EVIDENCE_PATH,
    roles_path: str | Path = DEFAULT_ROLES_PATH,
    out_path: str | Path | None = None,
    consume: bool = True,
    label: str = "final_oos_v1",
    manual_end: int | None = None,
) -> dict:
    """Orquesta la evaluacion completa del FINAL_OOS.

    Guards en orden estricto; en cuanto un guard falla, no se muta NADA
    (excepto el reporte de salida, que solo se escribe al final y con
    `out_path` explicito). El consumo (`mark_consumed`) es irreversible y
    persiste evidence + roles.
    """
    csv_path = Path(csv_path)
    evidence = evidence if evidence is not None else EvidenceRegistry.load_default()
    roles = roles if roles is not None else RoleRegistry.load_default()
    manifest = load_manifest(manifest_path)

    all_bars = read_ohlc_csv(csv_path)
    start = start_epoch_of(plan)
    end = manual_end if manual_end is not None else derive_end(plan["esquema"], manifest)
    interval = interval_of(plan)

    quality = validate_quality(
        all_bars,
        start_epoch=start,
        end_epoch=end,
        interval_seconds=interval,
    )
    require_valid_quality(quality)

    preflight(
        plan,
        evidence,
        roles,
        cfg,
        csv_path=csv_path,
        manifest=manifest,
        start_epoch=start,
        end_epoch=end,
        bars=all_bars,
    )

    status_before = evidence.final_oos_status.value
    declare_final_oos(plan, evidence, roles, start_epoch=start, end_epoch=end, label=label)
    evidence.guard(roles, start, end, purpose="FINAL_OOS post-declaracion")

    bars = [b for b in all_bars if start <= int(b["ts"]) < end]
    ev = evaluate(plan, cfg, bars_window=bars)
    ev["reading"] = apply_reading(
        plan,
        {
            "metrics": ev["metrics"],
            "contrasts": ev["contrasts"],
        },
    )

    integrity = validate_manifest(
        csv_path,
        manifest,
        start_epoch=start,
        end_epoch=end,
        interval_seconds=interval,
        bars=all_bars,
    )
    require_valid_manifest(integrity)

    if consume:
        evidence.mark_consumed(roles)
    report = build_oos_report(
        plan,
        cfg,
        ev=ev,
        integrity={
            "manifest": integrity["checks"],
            "final_oos_consumed": roles.final_oos_consumed,
            "final_oos_status": evidence.final_oos_status.value,
        },
        quality=quality,
        manifest=manifest,
        csv_path=csv_path,
        rng=roles.final_oos,
        roles=roles,
        evidence_status_before=status_before,
    )

    evidence.to_file(evidence_path)
    roles.to_file(roles_path)

    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report