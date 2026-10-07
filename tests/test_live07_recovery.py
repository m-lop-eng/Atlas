"""Tests LIVE-07: startup, reconnect y recovery operativo seguro (paper).

Contrato congelado (LIVE-07), SIN broker real ni persistencia nueva:

  * `UNKNOWN` de submit se refleja en el estado INTERNO de la orden
    (`OrderStatus.UNKNOWN`, transitorio; S-04-a) y solo sale por resolucion
    explicita del broker (lookup): NUNCA resubmit ciego;
  * `UNKNOWN` no resuelto => la orden interna permanece observable
    (Monitoring/EXECUTION) y el sistema NO autoriza nuevas entradas;
  * `reconnect != recovered`: la autoridad de recovery es
    `KillSwitchCoordinator` (B5) alimentado por Monitoring (B6) y
    Reconciliacion (B4); no se crea un segundo kill switch;
  * reconciliacion: broker observado NUNCA sobrescribe el ledger interno de
    forma silenciosa (mismatch critico => RECONCILIATION_BLOCKED);
  * idempotencia de fills `(client_order_id, fill_sequence)`.

LIMITES DEL CONTRATO (GAP-2 / GAP-2a — PERSISTENCE GAP, NO implementado):
  * `_pending_resolution` mantiene el UNKNOWN durante la MISMA vida del
    proceso; NO se persiste en `snapshot()`.
  * El replay de `bars.jsonl` NO es evidencia del estado real del broker: un
    submit live aceptado antes de un crash NO puede inferirse del replay.
  * Resolver un UNKNOWN real tras restart exige recuperar de forma fiable
    `client_order_id` + intent/metadata + relacion `client_order_id <->
    broker_order_id`; sin eso, queda fuera de contrato (operador), NUNCA se
    auto-resuelve ni se reenvia.

Estos tests NO reabren F-06-RECOVERY (`load_snapshot` = test/support) y no
cambian el mecanismo A4.
"""

from __future__ import annotations

from datetime import datetime, timezone

from brokers.models import (
    BrokerConnectionError,
    BrokerOrder,
    BrokerOrderState,
    Position,
)
from execution.order import Order, OrderSide, OrderStatus, OrderType
from killswitches.models import KillSource, KillSwitchState
from monitoring.checks import execution_check
from monitoring.models import HealthStatus, MonitorConfig
from monitoring.wiring import observe_execution
from reconciliation.models import ReconciliationStatus

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    START,
    _LevelLong,
    _audit_events,
    _drive,
    _stack,
)

UTC = timezone.utc
ENTRY_CID = f"TEST-LONG:{INSTRUMENT}:{START + 3600}"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _internal(env, cid: str):
    return next(
        (o for o in env.order_manager.orders.values() if o.client_order_id == cid),
        None,
    )


def _patch_uncertain(env, cid: str) -> None:
    """Hace que el lookup de `cid` sea LOOKUP_UNCERTAIN (sin resubmit)."""
    real = env.broker.get_order

    def flaky(client_order_id):
        if client_order_id == cid:
            raise BrokerConnectionError("lookup incierto (test)")
        return real(client_order_id)

    env.broker.get_order = flaky


def _phantom_order(cid: str, *, broker_order_id: str = "BRK-PHANTOM") -> BrokerOrder:
    return BrokerOrder(
        client_order_id=cid,
        broker_order_id=broker_order_id,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        state=BrokerOrderState.SUBMITTED,
    )


# --------------------------------------------------------------------------
# ORDEN: el submit UNKNOWN se refleja internamente y no se reenvia
# --------------------------------------------------------------------------


