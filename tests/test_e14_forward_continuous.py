"""Tests deterministas del driver operativo A4 (experiments/e14_forward_continuous).

Enfocados en las propiedades exigidas antes de versionar el driver:

  1. `build_report` (ruta `--status`) es SOLO LECTURA: no reconstruye el runner
     ni regenera archivos derivados.
  2. Un `DataFetchError` se registra como incidente, no procesa barras y no
     destruye el estado persistido (no hay falsa reconexion intra-sesion).

Sin red: la fuente real se sustituye por un doble que falla de forma explicita.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from experiments import e14_forward_continuous as e14


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows),
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_run_dir(tmp_path: Path) -> Path:
    out_dir = tmp_path / "FORWARD-PAPER-A4"
    out_dir.mkdir(parents=True)
    bar = {
        "bar": {
            "symbol": "BTCUSDT", "open_time": 1_789_000_000, "open": 1.0,
            "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
            "interval_seconds": 3600, "emission_sequence": 1,
        },
        "quality": "HEALTHY",
    }
    record = {
        "run_id": "FORWARD-PAPER-A4", "timestamp": "2026-09-01T00:00:00+00:00",
        "bar_open_time": 1_789_000_000, "bar_close_time": 1_789_003_600,
        "strategy_hash": "x", "signal": "NO_TRADE", "kill_switch": "ALLOW",
        "can_trade": True, "submission_status": None, "position": 0.0,
        "equity": 100_000.0, "reconciliation_status": "RECONCILIATION_OK",
        "reconciliation_diffs": 8, "monitoring_overall": "HEALTHY",
    }
    _write_ndjson(out_dir / "bars.jsonl", [bar])
    _write_ndjson(out_dir / "records.ndjson", [record])
    _write_ndjson(out_dir / "ops_log.ndjson", [{
        "at": "2026-09-01T00:00:00+00:00", "restart": False, "procesadas": 1,
        "deduplicadas": 0, "engine_state": "CONNECTED", "provider_error": None,
        "reconcile_rounds": 0,
    }])
    _write_ndjson(out_dir / "incidents.ndjson", [])
    (out_dir / "audit.jsonl").write_text("", encoding="utf-8")
    (out_dir / "snapshot.json").write_text("{}", encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps({
        "output_files": {
            "bars": "bars.jsonl", "records": "records.ndjson",
            "audit": "audit.jsonl", "snapshot": "snapshot.json",
        }
    }), encoding="utf-8")
    return out_dir


def _args(tmp_path: Path, *, run_id: str, now: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        run_id=run_id, out_root=str(tmp_path), seed_days=12,
        poll_seconds=3600, loop=False, status=False, once=True, now=now,
    )


def test_status_report_is_read_only(tmp_path: Path) -> None:
    out_dir = _minimal_run_dir(tmp_path)
    before = {p.name: _sha(p) for p in out_dir.iterdir() if p.is_file()}

    report = e14.build_report(
        _args(tmp_path, run_id="FORWARD-PAPER-A4"), out_dir, datetime.now(timezone.utc)
    )

    after = {p.name: _sha(p) for p in out_dir.iterdir() if p.is_file()}
    assert after == before
    assert report["ejecucion"]["barras_totales"] == 1
    assert report["integridad"]["ok"] is True
    assert report["reconciliacion"]["status"] == {"RECONCILIATION_OK": 1}


def test_data_fetch_error_is_incident_and_preserves_state(tmp_path, monkeypatch) -> None:
    class _BoomSource:
        def __init__(self, **_kwargs) -> None:
            pass

        def fetch(self, *_args, **_kwargs):
            raise e14.DataFetchError("boom proveedor")

    monkeypatch.setattr(e14, "BinanceVisionDailySource", _BoomSource)

    result = e14.run_cycle(
        _args(tmp_path, run_id="FORWARD-PAPER-A4T", now="2026-09-26T14:00:00")
    )

    cycle = result["cycle"]
    assert cycle["provider_error"] is not None
    assert cycle["new_bars"] == 0
    assert cycle["procesadas"] == 0
    assert cycle["engine_state"] == "CONNECTED"

    out_dir = tmp_path / "FORWARD-PAPER-A4T"
    incidents = e14._read_ndjson(out_dir / "incidents.ndjson")
    assert [i["kind"] for i in incidents] == ["PROVIDER_ERROR"]

    assert not (out_dir / "bars.jsonl").exists()
    assert e14.build_report(
        _args(tmp_path, run_id="FORWARD-PAPER-A4T"), out_dir, datetime.now(timezone.utc)
    )["ejecucion"]["barras_totales"] == 0


def test_instance_lock_prevents_second(tmp_path: Path) -> None:
    path = tmp_path / "FORWARD-PAPER-A4" / e14.LOCK_NAME
    lock1 = e14.ForwardInstanceLock(path)
    lock1.acquire()
    try:
        with pytest.raises(e14.AlreadyRunningError):
            e14.ForwardInstanceLock(path).acquire()
    finally:
        lock1.release()
    # reusable tras liberar
    lock2 = e14.ForwardInstanceLock(path)
    lock2.acquire()
    lock2.release()


def test_status_does_not_acquire_lock(tmp_path: Path) -> None:
    rc = e14.main(
        ["--status", "--run-id", "FORWARD-PAPER-STATUS", "--out-root", str(tmp_path)]
    )
    assert rc == 0
    assert not (tmp_path / "FORWARD-PAPER-STATUS" / e14.LOCK_NAME).exists()


def test_run_cycle_skips_when_locked(tmp_path: Path, monkeypatch) -> None:
    run_id = "FORWARD-PAPER-A4L"
    out_dir = tmp_path / run_id
    lock = e14.ForwardInstanceLock(out_dir / e14.LOCK_NAME)
    lock.acquire()

    class _NoNetwork:
        def __init__(self, **_kwargs) -> None:
            pass

        def fetch(self, *_a, **_k):
            raise AssertionError("no debe llegar a la red si el lock esta tomado")

    monkeypatch.setattr(e14, "BinanceVisionDailySource", _NoNetwork)
    try:
        result = e14.run_cycle(_args(tmp_path, run_id=run_id, now="2026-09-26T14:00:00"))
    finally:
        lock.release()

    assert result.get("locked") is True
    assert result["report"] is None


def test_run_cycle_releases_lock(tmp_path: Path, monkeypatch) -> None:
    class _EmptySource:
        def __init__(self, **_kwargs) -> None:
            pass

        def fetch(self, *_a, **_k):
            return []

    monkeypatch.setattr(e14, "BinanceVisionDailySource", _EmptySource)
    run_id = "FORWARD-PAPER-A4R"
    result = e14.run_cycle(_args(tmp_path, run_id=run_id, now="2026-09-26T14:00:00"))
    assert result.get("locked") is False

    lock = e14.ForwardInstanceLock(tmp_path / run_id / e14.LOCK_NAME)
    lock.acquire()
    lock.release()
