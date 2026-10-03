"""E14 — A4 FORWARD CONTINUO :: ingesta incremental programada (ops, no producto).

Un ciclo:
  1. reconstruye el ForwardRunner desde su bars.jsonl (resumible e idempotente),
  2. obtiene SOLO las velas 1h nuevas publicadas por el proveedor real
     (BinanceVisionDailySource; ~1 dia de retraso de publicacion),
  3. las enruta por B1 (LiveDataEngine + ReplayMarketDataAdapter como cola
     appendable + BackfillProvider sobre la misma fuente real) hacia el runner,
  4. acumula audit / records / snapshot / manifest y emite informe operativo
     + log de incidentes.

No introduce funcionalidad de producto ni cambia B1/B3/B7: solo orquesta
componentes existentes. Separacion estricta operational evidence != strategy
evidence: los resultados se registran, jamas se usan para recalibrar H002.

Modos:
  --once   (default) : un ciclo y salir (para Task Scheduler).
  --loop             : ciclos continuos cada --poll-seconds.
  --status           : no ingiere; imprime el informe operativo actual.
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

from data.sources import BinanceVisionDailySource, DataFetchError  # noqa: E402
from live.adapter import BackfillProvider  # noqa: E402
from live.engine import EngineState, LiveDataEngine  # noqa: E402
from live.events import MarketDataEvent  # noqa: E402
from papertrading.forward_runner import (  # noqa: E402
    ForwardConfigError,
    build_forward_runner,
)
from papertrading.replay import ReplayMarketDataAdapter  # noqa: E402

SYMBOL = "BTCUSDT"
INTERVAL = 3600
OUT_ROOT = ROOT / "experiments" / "forward" / "outputs"
LOCK_NAME = ".forward.lock"


class AlreadyRunningError(RuntimeError):
    """Otra instancia de A4 mantiene el lock de este directorio de estado."""


class ForwardInstanceLock:
    """Lock de instancia unica sobre el directorio de estado de A4.

    Usa un lock de fichero del SO (msvcrt en Windows / fcntl en POSIX): el SO
    libera el lock al morir el proceso, por lo que un proceso muerto NO deja un
    lock stale que requiera limpieza manual. NO se elimina el fichero de lock:
    asi JAMAS se borra un lock perteneciente a otra instancia.

    Se libera con `release()` (o como context manager). `--status` no lo usa.
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

    def acquire(self) -> "ForwardInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fh.seek(0)
            self._lock(fh)
        except OSError as exc:
            fh.close()
            raise AlreadyRunningError(
                f"otra instancia de A4 activa sobre {self.path}"
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

    def __enter__(self) -> "ForwardInstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _bar_to_event(bar: dict) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=SYMBOL,
        open_time=int(bar["ts"]),
        open=float(bar["open"]),
        high=float(bar["high"]),
        low=float(bar["low"]),
        close=float(bar["close"]),
        volume=float(bar.get("volume", 0.0)),
        is_closed=True,
    )


def _row_to_event(row: dict) -> MarketDataEvent:
    b = row["bar"]
    return MarketDataEvent(
        symbol=b["symbol"],
        open_time=int(b["open_time"]),
        open=float(b["open"]),
        high=float(b["high"]),
        low=float(b["low"]),
        close=float(b["close"]),
        volume=float(b["volume"]),
        is_closed=True,
    )


class VisionBackfill(BackfillProvider):
    """Backfill de velas cerradas desde la misma fuente real (data.binance.vision)."""

    def __init__(self, source: BinanceVisionDailySource) -> None:
        self.source = source

    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        end = int(time.time()) + 2 * interval_seconds
        bars = self.source.fetch(start_open_time, end)
        return [
            _bar_to_event(b)
            for b in bars
            if int(b["ts"]) >= start_open_time and int(b["ts"]) % interval_seconds == 0
        ]


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_ndjson(path: Path) -> list[dict]:
    return _read_rows(path)


