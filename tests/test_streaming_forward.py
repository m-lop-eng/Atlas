"""Tests deterministas de S7 (StreamingForwardRunner, provider, seed).

Sin red: se inyecta un `ScriptedStreamingAdapter` envuelto en un
`ReconnectManager` con relojes/sleep deterministas, y un `BackfillProvider`
falso. El `ForwardRunner` real se construye con la config congelada en tmp.

Casos (spec S7):
  1. anti-bypass conductual: B1 es la autoridad.
  2. anti-bypass estructural + paper-safe (scan de fuentes).
  3. warm-start silencioso (no emite, no observa S6).
  4. seed fiel reconstruido desde bars.jsonl.
  5. continuidad de emission_sequence.
  6. gap cross-restart recuperable.
  7. gap cross-restart no recuperable.
  8. DataFetchError -> ConnectionError (unit + integracion BLOCKED).
  9. duplicados no duplican records.
  10. solo PaperBrokerAdapter.
  11. S6 no modifica el KillSwitch.
  12. aislamiento de A4.
  13. determinismo de replay.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data.sources import DataFetchError
from live.adapter import BackfillProvider
from live.events import ClosedBarEvent, MarketDataEvent
from papertrading.forward_runner import build_forward_runner
from papertrading.streaming_forward import (
    S7_OUT_ROOT,
    SeedError,
    StreamingBackfillProvider,
    StreamingForwardRunner,
    load_seed_bar,
)
from streaming import (
    BackoffPolicy,
    IntegrityStatus,
    ReconnectManager,
    ReconnectPolicy,
    ScriptedStreamingAdapter,
    StreamData,
    StreamingConnectionError,
    StreamingHealthMonitor,
    StreamingMarketDataAdapter,
    StreamingPipeline,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"
FIXED_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------- utilidades


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


def _closed(open_time: int, seq: int) -> ClosedBarEvent:
    return ClosedBarEvent(
        symbol=SYMBOL,
        open_time=open_time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1.0,
        interval_seconds=HOUR,
        emission_sequence=seq,
    )


def _at(bar: ClosedBarEvent) -> datetime:
    return datetime.fromtimestamp(bar.close_time, tz=timezone.utc)


class _ListBackfill(BackfillProvider):
    def __init__(self, bars=None) -> None:
        self.bars = list(bars or [])
        self.calls: list[tuple[str, int, int]] = []

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        self.calls.append((symbol, interval_seconds, start_open_time))
        return [b for b in self.bars if b.open_time >= start_open_time]


class _BoomSource:
    def fetch(self, *_args, **_kwargs):
        raise DataFetchError("boom proveedor")


def _manager(events):
    adapter = ScriptedStreamingAdapter(
        events=list(events), auto_connect_event=True, clock=lambda: FIXED_NOW
    )
    return ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
        clock=lambda: 0.0,
        wall_clock=lambda: FIXED_NOW,
        sleep=lambda _s: None,
    )


def _seed_run(tmp_path: Path, run_id: str, bars: list[ClosedBarEvent]) -> None:
    """Crea un bars.jsonl previo (simula una sesion anterior reanudada)."""
    fwd = build_forward_runner(run_id=run_id, output_dir=str(tmp_path))
    fwd.start()
    for bar in bars:
        fwd.on_closed_bar(bar, at=_at(bar))
    fwd.checkpoint()


def _sfr(
    tmp_path: Path,
    run_id: str,
    events,
    *,
    backfill_bars=None,
    backfill_provider=None,
    monitor=None,
):
    runner = build_forward_runner(run_id=run_id, output_dir=str(tmp_path))
    provider = (
        backfill_provider
        if backfill_provider is not None
        else _ListBackfill(backfill_bars or [])
    )
    sfr = StreamingForwardRunner(
        forward_runner=runner,
        symbol=SYMBOL,
        interval_seconds=HOUR,
        manager=_manager(events),
        backfill_provider=provider,
        monitor=monitor,
        clock=lambda: 0.0,
    )
    return sfr, provider


class _CountingAdapter(StreamingMarketDataAdapter):
    """Adapter determinista que cuenta disconnect y puede fallar en connect."""

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self._connect_error = connect_error
        self.connect_calls = 0
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
        self.connect_calls += 1
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


def _sfr_with_adapter(tmp_path, run_id, adapter, *, runner=None):
    if runner is None:
        runner = build_forward_runner(run_id=run_id, output_dir=str(tmp_path))
    manager = ReconnectManager(
        adapter,
        policy=ReconnectPolicy(
            max_attempts=3, backoff=BackoffPolicy(base_delay_seconds=1.0, factor=2.0)
        ),
        clock=lambda: 0.0,
        wall_clock=lambda: FIXED_NOW,
        sleep=lambda _s: None,
    )
    return StreamingForwardRunner(
        forward_runner=runner,
        symbol=SYMBOL,
        interval_seconds=HOUR,
        manager=manager,
        backfill_provider=_ListBackfill([]),
        clock=lambda: 0.0,
    )


def _spy_checkpoint(runner):
    calls: list[int] = []
    orig = runner.checkpoint
    runner.checkpoint = lambda: (calls.append(1), orig())[1]
    return calls


# --------------------------------------------------------------- casos


def test_anti_bypass_b1_es_la_autoridad(tmp_path: Path) -> None:
    events = [_sdata(BASE), _sdata(BASE + HOUR), _sdata(BASE)]  # dup/out-of-order
    sfr, _ = _sfr(tmp_path, "S7-AB", events)
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=3)

    runner_open = [r.bar_open_time for r in sfr.forward_runner.records]
    b1_open = [b.open_time for b in sfr.pipeline.closed_bars]
    assert runner_open == b1_open == [BASE, BASE + HOUR]


def test_anti_bypass_estructural_y_paper_safe() -> None:
    for rel in (
        "papertrading/streaming_forward.py",
        "experiments/e18_streaming_forward.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "StreamData" not in src
        assert "engine.process(" not in src
        assert re.search(r"from\s+brokers|import\s+brokers", src) is None
        assert re.search(r"from\s+execution|import\s+execution", src) is None
    mod = (ROOT / "papertrading" / "streaming_forward.py").read_text(encoding="utf-8")
    assert "on_closed_bar=self._on_closed_bar" in mod


def test_warm_start_silencioso(tmp_path: Path) -> None:
    _seed_run(tmp_path, "S7-WS", [_closed(BASE, 1)])
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    sfr, _ = _sfr(tmp_path, "S7-WS", [_sdata(BASE + HOUR)], monitor=monitor)
    sfr.start()

    assert sfr.seed == _closed(BASE, 1)
    assert sfr.pipeline.closed_bars == []
    snap = monitor.snapshot(sfr.pipeline)
    assert snap.last_event_at is None
    assert snap.last_closed_bar is None
    assert snap.reconnections == 0


def test_seed_fiel(tmp_path: Path) -> None:
    bars = [_closed(BASE, 1), _closed(BASE + HOUR, 2), _closed(BASE + 2 * HOUR, 3)]
    _seed_run(tmp_path, "S7-SEED", bars)
    seed = load_seed_bar(tmp_path / "S7-SEED" / "bars.jsonl")
    assert seed == bars[-1]


def test_seed_malformado_raises(tmp_path: Path) -> None:
    path = tmp_path / "bars.jsonl"
    path.write_text('{"bar": {"symbol": "BTCUSDT"}}\n', encoding="utf-8")
    with pytest.raises(SeedError):
        load_seed_bar(path)


def test_seed_ausente_none(tmp_path: Path) -> None:
    assert load_seed_bar(tmp_path / "no-existe.jsonl") is None


def test_continuidad_emission_sequence(tmp_path: Path) -> None:
    _seed_run(tmp_path, "S7-SEQ", [_closed(BASE, 5)])
    sfr, _ = _sfr(tmp_path, "S7-SEQ", [_sdata(BASE + HOUR)])
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=2)
    assert sfr.forward_runner.records[-1].emission_sequence == 6


def test_gap_cross_restart_recuperable(tmp_path: Path) -> None:
    _seed_run(tmp_path, "S7-GAPR", [_closed(BASE, 1)])
    backfill = [_market(BASE + HOUR), _market(BASE + 2 * HOUR), _market(BASE + 3 * HOUR)]
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    sfr, provider = _sfr(
        tmp_path,
        "S7-GAPR",
        [_sdata(BASE + 3 * HOUR)],
        backfill_bars=backfill,
        monitor=monitor,
    )
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=3)

    assert [b.open_time for b in sfr.pipeline.closed_bars] == [
        BASE + HOUR,
        BASE + 2 * HOUR,
        BASE + 3 * HOUR,
    ]
    assert sfr.pipeline.engine.state.value == "CONNECTED"
    snap = monitor.snapshot(sfr.pipeline)
    assert snap.integrity != "UNKNOWN"
    assert snap.gaps_recovered == 1
    assert sfr.forward_runner.records[-1].emission_sequence == 4
    assert provider.calls == [(SYMBOL, HOUR, BASE + HOUR)]


def test_gap_cross_restart_no_recuperable(tmp_path: Path) -> None:
    _seed_run(tmp_path, "S7-GAPN", [_closed(BASE, 1)])
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    sfr, _ = _sfr(
        tmp_path, "S7-GAPN", [_sdata(BASE + 3 * HOUR)], backfill_bars=[], monitor=monitor
    )
    sfr.start()
    before = len(sfr.forward_runner.records)
    sfr.run_bounded(max_events=10, max_polls=3)

    assert sfr.pipeline.engine.state.value == "DEGRADED"
    assert sfr.pipeline.closed_bars == []
    assert len(sfr.forward_runner.records) == before
    snap = monitor.snapshot(sfr.pipeline)
    assert snap.gaps_unrecoverable == 1
    # Sin reconexion, `data_integrity_unknown` no se activa; S6 refleja el
    # estado real del ultimo IntegrityReport de S4 (fail-safe, nunca HEALTHY).
    assert sfr.pipeline.manager.data_integrity_unknown is False
    assert snap.integrity == "UNRECOVERABLE"
    assert snap.integrity != "HEALTHY"


def test_backfill_traduce_datafetcherror_a_connectionerror() -> None:
    provider = StreamingBackfillProvider(_BoomSource())
    with pytest.raises(ConnectionError):
        provider.fetch_closed_bars(SYMBOL, HOUR, BASE)


def test_backfill_error_blocked_no_escapa_del_poll(tmp_path: Path) -> None:
    _seed_run(tmp_path, "S7-BOOM", [_closed(BASE, 1)])
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    sfr, _ = _sfr(
        tmp_path,
        "S7-BOOM",
        [_sdata(BASE + 3 * HOUR)],
        backfill_provider=StreamingBackfillProvider(_BoomSource()),
        monitor=monitor,
    )
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=3)  # no debe lanzar

    assert sfr.pipeline.engine.state.value == "DEGRADED"
    assert sfr.pipeline.backfill.last_report.status is IntegrityStatus.BLOCKED
    assert monitor.snapshot(sfr.pipeline).gaps_unrecoverable == 1


def test_duplicados_no_duplican_records(tmp_path: Path) -> None:
    sfr, _ = _sfr(tmp_path, "S7-DUP", [_sdata(BASE), _sdata(BASE), _sdata(BASE + HOUR)])
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=2)
    assert [b.open_time for b in sfr.pipeline.closed_bars] == [BASE, BASE + HOUR]
    assert [r.bar_open_time for r in sfr.forward_runner.records] == [BASE, BASE + HOUR]


def test_solo_paperbroker(tmp_path: Path) -> None:
    fwd = build_forward_runner(run_id="S7-PAPER", output_dir=str(tmp_path))
    assert type(fwd.engine.broker).__name__ == "PaperBrokerAdapter"


def test_s6_no_modifica_kill_switch(tmp_path: Path) -> None:
    monitor = StreamingHealthMonitor(clock=lambda: FIXED_NOW, monotonic=lambda: 0.0)
    sfr, _ = _sfr(tmp_path, "S7-KS", [_sdata(BASE)], monitor=monitor)
    sfr.start()
    ks = sfr.forward_runner.engine.kill_switch
    before = ks.status()
    sfr.run_bounded(max_events=10, max_polls=2)
    monitor.snapshot(sfr.pipeline)
    monitor.snapshot(sfr.pipeline)
    after = ks.status()
    assert after.state is before.state
    assert after.can_trade == before.can_trade
    assert "can_trade" not in monitor.snapshot(sfr.pipeline).as_dict()


def test_aislamiento_de_a4(tmp_path: Path) -> None:
    assert S7_OUT_ROOT.name == "outputs"
    assert "streaming" in S7_OUT_ROOT.parts
    assert (ROOT / "experiments" / "forward" / "outputs") != S7_OUT_ROOT

    a4 = ROOT / "experiments" / "forward" / "outputs"
    before = (
        {str(p): p.stat().st_mtime_ns for p in a4.rglob("*") if p.is_file()}
        if a4.exists()
        else {}
    )

    sfr, _ = _sfr(tmp_path, "S7-ISO", [_sdata(BASE)])
    sfr.start()
    sfr.run_bounded(max_events=10, max_polls=2)
    sfr.close()

    after = (
        {str(p): p.stat().st_mtime_ns for p in a4.rglob("*") if p.is_file()}
        if a4.exists()
        else {}
    )
    assert after == before
    assert (tmp_path / "S7-ISO").exists()


def test_determinismo_de_replay(tmp_path: Path) -> None:
    events = [_sdata(BASE), _sdata(BASE + HOUR)]
    a, _ = _sfr(tmp_path / "a", "S7-DET", events)
    a.start()
    a.run_bounded(max_events=10, max_polls=2)
    b, _ = _sfr(tmp_path / "b", "S7-DET", events)
    b.start()
    b.run_bounded(max_events=10, max_polls=2)

    assert [r.to_dict() for r in a.forward_runner.records] == [
        r.to_dict() for r in b.forward_runner.records
    ]


# --------------------------------------------------------------- hardening


def test_start_connect_falla_limpia_y_repropaga(tmp_path: Path) -> None:
    adapter = _CountingAdapter(
        connect_error=StreamingConnectionError("boom connect")
    )
    sfr = _sfr_with_adapter(tmp_path, "S7-CONNFAIL", adapter)

    with pytest.raises(ConnectionError):
        sfr.start()

    assert adapter.disconnect_calls >= 1  # sin recursos de streaming abiertos
    assert sfr._started is False


def test_start_warm_start_falla_limpia_y_repropaga(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_run(tmp_path, "S7-WSFAIL", [_closed(BASE, 1)])
    runner = build_forward_runner(run_id="S7-WSFAIL", output_dir=str(tmp_path))
    checkpoint_calls = _spy_checkpoint(runner)

    def _boom(self, closed):
        raise RuntimeError("warm_start boom")

    monkeypatch.setattr(StreamingPipeline, "warm_start", _boom)

    adapter = _CountingAdapter()
    sfr = _sfr_with_adapter(tmp_path, "S7-WSFAIL", adapter, runner=runner)

    with pytest.raises(RuntimeError):
        sfr.start()

    assert adapter.disconnect_calls >= 1  # pipeline desconectado
    assert checkpoint_calls  # runner.checkpoint() ejecutado en cleanup
    assert sfr._started is False


def test_start_provenance_falla_limpia_y_repropaga(
    tmp_path: Path, monkeypatch
) -> None:
    runner = build_forward_runner(run_id="S7-PROVFAIL", output_dir=str(tmp_path))
    checkpoint_calls = _spy_checkpoint(runner)

    def _boom(self):
        raise RuntimeError("provenance boom")

    monkeypatch.setattr(StreamingForwardRunner, "_write_provenance", _boom)

    adapter = _CountingAdapter()
    sfr = _sfr_with_adapter(tmp_path, "S7-PROVFAIL", adapter, runner=runner)

    with pytest.raises(RuntimeError):
        sfr.start()

    assert adapter.disconnect_calls >= 1  # pipeline desconectado
    assert checkpoint_calls  # runner.checkpoint() ejecutado en cleanup
    assert sfr._started is False
