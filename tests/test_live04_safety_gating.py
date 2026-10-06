"""Tests LIVE-04: health, kill switch y safety gating (camino real a submit).

Contrato congelado (LIVE-04):
  * una unica fuente de verdad de `can_trade` = `KillSwitchCoordinator.status()`,
    alimentada por Monitoring (B6) a partir de observaciones REALES;
  * market data: HEALTHY/DEGRADED => no bloquea; STALE/FAILED/UNKNOWN => bloquea;
  * broker unhealthy / desconectado => no submit;
  * kill switch activo => no submit;
  * UNKNOWN order pendiente => no resubmit;
  * safety rejection => ni broker.submit_order ni mutacion de ledger;
  * lookup/reconciliacion de ordenes existentes NO se bloquea por un veto de
    nuevas entradas;
  * el estado real de `LiveDataEngine` ya no queda oculto por HEALTHY hardcodeado.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution.order import Order, OrderSide, OrderType
from killswitches.models import KillSource, KillSwitchState
from risk.engine import RiskEngine
from risk.types import RiskConfig

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    START,
    _LevelLong,
    _audit_events,
    _calendar,
    _closed_bar,
    _drive,
    _stack,
)
from tests.test_forward_runner import _at, _closed, _make, FLAT  # noqa: E402

UTC = timezone.utc


def _no_submit(env) -> bool:
    return "ENTRY_SUBMITTED" not in _audit_events(env) and env.broker._orders == {}


class TestMarketDataGating:
    def test_healthy_feed_allows_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("CONNECTED", "HEALTHY")
        _drive(env, HAPPY[:3])
        assert "ENTRY_SUBMITTED" in _audit_events(env)
        assert len(env.broker._orders) == 1

    def test_stale_feed_blocks_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("STALE", "STALE")
        _drive(env, HAPPY[:3])
        assert _no_submit(env)
        assert env.engine.ledger.cash == 1_000_000.0
        assert env.engine.ledger.positions() == ()

    def test_failed_feed_blocks_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("HALTED", "HEALTHY")
        _drive(env, HAPPY[:3])
        assert _no_submit(env)

    def test_disconnected_feed_blocks_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("DISCONNECTED", "HEALTHY")
        _drive(env, HAPPY[:3])
        assert _no_submit(env)

    def test_unknown_feed_fails_closed(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("CONNECTING", "HEALTHY")
        _drive(env, HAPPY[:3])
        assert _no_submit(env)

    def test_degraded_feed_does_not_block(self):
        # Contrato: DEGRADED se tolera (no es fallo para el switch).
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("DEGRADED", "GAP_DETECTED")
        _drive(env, HAPPY[:3])
        assert "ENTRY_SUBMITTED" in _audit_events(env)

    def test_engine_uses_injected_state_not_hardcoded(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.note_data_feed("STALE", "STALE")
        env.engine.run_monitoring()
        assert not env.kill_switch.status().can_trade


class TestBrokerAndKillSwitchGating:
    def test_broker_unhealthy_blocks_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # stagea la senal en B2
        env.broker.disconnect()
        _drive(env, HAPPY[2:3], start=START + 2 * 3600)
        assert _no_submit(env)

    def test_kill_switch_active_blocks_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])
        env.kill_switch.set_source(
            KillSource.MANUAL, KillSwitchState.BLOCK_NEW_ORDERS, "test"
        )
        _drive(env, HAPPY[2:3], start=START + 2 * 3600)
        assert _no_submit(env)
        assert env.engine.ledger.cash == 1_000_000.0

    def test_unknown_order_pending_blocks_new_entry(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine._pending_resolution.add("PENDING-X")
        _drive(env, HAPPY[:3])
        assert _no_submit(env)

    def test_lookup_continues_when_new_entries_blocked(self):
        # Un veto de nuevas entradas NO impide resolver/adoptar una orden ya
        # enviada (lookup/reconciliacion siguen).
        env = _stack(strategy=_LevelLong(level=LEVEL))
        cid = f"TEST-LONG:{INSTRUMENT}:{START + 3600}"
        broker_order = env.broker.submit_order(
            Order(
                strategy_id="TEST-LONG", strategy_version="1.0.0",
                account_id="PAPER-01", instrument=INSTRUMENT, side=OrderSide.BUY,
                quantity=1.0, order_type=OrderType.MARKET, client_order_id=cid,
            )
        ).broker_order
        assert broker_order is not None
        o = env.order_manager.create_order(
            strategy_id="TEST-LONG", strategy_version="1.0.0",
            account_id="PAPER-01", instrument=INSTRUMENT, side=OrderSide.BUY,
            quantity=1.0, order_type=OrderType.MARKET, client_order_id=cid,
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        env.engine._pending_resolution.add(cid)
        env.kill_switch.set_source(
            KillSource.MANUAL, KillSwitchState.BLOCK_NEW_ORDERS, "test"
        )
        env.engine._resolve_pending_submissions()
        assert cid not in env.engine._pending_resolution  # resuelto
        assert env.engine.ledger.position(INSTRUMENT) is not None  # fill adoptado


class TestForwardRunnerPropagation:
    def test_forward_runner_propagates_real_feed_state(self, tmp_path):
        # G-MD-C: el estado REAL de B1 se propaga; no se hardcodea HEALTHY.
        runner = _make(tmp_path, "L4-STALE")
        bar = _closed(0, FLAT)
        runner.on_closed_bar(
            bar, data_state="STALE", data_quality="STALE", at=_at(bar)
        )
        assert runner.engine._data_feed_state == "STALE"
        assert runner.engine._data_feed_quality == "STALE"
        assert not runner.engine.kill_switch.status().can_trade

    def test_forward_runner_healthy_allows(self, tmp_path):
        runner = _make(tmp_path, "L4-OK")
        bar = _closed(0, FLAT)
        runner.on_closed_bar(
            bar, data_state="CONNECTED", data_quality="HEALTHY", at=_at(bar)
        )
        assert runner.engine.kill_switch.status().can_trade


class TestDeterminism:
    def test_same_inputs_same_result(self):
        results = []
        for _ in range(2):
            env = _stack(strategy=_LevelLong(level=LEVEL))
            env.engine.note_data_feed("STALE", "STALE")
            _drive(env, HAPPY[:3])
            results.append(_no_submit(env))
        assert results == [True, True]


class TestDefenseInDepth:
    """`can_trade` = decision del KILL SWITCH, no la unica autorizacion.

    Demuestra que cada gate bloquea independientemente aunque el kill switch
    este sano, y que ninguno abre lo que otro rechaza.
    """

    def test_session_closed_blocks_even_with_healthy_kill_switch(self):
        env = _stack(
            strategy=_LevelLong(level=LEVEL),
            calendar=_calendar(open_minute=600, close_minute=720),  # 10:00-12:00Z
        )
        _drive(env, HAPPY[:3])  # barras a 15:00-18:00Z -> sesion CLOSED
        assert env.kill_switch.status().can_trade  # kill switch NO bloquea
        assert _no_submit(env)
        assert "ENTRY_SKIPPED_SESSION" in _audit_events(env)

    def test_broker_unhealthy_blocks_even_with_healthy_kill_switch(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # stagea; kill switch sano
        assert env.kill_switch.status().can_trade
        env.broker.disconnect()
        _drive(env, HAPPY[2:3], start=START + 2 * 3600)
        assert _no_submit(env)
        assert "ENTRY_SKIPPED_BROKER" in _audit_events(env)

    def test_market_data_stale_blocks_even_with_healthy_kill_switch(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:2])  # B2 fresca -> kill switch sano
        assert env.kill_switch.status().can_trade
        # B3: el reloj avanza > data_max_age_s antes del submit (feed stale),
        # pero el kill switch (de B2) sigue sano: bloquea el gate LIVE-03.
        bar3 = _closed_bar(START + 2 * 3600, HAPPY[2])
        env.clock.set(
            datetime.fromtimestamp(START + 3 * 3600, tz=UTC)
            + timedelta(seconds=120)
        )
        env.engine.on_closed_bar(bar3)
        assert _no_submit(env)
        stages = [e.refs.get("stage") for e in env.engine.audit_log]
        assert "market_data_pre_submit" in stages

    def test_projected_risk_rejects_even_with_healthy_kill_switch(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.engine.risk_engine = RiskEngine(
            RiskConfig(risk_per_trade=0.00025, max_gross_exposure=1.0)
        )
        _drive(env, HAPPY[:3])
        assert env.kill_switch.status().can_trade  # kill switch NO bloquea
        assert _no_submit(env)
        stages = [e.refs.get("stage") for e in env.engine.audit_log]
        assert "projected_pre_fill" in stages

    def test_all_gates_healthy_allows_submit(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        assert env.kill_switch.status().can_trade
        assert "ENTRY_SUBMITTED" in _audit_events(env)
        assert len(env.broker._orders) == 1
