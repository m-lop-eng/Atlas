"""E13 — PAPER/FORWARD :: Reproduccion continua de la estrategia congelada en paper.

`papertrading/forward_runner.py` orquesta la cadena B1-B7 consumiendo
SOLO ClosedBarEvent, con audit trail por barra y persistence idempotente.
Este runner sirve de tele-cmd para la Fase A (smoke 1-2h) y el forward 24h/7d:

  1) Modo CSV (--csv): reproduce un segmento OHLC histórico por el runner y
     genera outputs/forward/<run_id>/ (bars.jsonl, records.ndjson,
     audit.jsonl, snapshot.json, manifest.json). Determinista y reproducible.
  2) Modo live (--live): NO se implementa aqui (el feed real de B1 lo decide
     la operacion); sin --csv este script valida el stack e imprime el estado.

Guardas: la config es la CONGELADA (FINAL_OOS / H002, hash 491ed76d...), el
riesgo operativo es el de config/paper/settings.yaml (<= 1%) y el broker es
PaperBrokerAdapter. Nada de esto toca evidence.json / data_roles.json.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml  # noqa: F401  (mantiene el patron de e12 para extender configs)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.loader import read_ohlc_csv  # noqa: E402
from live.events import ClosedBarEvent  # noqa: E402
from papertrading.forward_runner import (  # noqa: E402
    FROZEN_STRATEGY_HASH,
    build_forward_runner,
)

OUT_BASE = Path(__file__).resolve().parent / "forward"


def _csv_to_events(csv_path: Path, interval: int, limit: int | None) -> list[ClosedBarEvent]:
    bars = read_ohlc_csv(csv_path)
    events: list[ClosedBarEvent] = []
    for i, bar in enumerate(bars):
        open_time = int(bar["ts"])
        if open_time % interval != 0:
            raise SystemExit(
                f"[e13] barra {open_time} no alineada a {interval}s en {csv_path}."
            )
        events.append(
            ClosedBarEvent(
                symbol="BTCUSDT",
                open_time=open_time,
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar.get("volume", 0.0)),
                interval_seconds=interval,
                emission_sequence=len(events) + 1,
            )
        )
        if limit is not None and len(events) >= limit:
            break
    return events


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E13 paper/forward (H002 congelada)")
    ap.add_argument("--csv", help="CSV OHLC para reproducir en paper")
    ap.add_argument("--run-id", default="FORWARD-PAPER-01")
    ap.add_argument("--out-dir", default=str(OUT_BASE))
    ap.add_argument("--limit", type=int, default=None, help="max barras a procesar")
    ap.add_argument("--interval", type=int, default=3600, help="intervalo en segundos (BTCUSDT 1h)")
    ap.add_argument("--initial-cash", type=float, default=None)
    ap.add_argument("--data-quality", default="HEALTHY")
    args = ap.parse_args(argv)

    print(f"E13 — paper/forward (strategy_hash {FROZEN_STRATEGY_HASH[:12]}...)")
    print(f"guard: config CONGELADA (final_oos) + riesgo paper <= 1% + PaperBrokerAdapter\n")

    runner = build_forward_runner(
        run_id=args.run_id,
        output_dir=args.out_dir,
        initial_cash=args.initial_cash,
    ).start()

    if not args.csv:
        print(f"rama listo (runs/replays en {runner.output_dir}).")
        print("Sin --csv no hay datos: pasa --csv <ohlc.csv> para reproducir la Fase A.")
        return 0

    csv_path = Path(args.csv)
    events = _csv_to_events(csv_path, args.interval, args.limit)
    if not events:
        print("[e13] sin barras en el rango del CSV.")
        return 1
    print(f"input ................... {csv_path} ({len(events)} barras, {_iso(events[0].open_time)} -> {_iso(events[-1].open_time)})")

    at = None
    for e in events:
        at = datetime.fromtimestamp(e.close_time, tz=timezone.utc)
        runner.on_closed_bar(e, data_quality=args.data_quality, at=at)

    equity = runner.engine.equity_curve
    print(f"procesadas .............. {len(runner.records)} barras (solo ClosedBarEvent)")
    print(f"trades .................. {len(runner.engine.trades)}")
    print(f"equity final ............ {equity[-1][1]:,.2f}" if equity else "equity final ............ n/d")
    print(f"status monitor .......... {runner.records[-1].monitoring_overall if runner.records else 'n/a'}")
    print(f"reconciliacion .......... {runner.records[-1].reconciliation_status if runner.records else 'n/a'}")
    print(f"outputs ................. {runner.output_dir}")
    print("  bars.jsonl / records.ndjson / audit.jsonl / snapshot.json / manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())