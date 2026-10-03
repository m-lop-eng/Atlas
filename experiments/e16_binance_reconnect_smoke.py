"""E16 — S3 :: Smoke read-only de reconexión sobre Binance WebSocket.

Prueba de integración MANUAL (no forma parte de la suite determinista): conecta
al stream público de klines de Binance a través del `ReconnectManager`, fuerza
una caída de transporte (`inner.disconnect()`) y verifica que el manager
reconecta y re-suscribe sin intervención. SOLO LECTURA: sin claves API ni vías
de órdenes.

Requiere la dependencia opcional: ``pip install "atlas[streaming]"``.

Uso:
    python experiments/e16_binance_reconnect_smoke.py --seconds 30 --max-events 6
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

from streaming import (  # noqa: E402
    BINANCE_WS_BASE,
    BackoffPolicy,
    BinanceStreamingAdapter,
    ReconnectManager,
    ReconnectPolicy,
    ReconnectState,
    StreamConnected,
    StreamData,
    StreamDisconnected,
    StreamingError,
)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S3 smoke read-only reconexión Binance")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--max-events", type=int, default=6)
    ap.add_argument("--drop-after", type=int, default=2, help="eventos antes de forzar caída")
    ap.add_argument("--base-url", default=BINANCE_WS_BASE)
    args = ap.parse_args(argv)

    print("E16 — S3 smoke read-only reconexión (Binance; sin claves, sin órdenes)")
    inner = BinanceStreamingAdapter(base_url=args.base_url)
    manager = ReconnectManager(
        inner,
        policy=ReconnectPolicy(
            max_attempts=4, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
    )

    data_events = 0
    reconnects = 0
    dropped = False
    deadline = time.monotonic() + args.seconds
    try:
        manager.connect()
        manager.subscribe(args.symbol, args.interval)
        print(f"estado inicial .... {manager.state.value}\n")
        while data_events < args.max_events and time.monotonic() < deadline:
            event = manager.receive()
            if isinstance(event, StreamDisconnected):
                reconnects += 1
                print(f"[reconexión] {event.reason} -> reconectando...")
            elif isinstance(event, StreamConnected):
                print(f"[conexión] establecida; estado {manager.state.value}")
            elif isinstance(event, StreamData):
                ev = event.event
                flag = "CERRADA" if ev.is_closed else "intrabar"
                print(
                    f"[{data_events + 1}] {flag:8} {ev.symbol} {_iso(ev.open_time)} "
                    f"C={ev.close} V={ev.volume}"
                )
                data_events += 1
                if not dropped and data_events >= args.drop_after:
                    print("    (forzando caída de transporte...)")
                    inner.disconnect()
                    dropped = True
    except StreamingError as exc:
        print(f"ERROR streaming .. {type(exc).__name__}: {exc}")
        return 1
    finally:
        manager.disconnect()
        print(f"\ncerrado ........... estado {manager.state.value}")
    print(f"eventos .......... {data_events}")
    print(f"reconexiones ..... {reconnects}")
    print(f"integridad ....... {'DESCONOCIDA (requiere S4)' if manager.data_integrity_unknown else 'sin incidencias'}")
    if not dropped or reconnects == 0:
        print("aviso ............ no se ejerció una reconexión real")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
