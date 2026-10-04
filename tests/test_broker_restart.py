"""Tests de F-06C-restart: persistencia/rehidratacion INDEPENDIENTE del broker.

Contrato (mini-spec F-06C-RESTART):
  * new run  : sin snapshot -> bootstrap desde configuracion.
  * restart  : snapshot con bloques `ledger` (expected) y `broker` (observed)
               que comparten `checkpoint_id`; ambos se restauran por separado.
  * independencia: el broker NO se reconstruye desde el ledger ni viceversa.
  * fail-closed : falta el bloque broker, checkpoint mismatch o bloque corrupto
                  -> error explicito (nunca un broker fresco en silencio).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers.models import BrokerOrderState
from brokers.paper import BrokerSnapshotError, PaperBrokerAdapter
from execution.order import Order, OrderSide, OrderType

from tests.test_paper_trading import (  # noqa: E402  (helpers compartidos)
    HAPPY,
    INSTRUMENT,
    LEVEL,
    START,
    _LevelLong,
    _audit_events,
    _drive,
    _stack,
)


def _now() -> datetime:
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _order(
    client_order_id: str = "CLIENT-1",
    *,
    instrument: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 1.0,
    order_type: OrderType = OrderType.MARKET,
) -> Order:
    return Order(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        account_id="PAPER-1",
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=order_type,
        client_order_id=client_order_id,
    )


def _paper(*, cash: float = 100_000.0, price: float = 50_000.0) -> PaperBrokerAdapter:
    adapter = PaperBrokerAdapter(
        account_id="PAPER-1",
        initial_cash=cash,
        price_source=lambda instrument: price,
        clock=_now,
    )
    adapter.connect()
    return adapter


# --------------------------------------------------------------------------
# Broker-level: snapshot propio / rehidratacion
# --------------------------------------------------------------------------


class TestBrokerSnapshotRoundtrip:
    def test_new_run_bootstrap_has_no_prior_state(self):
        b = _paper(cash=123_456.0)
        assert b._orders == {}
        assert b._cash == 123_456.0
        assert b.get_positions() == []

    def test_snapshot_restores_cash_orders_fills_and_identity(self):
        b1 = _paper(cash=100_000.0, price=50_000.0)
        sub = b1.submit_order(
            _order("C-1", quantity=1.0, order_type=OrderType.LIMIT)
        )
        assert sub.broker_order is not None
        b1.simulate_fill("C-1", 0.4, 51_000.0)  # parcial
        # segunda orden LIMIT queda pendiente
        b1.submit_order(
            _order("C-2", quantity=2.0, order_type=OrderType.LIMIT)
        )
        cash_before = b1._cash

        snap = b1.snapshot(checkpoint_id="CP-1")

        b2 = _paper(cash=999.0)  # bootstrap distinto a proposito
        b2.load_snapshot(snap, checkpoint_id="CP-1")

        assert b2._cash == cash_before
        assert set(b2._orders) == {"C-1", "C-2"}
        o1 = b2.get_order("C-1")
        assert o1.state is BrokerOrderState.PARTIALLY_FILLED
        assert len(o1.fills) == 1  # solo simulate_fill (LIMIT no auto-llena)
        # identity estable preservada
        assert [f.fill_sequence for f in o1.fills] == [0]
        assert o1.fills[0].quantity == pytest.approx(0.4)
        assert o1.fills[0].price == pytest.approx(51_000.0)
        o2 = b2.get_order("C-2")
        assert o2.state is BrokerOrderState.SUBMITTED

    def test_snapshot_restores_broker_sequence_no_id_collision(self):
        b1 = _paper()
        b1.submit_order(_order("C-1"))
        b1.submit_order(_order("C-2"))
        snap = b1.snapshot(checkpoint_id="CP-1")

        b2 = _paper()
        b2.load_snapshot(snap, checkpoint_id="CP-1")
        # La siguiente orden debe recibir un ID NUEVO (BRK-000003), no repetir.
        sub = b2.submit_order(_order("C-3"))
        assert sub.broker_order.broker_order_id == "BRK-000003"

    def test_snapshot_does_not_persist_marks_or_price_source(self):
        b1 = _paper()
        b1.submit_order(_order("C-1"))
        snap = b1.snapshot(checkpoint_id="CP-1")
        blob = repr(snap)
        assert "price_source" not in snap
        assert "mark" not in blob

    def test_positions_derive_from_restored_orders(self):
        b1 = _paper(price=50_000.0)
        b1.submit_order(_order("C-1", quantity=2.0))
        snap = b1.snapshot(checkpoint_id="CP-1")

        b2 = _paper(price=50_000.0)
        b2.load_snapshot(snap, checkpoint_id="CP-1")
        pos = {p.instrument: p.quantity for p in b2.get_positions()}
        assert pos == {INSTRUMENT: 2.0}

    def test_multi_fill_roundtrip_preserves_all_sequences(self):
        b1 = _paper(price=50_000.0)
        b1.submit_order(
            _order("C-1", quantity=3.0, order_type=OrderType.LIMIT)
        )
        b1.simulate_fill("C-1", 1.0, 50_100.0)
        b1.simulate_fill("C-1", 1.0, 50_200.0)
        b1.simulate_fill("C-1", 1.0, 50_300.0)
        o = b1.get_order("C-1")
        assert [f.fill_sequence for f in o.fills] == [0, 1, 2]
        cash_before = b1._cash

        snap = b1.snapshot(checkpoint_id="CP-1")
        b2 = _paper()
        b2.load_snapshot(snap, checkpoint_id="CP-1")
        o2 = b2.get_order("C-1")
        assert o2.state is BrokerOrderState.FILLED
        assert [f.fill_sequence for f in o2.fills] == [0, 1, 2]
        assert b2._cash == cash_before

    def test_lifecycle_states_survive_restart(self):
        b1 = _paper(price=50_000.0)
        b1.submit_order(_order("FILLED-1", quantity=1.0))  # MARKET -> FILLED
        b1.submit_order(
            _order("OPEN-1", quantity=1.0, order_type=OrderType.LIMIT)
        )  # -> SUBMITTED
        b1.submit_order(
            _order("CANCEL-1", quantity=1.0, order_type=OrderType.LIMIT)
        )
        b1.cancel_order("CANCEL-1")  # -> CANCELLED
        b1.submit_order(
            _order("REJECT-1", quantity=1.0, order_type=OrderType.LIMIT)
        )
        b1.simulate_rejection("REJECT-1", "motivo")  # -> REJECTED

        snap = b1.snapshot(checkpoint_id="CP-1")
        b2 = _paper()
        b2.load_snapshot(snap, checkpoint_id="CP-1")
        states = {cid: b2.get_order(cid).state for cid in b2._orders}
        assert states["FILLED-1"] is BrokerOrderState.FILLED
        assert states["OPEN-1"] is BrokerOrderState.SUBMITTED
        assert states["CANCEL-1"] is BrokerOrderState.CANCELLED
        assert states["REJECT-1"] is BrokerOrderState.REJECTED


class TestBrokerSnapshotFailModes:
    def test_missing_snapshot_fails_loud(self):
        b = _paper()
        with pytest.raises(BrokerSnapshotError):
            b.load_snapshot(None, checkpoint_id="CP-1")

    def test_corrupt_snapshot_fails_loud(self):
        b = _paper()
        with pytest.raises(BrokerSnapshotError):
            b.load_snapshot({"checkpoint_id": "CP-1", "orders": "nope"},
                            checkpoint_id="CP-1")

    def test_checkpoint_mismatch_rejected(self):
        b1 = _paper()
        b1.submit_order(_order("C-1"))
        snap = b1.snapshot(checkpoint_id="CP-1")
        b2 = _paper()
        with pytest.raises(BrokerSnapshotError):
            b2.load_snapshot(snap, checkpoint_id="CP-OTHER")


# --------------------------------------------------------------------------
# Engine-level: new run vs restart, independencia, fail-closed
# --------------------------------------------------------------------------


class TestEngineNewRunVsRestart:
    def test_new_run_bootstraps_broker_and_ledger_from_config(self):
        env = _stack(strategy=_LevelLong(level=LEVEL), initial_cash=1_000_000.0)
        # New run: no hay snapshot; ambos arrancan del MISMO initial_cash de
        # configuracion, pero son fuentes independientes.
        assert env.broker._cash == 1_000_000.0
        assert env.engine.ledger.cash == 1_000_000.0

    def test_restart_restores_both_independently(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()

        assert snap["broker"]["checkpoint_id"] == snap["checkpoint_id"]
        assert snap["ledger"]["checkpoint_id"] == snap["checkpoint_id"]

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        # Ambas fuentes restauradas y coincidentes (estado consistente).
        assert env2.engine.ledger.cash == env1.engine.ledger.cash
        assert env2.broker._cash == env1.broker._cash
        assert set(env2.broker._orders) == set(env1.broker._orders)

    def test_missing_broker_block_is_fail_closed(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        del snap["broker"]  # simula snapshot legacy / no broker-restore-capable

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        cash_before = env2.broker._cash
        with pytest.raises(BrokerSnapshotError):
            env2.engine.load_snapshot(snap)
        # NO se degrada a un broker fresco en silencio.
        assert env2.broker._cash == cash_before

    def test_checkpoint_mismatch_between_ledger_and_broker_rejected(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        snap["checkpoint_id"] = "TAMPERED"

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        with pytest.raises(BrokerSnapshotError):
            env2.engine.load_snapshot(snap)

    def test_corrupt_ledger_checkpoint_rejected(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        snap["ledger"]["checkpoint_id"] = "OTHER"

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        with pytest.raises(BrokerSnapshotError):
            env2.engine.load_snapshot(snap)


class TestRestartIndependence:
    def test_mutating_ledger_does_not_change_broker(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        broker_cash_before = env2.broker._cash
        broker_orders_before = set(env2.broker._orders)

        # Mutar deliberadamente el ledger tras el restore.
        env2.engine.ledger._cash += 12_345.0

        assert env2.broker._cash == broker_cash_before
        assert set(env2.broker._orders) == broker_orders_before

    def test_mutating_broker_does_not_change_ledger(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        ledger_cash_before = env2.engine.ledger.cash
        ledger_positions_before = env2.engine.ledger.positions()

        # Mutar deliberadamente el broker tras el restore.
        env2.broker._cash += 55_555.0

        assert env2.engine.ledger.cash == ledger_cash_before
        assert env2.engine.ledger.positions() == ledger_positions_before

    def test_snapshot_cash_divergence_is_preserved_not_copied(self):
        # CRITICAL NEGATIVE TEST: si alguien reconstruyera el broker desde el
        # ledger, ambos cash quedarian iguales (X). Deben coexistir X != Y.
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        snap["broker"]["cash"] = 777_777.0  # observed distinto del interno

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)

        assert env2.engine.ledger.cash == env1.engine.ledger.cash
        assert env2.broker._cash == 777_777.0
        assert env2.engine.ledger.cash != env2.broker._cash


class TestRestartOrderLifecycle:
    def test_restart_preserves_order_state_and_no_double_accounting(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)  # entrada + salida (2 ordenes)
        snap = env1.engine.snapshot()
        orders_before = set(env1.broker._orders)
        cash_before = env1.broker._cash

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        # Actividad historica re-procesada: no se duplica accounting.
        _drive(env2, HAPPY)
        assert set(env2.broker._orders) == orders_before
        assert env2.broker._cash == cash_before
        assert env2.engine.ledger.cash == env1.engine.ledger.cash
        assert env2.engine.trades == env1.engine.trades
        assert "RESTART_SKIP" in _audit_events(env2)


class TestBrokerRestoreAtomicity:
    def test_corrupt_order_after_cash_leaves_broker_untouched(self):
        # Broker con estado conocido.
        b = _paper(cash=100_000.0, price=50_000.0)
        b.submit_order(_order("C-1", quantity=1.0, order_type=OrderType.LIMIT))
        b.simulate_fill("C-1", 0.4, 51_000.0)
        b.submit_order(_order("C-2", quantity=2.0, order_type=OrderType.LIMIT))
        b.simulate_response_loss("C-2")

        snap = b.snapshot(checkpoint_id="CP-1")
        # El campo `cash` es valido; se corrompe una ORDEN posterior.
        snap["orders"][1]["state"] = "NO-SUCH-STATE"

        before_cash = b._cash
        before_orders = dict(b._orders)
        before_lossy = set(b._lossy)
        before_seq_last = b._broker_seq_last

        with pytest.raises(BrokerSnapshotError):
            b.load_snapshot(snap, checkpoint_id="CP-1")

        # NO debe existir mutacion parcial: todo el estado sigue identico.
        assert b._cash == before_cash
        assert b._orders == before_orders
        assert b._lossy == before_lossy
        assert b._broker_seq_last == before_seq_last
        # Los fills/estados originales permanecen.
        assert [f.fill_sequence for f in b.get_order("C-1").fills] == [0]

    def test_validate_snapshot_never_mutates(self):
        b = _paper(cash=100_000.0)
        b.submit_order(_order("C-1", quantity=1.0))
        snap = b.snapshot(checkpoint_id="CP-1")
        before = (b._cash, dict(b._orders), set(b._lossy), b._broker_seq_last)
        # Prevalidacion valida: no muta.
        b.validate_snapshot(snap, checkpoint_id="CP-1")
        assert (b._cash, b._orders, b._lossy, b._broker_seq_last) == before
        # Prevalidacion invalida: no muta y lanza.
        snap["orders"][0]["side"] = "SIDEWAYS"
        with pytest.raises(BrokerSnapshotError):
            b.validate_snapshot(snap, checkpoint_id="CP-1")
        assert (b._cash, b._orders, b._lossy, b._broker_seq_last) == before


class TestEngineRestoreAtomicity:
    def _state(self, env):
        return {
            "ledger_cash": env.engine.ledger.cash,
            "ledger_positions": env.engine.ledger.positions(),
            "ledger_realized": env.engine.ledger.realized_pnl,
            "position": env.engine.position,
            "broker_cash": env.broker._cash,
            "broker_orders": dict(env.broker._orders),
            "broker_seq_last": env.broker._broker_seq_last,
            "last_open_time": env.engine._last_open_time,
            "signed": set(env.engine._signed),
            "day_key": env.engine._day_key,
            "trades": env.engine.trades,
        }

    def test_corrupt_broker_block_leaves_engine_untouched(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        snap["broker"]["orders"][0]["state"] = "NO-SUCH-STATE"

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env2, HAPPY[:2])  # estado previo distinto y conocido
        before = self._state(env2)

        with pytest.raises(BrokerSnapshotError):
            env2.engine.load_snapshot(snap)

        after = self._state(env2)
        assert after == before  # ninguna mutacion parcial

    def test_missing_broker_block_leaves_engine_untouched(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        del snap["broker"]

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env2, HAPPY[:2])
        before = self._state(env2)

        with pytest.raises(BrokerSnapshotError):
            env2.engine.load_snapshot(snap)
        assert self._state(env2) == before

    def test_corrupt_ledger_block_leaves_engine_untouched(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        # Bloque ledger corrupto (posicion sin instrument).
        snap["ledger"]["positions"] = [{"quantity": 1.0}]

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env2, HAPPY[:2])
        before = self._state(env2)

        with pytest.raises(Exception):
            env2.engine.load_snapshot(snap)
        assert self._state(env2) == before

    def test_corrupt_trades_metadata_leaves_engine_untouched(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        snap["trades"][0]["exit_time"] = "not-a-datetime"

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env2, HAPPY[:2])
        before = self._state(env2)

        with pytest.raises(Exception):
            env2.engine.load_snapshot(snap)
        assert self._state(env2) == before
