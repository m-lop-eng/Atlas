"""E18 — S7 :: driver Streaming Paper/Forward (WebSocket -> B1 -> ForwardRunner).

Driver operativo del forward streaming, aislado de A4 (batch). NO introduce
funcionalidad de producto: solo compone `StreamingForwardRunner`
(`StreamingPipeline` + `ForwardRunner`) y persiste un informe operativo propio.

Camino unico de datos:

    Binance WebSocket -> S2/S3/S4 -> B1 LiveDataEngine -> ClosedBarEvent
        -> ForwardRunner (paper, PaperBrokerAdapter)

Reglas:
  * sin scheduler/deployment (se lanza a mano con `--once/--loop/--status`);
  * S6 (`StreamingHealthMonitor`) solo observa (se persiste, no autoriza);
  * `DataFetchError` del backfill -> `ConnectionError` (S4 BLOCKED, fail-safe);
  * namespace separado de A4: `experiments/streaming/outputs/<run_id>`;
  * shutdown limpio: `pipeline.disconnect()` + `runner.checkpoint()`.

Modos:
  --once   (default) : una sesion acotada (--max-events/--max-polls) y salir.
  --loop             : sesiones acotadas repetidas (--poll-seconds) hasta
                       Ctrl-C o --cycles.
  --status           : solo lectura; imprime el informe actual (sin lock).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.sources import BinanceVisionDailySource  # noqa: E402
from papertrading.forward_runner import (  # noqa: E402
    ForwardConfigError,
    build_forward_runner,
)
from papertrading.streaming_forward import (  # noqa: E402
    DEFAULT_MAX_POLLS,
    DEFAULT_POLL_MAX_EVENTS,
    DEFAULT_POLL_SECONDS,
    S7_HEALTH_LOG,
    S7_INCIDENTS,
    S7_LOCK,
    S7_OPS_LOG,
    S7_OUT_ROOT,
    S7_PROVENANCE,
    S7_REPORT,
    S7_RUN_ID,
    SeedError,
    StreamingBackfillProvider,
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

LOCAL_TZ = timezone.utc


class AlreadyRunningError(RuntimeError):
    """Otra instancia de S7 mantiene el lock de este directorio de estado."""


class StreamingInstanceLock:
    """Lock de instancia unica sobre el directorio de estado de S7.

    Usa un lock de fichero del SO (msvcrt/fcntl): el SO lo libera al morir el
    proceso, por lo que un proceso muerto no deja un lock stale. NO se borra el
    fichero: nunca se elimina el lock de otra instancia.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = None

    @staticmethod
    def _lock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> "StreamingInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fh.seek(0)
            self._lock(fh)
        except OSError as exc:
            fh.close()
            raise AlreadyRunningError(
                f"otra instancia de S7 activa sobre {self.path}"
            ) from exc
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} token={uuid.uuid4().hex}\n")
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "StreamingInstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# --------------------------------------------------------------- factories
# Se exponen para que los tests inyecten dobles sin red.


def build_streaming_adapter(args: argparse.Namespace):
    return BinanceStreamingAdapter()


def build_reconnect_manager(args: argparse.Namespace, adapter):
    return ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
    )


def build_backfill_provider(args: argparse.Namespace):
    source = BinanceVisionDailySource(
        symbol=args.symbol, interval=interval_to_binance(args.interval_seconds)
    )
    return StreamingBackfillProvider(source)


# --------------------------------------------------------------- utilidades