class TestUnknownOrderWiring:
    def test_submit_unknown_marks_internal_order_unknown(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _patch_uncertain(env, ENTRY_CID)
        _drive(env, HAPPY[:3])

        order = _internal(env, ENTRY_CID)
        assert order is not None
        assert order.status is OrderStatus.UNKNOWN
        assert ENTRY_CID in env.engine._pending_resolution
        # NUNCA se reenvia: una sola orden en el broker para ese cid.
        assert list(env.broker._orders) == [ENTRY_CID]
        assert _audit_events(env).count("ENTRY_SUBMITTED") == 1

    def test_unresolved_unknown_surfaces_in_execution_monitoring(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _patch_uncertain(env, ENTRY_CID)
        _drive(env, HAPPY[:3])

        obs = observe_execution(env.order_manager.orders.values())
        assert obs.unknown_orders == 1
        status, _reason, metrics = execution_check(
            obs, NOW, MonitorConfig()
        )
        assert status is HealthStatus.FAILED
        assert metrics["unknown_orders"] == 1

    def test_unresolved_unknown_blocks_trade_via_authority(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _patch_uncertain(env, ENTRY_CID)
        _drive(env, HAPPY[:3])

        assert not env.kill_switch.status().can_trade
        # recovery_required: solo un recover explicito rehabilita.
        assert env.kill_switch.status().recovery_required is True

    def test_unresolved_unknown_no_new_entry_staged(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _patch_uncertain(env, ENTRY_CID)
        _drive(env, HAPPY[:4])  # HAPPY[3] tambien señalaria LONG

        assert _audit_events(env).count("ENTRY_SUBMITTED") == 1
        assert "ENTRY_SKIPPED_BROKER" in _audit_events(env)

    def test_unknown_resolved_lookup_transitions_to_filled(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _drive(env, HAPPY[:3])  # lookup resuelve en la misma barra

        order = _internal(env, ENTRY_CID)
        assert order.status is OrderStatus.FILLED
        assert ENTRY_CID not in env.engine._pending_resolution
        assert order.broker_order_id == (
            env.broker._orders[ENTRY_CID].broker_order_id
        )


class TestUnknownResolutionStates:
    """Resolucion por lookup: el estado interno sigue la verdad del broker."""

    def _unresolved(self, cid: str):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        o = env.order_manager.create_order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.MARKET, client_order_id=cid,
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        env.order_manager.mark_unknown(o.order_id)
        env.engine._pending_resolution.add(cid)
        return env, o

    def test_resolution_to_submitted(self):
        env, o = self._unresolved("L7-SUBMITTED")

        def fake_get(cid):
            return BrokerOrder(
                client_order_id=cid, broker_order_id="BRK-S",
                instrument=INSTRUMENT, side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=1.0,
                state=BrokerOrderState.SUBMITTED,
            )

        env.broker.get_order = fake_get
        env.engine._resolve_pending_submissions()
        assert env.order_manager.orders[o.order_id].status is OrderStatus.SUBMITTED
        assert "L7-SUBMITTED" not in env.engine._pending_resolution

    def test_resolution_to_rejected(self):
        env, o = self._unresolved("L7-REJECTED")

        def fake_get(cid):
            return BrokerOrder(
                client_order_id=cid, broker_order_id="BRK-R",
                instrument=INSTRUMENT, side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=1.0,
                state=BrokerOrderState.REJECTED, reject_reason="motivo",
            )

        env.broker.get_order = fake_get
        env.engine._resolve_pending_submissions()
        order = env.order_manager.orders[o.order_id]
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason == "motivo"
        assert "L7-REJECTED" not in env.engine._pending_resolution

    def test_resolution_to_cancelled(self):
        env, o = self._unresolved("L7-CANCELLED")

        def fake_get(cid):
            return BrokerOrder(
                client_order_id=cid, broker_order_id="BRK-C",
                instrument=INSTRUMENT, side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=1.0,
                state=BrokerOrderState.CANCELLED,
            )

        env.broker.get_order = fake_get
        env.engine._resolve_pending_submissions()
        assert env.order_manager.orders[o.order_id].status is OrderStatus.CANCELLED

    def test_not_found_keeps_unknown_pending(self):
        env, o = self._unresolved("L7-NOTFOUND")
        # Broker conectado pero SIN conocer el cid -> get_order -> None.
        env.engine._resolve_pending_submissions()
        assert env.order_manager.orders[o.order_id].status is OrderStatus.UNKNOWN
        assert "L7-NOTFOUND" in env.engine._pending_resolution

    def test_lookup_uncertain_keeps_unknown_pending(self):
        env, o = self._unresolved("L7-UNCERTAIN")

        def flaky(cid):
            raise BrokerConnectionError("transporte (test)")

        env.broker.get_order = flaky
        env.engine._resolve_pending_submissions()
        assert env.order_manager.orders[o.order_id].status is OrderStatus.UNKNOWN
        assert "L7-UNCERTAIN" in env.engine._pending_resolution


# --------------------------------------------------------------------------
# GAP-2 / GAP-2a (PERSISTENCE GAP): documentacion del limite actual.
# NO implementa persistencia; el replay NO prueba el estado del broker.
# --------------------------------------------------------------------------


class TestRecoveryPersistenceGap:
    def _unknown_pending(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _patch_uncertain(env, ENTRY_CID)
        _drive(env, HAPPY[:3])
        assert _internal(env, ENTRY_CID).status is OrderStatus.UNKNOWN
        assert ENTRY_CID in env.engine._pending_resolution
        return env

    def test_snapshot_does_not_persist_unknown_order_intent(self):
        # CONTRACT TEST (GAP-2/GAP-2a): el `snapshot()` del engine es un
        # artefacto de lineage (ledger + broker observed), NO un write-ahead
        # log del intent operativo. Ni `OrderManager` ni `_pending_resolution`
        # se persisten, de modo que un restore/replay por si solo NO puede
        # confirmar el estado broker de un submit real.
        env = self._unknown_pending()
        snap = env.engine.snapshot()
        assert "orders" not in snap
        assert "pending_resolution" not in snap
        assert "order_intent" not in snap
        # El UNKNOWN sigue vivo en el proceso; NO se fabrico FILLED/CANCELLED.
        assert _internal(env, ENTRY_CID).status is OrderStatus.UNKNOWN

    def test_restart_without_identity_is_blocked_not_resubmitted(self):
        # Modela un submit ACEPTADO por el broker antes de un crash, cuya
        # identidad local se perdio: la orden existe en el broker pero NO en
        # Atlas. La reconciliacion debe BLOQUEAR (MISSING_INTERNAL), nunca
        # adoptarla ni reenviarla.
        env = _stack(strategy=_LevelLong(level=LEVEL))
        accepted = Order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.LIMIT, client_order_id="CRASH-1",
            limit_price=49_000.0,
        )
        env.broker.submit_order(accepted)  # aceptada por el broker (pre-crash)
        assert "CRASH-1" in env.broker._orders

        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        env.engine.run_monitoring()
        assert not env.kill_switch.status().can_trade
        # Sin auto-adopcion ni resubmit: sigue habiendo una sola orden.
        assert set(env.broker._orders) == {"CRASH-1"}
        assert env.engine._pending_resolution == set()


# --------------------------------------------------------------------------
# RECOVERY GATE: reconnect != recovered; autoridad = B5 (via B4/B6)
# --------------------------------------------------------------------------


class TestRecoveryGate:
    def test_disconnect_blocks_and_requires_explicit_recovery(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, [HAPPY[0]])
        assert env.kill_switch.status().can_trade

        # Broker cae -> monitoring bloquea (B5 lachado).
        env.broker.disconnect()
        _drive(env, [HAPPY[1]], start=START + 3600)
        assert not env.kill_switch.status().can_trade
        assert env.kill_switch.status().recovery_required is True

        # Reconecta y reconcilia OK: se despeja la fuente pero el latch sigue.
        env.broker.connect()
        _drive(env, [HAPPY[2]], start=START + 2 * 3600)
        assert not env.kill_switch.status().can_trade
        assert env.kill_switch.status().recovery_required is True

        # Solo un recovery EXPLICITO rehabilita.
        result = env.kill_switch.recover(reason="reconectado y reconciliado")
        assert result.accepted
        assert env.kill_switch.status().can_trade

    def test_no_submit_while_recovery_required(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.kill_switch.set_source(
            KillSource.SYSTEM,
            KillSwitchState.HALT,
            detail="recovery pendiente (test)",
        )
        _drive(env, HAPPY[:3])
        assert env.broker._orders == {}
        assert env.engine.position is None

    def test_healthy_stack_allows_entry(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        assert env.engine.position is not None
        assert env.engine.last_report.status is ReconciliationStatus.RECONCILIATION_OK


# --------------------------------------------------------------------------
# ORDERS observadas del broker: no adopcion silenciosa
# --------------------------------------------------------------------------


class TestObservedOrders:
    def test_unexpected_broker_open_order_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        env.broker.get_open_orders = lambda: [_phantom_order("PHANTOM")]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any(d.key == "PHANTOM" for d in report.diffs)
        # El ledger interno NO adopta la orden desconocida.
        assert _internal(env, "PHANTOM") is None

    def test_internal_pending_order_missing_at_broker_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        o = env.order_manager.create_order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.MARKET, client_order_id="LOCAL-ONLY",
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED

    def test_broker_order_id_mismatch_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        o = env.order_manager.create_order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.MARKET, client_order_id="L7-ID",
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        env.order_manager.set_broker_order_id(o.order_id, "BRK-1")
        env.broker.get_open_orders = lambda: [_phantom_order("L7-ID", broker_order_id="BRK-2")]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any(d.key == "L7-ID/broker_order_id" for d in report.diffs)


# --------------------------------------------------------------------------
# POSICIONES observadas: no overwrite silencioso
# --------------------------------------------------------------------------


class TestObservedPositions:
    def test_quantity_mismatch_blocks_without_overwrite(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        qty = env.engine.ledger.position(INSTRUMENT).quantity
        env.broker.get_positions = lambda: [
            Position(INSTRUMENT, qty + 5.0, 50_000.0, NOW)
        ]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert env.engine.ledger.position(INSTRUMENT).quantity == qty

    def test_unexpected_position_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:1])
        env.broker.get_positions = lambda: [
            Position("OTRO", 3.0, 10.0, NOW)
        ]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED

    def test_sign_mismatch_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        qty = env.engine.ledger.position(INSTRUMENT).quantity
        env.broker.get_positions = lambda: [
            Position(INSTRUMENT, -abs(qty), 50_000.0, NOW)
        ]
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED


# --------------------------------------------------------------------------
# CUENTA observada
# --------------------------------------------------------------------------


class TestObservedAccount:
    def test_currency_mismatch_blocks(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        env.broker.currency = "EUR"  # interno configurado en USD
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert any(d.key == "account/currency" for d in report.diffs)

    def test_account_unavailable_is_unknown(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        env.broker.disconnect()
        report = env.engine.reconcile()
        assert report.status is ReconciliationStatus.RECONCILIATION_UNKNOWN


# --------------------------------------------------------------------------
# ATOMICIDAD: crash-safe por reconciliacion + idempotencia (no transaccion
# distribuida)
# --------------------------------------------------------------------------


class TestCrashAtomicity:
    def test_fill_redelivery_does_not_double_account(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        broker_order = env.broker._orders[ENTRY_CID]
        cash = env.engine.ledger.cash
        qty = env.engine.ledger.position(INSTRUMENT).quantity
        # Re-entrega del MISMO lote (misma identidad) tras un "crash".
        env.engine._apply_fills(ENTRY_CID, broker_order, NOW)
        env.engine._apply_fills(ENTRY_CID, broker_order, NOW)
        assert env.engine.ledger.cash == cash
        assert env.engine.ledger.position(INSTRUMENT).quantity == qty

    def test_unknown_never_duplicates_broker_order(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _drive(env, HAPPY[:3])
        assert len(env.broker._orders) == 1
        # Reintentar la resolucion tampoco crea una segunda orden.
        env.engine._resolve_pending_submissions()
        assert len(env.broker._orders) == 1


class TestFrozenPerimeter:
    def test_final_oos_untouched(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        frozen = root / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
        assert frozen.exists()
        assert "FROZEN" in frozen.read_text(encoding="utf-8")