def _append_ndjson(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        fh.flush()


def _incident(out_dir: Path, kind: str, detail: dict, at: datetime) -> dict:
    rec = {"at": at.isoformat(), "kind": kind, **detail}
    _append_ndjson(out_dir / "incidents.ndjson", rec)
    return rec


def run_cycle(args: argparse.Namespace) -> dict:
    out_root = Path(args.out_root)
    out_dir = out_root / args.run_id
    now_dt = (
        datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc)
        if args.now
        else datetime.now(timezone.utc)
    )
    now = int(now_dt.timestamp())

    lock = ForwardInstanceLock(out_dir / LOCK_NAME)
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
        try:
            runner.start()
        except ForwardConfigError as exc:
            inc = _incident(
                out_dir, "INPUT_FINGERPRINT_MISMATCH", {"error": str(exc)}, now_dt
            )
            report = build_report(args, out_dir, now_dt)
            (out_dir / "report_a4.json").write_text(
                json.dumps(report, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            return {
                "locked": False,
                "cycle": {
                    "at": now_dt.isoformat(),
                    "fingerprint_mismatch": True,
                    "reason": str(exc),
                    "incidentes": 1,
                },
                "report": report,
                "incidents": [inc],
            }

        n0 = len(runner.records)
        persisted = _read_rows(runner._bars_path)
        last_open = max((int(r["bar"]["open_time"]) for r in persisted), default=None)
        restarted = bool(persisted)

        if last_open is None:
            start = now - args.seed_days * 24 * INTERVAL
        else:
            start = last_open + INTERVAL
        source = BinanceVisionDailySource(symbol=SYMBOL, interval="1h")

        new_bars: list[dict] = []
        fetch_error: str | None = None
        try:
            new_bars = [
                b for b in source.fetch(start, now + INTERVAL) if int(b["ts"]) >= start
            ]
        except DataFetchError as exc:
            fetch_error = str(exc)

        events = [_row_to_event(r) for r in persisted] + [
            _bar_to_event(b) for b in new_bars
        ]
        adapter = ReplayMarketDataAdapter(events)
        counters = {"emitted": 0, "delivered": 0, "deduped": 0}

        def on_closed(closed) -> None:
            counters["emitted"] += 1
            rec = runner.on_closed_bar(
                closed,
                data_quality="HEALTHY",
                at=datetime.fromtimestamp(closed.close_time, tz=timezone.utc),
            )
            if rec is None:
                counters["deduped"] += 1
            else:
                counters["delivered"] += 1

        engine = LiveDataEngine(
            SYMBOL,
            INTERVAL,
            adapter,
            stale_timeout_seconds=86_400.0,
            backfill_provider=VisionBackfill(source),
            on_closed_bar=on_closed,
            clock=time.time,
        )
        engine.start()
        engine.poll(max_events=len(events) + 10)
        reconcile_rounds = 0
        while engine.state == EngineState.DEGRADED and reconcile_rounds < 3:
            try:
                engine.reconcile()
            except (DataFetchError, ConnectionError) as exc:
                _incident(out_dir, "BACKFILL_FALLO", {"error": str(exc)}, now_dt)
                break
            engine.poll(max_events=len(events) + 10)
            reconcile_rounds += 1

        new_records = [r.to_dict() for r in runner.records[n0:]]

        incidents = []
        if fetch_error:
            incidents.append(
                _incident(out_dir, "PROVIDER_ERROR", {"error": fetch_error}, now_dt)
            )
        if engine.state == EngineState.DEGRADED:
            incidents.append(
                _incident(
                    out_dir,
                    "GAP_NO_RECONCILIADO",
                    {
                        "state": engine.state.value,
                        "quality": engine.quality.value,
                        "issues": [list(i) for i in engine.issues[-5:]],
                    },
                    now_dt,
                )
            )
        for rec in new_records:
            if rec["reconciliation_status"] == "RECONCILIATION_BLOCKED":
                incidents.append(
                    _incident(
                        out_dir,
                        "RECONCILIATION_BLOCKED",
                        {"bar": rec["timestamp"], "diffs": rec["reconciliation_diffs"]},
                        now_dt,
                    )
                )
            if rec["kill_switch"] != "ALLOW":
                incidents.append(
                    _incident(
                        out_dir,
                        "KILL_SWITCH_ACTIVO",
                        {
                            "bar": rec["timestamp"],
                            "state": rec["kill_switch"],
                            "can_trade": rec["can_trade"],
                        },
                        now_dt,
                    )
                )
            if rec["submission_status"] == "UNKNOWN":
                incidents.append(
                    _incident(out_dir, "ORDEN_UNKNOWN", {"bar": rec["timestamp"]}, now_dt)
                )

        # manifest identifica exactamente el input/estado en reposo de este ciclo
        runner.checkpoint()

        cycle = {
            "at": now_dt.isoformat(),
            "restart": restarted,
            "last_open_before": last_open,
            "seed_start": _iso(start),
            "new_bars": len(new_bars),
            "engine_state": engine.state.value,
            "engine_quality": engine.quality.value,
            "emitidas_b1": counters["emitted"],
            "procesadas": counters["delivered"],
            "deduplicadas": counters["deduped"],
            "reconcile_rounds": reconcile_rounds,
            "provider_error": fetch_error,
            "incidentes": len(incidents),
        }
        _append_ndjson(out_dir / "ops_log.ndjson", cycle)
        if new_records:
            _append_ndjson(
                out_dir / "new_records.ndjson",
                {
                    "at": now_dt.isoformat(),
                    "count": len(new_records),
                    "bars": [r["timestamp"] for r in new_records],
                },
            )
        report = build_report(args, out_dir, now_dt)
        (out_dir / "report_a4.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return {
            "locked": False,
            "cycle": cycle,
            "report": report,
            "incidents": incidents,
        }
    finally:
        lock.release()


def build_report(args: argparse.Namespace, out_dir: Path, now_dt: datetime) -> dict:
    records = _read_ndjson(out_dir / "records.ndjson")
    ops = _read_ndjson(out_dir / "ops_log.ndjson")
    incidents = _read_ndjson(out_dir / "incidents.ndjson")
    bars = _read_ndjson(out_dir / "bars.jsonl")

    recon = dict(Counter(r.get("reconciliation_status") for r in records))
    kill = dict(Counter(r.get("kill_switch") for r in records))
    monitoring = dict(Counter(r.get("monitoring_overall") for r in records))
    subs = dict(Counter(r.get("submission_status") for r in records if r.get("submission_status")))

    kill_episodes = 0
    prev = "ALLOW"
    for r in records:
        if r.get("kill_switch") != "ALLOW" and prev == "ALLOW":
            kill_episodes += 1
        prev = r.get("kill_switch")
    unknown_open = sum(1 for r in records if r.get("submission_status") == "UNKNOWN")
    order_through_blocked = sum(
        1 for r in records
        if r.get("kill_switch") != "ALLOW" and r.get("submission_status") == "FILLED"
    )

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    files_ok = all((out_dir / f).exists() for f in manifest.get("output_files", {}).values())
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
            "run_id": args.run_id,
            "generated_at": now_dt.isoformat(),
            "provider": "BinanceVisionDailySource (binance.vision daily klines 1h)",
            "strategy_hash": records[-1]["strategy_hash"] if records else None,
            "modo": "forward continuo (ingesta incremental; B1 -> B2-B7)",
        },
        "ciclos": {
            "total": len(ops),
            "reinicios": sum(1 for o in ops if o.get("restart")),
            "ultimo": ops[-1] if ops else None,
            "proveedor_errores": sum(1 for o in ops if o.get("provider_error")),
            "gaps_detectados_b1": sum(1 for o in ops if o.get("engine_state") == "DEGRADED"),
            "reconcile_rounds_total": sum(o.get("reconcile_rounds", 0) for o in ops),
        },
        "ejecucion": {
            "barras_totales": len(records),
            "primera_barra": records[0]["timestamp"] if records else None,
            "ultima_barra": records[-1]["timestamp"] if records else None,
            "procesadas_total": sum(o.get("procesadas", 0) for o in ops),
            "deduplicadas_total": sum(o.get("deduplicadas", 0) for o in ops),
        },
        "riesgo": {
            "kill_switch_estados": kill,
            "episodios_activacion": kill_episodes,
            "barras_can_trade_false": sum(1 for r in records if not r.get("can_trade")),
            "ordenes_atravesando_bloqueo": order_through_blocked,
        },
        "reconciliacion": {
            "status": recon,
            "bars_blocked": recon.get("RECONCILIATION_BLOCKED", 0),
            "bars_con_diffs": sum(1 for r in records if r.get("reconciliation_diffs", 0) > 0),
        },
        "monitoring": {"overall": monitoring},
        "barras": {"senales": dict(Counter(r.get("signal") for r in records)),
                   "submission_status": subs},
        "equity": {
            "equity_final": round(records[-1]["equity"], 6) if records else None,
            "mdd": mdd,
        },
        "posiciones": {
            "position_final": (records[-1]["position"] if records else None),
            "reconciliacion_posicion": (
                records[-1]["reconciliation_status"] if records else None
            ),
        },
        "audit": {
            "lineas_audit": len(_read_ndjson(out_dir / "audit.jsonl")),
        },
        "unknown": {"abiertos": unknown_open},
        "integridad": {
            "manifest_outputs_presentes": files_ok,
            "bars_igual_records": len(bars) == len(records),
            "ok": integrity_ok,
        },
        "incidentes": {"total": len(incidents), "por_tipo": dict(Counter(i["kind"] for i in incidents)),
                       "ultimos": incidents[-5:]},
    }


