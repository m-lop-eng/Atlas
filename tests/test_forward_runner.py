"""Tests de integración del ForwardRunner (paper/forward continuo).

Batería (repResenta los criterios de aceptación de la fase forward):

  1. Flujo completo barra -> señal H002 -> risco -> orden -> fill -> posición ->
     stop, con audit trail COMPLETO por barra (ForwardBarRecord).
  2. Solo ClosedBarEvent: dedupe por open_time al nivel runner y al nivel
     LiveDataEngine (B1): 0 barras duplicadas procesadas.
  3. Reinicio idempotente: replay de bars.jsonl sobre engine nuevo regenere
     outputs idénticos y continúe sin duplicar exposición.
  4. Reproducibilidad: dos runs independientes con mismo input+config ->
     mismos registros (mismo resultado).
  5. Guardas mecánicas: strategy_hash congelado (ForwardConfigError si no);
     PaperBrokerAdapter obligatorio; riesgo paper operativo <= 1% de la
     estrategia; FINAL_OOS / evidence / data_roles NO se mutan.
  6. Sizing reproducible acotado por el riesgo operativo de paper (0.25%).
"""

from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from live.engine import LiveDataEngine
from live.events import ClosedBarEvent, MarketDataEvent
from papertrading.forward_runner import (
    ForwardConfigError,
    ForwardRunner,
    build_forward_runner,
    FROZEN_STRATEGY_HASH,
)
from papertrading.models import PaperEvent

ROOT = Path(__file__).resolve().parents[1]

UTC = timezone.utc
EPOCH = 1_704_067_200
START = EPOCH + 15 * 3600
INTERVAL = 3600
INSTRUMENT = "BTCUSDT"

FLAT = (50000, 50100, 49900, 50050)


def _closed(i: int, ohlc: tuple[float, float, float, float]) -> ClosedBarEvent:
    o, h, l, c = ohlc
    return ClosedBarEvent(
        symbol=INSTRUMENT,
        open_time=START + i * INTERVAL,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1000.0,
        interval_seconds=INTERVAL,
        emission_sequence=i + 1,
    )


def _scenario() -> list[ClosedBarEvent]:
    rows = [FLAT] * 20
    rows.append((50150, 50200, 50050, 50200))  # 20: señal LONG (close > 50100)
    rows.append((50200, 50300, 50150, 50200))  # 21: entrada al open 50200
    rows.append((50200, 50300, 50100, 50250))  # 22: mantiene posición
    rows.append((50150, 50250, 49800, 49850))  # 23: STOP (low <= stop ATR)
    rows.append((49850, 49950, 49650, 49700))  # 24: NO_TRADE
    return [_closed(i, r) for i, r in enumerate(rows)]


def _at(bar: ClosedBarEvent) -> datetime:
    return datetime.fromtimestamp(bar.close_time, tz=UTC)


def _drive(
    runner: ForwardRunner, bars: list[ClosedBarEvent], *, quality: str = "HEALTHY"
) -> list:
    return [
        runner.on_closed_bar(b, data_quality=quality, at=_at(b)) for b in bars
    ]


def _make(tmp_path, run_id: str = "TEST-FWD"):
    runner = build_forward_runner(run_id=run_id, output_dir=str(tmp_path))
    runner.start()
    return runner


def _file(path) -> str:
    with path.open("r", encoding="utf-8", newline="\n") as fh:
        return fh.read()


