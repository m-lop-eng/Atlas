"""E17 — S5 :: Smoke read-only del pipeline Streaming -> B1.

Prueba de integración MANUAL (no forma parte de la suite determinista): conecta
a Binance, empuja el stream a través de S2/S3 y lo entrega a `LiveDataEngine`
(B1), que emite `ClosedBarEvent`. SOLO LECTURA: sin claves API ni órdenes; no
hay estrategia ni broker.

Requiere la dependencia opcional: ``pip install "atlas[streaming]"``.

Uso:
    python experiments/e17_streaming_pipeline_smoke.py --seconds 30 --interval 1
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.adapter import BackfillProvider  # noqa: E402

from streaming import (  # noqa: E402
    BINANCE_WS_BASE,
    BackoffPolicy,
    BinanceStreamingAdapter,
    ReconnectManager,
    ReconnectPolicy,
    ReconnectState,
    StreamingPipeline,
)


class _NoBackfill(BackfillProvider):
    """Sin backfill (el smoke no provoca huecos); nunca autoriza recuperación."""

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        return []


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S5 smoke read-only stream -> B1")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=1, help="segundos (1 = 1s)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--max-bars", type=int, default=3)
    args = ap.parse_args(argv)

    print("E17 — S5 smoke read-only stream -> B1 (Binance; sin claves, sin órdenes)")
    manager = ReconnectManager(
        BinanceStreamingAdapter(base_url=BINANCE_WS_BASE),
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
    )
    pipeline = StreamingPipeline(
        symbol=args.symbol,
        interval_seconds=args.interval,
        manager=manager,
        provider=_NoBackfill(),
        stale_timeout_seconds=60.0,
    )

    deadline = time.monotonic() + args.seconds
    try:
        pipeline.connect()
        print(f"suscripción ...... {args.symbol} @ {args.interval}s\n")
        while len(pipeline.closed_bars) < args.max_bars and time.monotonic() < deadline:
            pipeline.poll(max_events=1)
            if manager.state is not ReconnectState.CONNECTED:
                break
    except Exception as exc:  # noqa: BLE001 - smoke de integración
        print(f"ERROR pipeline ... {type(exc).__name__}: {exc}")
        return 1
    finally:
        pipeline.disconnect()

    print("ClosedBarEvent (B1):")
    for bar in pipeline.closed_bars:
        print(f"  {_iso(bar.open_time)}  C={bar.close}  seq={bar.emission_sequence}")
    print(f"\nbarras emitidas .. {len(pipeline.closed_bars)}")
    print(f"estado B1 ........ {pipeline.engine.state.value}")
    return 0 if pipeline.closed_bars else 1


if __name__ == "__main__":
    raise SystemExit(main())
