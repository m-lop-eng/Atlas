"""Tests deterministas del driver S8 (experiments/e19_streaming_operational).

Sin red: se monkeypatchean las factories (adapter/manager/backfill) con dobles
scriptados. Cubren gap recuperable/no recuperable, continuidad de
emission_sequence, anti-bypass, S6 observacional, restart/idempotencia,
aislamiento A4, --loop acotado, shutdown limpio, lock, determinismo, --status
read-only y source-scan sin credenciales/ordenes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from experiments import e19_streaming_operational as e19
from live.adapter import BackfillProvider
from live.events import MarketDataEvent
from streaming import (
    BackoffPolicy,
    ReconnectManager,
    ReconnectPolicy,
    ScriptedStreamingAdapter,
    StreamData,
    StreamingConnectionError,
    StreamingMarketDataAdapter,
)

BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"
FIXED_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _market(open_time: int, c: float = 1.5) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=SYMBOL,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=c,
        volume=1.0,
        is_closed=True,
    )


def _sdata(open_time: int) -> StreamData:
    return StreamData(received_at=FIXED_NOW, event=_market(open_time))


class _ListBackfill(BackfillProvider):
    def __init__(self, bars=None) -> None:
        self._bars = list(bars or [])
        self.calls: list[tuple[str, int, int]] = []

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        self.calls.append((symbol, interval_seconds, start_open_time))
        return [b for b in self._bars if b.open_time >= start_open_time]


class _CountingAdapter(StreamingMarketDataAdapter):
    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self._connect_error = connect_error
        self.disconnect_calls = 0
        self._state = "DISCONNECTED"
        self._subscription: tuple[str, int] | None = None

    @property
    def provider_name(self) -> str:
        return "counting"

    @property
    def state(self):  # type: ignore[override]
        return self._state

    @property
    def subscription(self):
        return self._subscription

    def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self._state = "CONNECTED"

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._state = "CLOSED"

    def subscribe(self, symbol, interval_seconds) -> None:
        if self._state != "CONNECTED":
            raise StreamingConnectionError("no conectado")
        self._subscription = (symbol, interval_seconds)

    def receive(self):
        return None


def _scripted_manager(args, adapter):
    return ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
        clock=lambda: 0.0,
        wall_clock=lambda: FIXED_NOW,
        sleep=lambda _s: None,
    )


def _args(tmp_path: Path, run_id: str, **overrides) -> argparse.Namespace:
    base = dict(
        run_id=run_id,
        out_root=str(tmp_path),
        symbol=SYMBOL,
        interval_seconds=HOUR,
        max_events=10,
        max_polls=2,
        poll_seconds=0.0,
        cycles=0,
        once=True,
        loop=False,
        status=False,
        now="2026-09-01T00:00:00",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch(monkeypatch, events, backfill_bars, *, adapter=None):
    def _adapter_factory(args):
        return adapter if adapter is not None else ScriptedStreamingAdapter(
            events=list(events), clock=lambda: FIXED_NOW
        )

    monkeypatch.setattr(e19, "build_streaming_adapter", _adapter_factory)
    monkeypatch.setattr(e19, "build_reconnect_manager", _scripted_manager)
    monkeypatch.setattr(
        e19, "build_backfill_provider", lambda args: _ListBackfill(backfill_bars)
    )


def _read_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _hash_dir(root: Path) -> dict:
    if not root.exists():
        return {}
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


# --------------------------------------------------------------- casos


def test_gap_recuperable_y_secuencia(tmp_path: Path, monkeypatch) -> None:
    events = [_sdata(BASE), _sdata(BASE + 3 * HOUR)]
    backfill = [_market(BASE + HOUR), _market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    _patch(monkeypatch, events, backfill)

    result = e19.run_cycle(_args(tmp_path, "S8-GAPR"))

    assert result["locked"] is False
    assert result["cycle"]["engine_state"] == "CONNECTED"
    assert result["cycle"]["closed_bars"] == 4
    records = _read_ndjson(tmp_path / "S8-GAPR" / "records.ndjson")
    assert [int(r["bar_open_time"]) for r in records] == [
        BASE,
        BASE + HOUR,
        BASE + 2 * HOUR,
        BASE + 3 * HOUR,
    ]
    assert [r["emission_sequence"] for r in records] == [1, 2, 3, 4]


def test_gap_no_recuperable(tmp_path: Path, monkeypatch) -> None:
    events = [_sdata(BASE), _sdata(BASE + 3 * HOUR)]
    _patch(monkeypatch, events, [])

    result = e19.run_cycle(_args(tmp_path, "S8-GAPN"))

    assert result["cycle"]["engine_state"] == "DEGRADED"
    assert result["cycle"]["closed_bars"] == 1
    kinds = [i["kind"] for i in result["incidents"]]
    assert "BACKFILL_UNRECOVERABLE" in kinds
    records = _read_ndjson(tmp_path / "S8-GAPN" / "records.ndjson")
    assert [int(r["bar_open_time"]) for r in records] == [BASE]


def test_anti_bypass_duplicado_no_duplica(tmp_path: Path, monkeypatch) -> None:
    events = [_sdata(BASE), _sdata(BASE), _sdata(BASE + HOUR)]
    _patch(monkeypatch, events, [])

    e19.run_cycle(_args(tmp_path, "S8-AB"))
    records = _read_ndjson(tmp_path / "S8-AB" / "records.ndjson")
    assert [int(r["bar_open_time"]) for r in records] == [BASE, BASE + HOUR]


def test_anti_bypass_estructural() -> None:
    for rel in (
        "data/binance_rest.py",
        "papertrading/streaming_backfill_rest.py",
        "experiments/e19_streaming_operational.py",
    ):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert "StreamData" not in src, rel


def test_s6_observacional(tmp_path: Path, monkeypatch) -> None:
    _patch(monkeypatch, [_sdata(BASE)], [])
    result = e19.run_cycle(_args(tmp_path, "S8-S6"))
    records = _read_ndjson(tmp_path / "S8-S6" / "records.ndjson")
    assert all(r["kill_switch"] == "ALLOW" for r in records)
    assert all(r["monitoring_overall"] == "HEALTHY" for r in records)
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "data/binance_rest.py",
        "papertrading/streaming_backfill_rest.py",
        "experiments/e19_streaming_operational.py",
    ):
        src = (root / rel).read_text(encoding="utf-8")
        assert "apply_to_kill_switch" not in src
        assert "KillSwitch" not in src


def test_restart_idempotencia(tmp_path: Path, monkeypatch) -> None:
    events = [_sdata(BASE), _sdata(BASE + 3 * HOUR)]
    backfill = [_market(BASE + HOUR), _market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    _patch(monkeypatch, events, backfill)

    e19.run_cycle(_args(tmp_path, "S8-RESTART"))
    e19.run_cycle(_args(tmp_path, "S8-RESTART"))

    records = _read_ndjson(tmp_path / "S8-RESTART" / "records.ndjson")
    opens = [int(r["bar_open_time"]) for r in records]
    assert opens == [BASE, BASE + HOUR, BASE + 2 * HOUR, BASE + 3 * HOUR]
    assert len(opens) == len(set(opens))  # sin duplicados


def test_aislamiento_a4(tmp_path: Path, monkeypatch) -> None:
    a4 = Path(__file__).resolve().parents[1] / "experiments" / "forward" / "outputs"
    before = _hash_dir(a4)

    _patch(monkeypatch, [_sdata(BASE)], [])
    e19.run_cycle(_args(tmp_path, "S8-ISO"))

    assert _hash_dir(a4) == before


def test_loop_acotado(tmp_path: Path, monkeypatch) -> None:
    _patch(monkeypatch, [_sdata(BASE)], [])
    rc = e19.main(
        [
            "--loop",
            "--cycles",
            "2",
            "--poll-seconds",
            "0",
            "--run-id",
            "S8-LOOP",
            "--out-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    ops = _read_ndjson(tmp_path / "S8-LOOP" / e19.S7_OPS_LOG)
    assert len(ops) == 2


def test_shutdown_limpio_y_lock(tmp_path: Path, monkeypatch) -> None:
    adapter = _CountingAdapter()
    _patch(monkeypatch, [], [], adapter=adapter)

    result = e19.run_cycle(_args(tmp_path, "S8-SHUT"))
    assert result["locked"] is False
    assert adapter.disconnect_calls >= 1  # shutdown limpio (disconnect)

    lock = e19.StreamingInstanceLock(tmp_path / "S8-SHUT" / e19.S7_LOCK)
    lock.acquire()
    lock.release()


def test_lock_segunda_instancia(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "S8-LOCK"
    lock = e19.StreamingInstanceLock(out_dir / e19.S7_LOCK)
    lock.acquire()
    try:
        result = e19.run_cycle(_args(tmp_path, "S8-LOCK"))
    finally:
        lock.release()
    assert result.get("locked") is True
    assert result["report"] is None


def test_determinismo(tmp_path: Path, monkeypatch) -> None:
    events = [_sdata(BASE), _sdata(BASE + 3 * HOUR)]
    backfill = [_market(BASE + HOUR), _market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    _patch(monkeypatch, events, backfill)
    e19.run_cycle(_args(tmp_path / "a", "S8-DET"))
    e19.run_cycle(_args(tmp_path / "b", "S8-DET"))
    a = (tmp_path / "a" / "S8-DET" / "records.ndjson").read_text(encoding="utf-8")
    b = (tmp_path / "b" / "S8-DET" / "records.ndjson").read_text(encoding="utf-8")
    assert a == b


def test_status_read_only(tmp_path: Path, monkeypatch) -> None:
    rc = e19.main(
        ["--status", "--run-id", "S8-STATUS", "--out-root", str(tmp_path)]
    )
    assert rc == 0
    assert not (tmp_path / "S8-STATUS" / e19.S7_LOCK).exists()


def test_source_scan_sin_credenciales_ni_ordenes() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = [
        r"api[_-]?key",
        r"secret",
        r"signature",
        r"/api/v3/order",
        r"place_order",
        r"create_order",
        r"withdraw",
    ]
    for rel in (
        "data/binance_rest.py",
        "papertrading/streaming_backfill_rest.py",
        "experiments/e19_streaming_operational.py",
    ):
        src = (root / rel).read_text(encoding="utf-8").lower()
        for pattern in forbidden:
            import re

            assert re.search(pattern, src) is None, f"{rel}: {pattern}"
