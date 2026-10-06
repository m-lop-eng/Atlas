"""Tests LIVE-02: identidad de orden y conservacion de la correlacion.

Contrato congelado (LIVE-02):
  * identidad interna estable = `client_order_id` (obligatoria);
  * el `broker_order_id` observado se correlaciona en la orden interna en cuanto
    se conoce (submit confirmado o resolucion por lookup);
  * timeout/perdida de respuesta => UNKNOWN (no FAILED), sin resubmit automatico;
  * lookup por `client_order_id`; distinguir "no encontrada" de "respuesta
    desconocida";
  * idempotencia de fills por `(client_order_id, fill_sequence)`.

Estos tests NO implementan un broker live; congelan el contrato dentro del
dominio interno (paper).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers import BrokerOrderState, PaperBrokerAdapter
from brokers.models import BrokerConnectionError, OrderNotFoundError, SubmitStatus
from brokers.reconcile import resolve_order
from execution.order import Order, OrderSide, OrderType
from papertrading.ledger import AccountLedger

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

ENTRY_CID = f"TEST-LONG:{INSTRUMENT}:{START + 3600}"


def _internal_order(env, cid: str):
    return next(
        o for o in env.engine.order_manager.orders.values()
        if o.client_order_id == cid
    )


def _paper(*, cash: float = 100_000.0, price: float = 50_000.0) -> PaperBrokerAdapter:
    adapter = PaperBrokerAdapter(
        account_id="PAPER-1",
        initial_cash=cash,
        price_source=lambda instrument: price,
        clock=lambda: datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    adapter.connect()
    return adapter


def _order(cid: str, *, quantity: float = 1.0) -> Order:
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="PAPER-1",
        instrument="BTCUSDT", side=OrderSide.BUY, quantity=quantity,
        order_type=OrderType.MARKET, client_order_id=cid,
    )


# --------------------------------------------------------------------------
# Correlacion client_order_id <-> broker_order_id
# --------------------------------------------------------------------------


class TestOrderIdCorrelation:
    def test_confirmed_submit_correlates_broker_order_id(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # B2 decide LONG, B3 ejecuta
        internal = _internal_order(env, ENTRY_CID)
        broker_order = env.broker._orders[ENTRY_CID]
        assert internal.broker_order_id == broker_order.broker_order_id
        assert internal.broker_order_id is not None

    def test_unknown_submit_correlates_after_lookup_resolution(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _drive(env, HAPPY[:3])  # submit UNKNOWN -> resolucion por lookup
        assert "SUBMISSION_UNKNOWN" in _audit_events(env)
        assert "SUBMISSION_RESOLVED" in _audit_events(env)
        internal = _internal_order(env, ENTRY_CID)
        broker_order = env.broker._orders[ENTRY_CID]
        assert internal.broker_order_id == broker_order.broker_order_id

    def test_no_resubmit_while_unknown(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _drive(env, HAPPY[:3])
        events = _audit_events(env)
        # Una sola entrada enviada; el broker tiene una sola orden para el cid.
        assert events.count("ENTRY_SUBMITTED") == 1
        assert list(env.broker._orders) == [ENTRY_CID]


# --------------------------------------------------------------------------
# Lookup: "no encontrada" != "respuesta desconocida"
# --------------------------------------------------------------------------


class TestLookupSemantics:
    def test_lossy_order_is_lookupable_by_client_order_id(self):
        adapter = _paper()
        adapter.simulate_response_loss("LOSSY")
        sub = adapter.submit_order(_order("LOSSY"))
        assert sub.status is SubmitStatus.UNKNOWN  # respuesta perdida
        # El broker SI tiene la orden: lookup por client_order_id la resuelve.
        found = adapter.get_order("LOSSY")
        assert found is not None
        assert found.state is BrokerOrderState.FILLED

    def test_unknown_client_order_id_is_not_found(self):
        adapter = _paper()
        # Cid nunca enviado -> el broker no lo conoce (None != UNKNOWN).
        assert adapter.get_order("NEVER-SENT") is None


class TestLookupNotFoundVsUncertain:
    """LIVE-02: NOT_FOUND (negativa autoritativa) != LOOKUP_UNCERTAIN (timeout)."""

    def test_not_found_is_authoritative_negative_not_transport_error(self):
        adapter = _paper()
        # NOT_FOUND: el broker responde que no conoce la orden (None, sin excepcion).
        assert adapter.get_order("NEVER-SENT") is None
        # resolve_order lo clasifica como OrderNotFoundError (NO es transporte).
        with pytest.raises(OrderNotFoundError):
            resolve_order(adapter, "NEVER-SENT")

    def test_transport_error_is_not_not_found(self):
        adapter = _paper()
        adapter.disconnect()
        # Sin respuesta: error de transporte, NO "no encontrada".
        with pytest.raises(BrokerConnectionError):
            adapter.get_order("ANY")
        with pytest.raises(BrokerConnectionError):
            resolve_order(adapter, "ANY")

    def _submitted_order(self, env, cid: str):
        o = env.order_manager.create_order(
            strategy_id="s", strategy_version="1.0.0", account_id="PAPER-01",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.MARKET, client_order_id=cid,
        )
        env.order_manager.approve(o.order_id)
        env.order_manager.submit(o.order_id)
        return o

    def test_uncertain_lookup_keeps_unknown_pending_no_terminal(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        o = self._submitted_order(env, "UNK-UNCERTAIN")
        before = env.order_manager.orders[o.order_id].status
        env.engine._pending_resolution.add("UNK-UNCERTAIN")
        env.broker.disconnect()  # lookup -> BrokerConnectionError
        env.engine._resolve_pending_submissions()
        after = env.order_manager.orders[o.order_id]
        assert "UNK-UNCERTAIN" in env.engine._pending_resolution  # sigue pendiente
        assert after.status == before and not after.status.terminal  # sin mutacion
        assert env.engine.position is None  # sin exposicion fabricada

    def test_not_found_resolution_keeps_unknown_pending_no_terminal(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        o = self._submitted_order(env, "UNK-NOTFOUND")
        before = env.order_manager.orders[o.order_id].status
        env.engine._pending_resolution.add("UNK-NOTFOUND")
        # Broker conectado y SIN conocer el cid -> get_order -> None (NOT_FOUND).
        env.engine._resolve_pending_submissions()
        after = env.order_manager.orders[o.order_id]
        assert "UNK-NOTFOUND" in env.engine._pending_resolution  # NO se descarta
        assert after.status == before and not after.status.terminal  # NO terminal
        assert env.engine.position is None

    def test_resolved_lookup_adopts_and_clears_pending(self):
        # Contraste: un lookup RESUELTO si adopta y limpia pending.
        env = _stack(strategy=_LevelLong(level=LEVEL))
        env.broker.simulate_response_loss(ENTRY_CID)
        _drive(env, HAPPY[:3])
        assert ENTRY_CID not in env.engine._pending_resolution  # resuelto
        assert env.engine.position is not None


# --------------------------------------------------------------------------
# Idempotencia de fills
# --------------------------------------------------------------------------


class TestFillIdempotency:
    def test_same_fill_identity_applied_once(self):
        led = AccountLedger(initial_cash=10_000.0)
        kwargs = dict(
            instrument="X", side="BUY", quantity=1.0, price=100.0,
            client_order_id="C-1", fill_sequence=0,
        )
        led.apply_fill(**kwargs)
        cash = led.cash
        pos = led.position("X")
        # Reaplicar el MISMO fill (misma identidad) es un no-op.
        led.apply_fill(**kwargs)
        assert led.cash == cash
        assert led.position("X") == pos
        # Un fill DISTINTO (otra fill_sequence) si se aplica.
        led.apply_fill(**{**kwargs, "fill_sequence": 1})
        assert led.position("X").quantity == pytest.approx(2.0)

    def test_engine_reapply_of_same_broker_fills_is_idempotent(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        broker_order = env.broker._orders[ENTRY_CID]
        cash = env.engine.ledger.cash
        qty = env.engine.ledger.position(INSTRUMENT).quantity
        # Reaplicar el lote del broker (mismo fill_sequence) no duplica.
        env.engine._apply_fills(ENTRY_CID, broker_order, env.clock.value)
        assert env.engine.ledger.cash == cash
        assert env.engine.ledger.position(INSTRUMENT).quantity == qty
