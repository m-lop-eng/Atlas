"""E11 — H004-B :: Diagnostico de localizacion temporal de la inestabilidad.

Ejecuta el esquema CONGELADO en research/decisions/H004B_PRE_REGISTRATION.json
(12 ventanas trimestrales re-ancladas 2021-04-01 -> 2024-04-01) con la estrategia
fija (hash 491ed76d...), sin recalibracion ni seleccion.

Metodologia:
  * Estrategia CONGELADA = H002-A/H003-A/H004 (direction=long, fixed-parameter).
  * Cada ventana se evalua en frio (cold-start) sobre sus propias barras, igual
    que e9 _run(cfg, valid_bars). NO hay train que seleccione: es estrategia
    fija x ventana, no un walk-forward que recalibre.
  * Benchmarks y regimen identicos a e9/e10 (research/benchmarking).
  * Criterios de lectura LOCALIZADO / SISTEMATICO / INCONCLUSO y precedencia:
    se evaluan tal como estan escritos en el pre-registro, SIN cambiarlos.

Guards mecanicos:
  * No se ejecuta si el pre-registro no esta en estado AUTHORIZED/RUN_IN_PROGRESS
    (impide correr sin autorizacion o re-ejecutar tras COMPLETE).
  * evidence.guard verifica tramo dentro de DEVELOPMENT, sin OBSERVED/FINAL_OOS.
  * params hash debe coincidir con el hash congelado; si cambia, aborta.

Salidas:
  * experiments/outputs/h004b_diagnosis.json — resultado bruto por ventana + agregado.
  * research/decisions/H004B_RESULT.json — decision con el pre-registro embebido
    (prueba de que el criterio existia antes de conocer los numeros).
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import e9_h004_walk_forward as e9  # helpers: metodologia identica a H004

from data.loader import read_ohlc_csv
from research.data_role import RoleRegistry
from research.evidence import EvidenceError, EvidenceRegistry, FinalOosStatus
from research.experiment import make_config_hash

EXPERIMENTS = Path(__file__).resolve().parent
PREREG_PATH = ROOT / "research" / "decisions" / "H004B_PRE_REGISTRATION.json"
RESULT_PATH = ROOT / "research" / "decisions" / "H004B_RESULT.json"
CSV_PATH = ROOT / "experiments" / "data" / "market_BTCUSDT_60min.csv"
H002_CONFIG = EXPERIMENTS / "h002" / "config.yaml"

FROZEN_HASH = "491ed76d79f1034452e98f72453141a4c69110cc8d487f166a89072df332687f"
DEV_START = 1_546_300_800  # 2019-01-01


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _time_exit_win_rate(res: e9.BacktestResult) -> float | None:
    time_trades = [t for t in res.trades if t.get("exit_reason") == "TIME"]
    if not time_trades:
        return None
    return round(sum(1 for t in time_trades if float(t["pnl"]) > 0) / len(time_trades), 4)


def _bench(row: dict) -> dict:
    return {
        "net_return_pct": row["net_return"],
        "net_pnl": row["net_pnl"],
        "max_drawdown": row["max_drawdown"],
    }


def _verdict(rows: list[dict]) -> dict:
    """Implementacion literal de los criterios congelados (orden fijo)."""
    labels = [r["label"] for r in rows]
    neg = [r["label"] for r in rows if r["metricas"]["net_return_pct"] < 0]
    pos = [r["label"] for r in rows if r["metricas"]["net_return_pct"] >= 0]

    vecindades: list[list[str]] = []
    run: list[str] = []
    for label in labels:
        if label in neg:
            run.append(label)
        else:
            if run:
                vecindades.append(run)
                run = []
    if run:
        vecindades.append(run)
    nv = len(vecindades)
    negset = set(neg)
    fuera = [r["label"] for r in rows if r["label"] not in negset]
    deltas_outside = {
        r["label"]: r["metricas"]["delta_vs_neutral_pct"]
        for r in rows if r["label"] in fuera
    }

    es_localizado = (nv <= 2 and len(pos) >= 9
                     and all(v > 0 for v in deltas_outside.values()))
    es_sistematico = (nv >= 3 or len(pos) <= 8)

    if es_localizado:
        clasificacion = "LOCALIZADO"
    elif es_sistematico:
        clasificacion = "SISTEMATICO"
    else:
        clasificacion = "INCONCLUSO"

    return {
        "clasificacion": clasificacion,
        "folds_positivos": sorted(pos),
        "folds_negativos": sorted(neg),
        "count_folds_positivos": len(pos),
        "num_vecindades_negativas": nv,
        "vecindades_negativas": vecindades,
        "fuera_de_vecindad": sorted(fuera),
        "deltas_vs_neutral_fuera_de_vecindad": deltas_outside,
        "evaluacion": {
            "localizado": {
                "num_vecindades_negativas <= 2": nv <= 2,
                "count_folds_positivos >= 9": len(pos) >= 9,
                "deltas_vs_neutral_fuera_de_vecindad > 0": all(
                    v > 0 for v in deltas_outside.values()),
            },
            "sistematico": {
                "num_vecindades_negativas >= 3": nv >= 3,
                "count_folds_positivos <= 8": len(pos) <= 8,
            },
        },
    }


def main() -> int:
    prereg = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    estado = prereg["estado"]
    if estado not in ("AUTHORIZED", "RUN_IN_PROGRESS"):
        raise SystemExit(
            f"[e11] H004-B no autorizado (estado={estado}). Requiere AUTHORIZED."
        )

    folds = prereg["esquema_de_folds"]["folds"]
    assert len(folds) == prereg["esquema_de_folds"]["cantidad_congelada"] == 12
    total_start = prereg["data_roles"]["rango_total_diagnostico"]["start_epoch"]
    total_end = prereg["data_roles"]["rango_total_diagnostico"]["end_epoch"]

    roles = RoleRegistry.load_default()
    evidence = EvidenceRegistry.load_default()
    evidence.guard(roles, total_start, total_end, purpose="H004-B ejecucion")
    assert evidence.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY, \
        "H004-B no debe ejecutarse con FINAL_OOS desbloqueado"

    base = yaml.safe_load(H002_CONFIG.read_text(encoding="utf-8"))
    cfg = e9._long_cfg(base)
    params_hash = make_config_hash(cfg["strategy"]["params"])
    if params_hash != FROZEN_HASH:
        raise SystemExit(
            f"[e11] hash de params {params_hash} != congelado {FROZEN_HASH}. "
            "No se ejecuta con configuracion distinta."
        )

    bars = read_ohlc_csv(CSV_PATH)
    print(
        f"H004-B — BTC 1h {len(bars)} barras | esquema 12 ventanas "
        f"{_iso(total_start)} -> {_iso(total_end)} | hash {params_hash[:12]}..."
    )
    print(f"FINAL_OOS status = {evidence.final_oos_status.value} "
          f"(guard activo) | estado pre-registro = {estado}")

    rows = []
    for f in folds:
        label = f["label"]
        vs, ve = f["start_epoch"], f["end_epoch"]
        w = e9._eval_window(cfg, bars, label, DEV_START, vs, ve)
        va = w["validation"]
        res = e9._run(cfg, e9._slice(bars, vs, ve))

        net_return_pct = round(va["net_return"] * 100, 4)
        m = {
            "label": label,
            "range": [vs, ve],
            "iso_range": [_iso(vs), _iso(ve)],
            "bars": w["valid_bars"],
            "metricas": {
                "net_return_pct": net_return_pct,
                "net_pnl": round(va["net_pnl"], 2),
                "max_drawdown": va["max_drawdown"],
                "expectancy": va["expectancy"],
                "expectancy_r": va["expectancy_r"],
                "profit_factor": va["profit_factor"],
                "num_trades": va["trades"],
                "win_rate": va["win_rate"],
                "sharpe": va["sharpe"],
                "sortino": va["sortino"],
                "calmar": va["calmar"],
                "exposure_time": va["exposure_time"],
                "capital_exposure_cond": va["capital_exposure_cond"],
                "avg_mae": va["avg_mae"],
                "avg_mfe": va["avg_mfe"],
                "mfe_mae_ratio": va["mfe_mae_ratio"],
                "time_exit_win_rate": _time_exit_win_rate(res),
                "delta_vs_neutral_pct": round(
                    w["contrasts"]["delta_breakout_vs_neutral_ret"] * 100, 4),
                "delta_vs_bh_scaled_pct": round(
                    w["contrasts"]["delta_breakout_vs_bh_scaled_ret"] * 100, 4),
            },
            "regimen": [
                {k: r[k] for k in ("regime", "trades", "win_rate", "profit_factor", "net_pnl")}
                for r in w["regimes"]
            ],
            "benchmarks": {
                "bh": _bench(w["benchmarks"]["bh"]),
                "bh_scaled": _bench(w["benchmarks"]["bh_scaled"]),
                "neutral": _bench(w["benchmarks"]["neutral"]),
                "bh_scaled_frac": w["benchmarks"]["bh_scaled_frac"],
                "neutral_target_frac": w["benchmarks"]["neutral_target_frac"],
            },
            "clasificacion_de_ventana": "POSITIVA" if net_return_pct >= 0 else "NEGATIVA",
        }
        rows.append(m)

        print(
            f"  {label}: net {m['metricas']['net_return_pct']:+7.2f}%  "
            f"trades {m['metricas']['num_trades']:>3}  PF {m['metricas']['profit_factor']}  "
            f"dd {m['metricas']['max_drawdown'] or 0.0:>7.1%}  "
            f"dvs-neutral {m['metricas']['delta_vs_neutral_pct']:+6.2f}pp  "
            f"{m['clasificacion_de_ventana']}"
        )

    agregado = _verdict(rows)
    rets = [r["metricas"]["net_return_pct"] / 100 for r in rows]
    print("\n" + "  ".join(f"{r['metricas']['net_return_pct']:+7.2f}" for r in rows))
    print(
        f"agregado: positivas {agregado['count_folds_positivos']}/12  "
        f"vecindades negativas {agregado['num_vecindades_negativas']}  "
        f"media {statistics.mean(rets) * 100:+.2f}%  mediana {statistics.median(rets) * 100:+.2f}%"
    )
    print(f"VEREDICTO: {agregado['clasificacion']}")
    print(f"  localizado {agregado['evaluacion']['localizado']}")
    print(f"  sistematico {agregado['evaluacion']['sistematico']}")

    result = {
        "hypothesis": "H004-B: la inestabilidad temporal observada en H004 (W2/W3)"
                      " es LOCALIZADA en su vecindad o SISTEMATICA a traves de "
                      "ventanas trimestrales re-ancladas.",
        "method": "Diagnostico de estrategia fija x 12 ventanas trimestrales "
                  "consecutivas re-ancladas (2021-04-01 -> 2024-04-01), cold-start "
                  "por ventana, sin recalibracion ni seleccion. Esquema, criterios "
                  "y configuracion congelados en H004B_PRE_REGISTRATION.json.",
        "lineage": {
            "hypothesis_id": "H004-B",
            "parent_hypothesis": "H004",
            "strategy_id": cfg["strategy"]["strategy_id"],
            "params": cfg["strategy"]["params"],
            "strategy_config_hash": params_hash,
            "parameters_changed": False,
            "optimization_performed": False,
            "scheme_frozen": True,
            "criteria_frozen": True,
            "previous_oos_consumed": roles.previous_oos_consumed,
            "final_oos_consumed": roles.final_oos_consumed,
            "final_oos_status": evidence.final_oos_status.value,
        },
        "pre_registration_snapshot": prereg,
        "folds": rows,
        "agregado": agregado,
        "conclusion": (
            f"H004-B -> {agregado['clasificacion']}. Folds positivos "
            f"{agregado['count_folds_positivos']}/12, vecindades negativas "
            f"{agregado['num_vecindades_negativas']}. Diagnostico de ubicacion; "
            f"NO modifica la estrategia, NO abre H006 y NO desbloquea el FINAL_OOS."
        ),
    }
    dest = EXPERIMENTS / "outputs" / "h004b_diagnosis.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    RESULT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nguardado en: {dest}")
    print(f"guardado en: {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())