"""E12 — FINAL_OOS :: Protocolo de evaluacion unica con datos nuevos (>= 2026-09-01).

`research/final_oos_pipeline.py` + `research/decisions/FINAL_OOS_PRE_REGISTRATION.json`
fijan ANTES de ver numeros: rango, metricas, benchmarks, criterios y guards.

SEMANTICA DEL DESBLOQUEO (congelada): la evaluacion se autoriza UNICAMENTE cuando el
dataset de datos nuevos cubre [start_epoch, start_epoch + min_span) con
start_epoch=1788220800 (2026-09-01T00:00Z) y min_span=31536000 (365 dias), es decir
hasta >= 2027-09-01T00:00Z. LA APARICION DE UN ZIP MENSUAL CONCRETO (p. ej.
BTCUSDT-1h-2026-09.zip) NO DESBLOQUEA EL FINAL_OOS: monthly/daily son solo mecanismos
de adquisicion; la unidad relevante es el dataset congelado (CSV + manifest sha256).
Hasta que el manifest no cubra [2026-09-01, 2027-09-01), el estado permanece
BLOCKED_BY_DATA_AVAILABILITY (matriz verificada en tests/test_final_oos_semantics.py).

Este runner tiene DOS modos:

1) Preparacion (sin --authorize): valida el estado real del sistema y, si recibe
   --csv/--manifest, ejecuta TODO el protocolo sobre COPIES temporales de los
   registros (outputs/dry_run_*) para NO mutar evidence.json / data_roles.json.
   Sin --csv/--manifest imprime solo el status (lo que corresponde hacer mientras el
   dataset nuevo no cubra [2026-09-01, 2027-09-01)).

2) Autorizacion (--csv/--manifest --authorize): ejecuta el protocolo REAL con
   consumo irreversible (CONSUMED) y persiste evidence.json / data_roles.json.
   Requiere que el pre-registro este en estado AUTHORIZED o RUN_IN_PROGRESS
   (transicion FROZEN -> AUTHORIZED es una decision humana explicita) y el flag
   explicito --yes-consume.

Nada de lo que hace este runner depende de los numeros del OOS (criterios null
de falsacion). El reporte final se guarda en experiments/outputs/ y la decision
(con el pre-registro embebido como snapshot) en research/decisions/FINAL_OOS_RESULT.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.final_oos_pipeline import (
    FinalOosError,
    derive_end,
    frozen_strategy_matches,
    load_manifest,
    load_plan,
    run_final_oos,
    validate_manifest,
    validate_quality,
)
from research.data_role import RoleRegistry
from research.evidence import EvidenceRegistry
from data.loader import read_ohlc_csv

EXPERIMENTS = Path(__file__).resolve().parent
PLAN_PATH = ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
CONFIG_PATH = ROOT / "config" / "final_oos" / "config.yaml"
EVIDENCE_PATH = ROOT / "research" / "evidence.json"
ROLES_PATH = ROOT / "research" / "data_roles.json"
OUT_REPORT = EXPERIMENTS / "outputs" / "final_oos_report.json"
OUT_DECISION = ROOT / "research" / "decisions" / "FINAL_OOS_RESULT.json"
DRY_DIR = EXPERIMENTS / "outputs" / "final_oos_dry_run"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _print_status(plan: dict, cfg: dict) -> None:
    evidence = EvidenceRegistry.load_default()
    roles = RoleRegistry.load_default()
    status = evidence.final_oos_status
    print(f"FINAL_OOS status ......... {status.value}")
    print(f"observed_through ......... {_iso(evidence.dataset.observed_through)}")
    print(f"minimum_start ............ {_iso(evidence.dataset.final_oos.minimum_start)}")
    print(f"plan estado .............. {plan['estado']} (ciclo: {plan['ciclo_estado']})")
    print(f"final_oos_consumed ....... {roles.final_oos_consumed}")
    print(f"previous_oos_consumed .... {roles.previous_oos_consumed}")

    locked = True
    try:
        frozen_strategy_matches(cfg, plan)
    except FinalOosError as exc:
        locked = False
        print(f"config congelada .......... NO ({exc})")
    if locked:
        print("config congelada .......... SI (hash params + config + efectivos OK)")


def _print_readiness(plan: dict, cfg: dict, csv_path: Path, manifest: dict) -> None:
    evidence = EvidenceRegistry.load_default()
    start = int(plan["esquema"]["start_epoch"])
    interval = int(plan["esquema"]["interval_seconds"])
    end = derive_end(plan["esquema"], manifest)
    span_days = (end - start) / 86_400
    print(f"dataset ................... {csv_path}")
    print(f"manifest bars ............. {manifest.get('bars')} | sha256 {str(manifest.get('sha256'))[:12]}...")
    print(f"rango derivado ............ {_iso(start)} -> {_iso(end)} ({span_days:.0f} dias)")

    quality = validate_quality(
        read_ohlc_csv(csv_path), start_epoch=start, end_epoch=end, interval_seconds=interval
    )
    integrity = validate_manifest(
        csv_path, manifest,
        start_epoch=start, end_epoch=end, interval_seconds=interval,
        bars=read_ohlc_csv(csv_path),
    )
    span_ok = (end - start) >= int(plan["esquema"]["min_span_seconds"])
    enough = quality["ok"] and integrity["ok"] and span_ok

    print(f"span >= 365 dias .......... {'SI' if span_ok else 'NO'}")
    print(f"calidad (issues) .......... {len(quality['issues'])} -> {'OK' if quality['ok'] else quality['issues'][:1]}")
    print(f"manifest (checks) ......... {integrity['checks'] or 'parcial'}")
    print(f"estado minimo_start ....... {'cumple' if start >= (evidence.dataset.final_oos.minimum_start or 0) else 'NO'}")

    if enough:
        print("READY -> el dataset cubre [start, start+min_span). Pendiente de autorizacion humana.")
    else:
        print("NOT_READY -> FINAL_OOS permanece BLOCKED_BY_DATA_AVAILABILITY por calendario/calidad.")


def _dry_run(plan: dict, cfg: dict, args) -> int:
    """Ejecuta el protocolo completo sobre copias temporales (nada REAL se muta)."""
    csv_path = Path(args.csv)
    manifest = load_manifest(args.manifest)
    evidence = EvidenceRegistry.load_default()
    roles = RoleRegistry.load_default()

    DRY_DIR.mkdir(parents=True, exist_ok=True)
    ev_copy = DRY_DIR / "evidence.json"
    roles_copy = DRY_DIR / "data_roles.json"
    report_copy = DRY_DIR / "final_oos_report.json"
    shutil.copyfile(EVIDENCE_PATH, ev_copy)
    shutil.copyfile(ROLES_PATH, roles_copy)

    print("[dry-run] registros copiados a outputs/final_oos_dry_run/ -> los REALES no se tocan")
    report = run_final_oos(
        plan, cfg,
        csv_path=csv_path,
        manifest_path=args.manifest,
        evidence=evidence,
        roles=roles,
        evidence_path=ev_copy,
        roles_path=roles_copy,
        out_path=report_copy,
        consume=True,
    )
    primarios = report["reading"]["primarios"]
    print(f"DRY CONCLUSIÓN: {report['reading']['conclusion']} | {primarios}")
    print(f"evidencia dry (CONSUMED sobre copias): {ev_copy}")
    print("LOS REGISTROS REALES SIGUEN INTACTOS (verificar con --status)")
    return 0


def _authorize(plan: dict, cfg: dict, args) -> int:
    if plan["estado"] not in ("AUTHORIZED", "RUN_IN_PROGRESS"):
        raise SystemExit(
            f"[e12] plan en estado {plan['estado']}: se requiere AUTHORIZED/RUN_IN_PROGRESS. "
            "La transicion FROZEN -> AUTHORIZED es una decision humana explicita."
        )
    if not args.yes_consume:
        raise SystemExit(
            "[e12] consumo irreversible. Confirma con --yes-consume."
        )

    csv_path = Path(args.csv)
    manifest = load_manifest(args.manifest)
    evidence = EvidenceRegistry.load_default()
    roles = RoleRegistry.load_default()

    report = run_final_oos(
        plan, cfg,
        csv_path=csv_path,
        manifest_path=args.manifest,
        evidence=evidence,
        roles=roles,
        evidence_path=EVIDENCE_PATH,
        roles_path=ROLES_PATH,
        out_path=OUT_REPORT,
        consume=True,
    )

    primarios = report["reading"]["primarios"]
    print("\n".join(f"  {k}: {v}" for k, v in primarios.items()))
    print(f"CONCLUSION: {report['reading']['conclusion']}")
    print(f"FINAL_OOS marcado CONSUMED en {EVIDENCE_PATH} y {ROLES_PATH} (irreversible)")

    decision = {
        "id": "FINAL_OOS_RESULT",
        "hypothesis": plan["pregunta"],
        "method": report["method"],
        "lineage": report["lineage"],
        "pre_registration_snapshot": plan,
        "report": report,
        "conclusion": (
            f"{report['reading']['conclusion']} — lectura single-shot segun "
            "FINAL_OOS_PRE_REGISTRATION.json. La hipotesis madre sigue "
            "ALLOWED_TRANSITIONS de evidence.py."
        ),
    }
    OUT_DECISION.write_text(json.dumps(decision, indent=2, default=str), encoding="utf-8")
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    print(f"reporte: {OUT_REPORT}")
    print(f"decision: {OUT_DECISION}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E12 FINAL_OOS (protocolo pre-registrado)")
    ap.add_argument("--csv", help="dataset OOS (CSV OHLC) — obligatorio para preparar/autorizar")
    ap.add_argument("--manifest", help="manifest del dataset (JSON)")
    ap.add_argument("--dry-run", action="store_true", help="protocolo completo sobre copias temporales")
    ap.add_argument("--authorize", action="store_true", help="ejecucion REAL con consumo irreversible")
    ap.add_argument("--yes-consume", action="store_true", help="confirma el consumo irreversible")
    args = ap.parse_args(argv)

    plan = load_plan(PLAN_PATH)
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    if not args.csv or not args.manifest:
        print("E12 — RESTAURAR: sin dataset no hay FINAL_OOS (requiere cobertura de datos nuevos [2026-09-01, 2027-09-01)).\n")
        _print_status(plan, cfg)
        if args.csv or args.manifest:
            raise SystemExit("[e12] se requiere --csv Y --manifest juntos")
        return 0

    _print_status(plan, cfg)
    _print_readiness(plan, cfg, Path(args.csv), load_manifest(args.manifest))

    if args.authorize:
        return _authorize(plan, cfg, args)
    return _dry_run(plan, cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())