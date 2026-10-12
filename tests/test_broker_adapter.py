"""Tests del BrokerAdapter y PaperBrokerAdapter (B3).

Batería:

  1. Contrato: métodos abstractos, errores explícitos, broker como verdad.
  2. Ciclo connect/disconnect/health con excepciones al no estar conectado.
  3. submit_order: market fill al precio de referencia; idempotencia por
     client_order_id (NUNCA duplica exposición).
  4. Estados de orden y fills: SUBMITTED, PARTIALLY_FILLED, FILLED,
     CANCELLED, REJECTED; promedio ponderado.
  5. UNKNOWN / pérdida de respuesta: `submit_order` devuelve UNKNOWN y la
     orden se resuelve con `get_order`/`resolve_order` (06 §8): NUNCA
     reenviar a ciegas.
  6. get_positions / get_account_state reflejan fills.
  7. Determinismo: mismos inputs + reloj inyectado => mismos resultados,
     sin red, sin efectos del reloj real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers import (
    BrokerConnectionError,
    BrokerOrderState,
    OrderNotFoundError,
    OrderRejectedError,
    PaperBrokerAdapter,
    SubmitStatus,
)
from brokers.models import BrokerInvalidOrderError, BrokerOrder, Position
from brokers.reconcile import is_uncertain, resolve_order
from execution.order import Order, OrderSide, OrderType


def _now() -> datetime:
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _order(
    client_order_id: str = "CLIENT-1",
    *,
    instrument: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 1.0,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
    reduce_only: bool = False,
) -> Order:
    return Order(
        strategy_id="strat-1",
        strategy_version="1.0.0",
        account_id="acc-1",
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=order_type,
        client_order_id=client_order_id,
        limit_price=limit_price,
        reduce_only=reduce_only,
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


class TestContract:
    def test_abstract_contract_present(self) -> None:
        from brokers.base import BrokerAdapter

        for method in (
            "connect", "disconnect", "health", "submit_order",
            "cancel_order", "get_order", "get_open_orders",
            "get_positions", "get_account_state",
        ):
            assert getattr(BrokerAdapter, method).__isabstractmethod__ is True

    def test_paper_implements_all_abstract(self) -> None:
        from brokers.base import BrokerAdapter

        implemented = PaperBrokerAdapter.__abstractmethods__
        assert not implemented, f"falta implementar: {implemented}"
        assert issubclass(PaperBrokerAdapter, BrokerAdapter)

    def test_broker_rejects_submit_without_connection(self) -> None:
        adapter = PaperBrokerAdapter()
        with pytest.raises(BrokerConnectionError):
            adapter.submit_order(_order())

    def test_errors_are_explicit_hierarchy(self) -> None:
        from brokers import BrokerError

        assert issubclass(BrokerConnectionError, BrokerError)
        assert issubclass(OrderNotFoundError, BrokerError)
        assert issubclass(OrderRejectedError, BrokerError)


class TestConnectHealth:
    def test_health_false_initially(self) -> None:
        adapter = PaperBrokerAdapter()
        assert adapter.health() is False

    def test_connect_then_health_true(self) -> None:
        adapter = PaperBrokerAdapter()
        adapter.connect()
        assert adapter.health() is True

    def test_disconnect_then_health_false(self) -> None:
        adapter = PaperBrokerAdapter()
        adapter.connect()
        adapter.disconnect()
        assert adapter.health() is False

    def test_query_raises_when_disconnected(self) -> None:
        adapter = PaperBrokerAdapter()
        adapter.connect()
        adapter.submit_order(_order())
        adapter.disconnect()
        with pytest.raises(BrokerConnectionError):
            adapter.get_positions()
        with pytest.raises(BrokerConnectionError):
            adapter.get_account_state()


class TestSubmitMarketFill:
    def test_market_fill_at_reference_price(self) -> None:
        adapter = _paper(price=50_000.0)
        submission = adapter.submit_order(_order(quantity=2.0))
        assert submission.status == SubmitStatus.CONFIRMED
        order = submission.broker_order
        assert order is not None
        assert order.state == BrokerOrderState.FILLED
        assert order.filled_quantity == 2.0
        assert order.avg_fill_price == 50_000.0
        assert order.broker_order_id.startswith("BRK-")

    def test_fill_updates_account_cash(self) -> None:
        adapter = _paper(cash=100_000.0, price=10_000.0)
        adapter.submit_order(_order(quantity=3.0))
        state = adapter.get_account_state()
        assert state.cash == 100_000.0 - 30_000.0
        assert state.currency == "USD"

    def test_rejection_when_insufficient_cash(self) -> None:
        adapter = _paper(cash=5_000.0, price=10_000.0)
        submission = adapter.submit_order(_order(quantity=2.0))
        assert submission.status == SubmitStatus.REJECTED
        order = submission.broker_order
        assert order is not None
        assert order.state == BrokerOrderState.REJECTED
        assert "caja" in (order.reject_reason or "")

    def test_negative_quantity_rejected_before_submit(self) -> None:
        adapter = _paper()
        with pytest.raises(Exception):
            adapter.submit_order(_order(quantity=0))


class TestIdempotency:
    def test_duplicate_client_order_id_returns_same_order(self) -> None:
        adapter = _paper()
        first = adapter.submit_order(_order("CLIENT-IDEMP", quantity=1.0))
        second = adapter.submit_order(_order("CLIENT-IDEMP", quantity=5.0))
        assert second.status == SubmitStatus.CONFIRMED
        assert second.broker_order.broker_order_id == (
            first.broker_order.broker_order_id
        )
        # NUNCA duplica exposición: el broker conserva la orden original
        assert second.broker_order.quantity == 1.0
        assert len(adapter.get_open_orders()) == 0  # fill único

    def test_no_double_position_from_retry(self) -> None:
        adapter = _paper()
        adapter.submit_order(_order("RETRY", instrument="BTCUSDT"))
        adapter.submit_order(_order("RETRY", instrument="BTCUSDT"))
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 1.0


class TestOrderStatesAndFills:
    def test_limit_order_stays_submitted(self) -> None:
        adapter = _paper(price=50_000.0)
        submission = adapter.submit_order(_order(
            "LIMIT-1", order_type=OrderType.LIMIT, limit_price=49_000.0,
        ))
        assert submission.status == SubmitStatus.CONFIRMED
        assert submission.broker_order.state == BrokerOrderState.SUBMITTED
        assert adapter.get_open_orders() == [submission.broker_order]

    def test_partial_fill_then_full_with_weighted_avg(self) -> None:
        adapter = _paper(price=50_000.0)
        order = _order("PART-1", order_type=OrderType.LIMIT,
                       limit_price=49_000.0, quantity=4.0)
        adapter.submit_order(order)
        partial = adapter.simulate_fill("PART-1", 1.0, 49_000.0)
        assert partial.state == BrokerOrderState.PARTIALLY_FILLED
        assert partial.filled_quantity == 1.0
        assert partial.avg_fill_price == 49_000.0

        full = adapter.simulate_fill("PART-1", 2.0, 50_000.0)
        assert full.state == BrokerOrderState.PARTIALLY_FILLED
        assert full.filled_quantity == 3.0

        done = adapter.simulate_fill("PART-1", 1.0, 51_000.0)
        assert done.state == BrokerOrderState.FILLED
        # promedio ponderado: (1*49k + 2*50k + 1*51k)/4
        assert done.avg_fill_price == pytest.approx(50_000.0)
        assert done.remaining == 0
        # una vez llena deja de ser abierta
        assert all(o.client_order_id != "PART-1" for o in adapter.get_open_orders())

    def test_simulate_fill_exceeding_quantity_raises(self) -> None:
        adapter = _paper()
        adapter.submit_order(_order("XFILL", order_type=OrderType.LIMIT,
                                    limit_price=10_000.0, quantity=2.0))
        with pytest.raises(Exception):
            adapter.simulate_fill("XFILL", 3.0, 10_000.0)

    def test_cancel_working_order(self) -> None:
        adapter = _paper()
        adapter.submit_order(_order("CANCEL-1", order_type=OrderType.LIMIT,
                                    limit_price=10_000.0))
        cancelled = adapter.cancel_order("CANCEL-1")
        assert cancelled.state == BrokerOrderState.CANCELLED
        assert adapter.get_order("CANCEL-1").state == BrokerOrderState.CANCELLED
        assert adapter.get_open_orders() == []

    def test_cancel_unknown_order_raises(self) -> None:
        adapter = _paper()
        with pytest.raises(OrderNotFoundError):
            adapter.cancel_order("NO-EXISTE")

    def test_cancel_filled_order_raises(self) -> None:
        adapter = _paper()
        adapter.submit_order(_order("FILLED-CANCEL"))
        with pytest.raises(OrderRejectedError):
            adapter.cancel_order("FILLED-CANCEL")

    def test_simulate_rejection_late(self) -> None:
        adapter = _paper()
        adapter.submit_order(_order("REJ-1", order_type=OrderType.LIMIT,
                                    limit_price=10_000.0))
        rejected = adapter.simulate_rejection("REJ-1", "regla del broker")
        assert rejected.state == BrokerOrderState.REJECTED
        assert rejected.reject_reason == "regla del broker"
        assert adapter.get_order("REJ-1").state == BrokerOrderState.REJECTED


class TestUnknownResolution:
    def test_response_loss_returns_unknown(self) -> None:
        adapter = _paper()
        adapter.simulate_response_loss("LOST-1")
        submission = adapter.submit_order(_order("LOST-1", quantity=1.0))
        assert submission.status == SubmitStatus.UNKNOWN
        # no sabemos si llegó, pero la orden NO se reenvía
        assert submission.broker_order is None

    def test_broker_still_holds_the_lost_order(self) -> None:
        """06 §8: la orden pudo llegar al broker pese a la pérdida de respuesta."""
        adapter = _paper()
        adapter.simulate_response_loss("LOST-2")
        adapter.submit_order(_order("LOST-2"))
        resolved = adapter.get_order("LOST-2")
        assert resolved is not None
        assert resolved.state == BrokerOrderState.FILLED  # el broker la ejecutó
        assert len(adapter.get_positions()) == 1

    def test_resolve_order_via_reconcile_primitive(self) -> None:
        adapter = _paper()
        adapter.simulate_response_loss("LOST-3")
        adapter.submit_order(_order("LOST-3", quantity=2.0))
        authoritative = resolve_order(adapter, "LOST-3")
        assert authoritative.state == BrokerOrderState.FILLED
        assert authoritative.quantity == 2.0

    def test_resolve_unknown_order_not_found(self) -> None:
        adapter = _paper()
        with pytest.raises(OrderNotFoundError):
            resolve_order(adapter, "NADIE-LA-ENVIO")

    def test_retry_after_loss_is_idempotent_no_duplicate(self) -> None:
        """El reintento NUNCA reenvía una orden perdida: pide estado primero."""
        adapter = _paper()
        adapter.simulate_response_loss("LOST-RETRY")
        first = adapter.submit_order(_order("LOST-RETRY"))
        assert first.status == SubmitStatus.UNKNOWN
        # resolver antes de cualquier acción
        resolve_order(adapter, "LOST-RETRY")
        # si el operador SI insistiera en reintentar, no se duplica:
        retry = adapter.submit_order(_order("LOST-RETRY"))
        assert retry.broker_order.broker_order_id == (
            adapter.get_order("LOST-RETRY").broker_order_id
        )
        assert len(adapter.get_positions()) == 1

    def test_is_uncertain(self) -> None:
        assert is_uncertain(BrokerOrderState.UNKNOWN) is True
        assert is_uncertain(BrokerOrderState.FILLED) is False
        assert is_uncertain(BrokerOrderState.SUBMITTED) is False


class TestPositionsAndAccount:
    def test_buy_position_after_fill(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("POS-BUY", instrument="BTCUSDT",
                                    quantity=2.0))
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].instrument == "BTCUSDT"
        assert positions[0].quantity == 2.0
        assert positions[0].avg_entry_price == 50_000.0

    def test_sell_builds_negative_position(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("POS-SELL", instrument="BTCUSDT",
                                    side=OrderSide.SELL, quantity=1.5))
        positions = adapter.get_positions()
        assert positions[0].quantity == -1.5

    def test_buy_then_sell_nets_position(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("A", instrument="BTCUSDT", quantity=2.0))
        adapter.submit_order(_order("B", instrument="BTCUSDT",
                                    side=OrderSide.SELL, quantity=0.5))
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == pytest.approx(1.5)

    def test_avg_entry_only_open_quantity_fifo(self) -> None:
        """El precio medio NO se diluye con la cantidad ya cerrada (multi-trade).

        Trade 1: buy 0.5 @80000, sell 0.5 @90000 (cierra). Reapertura: buy
        0.2 @90000. La posición abierta es 0.2 @90000; el promedio bruto de
        todos los buys del lado LONG (0.7) daría ~82857 y haría que la
        reconciliación bloquee en la 2ª operación de una sesión.
        """
        prices = iter([80_000.0, 90_000.0, 90_000.0, 90_000.0])
        adapter = PaperBrokerAdapter(
            account_id="PAPER-1",
            initial_cash=100_000.0,
            price_source=lambda instrument: next(prices),
            clock=_now,
        )
        adapter.connect()
        adapter.submit_order(_order("T1-OPEN", quantity=0.5))
        adapter.submit_order(_order("T1-CLOSE", side=OrderSide.SELL, quantity=0.5))
        adapter.submit_order(_order("T2-OPEN", quantity=0.2))

        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == pytest.approx(0.2)
        assert positions[0].avg_entry_price == pytest.approx(90_000.0)
        assert adapter.get_account_state().cash == pytest.approx(
            100_000.0 - 0.5 * 80_000.0 + 0.5 * 90_000.0 - 0.2 * 90_000.0
        )

    def test_account_equity_reflects_unrealized(self) -> None:
        adapter = _paper(cash=100_000.0, price=10_000.0)
        adapter.submit_order(_order("EQ-1", instrument="BTCUSDT", quantity=2.0))
        state = adapter.get_account_state()
        assert state.cash == 80_000.0
        assert state.equity == pytest.approx(100_000.0)  # 80k + 2*10k

    def test_account_state_timestamps(self) -> None:
        adapter = _paper()
        state = adapter.get_account_state()
        assert state.as_of == _now()


class TestAccountEquitySign:
    """F-05: equity mark-to-market correcta para LONG y SHORT (sin doble conteo).

    Invariante: para posicion abierta,
        unrealized = quantity * (mark_price - avg_entry_price)
    con `quantity` dirigida. El modelo no aplica fees (BrokerFill.commission=0).
    """

    def _open(
        self, side: OrderSide, qty: float, entry: float, mark: float, *,
        cash: float = 10_000.0,
    ) -> "PaperBrokerAdapter":
        prices = iter([entry, mark])
        adapter = PaperBrokerAdapter(
            account_id="PAPER-EQ",
            initial_cash=cash,
            price_source=lambda instrument: next(prices),
            clock=_now,
        )
        adapter.connect()
        adapter.submit_order(_order("OPEN", side=side, quantity=qty))  # fill @entry
        return adapter  # price_source ahora devuelve `mark`

    def test_long_sin_movimiento_equity_igual_a_inicial(self) -> None:
        adapter = self._open(OrderSide.BUY, 1.0, 100.0, 100.0)
        assert adapter.get_account_state().equity == pytest.approx(10_000.0)

    def test_long_con_ganancia(self) -> None:
        adapter = self._open(OrderSide.BUY, 1.0, 100.0, 110.0)
        assert adapter.get_account_state().equity == pytest.approx(10_010.0)

    def test_long_con_perdida(self) -> None:
        adapter = self._open(OrderSide.BUY, 1.0, 100.0, 90.0)
        assert adapter.get_account_state().equity == pytest.approx(9_990.0)

    def test_short_sin_movimiento_equity_igual_a_inicial(self) -> None:
        adapter = self._open(OrderSide.SELL, 1.0, 100.0, 100.0)
        state = adapter.get_account_state()
        # cash ingreso la venta (10_100); equity NO debe ser 10_100 ni 10_200.
        assert state.cash == pytest.approx(10_100.0)
        assert state.equity == pytest.approx(10_000.0)

    def test_short_ganador(self) -> None:
        adapter = self._open(OrderSide.SELL, 1.0, 100.0, 90.0)
        assert adapter.get_account_state().equity == pytest.approx(10_010.0)

    def test_short_perdedor(self) -> None:
        adapter = self._open(OrderSide.SELL, 1.0, 100.0, 110.0)
        assert adapter.get_account_state().equity == pytest.approx(9_990.0)

    def test_invariante_unrealized_con_signo(self) -> None:
        for side, qty in ((OrderSide.BUY, 2.0), (OrderSide.SELL, -3.0)):
            adapter = self._open(side, abs(qty), 100.0, 105.0)
            positions = adapter.get_positions()
            pos = positions[0]
            entry = pos.avg_entry_price
            unrealized = pos.quantity * (105.0 - entry)
            expected = 10_000.0 + unrealized
            assert adapter.get_account_state().equity == pytest.approx(expected)

    def test_posicion_cerrada_unrealized_cero(self) -> None:
        prices = iter([100.0, 120.0])
        adapter = PaperBrokerAdapter(
            account_id="PAPER-EQ", initial_cash=10_000.0,
            price_source=lambda instrument: next(prices), clock=_now,
        )
        adapter.connect()
        adapter.submit_order(_order("OPEN", side=OrderSide.BUY, quantity=1.0))  # @100
        adapter.submit_order(_order("CLOSE", side=OrderSide.SELL, quantity=1.0))  # @120
        assert adapter.get_positions() == []
        # realizado: +20 (sin fees) → equity 10_020
        assert adapter.get_account_state().equity == pytest.approx(10_020.0)

    def test_multi_fill_short_avg_y_equity(self) -> None:
        """SHORT en dos tramos + cierre parcial: avg_entry, qty y equity."""
        adapter = _paper(price=100.0)
        # SHORT 1 @100 (market), SHORT 1 @110 (market)
        adapter.submit_order(_order("S1", side=OrderSide.SELL, quantity=1.0))
        adapter.price_source = lambda instrument: 110.0
        adapter.submit_order(_order("S2", side=OrderSide.SELL, quantity=1.0))
        pos = adapter.get_positions()[0]
        assert pos.quantity == pytest.approx(-2.0)
        assert pos.avg_entry_price == pytest.approx(105.0)
        # `_paper` usa cash=100_000. mark 110: unrealized = -2*(110-105) = -10
        adapter.price_source = lambda instrument: 110.0
        assert adapter.get_account_state().equity == pytest.approx(99_990.0)


class TestFullLifecycleAccounting:
    """F-05 validacion formal: equity = cash + Σ(quantity*mark) durante todo el ciclo."""

    def _adapter(self, prices: list[float]) -> "PaperBrokerAdapter":
        """Precio dirigido por una lista consumida SOLO en submit_order.

        El adapter expone `.px` para fijar el mark sin consumir la cola.
        """
        seq = list(prices)

        class _Src:
            def __init__(self) -> None:
                self.value = seq[0]

            def next_from(self) -> None:
                if seq:
                    self.value = seq.pop(0)

            def __call__(self, instrument: str) -> float:
                return self.value

        src = _Src()
        adapter = PaperBrokerAdapter(
            account_id="PAPER-CYCLE", initial_cash=100_000.0,
            price_source=src, clock=_now,
        )
        adapter.connect()
        adapter._src = src  # type: ignore[attr-defined]
        return adapter

    def test_ciclo_completo_long(self) -> None:
        adapter = self._adapter([100.0, 110.0])  # open@100, close@110
        adapter.submit_order(_order("L-OPEN", side=OrderSide.BUY, quantity=1.0))  # @100
        adapter._src.value = 110.0  # mark durante la posicion
        mid = adapter.get_account_state()
        assert mid.cash == pytest.approx(99_900.0)
        assert mid.equity == pytest.approx(100_010.0)
        adapter._src.value = 110.0  # precio de cierre
        adapter.submit_order(_order("L-CLOSE", side=OrderSide.SELL, quantity=1.0))  # @110
        assert adapter.get_positions() == []
        final = adapter.get_account_state()
        assert final.equity == pytest.approx(100_010.0)
        assert final.cash == pytest.approx(100_010.0)  # realizado +10 reflejado en cash

    def test_ciclo_completo_short(self) -> None:
        adapter = self._adapter([100.0, 90.0])
        adapter.submit_order(_order("S-OPEN", side=OrderSide.SELL, quantity=1.0))  # @100
        adapter._src.value = 90.0
        assert adapter.get_account_state().equity == pytest.approx(100_010.0)
        adapter.submit_order(_order("S-CLOSE", side=OrderSide.BUY, quantity=1.0))  # @90
        assert adapter.get_positions() == []
        final = adapter.get_account_state()
        assert final.equity == pytest.approx(100_010.0)
        assert final.cash == pytest.approx(100_010.0)

    def test_short_perdedor_ciclo_completo(self) -> None:
        adapter = self._adapter([100.0, 110.0])
        adapter.submit_order(_order("SP-OPEN", side=OrderSide.SELL, quantity=1.0))  # @100
        adapter._src.value = 110.0
        assert adapter.get_account_state().equity == pytest.approx(99_990.0)
        adapter.submit_order(_order("SP-CLOSE", side=OrderSide.BUY, quantity=1.0))  # @110
        assert adapter.get_positions() == []
        assert adapter.get_account_state().equity == pytest.approx(99_990.0)

    def test_invariante_delta_equity_igual_unrealized(self) -> None:
        cases = [
            (OrderSide.BUY, 100.0, 110.0),   # LONG ganador
            (OrderSide.BUY, 100.0, 90.0),    # LONG perdedor
            (OrderSide.SELL, 100.0, 90.0),   # SHORT ganador
            (OrderSide.SELL, 100.0, 110.0),  # SHORT perdedor
        ]
        for side, entry, mark in cases:
            adapter = self._adapter([entry])
            adapter.submit_order(_order("OPEN", side=side, quantity=1.0))
            at_entry = adapter.get_account_state().equity
            pos = adapter.get_positions()[0]
            adapter._src.value = mark
            now = adapter.get_account_state().equity
            expected_delta = pos.quantity * (mark - pos.avg_entry_price)
            assert now - at_entry == pytest.approx(expected_delta)

    def test_multi_fill_short_ciclo_cierre(self) -> None:
        # SHORT 1 @100, SHORT 1 @110 → avg 105, qty -2; mark 100 → unrealized +10
        adapter = self._adapter([100.0, 110.0])
        adapter._src.value = 100.0
        adapter.submit_order(_order("S1", side=OrderSide.SELL, quantity=1.0))  # @100
        adapter._src.value = 110.0
        adapter.submit_order(_order("S2", side=OrderSide.SELL, quantity=1.0))  # @110
        pos = adapter.get_positions()[0]
        assert pos.quantity == pytest.approx(-2.0)
        assert pos.avg_entry_price == pytest.approx(105.0)
        adapter._src.value = 100.0
        assert adapter.get_account_state().equity == pytest.approx(100_010.0)
        # cierre completo: BUY 2 @100
        adapter.submit_order(_order("SC", side=OrderSide.BUY, quantity=2.0))
        assert adapter.get_positions() == []
        assert adapter.get_account_state().equity == pytest.approx(100_010.0)


class TestDeterminism:
    def test_same_inputs_same_output(self) -> None:
        def run() -> tuple:
            adapter = _paper(price=20_000.0)
            adapter.submit_order(_order("D1", quantity=3.0))
            adapter.submit_order(_order("D2", quantity=1.5))
            positions = adapter.get_positions()
            account = adapter.get_account_state()
            return (
                tuple((p.instrument, p.quantity, p.avg_entry_price)
                      for p in positions),
                (account.cash, account.equity),
            )

        assert run() == run()

    def test_no_network_or_sleep_required(self) -> None:
        """El sandbox es 100% en memoria y síncrono."""
        adapter = _paper()
        adapter.submit_order(_order("NO-NET"))
        assert adapter.health() is True


class TestPaperReduceOnly:
    """02B: PaperBrokerAdapter NO ignora `reduce_only` (no inversion)."""

    def test_reduce_only_without_position_rejected(self) -> None:
        adapter = _paper()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(
                "R-NOPOS", side=OrderSide.SELL, quantity=1.0, reduce_only=True,
            ))
        assert adapter.get_positions() == []

    def test_reduce_only_exceeding_exposure_no_inversion(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("LONG-1", side=OrderSide.BUY, quantity=1.0))
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(
                "R-BIG", side=OrderSide.SELL, quantity=2.0, reduce_only=True,
            ))
        # El ledger/posicion NO se invierte por una reduccion invalida.
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == pytest.approx(1.0)

    def test_reduce_only_wrong_side_rejected(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("LONG-2", side=OrderSide.BUY, quantity=1.0))
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(
                "R-SAME", side=OrderSide.BUY, quantity=1.0, reduce_only=True,
            ))
        assert adapter.get_positions()[0].quantity == pytest.approx(1.0)

    def test_reduce_only_valid_partial(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("LONG-3", side=OrderSide.BUY, quantity=1.0))
        sub = adapter.submit_order(_order(
            "R-OK", side=OrderSide.SELL, quantity=0.4, reduce_only=True,
        ))
        assert sub.status == SubmitStatus.CONFIRMED
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == pytest.approx(0.6)

    def test_reduce_only_full_close_ok(self) -> None:
        adapter = _paper(price=50_000.0)
        adapter.submit_order(_order("LONG-4", side=OrderSide.BUY, quantity=1.0))
        adapter.submit_order(_order(
            "R-FULL", side=OrderSide.SELL, quantity=1.0, reduce_only=True,
        ))
        assert adapter.get_positions() == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))