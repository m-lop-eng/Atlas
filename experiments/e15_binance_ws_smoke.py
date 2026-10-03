"""E15 — S2 :: Smoke read-only del adaptador Binance WebSocket.

Prueba de integración MANUAL (no forma parte de la suite determinista): conecta
al stream público de klines de Binance, suscribe un símbolo/intervalo, imprime
los `StreamEvent` recibidos y cierra limpiamente. SOLO LECTURA: no hay claves API
ni ninguna vía de órdenes.

Requiere la dependencia opcional: ``pip install "atlas[streaming]"``.

Uso:
    python experiments/e15_binance_ws_smoke.py --seconds 20 --max-events 5
    python experiments/e15_binance_ws_smoke.py --symbol BTCUSDT --interval 60
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
    BinanceStreamingAdapter,
    StreamData,
    StreamingError,
)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S2 smoke read-only Binance WebSocket")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=60, help="intervalo en segundos")
    ap.add_argument("--seconds", type=float, default=20.0, help="duración máxima")
    ap.add_argument("--max-events", type=int, default=5)
    ap.add_argument("--base-url", default=BINANCE_WS_BASE)
    args = ap.parse_args(argv)

    print("E15 — S2 smoke read-only (Binance WebSocket; sin claves, sin órdenes)")
    print(f"endpoint ......... {args.base_url}/ws")
    print(f"suscripción ...... {args.symbol} @ {args.interval}s\n")

    adapter = BinanceStreamingAdapter(base_url=args.base_url)
    received = 0
    deadline = time.monotonic() + args.seconds
    try:
        adapter.connect()
        adapter.subscribe(args.symbol, args.interval)
        print(f"estado ........... {adapter.state.value}")
        while received < args.max_events and time.monotonic() < deadline:
            data = adapter.receive()
            if data is None:
                continue
            assert isinstance(data, StreamData)
            ev = data.event
            flag = "CERRADA" if ev.is_closed else "intrabar"
            print(
                f"[{received + 1}] {flag:8} {ev.symbol} {_iso(ev.open_time)} "
                f"O={ev.open} H={ev.high} L={ev.low} C={ev.close} V={ev.volume}"
            )
            received += 1
    except StreamingError as exc:
        print(f"ERROR streaming .. {type(exc).__name__}: {exc}")
        return 1
    finally:
        adapter.disconnect()
        print(f"\ndesconectado ...... estado {adapter.state.value}")
    print(f"eventos recibidos . {received}")
    return 0 if received > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
