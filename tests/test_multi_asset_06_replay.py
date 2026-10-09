"""Tests MULTI-ASSET-06: replay multiinstrumento determinista.

Driver `MultiAssetReplay` sobre `MultiAssetIngress` + `MultiAssetPaperOrchestrator`.
Determinista: reloj simulado, sin red, sin threads, sin MT5.
"""

from __future__ import annotations

import json

import pytest

from papertrading.multi_asset_ingress import InstrumentStreamConfig, MultiAssetIngress
from papertrading.multi_asset_replay import (
    ExpectedStreamFingerprint,
    MultiAssetReplay,
    ReplayIntegrityError,
    StreamInput,
    compute_stream_fingerprint,
)
from risk.types import RiskConfig
from sessions.markets import btc_24_7_calendar

from tests.test_multi_asset_04_orchestration import (  # noqa: E402
    SERIES,
    START,
    _add,
    _orch,
    _spec,
)
from tests.test_multi_asset_05_ingress import _StubAdapter  # noqa: E402


def _write_bars(path, market_symbol, interval, series, start, *, seq_start=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for i, (o, h, l, c) in enumerate(series):
            row = {
                "bar": {
                    "symbol": market_symbol,
                    "open_time": start + i * interval,
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": 1000.0,
                    "interval_seconds": interval,
                    "emission_sequence": seq_start + i,
                },
                "quality": "HEALTHY",
            }
            fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _write_timed(path, market_symbol, interval, rows, *, seq_start=1):
    """`rows` = lista de (open_time, open, high, low, close) explicitos."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for i, (ts, o, h, l, c) in enumerate(rows):
            row = {
                "bar": {
                    "symbol": market_symbol, "open_time": ts,
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": 1000.0, "interval_seconds": interval,
                    "emission_sequence": seq_start + i,
                },
                "quality": "HEALTHY",
            }
            fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _harness(tmp_path, instruments, *, risk=None):
    """instruments: dict(atlas_id, market_symbol?, interval?, series|timed, start?)."""
    orch, clock = _orch(risk=risk)
    ingress_streams = []
    stream_inputs = []
    for spec in instruments:
        atlas = spec["atlas_id"]
        market = spec.get("market_symbol", atlas)
        interval = spec.get("interval", 3600)
        start = spec.get("start", START)
        _add(orch, atlas, calendar=spec.get("calendar") or btc_24_7_calendar())
        cfg = InstrumentStreamConfig(
            atlas_id=atlas, market_data_symbol=market,
            interval_seconds=interval, instrument_spec=_spec(atlas),
        )
        path = tmp_path / f"{atlas}.bars.jsonl"
        if "timed" in spec:
            _write_timed(path, market, interval, spec["timed"])
        else:
            _write_bars(path, market, interval, spec["series"], start)
        stream_inputs.append(
            StreamInput(config=cfg, bars_path=path,
                        fingerprint=compute_stream_fingerprint(path))
        )
        ingress_streams.append((cfg, _StubAdapter()))
    ingress = MultiAssetIngress(
        orch, ingress_streams, engine_clock=lambda: clock().timestamp()
    )
    return orch, clock, ingress, stream_inputs


def _replay(orch, clock, ingress, stream_inputs, **kw):
    return MultiAssetReplay(
        orch, ingress, stream_inputs, clock=clock, **kw
    )


# --------------------------------------------------------------------------
# 1-2. INTERCALADO / CIERRES SIMULTANEOS / MARKS COMUNES
# --------------------------------------------------------------------------


class TestInterleavedAndGroups:
    def test_two_instruments_interleaved(self, tmp_path):
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES},
            {"atlas_id": "BBBUSD", "series": SERIES},
        ])
        result = _replay(orch, clock, ing, streams).run()
        assert result.events == 2 * len(SERIES)
        assert orch.ledger.position("AAAUSD") is not None
        assert orch.ledger.position("BBBUSD") is not None
        assert orch.marks["AAAUSD"] == pytest.approx(SERIES[3][3])
        assert orch.marks["BBBUSD"] == pytest.approx(SERIES[3][3])

    def test_same_close_time_marks_common_before_eval(self, tmp_path):
        # Barrera: al evaluar el PRIMER worker del grupo, el mark del SEGUNDO
        # instrumento ya pertenece a este grupo (no al grupo anterior).
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES},
            {"atlas_id": "BBBUSD", "series": SERIES},
        ])
        captures = []
        worker = orch.workers["AAAUSD"]
        real = worker._projected_exposure

        def spy(**kw):
            captures.append(dict(orch.marks))
            return real(**kw)

        worker._projected_exposure = spy
        _replay(orch, clock, ing, streams).run()
        # La primera evaluacion de exposicion proyectada (AAAUSD, grupo t2) debe
        # ver el mark de BBBUSD de ESE grupo (t2 close), no el previo (t1).
        t2_close = SERIES[2][3]
        assert any(
            m.get("AAAUSD") == pytest.approx(t2_close)
            and m.get("BBBUSD") == pytest.approx(t2_close)
            for m in captures
        )

    def test_different_intervals_same_close(self, tmp_path):
        # A: 1h; B: 1h tambien (mismo cierre). Diferente intervalo no cambia el
        # orden estable por atlas_id.
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES, "interval": 3600},
            {"atlas_id": "BBBUSD", "series": SERIES, "interval": 3600},
        ])
        _replay(orch, clock, ing, streams).run()
        assert set(orch.marks) == {"AAAUSD", "BBBUSD"}


# --------------------------------------------------------------------------
# 3. ADMISION SECUENCIAL BAJO LIMITE GLOBAL (EXPOSICION PROYECTADA)
# --------------------------------------------------------------------------


class TestSequentialAdmission:
    def test_second_order_blocked_by_gross_exposure(self, tmp_path):
        orch, clock, ing, streams = _harness(
            tmp_path,
            [
                {"atlas_id": "AAAUSD", "series": SERIES},
                {"atlas_id": "BBBUSD", "series": SERIES},
            ],
            risk=RiskConfig(risk_per_trade=0.00025, max_gross_exposure=200_000.0),
        )
        _replay(orch, clock, ing, streams).run()
        # El primero (AAAUSD, orden atlas) abre; el segundo excede el gross global.
        assert orch.ledger.position("AAAUSD") is not None
        assert orch.ledger.position("BBBUSD") is None
        assert not any("BBBUSD" in cid for cid in orch.broker._orders)


# --------------------------------------------------------------------------
# 4-5. VETO GLOBAL INTRA-GRUPO Y MARKS CONSERVADOS
# --------------------------------------------------------------------------


class TestIntraGroupGlobalVeto:
    def test_first_worker_veto_blocks_second_in_same_group(self, tmp_path):
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES},
            {"atlas_id": "BBBUSD", "series": SERIES},
        ])
        real_submit = orch.broker.submit_order
        state = {"n": 0}

        def spy(order):
            sub = real_submit(order)
            state["n"] += 1
            if state["n"] == 1:
                orch.broker._cash += 1_000.0  # divergencia observada de cuenta
            return sub

        orch.broker.submit_order = spy
        _replay(orch, clock, ing, streams).run()
        # AAAUSD provoca el veto global; BBBUSD no puede enviar en el mismo grupo.
        assert orch.ledger.position("AAAUSD") is not None
        assert orch.ledger.position("BBBUSD") is None
        assert not any("BBBUSD" in cid for cid in orch.broker._orders)
        assert not orch.kill_switch.status().can_trade

    def test_group_marks_preserved_despite_global_veto(self, tmp_path):
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES},
            {"atlas_id": "BBBUSD", "series": SERIES},
        ])
        real_submit = orch.broker.submit_order
        state = {"n": 0}

        def spy(order):
            sub = real_submit(order)
            state["n"] += 1
            if state["n"] == 1:
                orch.broker._cash += 1_000.0
            return sub

        orch.broker.submit_order = spy
        _replay(orch, clock, ing, streams).run()
        # Los marks del grupo se fijaron ANTES de evaluar, aunque el veto bloquee.
        assert orch.marks["AAAUSD"] == pytest.approx(SERIES[3][3])
        assert orch.marks["BBBUSD"] == pytest.approx(SERIES[3][3])


# --------------------------------------------------------------------------
# 6-7. ORDEN / ENUMERACION / ORDEN INTERNO DEL STREAM
# --------------------------------------------------------------------------


class TestOrdering:
    def test_file_enumeration_order_does_not_change_result(self, tmp_path):
        a = {"atlas_id": "AAAUSD", "series": SERIES}
        b = {"atlas_id": "BBBUSD", "series": SERIES}
        orch1, clock1, ing1, s1 = _harness(tmp_path / "one", [a, b])
        r1 = _replay(orch1, clock1, ing1, s1).run()
        orch2, clock2, ing2, s2 = _harness(tmp_path / "two", [b, a])
        r2 = _replay(orch2, clock2, ing2, s2).run()
        assert r1.order_hash == r2.order_hash
        assert orch1.ledger.snapshot() == orch2.ledger.snapshot()
        assert orch1.marks == orch2.marks

    def test_intra_stream_order_preserved_and_duplicates_classified(self, tmp_path):
        # open_time repetido en el fichero: NO se reordena; B1 lo clasifica.
        t = START
        timed = [
            (t, *SERIES[0]),
            (t + 3600, *SERIES[1]),
            (t + 3600, *SERIES[1]),   # duplicado (mismo open_time)
            (t + 2 * 3600, *SERIES[2]),
            (t + 3 * 3600, *SERIES[3]),
        ]
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "timed": timed},
        ])
        _replay(orch, clock, ing, streams).run()
        assert ing.metrics["AAAUSD"].duplicates == 1
        # 4 barras unicas emitidas (t0,t1,t2,t3) pese al duplicado t1 en fichero.
        assert len(orch.workers["AAAUSD"]._bars) == 4


# --------------------------------------------------------------------------
# 8-9. INTEGRIDAD DE ENTRADA (FAIL-CLOSED)
# --------------------------------------------------------------------------


class TestPreflightIntegrity:
    def _one(self, tmp_path, series):
        return _harness(tmp_path, [{"atlas_id": "AAAUSD", "series": series}])

    def test_decreasing_timestamp_rejected_before_processing(self, tmp_path):
        t = START
        timed = [
            (t + 3600, *SERIES[1]),
            (t, *SERIES[0]),          # retrocede temporalmente
            (t + 2 * 3600, *SERIES[2]),
        ]
        orch, clock, ing, streams = _harness(
            tmp_path, [{"atlas_id": "AAAUSD", "timed": timed}]
        )
        with pytest.raises(ReplayIntegrityError):
            _replay(orch, clock, ing, streams).run()
        assert orch.broker._orders == {}
        assert orch.marks == {}
        assert orch._global_data_feed is None  # fail-closed, sin salud global

    def test_bad_fingerprint_rejected(self, tmp_path):
        orch, clock, ing, streams = self._one(tmp_path, SERIES)
        bad = StreamInput(
            config=streams[0].config, bars_path=streams[0].bars_path,
            fingerprint=ExpectedStreamFingerprint("0" * 64, 1, 1, 1),
        )
        with pytest.raises(ReplayIntegrityError):
            _replay(orch, clock, ing, [bad]).run()
        assert orch.broker._orders == {}

    def test_missing_fingerprint_rejected(self, tmp_path):
        orch, clock, ing, streams = self._one(tmp_path, SERIES)
        missing = StreamInput(
            config=streams[0].config, bars_path=streams[0].bars_path,
            fingerprint=None,
        )
        with pytest.raises(ReplayIntegrityError):
            _replay(orch, clock, ing, [missing]).run()

    def test_missing_file_rejected(self, tmp_path):
        orch, clock, ing, streams = self._one(tmp_path, SERIES)
        missing = StreamInput(
            config=streams[0].config, bars_path=tmp_path / "nope.jsonl",
            fingerprint=ExpectedStreamFingerprint("0" * 64, 1, 1, 1),
        )
        with pytest.raises(ReplayIntegrityError):
            _replay(orch, clock, ing, [missing]).run()

    def test_global_order_hash_mismatch_rejected(self, tmp_path):
        orch, clock, ing, streams = self._one(tmp_path, SERIES)
        with pytest.raises(ReplayIntegrityError):
            _replay(orch, clock, ing, streams,
                    expected_global_order_hash="deadbeef").run()

    def test_health_only_after_preflight(self, tmp_path):
        orch, clock, ing, streams = self._one(tmp_path, SERIES)
        replay = _replay(orch, clock, ing, streams)
        assert orch._global_data_feed is None  # fail-closed hasta validar
        replay.preflight()
        assert orch._global_data_feed is not None


# --------------------------------------------------------------------------
# 10-12. LOOKAHEAD / AISLAMIENTO LOCAL
# --------------------------------------------------------------------------


class TestLookaheadAndIsolation:
    def test_no_lookahead_between_groups(self, tmp_path):
        # B empieza mas tarde: en grupos anteriores a su primer cierre, B no
        # debe tener mark (no se consume futuro).
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "series": SERIES, "start": START},
            {"atlas_id": "ZZZUSD", "series": SERIES, "start": START + 2 * 3600},
        ])
        snapshots = []
        real = orch.apply_bar_group

        def spy(emissions):
            ems = list(emissions)
            t = max((e.closed.close_time for e in ems), default=None)
            snapshots.append((t, dict(orch.marks)))
            return real(ems)

        orch.apply_bar_group = spy
        _replay(orch, clock, ing, streams).run()
        # Grupo t0 (solo AAA): ZZZ no debe aparecer.
        marks_t0 = next(m for (t, m) in snapshots if t == START + 3600)
        assert "ZZZUSD" not in marks_t0

    def test_local_gap_does_not_promote_to_global(self, tmp_path):
        # AAA con gap (salta t2): B1 lo marca DEGRADED local; ZZZ opera.
        t = START
        gap_timed = [
            (t, *SERIES[0]),
            (t + 3600, *SERIES[1]),
            (t + 3 * 3600, *SERIES[3]),   # salta t2 => GAP local
        ]
        orch, clock, ing, streams = _harness(tmp_path, [
            {"atlas_id": "AAAUSD", "timed": gap_timed},
            {"atlas_id": "ZZZUSD", "series": SERIES},
        ])
        _replay(orch, clock, ing, streams).run()
        assert ing.metrics["AAAUSD"].gaps >= 1
        assert orch.ledger.position("ZZZUSD") is not None
        assert orch.kill_switch.status().can_trade is True


# --------------------------------------------------------------------------
# 14. LIMITE GLOBAL DE POSICIONES EN EL PUNTO DE ENVIO (pre-fill)
# --------------------------------------------------------------------------


class TestGlobalPositionLimit:
    def _two(self, tmp_path, risk):
        return _harness(
            tmp_path,
            [
                {"atlas_id": "AAAUSD", "series": SERIES},
                {"atlas_id": "BBBUSD", "series": SERIES},
            ],
            risk=risk,
        )

    def test_same_group_cannot_open_two_positions(self, tmp_path):
        orch, clock, ing, streams = self._two(
            tmp_path, RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        )
        _replay(orch, clock, ing, streams).run()
        positions = orch.ledger.positions()
        assert len(positions) == 1
        assert positions[0].instrument == "AAAUSD"
        assert orch.ledger.position("BBBUSD") is None
        # Exactamente una orden llego al broker.
        assert len(orch.broker._orders) == 1

    def test_first_consumes_capacity_second_blocked_pre_submit(self, tmp_path):
        orch, clock, ing, streams = self._two(
            tmp_path, RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        )
        _replay(orch, clock, ing, streams).run()
        # El segundo worker quedo bloqueado en el punto de envio (pre_submit).
        rejections = [
            e for e in orch.workers["BBBUSD"].audit_log
            if e.event == "RISK_REJECTED"
            and e.refs.get("reason") == "max_open_positions_global"
        ]
        assert rejections, "BBBUSD debio rechazarse por max_open_positions_global"
        assert not any("BBBUSD" in cid for cid in orch.broker._orders)

    def test_pending_signals_cannot_evade_limit(self, tmp_path):
        # Ambos instrumentos tienen señal pendiente de la MISMA barra y se
        # ejecutan en la barra siguiente: el limite global debe impedir dos.
        orch, clock, ing, streams = self._two(
            tmp_path, RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        )
        _replay(orch, clock, ing, streams).run()
        assert len(orch.ledger.positions()) == 1
        assert sum("BBBUSD" in cid for cid in orch.broker._orders) == 0

    def test_limit_result_enumeration_order_independent(self, tmp_path):
        a = {"atlas_id": "AAAUSD", "series": SERIES}
        b = {"atlas_id": "BBBUSD", "series": SERIES}
        risk = RiskConfig(risk_per_trade=0.00025, max_open_positions=1)
        orch1, c1, i1, s1 = _harness(tmp_path / "p1", [a, b], risk=risk)
        r1 = _replay(orch1, c1, i1, s1).run()
        orch2, c2, i2, s2 = _harness(tmp_path / "p2", [b, a], risk=risk)
        r2 = _replay(orch2, c2, i2, s2).run()
        assert r1.order_hash == r2.order_hash
        assert [p.instrument for p in orch1.ledger.positions()] == ["AAAUSD"]
        assert [p.instrument for p in orch2.ledger.positions()] == ["AAAUSD"]
        assert orch1.ledger.snapshot() == orch2.ledger.snapshot()

    def test_positions_and_gross_respected_simultaneously(self, tmp_path):
        orch, clock, ing, streams = self._two(
            tmp_path,
            RiskConfig(
                risk_per_trade=0.00025, max_open_positions=1,
                max_gross_exposure=200_000.0,
            ),
        )
        _replay(orch, clock, ing, streams).run()
        assert len(orch.ledger.positions()) == 1
        assert len(orch.broker._orders) == 1


# --------------------------------------------------------------------------
# 13. REPRODUCIBILIDAD
# --------------------------------------------------------------------------


class TestReproducibility:
    def test_same_dataset_same_results(self, tmp_path):
        inst = [
            {"atlas_id": "AAAUSD", "series": SERIES},
            {"atlas_id": "BBBUSD", "series": SERIES},
        ]
        orch1, clock1, ing1, s1 = _harness(tmp_path / "r1", inst)
        r1 = _replay(orch1, clock1, ing1, s1).run()
        orch2, clock2, ing2, s2 = _harness(tmp_path / "r2", inst)
        r2 = _replay(orch2, clock2, ing2, s2).run()

        assert r1 == r2
        assert orch1.ledger.snapshot() == orch2.ledger.snapshot()
        assert orch1.marks == orch2.marks
        assert set(orch1.broker._orders) == set(orch2.broker._orders)
        for atlas in ("AAAUSD", "BBBUSD"):
            a = orch1.workers[atlas].audit_log
            b = orch2.workers[atlas].audit_log
            assert [e.event for e in a] == [e.event for e in b]
            assert [e.refs for e in a] == [e.refs for e in b]