def _print_report(report: dict) -> None:
    print(f"run ................... {report['_meta']['run_id']}")
    print(f"ciclos ................ {report['ciclos']['total']} (reinicios {report['ciclos']['reinicios']})")
    print(f"barras ................ {report['ejecucion']['barras_totales']} "
          f"({report['ejecucion']['primera_barra']} -> {report['ejecucion']['ultima_barra']})")
    print(f"procesadas/dedup ...... {report['ejecucion']['procesadas_total']} / {report['ejecucion']['deduplicadas_total']}")
    print(f"reconciliacion ........ {report['reconciliacion']['status']}")
    print(f"kill switch ........... {report['riesgo']['kill_switch_estados']} (episodios {report['riesgo']['episodios_activacion']})")
    print(f"monitoring ............ {report['monitoring']['overall']}")
    print(f"equity ................ {report['equity']['equity_final']} | mdd {report['equity']['mdd']}")
    print(f"UNKNOWN abiertos ...... {report['unknown']['abiertos']}")
    print(f"integridad ............ {report['integridad']}")
    print(f"incidentes ............ {report['incidentes']['por_tipo']} (total {report['incidentes']['total']})")
    print(f"ultimo ciclo .......... {report['ciclos']['ultimo']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A4 forward continuo (ingesta incremental)")
    ap.add_argument("--run-id", default="FORWARD-PAPER-A4")
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--seed-days", type=int, default=12,
                    help="historial inicial solo si el run no existe aun")
    ap.add_argument("--poll-seconds", type=int, default=3600)
    ap.add_argument("--once", action="store_true", help="un solo ciclo (default)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--now", default=None, help="ISO UTC para tests deterministas")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_root) / args.run_id
    if args.status:
        report = build_report(args, out_dir, datetime.now(timezone.utc))
        _print_report(report)
        return 0

    while True:
        result = run_cycle(args)
        if result.get("locked"):
            print(f"[{result['cycle']['at']}] SKIP: {result['cycle']['reason']}")
            return 0
        c = result["cycle"]
        if c.get("fingerprint_mismatch"):
            print(
                f"[{c['at']}] INPUT FINGERPRINT MISMATCH -> fail-safe (sin procesar): "
                f"{c['reason']}"
            )
            _print_report(result["report"])
            return 1
        print(
            f"[{c['at']}] restart={c['restart']} nuevas={c['new_bars']} "
            f"b1={c['emitidas_b1']} procesadas={c['procesadas']} dedup={c['deduplicadas']} "
            f"estado={c['engine_state']} incidentes={c['incidentes']}"
        )
        if not args.loop:
            _print_report(result["report"])
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())