def _now(args: argparse.Namespace) -> datetime:
    if getattr(args, "now", None):
        return datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _read_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_ndjson(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        fh.flush()


def _incident(out_dir: Path, kind: str, detail: dict, at: datetime) -> dict:
    rec = {"at": at.isoformat(), "kind": kind, **detail}
    _append_ndjson(out_dir / S7_INCIDENTS, rec)
    return rec


# --------------------------------------------------------------- informe


def build_report(out_dir: Path, now_dt: datetime) -> dict:
    """Informe operativo de S7, SOLO LECTURA (no reconstruye ni regenera)."""
    records = _read_ndjson(out_dir / "records.ndjson")
    ops = _read_ndjson(out_dir / S7_OPS_LOG)
    incidents = _read_ndjson(out_dir / S7_INCIDENTS)
    bars = _read_ndjson(out_dir / "bars.jsonl")
    health = _read_ndjson(out_dir / S7_HEALTH_LOG)

    recon = dict(Counter(r.get("reconciliation_status") for r in records))
    kill = dict(Counter(r.get("kill_switch") for r in records))
    monitoring = dict(Counter(r.get("monitoring_overall") for r in records))
    subs = dict(
        Counter(r.get("submission_status") for r in records if r.get("submission_status"))
    )
    unknown_open = sum(1 for r in records if r.get("submission_status") == "UNKNOWN")

    manifest_path = out_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    output_files = manifest.get("output_files", {})
    files_ok = bool(output_files) and all(
        (out_dir / name).exists() for name in output_files.values()
    )
    integrity_ok = files_ok and len(bars) == len(records)

    equity = [(r["bar_open_time"], r["equity"]) for r in records]
    peak = -1e18
    mdd = 0.0
    for _t, eq in equity:
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    mdd = round(mdd, 6) if equity else None

    return {
        "_meta": {
            "run_id": out_dir.name,
            "generated_at": now_dt.isoformat(),
            "source": "binance-websocket",
            "modo": "streaming forward continuo (S7; B1 -> ForwardRunner)",
            "strategy_hash": records[-1]["strategy_hash"] if records else None,
            "manifest_presente": manifest_path.exists(),
        },
        "ciclos": {
            "total": len(ops),
            "ultimo": ops[-1] if ops else None,
        },
        "ejecucion": {
            "barras_totales": len(records),
            "primera_barra": records[0]["timestamp"] if records else None,
            "ultima_barra": records[-1]["timestamp"] if records else None,
        },
        "riesgo": {
            "kill_switch_estados": kill,
            "barras_can_trade_false": sum(1 for r in records if not r.get("can_trade")),
        },
        "reconciliacion": {
            "status": recon,
            "bars_blocked": recon.get("RECONCILIATION_BLOCKED", 0),
        },
        "monitoring": {"overall": monitoring},
        "barras": {
            "senales": dict(Counter(r.get("signal") for r in records)),
            "submission_status": subs,
        },
        "equity": {
            "equity_final": round(records[-1]["equity"], 6) if records else None,
            "mdd": mdd,
        },
        "unknown": {"abiertos": unknown_open},
        "integridad": {
            "bars_igual_records": len(bars) == len(records),
            "manifest_outputs_presentes": files_ok,
            "ok": files_ok and integrity_ok,
        },
        "health": {"ultimo": health[-1] if health else None, "lineas": len(health)},
        "incidentes": {
            "total": len(incidents),
            "por_tipo": dict(Counter(i["kind"] for i in incidents)),
            "ultimos": incidents[-5:],
        },
    }


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
    print(f"UNKNOWN abiertos ...... {report['unknown']['abiertos']}")
    print(f"integridad ............ {report['integridad']}")
    print(f"health (S6) ........... {report['health']['lineas']} snapshots")
    print(f"incidentes ............ {report['incidentes']['por_tipo']} (total {report['incidentes']['total']})")
    print(f"ultimo ciclo .......... {report['ciclos']['ultimo']}")


# --------------------------------------------------------------- ciclo


def _startup_failure(
    out_dir: Path,
    now_dt: datetime,
    *,
    kind: str,
    flag: str,
    exc: BaseException,
) -> dict:
    """Fallo de arranque controlado: incidente + informe, sin traceback.

    No oculta el fallo: registra el tipo y el mensaje del error; el driver
    `main` devuelve codigo != 0. El cleanup (disconnect/checkpoint) ya lo
    garantiza `StreamingForwardRunner.start()`.
    """
    inc = _incident(
        out_dir,
        kind,
        {"error": str(exc), "error_type": type(exc).__name__},
        now_dt,
    )
    report = build_report(out_dir, now_dt)
    _write_report(out_dir, report)
    return {
        "locked": False,
        "cycle": {"at": now_dt.isoformat(), flag: True, "reason": str(exc)},
        "report": report,
        "incidents": [inc],
    }


def run_cycle(args: argparse.Namespace) -> dict:
    """Una sesion acotada de S7 (idempotente y aislada de A4)."""
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

        try:
            cycle = sfr.run_bounded(
                max_events=args.max_events, max_polls=args.max_polls
            )
        finally:
            sfr.close()

        incidents: list[dict] = []
        last_report = sfr.pipeline.backfill.last_report
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
                    {
                        "state": cycle["engine_state"],
                        "quality": cycle["engine_quality"],
                    },
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


def _write_report(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / S7_REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


# --------------------------------------------------------------- main


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S7 forward streaming (WebSocket -> B1 -> ForwardRunner)"
    )
    ap.add_argument("--run-id", default=S7_RUN_ID)
    ap.add_argument("--out-root", default=str(S7_OUT_ROOT))
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
        print("\nS7: parada solicitada; estado persistido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
