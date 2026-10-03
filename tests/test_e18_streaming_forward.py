"""Tests deterministas del driver S7 (experiments/e18_streaming_forward).

Sin red: las factories de adapter/manager/backfill se monkeypatchean por dobles
scriptados. Se verifican: status solo-lectura, lock de instancia, sesion --once
acotada y liberacion de lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from experiments import e18_streaming_forward as e18
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


def _market(open_time: int) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=SYMBOL,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1.0,
        is_closed=True,
    )


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows),
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_run_dir(tmp_path: Path, run_id: str) -> Path:
    out_dir = tmp_path / run_id
    out_dir.mkdir(parents=True)
    bar = {
        "bar": {
            "symbol": "BTCUSDT",
            "open_time": BASE,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
            "interval_seconds": 3600,
            "emission_sequence": 1,
        },
        "quality": "HEALTHY",
    }
    record = {
        "run_id": run_id,
        "timestamp": "2026-09-01T01:00:00+00:00",
        "bar_open_time": BASE,
        "bar_close_time": BASE + HOUR,
        "strategy_hash": "x",
        "signal": "NO_TRADE",
        "kill_switch": "ALLOW",
        "can_trade": True,
        "submission_status": None,
        "position": 0.0,
        "equity": 100_000.0,
        "reconciliation_status": "RECONCILIATION_OK",
        "reconciliation_diffs": 0,
        "monitoring_overall": "HEALTHY",
    }
    _write_ndjson(out_dir / "bars.jsonl", [bar])
    _write_ndjson(out_dir / "records.ndjson", [record])
    _write_ndjson(out_dir / e18.S7_OPS_LOG, [{"at": "2026-09-01T01:00:00+00:00"}])
    _write_ndjson(out_dir / e18.S7_INCIDENTS, [])
    _write_ndjson(out_dir / e18.S7_HEALTH_LOG, [{"level": "UNKNOWN"}])
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "output_files": {
                    "bars": "bars.jsonl",
                    "records": "records.ndjson",
                    "audit": "audit.jsonl",
                    "snapshot": "snapshot.json",
                }
            }
        ),
        encoding="utf-8",
    )
    return out_dir


def _args(tmp_path: Path, *, run_id: str, **overrides) -> argparse.Namespace:
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


def _patch_factories(monkeypatch, events) -> None:
    monkeypatch.setattr(
        e18,
        "build_streaming_adapter",
        lambda args: ScriptedStreamingAdapter(events=list(events), clock=lambda: FIXED_NOW),
    )
    monkeypatch.setattr(e18, "build_reconnect_manager", _scripted_manager)
    monkeypatch.setattr(e18, "build_backfill_provider", lambda args: _EmptyBackfill())


class _EmptyBackfill:
    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        return []


class _CountingAdapter(StreamingMarketDataAdapter):
    """Adapter determinista que puede fallar en connect y cuenta disconnect."""

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


# --------------------------------------------------------------- status


def test_status_report_is_read_only(tmp_path: Path) -> None:
    run_id = "FORWARD-STREAM-B1"
    out_dir = _minimal_run_dir(tmp_path, run_id)
    before = {p.name: _sha(p) for p in out_dir.iterdir() if p.is_file()}

    rc = e18.main(["--status", "--run-id", run_id, "--out-root", str(tmp_path)])

    after = {p.name: _sha(p) for p in out_dir.iterdir() if p.is_file()}
    assert rc == 0
    assert after == before


def test_status_does_not_acquire_lock(tmp_path: Path) -> None:
    run_id = "FORWARD-STREAM-STATUS"
    rc = e18.main(["--status", "--run-id", run_id, "--out-root", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / run_id / e18.S7_LOCK).exists()


def test_build_report_missing_files_ok(tmp_path: Path) -> None:
    report = e18.build_report(tmp_path / "vacio", datetime.now(timezone.utc))
    assert report["ejecucion"]["barras_totales"] == 0
    assert report["integridad"]["ok"] is False


# --------------------------------------------------------------- lock


def test_instance_lock_prevents_second(tmp_path: Path) -> None:
    path = tmp_path / "S7" / e18.S7_LOCK
    lock1 = e18.StreamingInstanceLock(path)
    lock1.acquire()
    try:
        with pytest.raises(e18.AlreadyRunningError):
            e18.StreamingInstanceLock(path).acquire()
    finally:
        lock1.release()
    lock2 = e18.StreamingInstanceLock(path)
    lock2.acquire()
    lock2.release()


def test_run_cycle_skips_when_locked(tmp_path: Path, monkeypatch) -> None:
    run_id = "FORWARD-STREAM-LOCK"
    out_dir = tmp_path / run_id
    lock = e18.StreamingInstanceLock(out_dir / e18.S7_LOCK)
    lock.acquire()

    def _boom(_args):
        raise AssertionError("no debe construir si el lock esta tomado")

    monkeypatch.setattr(e18, "build_streaming_adapter", _boom)
    try:
        result = e18.run_cycle(_args(tmp_path, run_id=run_id))
    finally:
        lock.release()

    assert result.get("locked") is True
    assert result["report"] is None


def test_run_cycle_releases_lock(tmp_path: Path, monkeypatch) -> None:
    _patch_factories(monkeypatch, [])
    run_id = "FORWARD-STREAM-RELEASE"
    result = e18.run_cycle(_args(tmp_path, run_id=run_id))
    assert result.get("locked") is False

    lock = e18.StreamingInstanceLock(tmp_path / run_id / e18.S7_LOCK)
    lock.acquire()
    lock.release()


# --------------------------------------------------------------- --once


def test_run_cycle_once_bounded(tmp_path: Path, monkeypatch) -> None:
    events = [
        StreamData(received_at=FIXED_NOW, event=_market(BASE)),
        StreamData(received_at=FIXED_NOW, event=_market(BASE + HOUR)),
    ]
    _patch_factories(monkeypatch, events)

    run_id = "FORWARD-STREAM-ONCE"
    result = e18.run_cycle(_args(tmp_path, run_id=run_id))

    assert result["locked"] is False
    cycle = result["cycle"]
    assert cycle["processed"] == 2
    assert cycle["emitted"] == 2
    assert cycle["engine_state"] == "CONNECTED"

    out_dir = tmp_path / run_id
    assert (out_dir / e18.S7_REPORT).exists()
    assert (out_dir / e18.S7_PROVENANCE).exists()
    assert (out_dir / "records.ndjson").exists()

    provenance = json.loads((out_dir / e18.S7_PROVENANCE).read_text(encoding="utf-8"))
    assert provenance["data_source"] == "binance-websocket"
    assert provenance["symbol"] == SYMBOL
    assert provenance["namespace"]["run_id"] == run_id

    report = e18.build_report(out_dir, datetime.now(timezone.utc))
    assert report["ejecucion"]["barras_totales"] == 2


# --------------------------------------------------------------- hardening


def test_run_cycle_connect_failure_controlado(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        e18,
        "build_streaming_adapter",
        lambda args: _CountingAdapter(
            connect_error=StreamingConnectionError("boom connect")
        ),
    )
    monkeypatch.setattr(e18, "build_reconnect_manager", _scripted_manager)
    monkeypatch.setattr(e18, "build_backfill_provider", lambda args: _EmptyBackfill())

    run_id = "FORWARD-STREAM-CONNFAIL"
    result = e18.run_cycle(_args(tmp_path, run_id=run_id))

    assert result["locked"] is False  # no escapa excepcion; lock liberado
    assert result["cycle"]["connect_error"] is True
    assert [i["kind"] for i in result["incidents"]] == ["STREAM_CONNECT_ERROR"]

    out_dir = tmp_path / run_id
    assert (out_dir / e18.S7_REPORT).exists()
    assert (out_dir / e18.S7_INCIDENTS).exists()

    lock = e18.StreamingInstanceLock(out_dir / e18.S7_LOCK)
    lock.acquire()
    lock.release()


def test_run_cycle_startup_failure_controlado(tmp_path: Path, monkeypatch) -> None:
    _patch_factories(monkeypatch, [])

    def _boom(self):
        raise RuntimeError("provenance boom")

    monkeypatch.setattr(e18.StreamingForwardRunner, "_write_provenance", _boom)

    run_id = "FORWARD-STREAM-PROVFAIL"
    result = e18.run_cycle(_args(tmp_path, run_id=run_id))

    assert result["locked"] is False
    assert result["cycle"]["startup_error"] is True
    assert [i["kind"] for i in result["incidents"]] == ["STARTUP_ERROR"]

    out_dir = tmp_path / run_id
    assert (out_dir / e18.S7_REPORT).exists()

    lock = e18.StreamingInstanceLock(out_dir / e18.S7_LOCK)
    lock.acquire()
    lock.release()


def test_main_connect_failure_devuelve_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        e18,
        "build_streaming_adapter",
        lambda args: _CountingAdapter(
            connect_error=StreamingConnectionError("boom connect")
        ),
    )
    monkeypatch.setattr(e18, "build_reconnect_manager", _scripted_manager)
    monkeypatch.setattr(e18, "build_backfill_provider", lambda args: _EmptyBackfill())

    rc = e18.main(
        ["--once", "--run-id", "FORWARD-STREAM-MAINFAIL", "--out-root", str(tmp_path)]
    )
    assert rc == 1