class TestForwardFullFlow:
    def test_build_hooks_frozen_config_and_paper_operational_risk(self, tmp_path):
        runner = _make(tmp_path)
        assert type(runner.engine.broker).__name__ == "PaperBrokerAdapter"
        risk = runner.engine.risk_engine.config
        assert risk.risk_per_trade == pytest.approx(0.0025)
        assert risk.risk_per_trade <= 0.01
        strat = runner.engine.strategy
        params = {
            "lookback": strat.params.lookback,
            "atr_period": strat.params.atr_period,
            "stop_atr_mult": strat.params.stop_atr_mult,
            "direction": strat.params.direction,
        }
        assert params == {
            "lookback": 20,
            "atr_period": 14,
            "stop_atr_mult": 2.0,
            "direction": "long",
        }
        assert runner.strategy_hash == FROZEN_STRATEGY_HASH
        assert strat.metadata.status == "FROZEN"

    def test_full_cycle_and_per_bar_audit_trail(self, tmp_path):
        runner = _make(tmp_path)
        bars = _scenario()
        _drive(runner, bars)

        records = runner.records
        assert len(records) == len(bars)
        assert len(runner.engine.trades) == 1
        trade = runner.engine.trades[0]
        assert trade.exit_reason == "STOP"
        assert trade.entry_price == pytest.approx(bars[21].open)  # open de t+1

        signal_stop = trade.stop_price

        for i, rec in enumerate(records):
            assert rec.data_quality == "HEALTHY"
            assert rec.session_state == "OPEN"
            assert rec.session_can_open is True
            assert rec.kill_switch == "ALLOW"
            assert rec.can_trade is True
            assert rec.strategy_id == "atlas-breakout"
            assert rec.strategy_hash == FROZEN_STRATEGY_HASH
            assert rec.monitoring_overall == "HEALTHY"
            assert rec.reconciliation_status == "RECONCILIATION_OK"
            assert rec.timestamp == datetime.fromtimestamp(
                record_close(rec), tz=UTC
            ).isoformat()

        r20 = records[20]
        assert r20.signal == "LONG"
        assert r20.risk_decision == "APPROVED"
        assert r20.order_intent and r20.order_intent.startswith("ENTRY:")
        assert r20.submission_status is None
        assert r20.position == 0.0
        assert r20.signal == "LONG"

        r21 = records[21]
        assert r21.submission_status == "FILLED"
        assert r21.fills_quantity == pytest.approx(r20.approved_quantity)
        assert r21.fills_vwap == pytest.approx(bars[21].open)
        assert r21.position == pytest.approx(r20.approved_quantity)
        assert r21.position_stop == pytest.approx(signal_stop)

        r22 = records[22]
        assert r22.position == pytest.approx(r20.approved_quantity)
        assert r22.signal == "NO_TRADE"

        r23 = records[23]
        assert r23.signal == "NO_TRADE"
        assert r23.order_intent and r23.order_intent.startswith("EXIT:")
        assert r23.submission_status == "FILLED"
        assert r23.fills_vwap == pytest.approx(signal_stop)
        assert r23.position == 0.0
        assert r23.equity == pytest.approx(100000.0 - 250.0, abs=0.01)

        r24 = records[24]
        assert r24.signal == "NO_TRADE"
        assert r24.position == 0.0

        material = {
            d.classification.value
            for d in runner.engine.last_report.diffs
            if d.classification.value != "MATCH"
        }
        assert material == set()
        assert runner.engine.last_report.status.value == "RECONCILIATION_OK"

    def test_sizing_reproducible_bounded_by_paper_risk(self, tmp_path):
        runner = _make(tmp_path)
        _drive(runner, _scenario())
        r21 = runner.records[21]
        distance = r21.fills_vwap - r21.position_stop
        risk_cap = 100000.0 * 0.0025
        assert r21.position * distance == pytest.approx(risk_cap, rel=1e-9)
        assert r21.position * distance <= 100000.0 * 0.01

    def test_no_trade_without_signal(self, tmp_path):
        runner = _make(tmp_path)
        bars = [_closed(i, FLAT) for i in range(25)]
        _drive(runner, bars)
        assert len(runner.records) == 25
        assert runner.engine.position is None
        assert runner.engine.trades == ()
        for rec in runner.records:
            assert rec.signal == "NO_TRADE"
            assert rec.risk_decision == "NOT_EVALUATED"
            assert rec.order_intent is None
            assert rec.position == 0.0
            assert rec.equity == pytest.approx(100000.0)

    def test_data_quality_passthrough(self, tmp_path):
        runner = _make(tmp_path)
        _drive(runner, [_closed(i, FLAT) for i in range(3)], quality="DEGRADED")
        for rec in runner.records:
            assert rec.data_quality == "DEGRADED"


