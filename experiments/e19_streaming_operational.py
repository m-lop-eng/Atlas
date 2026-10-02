"""E19 — S8 :: operacion paper streaming con backfill REST oportuno.

Driver INDEPENDIENTE de S7 (no modifica e18). Reutiliza `StreamingForwardRunner`
(S7, sin cambios) y le inyecta un `BackfillProvider` basado en el REST publico
read-only de Binance (`RestStreamingBackfillProvider`), que solo alimenta a
`IntegrityReconciler.reconcile_open_ended` (S4). NO crea ni emite
`ClosedBarEvent`: B1 (`LiveDataEngine`) sigue siendo la unica autoridad.

Camino unico:

    Binance WebSocket -> S2/S3 -> B1 -> ClosedBarEvent -> ForwardRunner
    Binance REST klines -> RestStreamingBackfillProvider -> S4 (solo reconcile)

Paper-only, read-only market data, sin credenciales, sin ordenes, sin broker
real, sin scheduler. Namespace separado de A4 y de S7.

Modos:
  --once   (default) : una sesion acotada (--max-events/--max-polls) y salir.
  --loop             : sesiones acotadas repetidas (--poll-seconds) hasta
                       Ctrl-C o --cycles. Sin scheduler.
  --status           : solo lectura; imprime el informe actual (sin lock).

D3: no se modifica ni refactoriza e18/S7; se reutilizan sus componentes
publicos (`StreamingInstanceLock`, `AlreadyRunningError`, `build_report`).

Acotacion operacional (S8-C2-POLLDUR-001):
  * `--cycle-timeout-seconds` es un presupuesto SOFT: se comprueba SOLO entre
    `poll_once()`; NO interrumpe una lectura de socket en curso. Un unico
    `poll_once()` puede seguir bloqueando hasta `recv_timeout` (+frames) y una
    reconciliacion REST su propio timeout HTTP.
  * Si el presupuesto se agota, el ciclo se cierra de forma controlada y se
    registra `status="budget_exhausted"` + `completed=false` + incidente
    `CYCLE_BUDGET_EXHAUSTED`. NUNCA se cuenta como ciclo completado.
  * No existe un "modo rapido": no se omite replay, warm-start, S4 ni REST.
  * El incidente de presupuesto lo emite el PROPIO ciclo; una interrupcion
    externa del proceso (kill/timeout de la herramienta) no genera ese
    incidente, porque no ejecuta el cierre del ciclo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.binance_rest import (  # noqa: E402
    BINANCE_KLINES_PATH,
    BINANCE_REST_BASE,
    BinanceRestKlinesSource,
)
from experiments.e18_streaming_forward import (  # noqa: E402
    AlreadyRunningError,
    StreamingInstanceLock,
    build_report,
)
from papertrading.forward_runner import (  # noqa: E402
    FROZEN_CONFIG_HASH,
    FROZEN_STRATEGY_HASH,
    ForwardConfigError,
    build_forward_runner,
)
from papertrading.streaming_backfill_rest import (  # noqa: E402
    RestStreamingBackfillProvider,
)
from papertrading.streaming_forward import (  # noqa: E402
    DEFAULT_MAX_POLLS,
    DEFAULT_POLL_MAX_EVENTS,
    DEFAULT_POLL_SECONDS,
    S7_INCIDENTS,
    S7_LOCK,
    S7_OPS_LOG,
    S7_PROVENANCE,
    S7_REPORT,
    SeedError,
    StreamingForwardRunner,
)
from streaming import (  # noqa: E402
    BackoffPolicy,
    BinanceStreamingAdapter,
    ReconnectManager,
    ReconnectPolicy,
    StreamingHealthMonitor,
    interval_to_binance,
)

S8_RUN_ID = "FORWARD-STREAM-OPS-B1"
S8_OUT_ROOT = ROOT / "experiments" / "streaming_operational" / "outputs"

_UTC = timezone.utc


# --------------------------------------------------------------- factories
# Se exponen para que los tests inyecten dobles sin red.


def build_rest_source(args: argparse.Namespace) -> BinanceRestKlinesSource:
    return BinanceRestKlinesSource(
        symbol=args.symbol, interval_seconds=args.interval_seconds
    )


def build_backfill_provider(args: argparse.Namespace) -> RestStreamingBackfillProvider:
    return RestStreamingBackfillProvider(build_rest_source(args))


def build_streaming_adapter(args: argparse.Namespace):
    return BinanceStreamingAdapter(
        recv_timeout_seconds=args.recv_timeout,
        max_control_frames=args.max_control_frames,
    )


def build_reconnect_manager(args: argparse.Namespace, adapter):
    return ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
        heartbeat_timeout_seconds=args.heartbeat_timeout,
        pong_timeout_seconds=args.pong_timeout,
        poll_interval_seconds=args.poll_interval,
    )


# --------------------------------------------------------------- utilidades


def _now(args: argparse.Namespace) -> datetime:
    if getattr(args, "now", None):
        return datetime.fromisoformat(args.now).replace(tzinfo=_UTC)
    return datetime.now(_UTC)


def _monotonic() -> float:
    """Reloj monotono para medir el presupuesto del ciclo (no de pared)."""
    return time.monotonic()


def _append_ndjson(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        fh.flush()


def _incident(out_dir: Path, kind: str, detail: dict, at: datetime) -> dict:
    rec = {"at": at.isoformat(), "kind": kind, **detail}
    _append_ndjson(out_dir / S7_INCIDENTS, rec)
    return rec


def _write_report(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / S7_REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _print_report(report: dict) -> None:
    print(f"run ................... {report['_meta']['run_id']}")
    print(f"origen ................ {report['_meta']['source']}")
    print(f"ciclos ................ {report['ciclos']['total']}")
    print(
        f"barras ................ {report['ejecucion']['barras_totales']} "
        f"({report['ejecucion']['primera_barra']} -> {report['ejecucion']['ultima_barra']})"
    )
    print(f"reconciliacion ........ {report['reconciliacion']['status']}")
    print(f"kill switch ........... {report['riesgo']['kill_switch_estados']}")
    print(f"monitoring ............ {report['monitoring']['overall']}")
    print(f"equity ................ {report['equity']['equity_final']} | mdd {report['equity']['mdd']}")
    print(f"integridad ............ {report['integridad']}")
    print(f"health (S6) ........... {report['health']['lineas']} snapshots")
    print(
        f"incidentes ............ {report['incidentes']['por_tipo']} "
        f"(total {report['incidentes']['total']})"
    )
    print(f"ultimo ciclo .......... {report['ciclos']['ultimo']}")


def _startup_failure(
    out_dir: Path,
    now_dt: datetime,
    *,
    kind: str,
    flag: str,
    exc: BaseException,
) -> dict:
    """Fallo de arranque controlado: incidente + informe, sin traceback."""
    inc = _incident(
        out_dir, kind, {"error": str(exc), "error_type": type(exc).__name__}, now_dt
    )
    report = build_report(out_dir, now_dt)
    _write_report(out_dir, report)
    return {
        "locked": False,
        "cycle": {"at": now_dt.isoformat(), flag: True, "reason": str(exc)},
        "report": report,
        "incidents": [inc],
    }


def _write_provenance_s8(
    out_dir: Path,
    args: argparse.Namespace,
    manager: ReconnectManager,
    seed,
) -> None:
    """Provenance S8 (sobrescribe la de S7, que asume backfill binance.vision).

    Se escribe en `streaming_manifest.json` (mismo nombre de artefacto, namespace
    S8 separado). No modifica ningun archivo de S7.
    """
    payload = {
        "run_id": args.run_id,
        "created_at": datetime.now(_UTC).isoformat(),
        "data_source": "binance-websocket",
        "streaming_adapter": "BinanceStreamingAdapter",
        "provider_name": manager.provider_name,
        "symbol": args.symbol,
        "interval_seconds": args.interval_seconds,
        "binance_interval": interval_to_binance(args.interval_seconds),
        "backfill": {
            "source": "binance-rest-klines",
            "host": BINANCE_REST_BASE,
            "endpoint": BINANCE_KLINES_PATH,
            "auth": "none",
            "policy": "reconcile_open_ended / IntegrityBackfillProvider",
            "error_translation": "DataFetchError -> ConnectionError",
            "on_unrecoverable": "BLOCKED/UNRECOVERABLE -> B1 DEGRADED, sin emision",
        },
        "integrity": {
            "authority": "LiveDataEngine (B1)",
            "clears_on": "IntegrityReport.ok (HEALTHY)",
            "warm_start": "silencio (no emite, no observa S6)",
        },
        "namespace": {
            "out_dir": str(out_dir),
            "run_id": args.run_id,
            "artifacts": [
                "bars.jsonl",
                "records.ndjson",
                "audit.jsonl",
                "snapshot.json",
                "manifest.json",
                S7_PROVENANCE,
                "streaming_health.ndjson",
                S7_OPS_LOG,
                S7_INCIDENTS,
                S7_REPORT,
            ],
        },
        "strategy": {
            "hash": FROZEN_STRATEGY_HASH,
            "config_hash": FROZEN_CONFIG_HASH,
        },
        "seed": {
            "present": seed is not None,
            "open_time": seed.open_time if seed else None,
            "emission_sequence": seed.emission_sequence if seed else None,
        },
    }
    path = out_dir / S7_PROVENANCE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    tmp.replace(path)


# --------------------------------------------------------------- ciclo


def run_cycle(args: argparse.Namespace) -> dict:
    """Una sesion acotada de S8 (idempotente y aislada de A4/S7)."""
    out_root = Path(args.out_root)
    out_dir = out_root / args.run_id
    now_dt = _now(args)

    lock = StreamingInstanceLock(out_dir / S7_LOCK)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        return {
            "locked": True,
            "cycle": {"at": now_dt.isoformat(), "locked": True, "reason": str(exc)},
            "report": None,
            "incidents": [],
        }

    try:
        runner = build_forward_runner(run_id=args.run_id, output_dir=out_root)
        adapter = build_streaming_adapter(args)
        manager = build_reconnect_manager(args, adapter)
        provider = build_backfill_provider(args)
        monitor = StreamingHealthMonitor()
        sfr = StreamingForwardRunner(
            forward_runner=runner,
            symbol=args.symbol,
            interval_seconds=args.interval_seconds,
            manager=manager,
            backfill_provider=provider,
            monitor=monitor,
        )
        try:
            sfr.start()
        except ForwardConfigError as exc:
            return _startup_failure(
                out_dir,
                now_dt,
                kind="INPUT_FINGERPRINT_MISMATCH",
                flag="fingerprint_mismatch",
                exc=exc,
            )
        except SeedError as exc:
            return _startup_failure(
                out_dir, now_dt, kind="SEED_INVALID", flag="seed_invalid", exc=exc
            )
        except ConnectionError as exc:
            return _startup_failure(
                out_dir,
                now_dt,
                kind="STREAM_CONNECT_ERROR",
                flag="connect_error",
                exc=exc,
            )
        except Exception as exc:  # noqa: BLE001 - arranque controlado
            return _startup_failure(
                out_dir, now_dt, kind="STARTUP_ERROR", flag="startup_error", exc=exc
            )

        _write_provenance_s8(out_dir, args, manager, sfr.seed)

        # Ciclo acotado por presupuesto SOFT (comprobado entre poll_once()).
        # NUNCA se omite replay/warm-start/S4/REST: solo se decide si seguir.
        started_at = _monotonic()
        budget = args.cycle_timeout_seconds
        deadline = None if budget is None else started_at + budget
        processed = 0
        polls = 0
        budget_exhausted = False
        try:
            while polls < args.max_polls and processed < args.max_events:
                if deadline is not None and _monotonic() >= deadline:
                    budget_exhausted = True
                    break
                processed += sfr.poll_once()
                polls += 1
            # Capturar estado ANTES de close(): close() deja B1 en DISCONNECTED.
            last_report = sfr.pipeline.backfill.last_report
            engine_state = sfr.pipeline.engine.state.value
            engine_quality = sfr.pipeline.engine.quality.value
            closed_bars = len(sfr.pipeline.closed_bars)
        finally:
            sfr.checkpoint()
            sfr.close()
        start_seconds = round(_monotonic() - started_at, 3)

        cycle = {
            "processed": processed,
            "polls": polls,
            "emitted": sfr.emitted,
            "deduped": sfr.deduped,
            "closed_bars": closed_bars,
            "engine_state": engine_state,
            "engine_quality": engine_quality,
            "backfill_status": last_report.status.value if last_report else None,
            "restart": sfr.seed is not None,
            "status": "budget_exhausted" if budget_exhausted else "completed",
            "completed": not budget_exhausted,
            "start_seconds": start_seconds,
        }

        incidents: list[dict] = []
        if budget_exhausted:
            incidents.append(
                _incident(
                    out_dir,
                    "CYCLE_BUDGET_EXHAUSTED",
                    {
                        "cycle_timeout_seconds": budget,
                        "processed": processed,
                        "polls": polls,
                        "max_polls": args.max_polls,
                        "max_events": args.max_events,
                    },
                    now_dt,
                )
            )
        if last_report is not None and not last_report.ok:
            incidents.append(
                _incident(
                    out_dir,
                    f"BACKFILL_{last_report.status.value}",
                    {"reason": last_report.reason},
                    now_dt,
                )
            )
        if cycle["engine_state"] == "DEGRADED":
            incidents.append(
                _incident(
                    out_dir,
                    "GAP_NO_RECONCILIADO",
                    {"state": cycle["engine_state"], "quality": cycle["engine_quality"]},
                    now_dt,
                )
            )

        cycle["at"] = now_dt.isoformat()
        _append_ndjson(out_dir / S7_OPS_LOG, cycle)

        report = build_report(out_dir, now_dt)
        _write_report(out_dir, report)
        return {
            "locked": False,
            "cycle": cycle,
            "report": report,
            "incidents": incidents,
        }
    finally:
        lock.release()


# --------------------------------------------------------------- main


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S8 forward streaming operacional (REST backfill read-only)"
    )
    ap.add_argument("--run-id", default=S8_RUN_ID)
    ap.add_argument("--out-root", default=str(S8_OUT_ROOT))
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval-seconds", type=int, default=3600)
    ap.add_argument("--max-events", type=int, default=DEFAULT_POLL_MAX_EVENTS)
    ap.add_argument("--max-polls", type=int, default=DEFAULT_MAX_POLLS)
    ap.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    ap.add_argument("--cycles", type=int, default=0, help="0 = sin limite (loop)")
    ap.add_argument("--once", action="store_true", help="una sesion acotada (default)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", default=None, help="ISO UTC (tests deterministas)")
    # Acotacion operacional (S8-C2-POLLDUR-001). Defaults = comportamiento actual.
    ap.add_argument(
        "--cycle-timeout-seconds",
        type=float,
        default=None,
        help="Presupuesto SOFT del ciclo (entre poll_once); None = sin limite",
    )
    ap.add_argument("--recv-timeout", type=float, default=5.0)
    ap.add_argument("--max-control-frames", type=int, default=10)
    ap.add_argument("--heartbeat-timeout", type=float, default=30.0)
    ap.add_argument("--pong-timeout", type=float, default=10.0)
    ap.add_argument("--poll-interval", type=float, default=0.5)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    out_dir = Path(args.out_root) / args.run_id

    if args.status:
        report = build_report(out_dir, _now(args))
        _print_report(report)
        return 0

    if args.loop:
        return _run_loop(args)

    result = run_cycle(args)
    if result.get("locked"):
        print(f"[{result['cycle']['at']}] SKIP: {result['cycle']['reason']}")
        return 0
    cycle = result["cycle"]
    if (
        cycle.get("fingerprint_mismatch")
        or cycle.get("seed_invalid")
        or cycle.get("connect_error")
        or cycle.get("startup_error")
    ):
        print(f"[{cycle['at']}] fail-safe (sin procesar): {cycle}")
        _print_report(result["report"])
        return 1
    print(
        f"[{cycle['at']}] procesadas={cycle['processed']} emitidas={cycle['emitted']} "
        f"dedup={cycle['deduped']} estado={cycle['engine_state']} "
        f"backfill={cycle['backfill_status']} incidentes={len(result['incidents'])}"
    )
    _print_report(result["report"])
    return 0


def _run_loop(args: argparse.Namespace) -> int:
    cycles = 0
    try:
        while args.cycles <= 0 or cycles < args.cycles:
            result = run_cycle(args)
            if result.get("locked"):
                print(f"SKIP: {result['cycle']['reason']}")
            else:
                cycle = result["cycle"]
                print(
                    f"[{cycle['at']}] procesadas={cycle.get('processed')} "
                    f"estado={cycle.get('engine_state')} "
                    f"incidentes={len(result['incidents'])}"
                )
            cycles += 1
            if args.cycles <= 0 or cycles < args.cycles:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("\nS8: parada solicitada; estado persistido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