class TestIdempotencyAndReproducibility:
    def test_duplicates_and_out_of_order_not_processed(self, tmp_path):
        runner = _make(tmp_path)
        bars = _scenario()
        _drive(runner, bars)
        before = len(runner.records)

        dup = _closed(20, (bars[20].open, bars[20].high, bars[20].low, bars[20].close))
        assert runner.on_closed_bar(dup, at=_at(dup)) is None
        out_of_order = _closed(5, (50000, 50100, 49900, 50050))
        assert runner.on_closed_bar(out_of_order, at=_at(out_of_order)) is None

        assert len(runner.records) == before
        lines = _file(runner._bars_path).strip().splitlines()
        assert len(lines) == before

    def test_restart_replays_and_regenerates_identical_outputs(self, tmp_path):
        run_id = "RESTART"
        bars = _scenario()
        runner_a = _make(tmp_path, run_id=run_id)
        _drive(runner_a, bars)

        bars_bytes = _file(runner_a._bars_path)
        records_a = _file(runner_a._records_path)
        audit_a = _file(runner_a._audit_path)
        snapshot_a = _file(runner_a._snapshot_path)

        runner_b = _make(tmp_path, run_id=run_id)
        assert _file(runner_b._records_path) == records_a
        assert _file(runner_b._audit_path) == audit_a
        assert _file(runner_b._snapshot_path) == snapshot_a
        assert _file(runner_b._bars_path) == bars_bytes
        assert [r.to_dict() for r in runner_b.records] == [
            r.to_dict() for r in runner_a.records
        ]
        assert len(runner_b.engine.trades) == 1  # sin duplicar exposición

        next_bar = _closed(len(bars), (49700, 49900, 49500, 49650))
        rec = runner_b.on_closed_bar(next_bar, at=_at(next_bar))
        assert rec is not None
        assert len(runner_b.records) == len(bars) + 1
        assert len(runner_b.engine.trades) == 1

    def test_two_independent_runs_produce_identical_records(self, tmp_path):
        bars = _scenario()
        r1 = _make(tmp_path / "a", run_id="R")
        _drive(r1, bars)
        r2 = _make(tmp_path / "b", run_id="R")
        _drive(r2, bars)

        recs1 = [r.to_dict() for r in r1.records]
        recs2 = [r.to_dict() for r in r2.records]
        assert recs1 == recs2
        curve1 = [list(p) for p in r1.engine.equity_curve]
        curve2 = [list(p) for p in r2.engine.equity_curve]
        assert curve1 == curve2


class TestFrozenGuards:
    def test_wrong_strategy_hash_raises(self, tmp_path):
        engine = _make(tmp_path).engine
        with pytest.raises(ForwardConfigError):
            ForwardRunner(
                engine,
                run_id="X",
                output_dir=str(tmp_path / "x"),
                instrument=INSTRUMENT,
                strategy_hash="deadbeef",
            )

    def test_non_paper_broker_rejected(self, tmp_path):
        engine = SimpleNamespace(broker=object())
        with pytest.raises(TypeError):
            ForwardRunner(
                engine,
                run_id="X",
                output_dir=str(tmp_path / "x"),
                instrument=INSTRUMENT,
                strategy_hash=FROZEN_STRATEGY_HASH,
            )

    def test_tampered_frozen_params_raise(self, tmp_path):
        frozen_path = ROOT / "config" / "final_oos" / "config.yaml"
        cfg = yaml.safe_load(frozen_path.read_text(encoding="utf-8"))
        cfg["strategy"]["params"]["lookback"] = 21
        tampered = tmp_path / "config.yaml"
        tampered.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ForwardConfigError):
            build_forward_runner(frozen_config_path=tampered)

    def test_forward_runner_needs_start_before_on_closed_bar(self, tmp_path):
        runner = build_forward_runner(run_id="STARTG", output_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            runner.on_closed_bar(_closed(0, FLAT))

    def test_final_oos_perimeter_not_mutated(self, tmp_path):
        perimeter = [
            ROOT / "research" / "evidence.json",
            ROOT / "research" / "data_roles.json",
            ROOT / "config" / "final_oos" / "config.yaml",
            ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json",
        ]
        before = {str(p): p.read_bytes() for p in perimeter}
        _drive(_make(tmp_path), _scenario())
        for p in perimeter:
            assert p.read_bytes() == before[str(p)], f"{p.name} se mutó"


class TestB1Integration:
    def test_live_engine_filters_duplicates_before_runner(self, tmp_path):
        runner = _make(tmp_path)
        events = [
            MarketDataEvent(
                symbol=INSTRUMENT,
                open_time=START,
                open=50000,
                high=50100,
                low=49900,
                close=50050,
                volume=1000.0,
                is_closed=True,
            ),
            MarketDataEvent(
                symbol=INSTRUMENT,
                open_time=START,
                open=50000,
                high=50100,
                low=49900,
                close=50050,
                volume=1000.0,
                is_closed=True,
            ),
        ]

        class _Adapter:
            def __init__(self, events):
                self._events = list(events)
                self.connected = False

            def connect(self):
                self.connected = True

            def disconnect(self):
                self.connected = False

            def subscribe(self, *args):
                pass

            def receive(self):
                return self._events.pop(0) if self._events else None

        live = LiveDataEngine(
            INSTRUMENT,
            INTERVAL,
            _Adapter(events),
            on_closed_bar=runner.on_closed_bar,
            clock=lambda: 1_000_000.0,
        )
        live.start()
        processed = live.poll(max_events=10)
        live.stop()

        assert processed == 2
        assert len(runner.records) == 1
        assert len(_file(runner._bars_path).strip().splitlines()) == 1
        assert any(
            e.event == PaperEvent.BAR_CLOSED.value for e in runner.engine.audit_log
        )
        issues = live.issues
        assert any(q.value == "DUPLICATE" for q, _ in issues)


def record_close(rec) -> int:
    return rec.bar_close_time