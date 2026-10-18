"""Tests LIVE-09: MT5BrokerAdapter (DEMO-first, sin terminal MT5 real).

El adapter aisla la dependencia `MetaTrader5`: estos tests inyectan un fake
determinista (sin red, sin terminal, sin credenciales reales). No se envia
ninguna orden real.

Cubre: CONFIG, CONNECT, ACCOUNT, SYMBOL, SUBMIT/order_check/order_send, UNKNOWN,
correlacion cid<->ticket, LOOKUP, FILLS, OPEN ORDERS, POSITIONS, CANCEL, SAFETY,
RECONCILIATION. No usa sleeps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from execution.intent_journal import IntentJournal, OrderIntent
from execution.order import Order, OrderSide, OrderType
from reconciliation.orchestrator import broker_state_from_adapter

from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    HISTORY_INCOMPLETE,
    MT5BrokerAdapter,
    MT5Config,
    MT5ConfigError,
    MT5Credentials,
    MT5Environment,
    ORDER_STATE_CANCELED,
    ORDER_STATE_FILLED,
    ORDER_STATE_PLACED,
    ORDER_STATE_REJECTED,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_REMOVE,
    TRADE_RETCODE_CONNECTION,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_DONE_PARTIAL,
    TRADE_RETCODE_PLACED,
    TRADE_RETCODE_TIMEOUT,
    _AMBIGUOUS_RETCODES,
)
from brokers.mt5_symbols import MT5SymbolMappingError
from brokers.models import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    BrokerInvalidOrderError,
    BrokerOrderState,
    BrokerPermissionError,
    OrderNotFoundError,
    OrderRejectedError,
    SubmitStatus,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
ATLAS_INSTRUMENT = "BTCUSDT"
MT5_SYMBOL = "BTCUSD"
PASSWORD_CANARY = "not-a-real-password"


# --------------------------------------------------------------------------
# Fake MT5 (inyectable) — sin terminal real
# --------------------------------------------------------------------------


class FakeMT5:
    def __init__(self) -> None:
        self.initialize_ok = True
        self.initialize_kwargs = None
        self.terminal = SimpleNamespace(connected=True)
        self.account = SimpleNamespace(
            login=123456, balance=10_000.0, equity=10_000.0,
            margin_free=9_000.0, currency="USD", margin_mode=0,
            trade_allowed=True, trade_expert=True, trade_mode=0,
        )
        self.last_error_value = (0, "ok")
        self.symbols: dict = {}
        self.ticks: dict = {}
        self.check_retcode = 0
        self.send_mode = "ok"        # ok | reject | none | raise | ambiguous
        self.remove_mode = "ok"      # ok | reject | none | raise | ambiguous
        self.accept_but_lose_response = False
        self.send_count = 0
        self.last_request: dict | None = None
        self.fail_orders_get = False
        self.fail_history_orders_get = False
        self.fail_deals = False
        # Contratos de retorno reales de MetaTrader5: `None` = error (detalle en
        # last_error()); coleccion vacia = sin resultados.
        self.none_orders_get = False
        self.none_history_orders_get = False
        self.none_history_deals_get = False
        self.none_positions_get = False
        self.last_deals_kwargs: dict | None = None
        self._orders: dict = {}
        self._open_orders: dict = {}
        self._deals: dict = {}
        self._positions: dict = {}
        self._next = 1

    # --- helpers de configuracion de test
    def add_symbol(self, name: str, **over) -> None:
        fields = dict(
            name=name, visible=True, volume_min=0.01, volume_step=0.01,
            volume_max=100.0, point=0.01, trade_tick_size=0.01,
            trade_tick_value=1.0, trade_contract_size=1.0,
            currency_base="BTC", currency_profit="USD", currency_margin="USD",
            trade_mode=4, filling_mode=1, digits=2,
        )
        fields.update(over)
        self.symbols[name] = SimpleNamespace(**fields)

    def add_tick(self, name: str, bid: float, ask: float) -> None:
        self.ticks[name] = SimpleNamespace(bid=bid, ask=ask)

    def add_open_order(self, ticket: int, *, symbol=MT5_SYMBOL, magic=0,
                       comment="", type=0, volume=1.0, price=50_000.0) -> None:
        self._open_orders[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, magic=magic, comment=comment,
            type=type, state=ORDER_STATE_PLACED, volume_initial=volume,
            volume_current=volume, price_open=price,
        )

    def add_deal(self, ticket: int, order: int, *, volume=1.0, price=50_000.0,
                 commission=0.0, time=1_700_000_000) -> None:
        self._deals[ticket] = SimpleNamespace(
            ticket=ticket, order=order, volume=volume, price=price,
            commission=commission, time=time,
        )

    def add_position(self, ticket: int, *, symbol=MT5_SYMBOL, volume=1.0,
                     price_open=50_000.0, type=0) -> None:
        self._positions[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, volume=volume, price_open=price_open,
            type=type, time=1_700_000_000,
        )

    # --- API MT5 simulada
    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs
        return self.initialize_ok

    def shutdown(self):
        return True

    def last_error(self):
        return self.last_error_value

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable):
        if name in self.symbols:
            self.symbols[name].visible = bool(enable)
            return True
        return False

    def symbol_info_tick(self, name):
        return self.ticks.get(name)

    def order_check(self, request):
        return SimpleNamespace(retcode=self.check_retcode, comment="")

    def _mk_order(self, ticket, request, state, volume_initial, volume_current):
        return SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], magic=request.get("magic", 0),
            comment=request.get("comment", ""), type=request["type"], state=state,
            volume_initial=volume_initial, volume_current=volume_current,
            price_open=request.get("price", 0.0),
        )

    def order_send(self, request):
        self.send_count += 1
        self.last_request = dict(request)
        if request.get("action") == TRADE_ACTION_REMOVE:
            return self._send_remove(request)
        if self.send_mode == "raise":
            raise RuntimeError("transporte")
        if self.send_mode == "none":
            return None
        if self.send_mode == "ambiguous":
            return SimpleNamespace(
                retcode=next(iter(_AMBIGUOUS_RETCODES)), order=0, deal=0,
                volume=0.0, price=0.0,
            )
        if self.send_mode == "timeout":
            return SimpleNamespace(
                retcode=10012, order=0, deal=0, volume=0.0, price=0.0
            )
        if self.send_mode == "reject":
            return SimpleNamespace(
                retcode=10006, order=0, deal=0, volume=0.0, price=0.0
            )
        if self.send_mode == "placed":
            ticket = self._next; self._next += 1
            self._open_orders[ticket] = self._mk_order(
                ticket, request, ORDER_STATE_PLACED,
                request["volume"], request["volume"],
            )
            return SimpleNamespace(
                retcode=10008, order=ticket, deal=0, volume=0.0,
                price=request["price"],
            )
        if self.send_mode == "partial":
            ticket = self._next; self._next += 1
            deal_ticket = self._next; self._next += 1
            half = request["volume"] / 2
            self._orders[ticket] = self._mk_order(
                ticket, request, 2, request["volume"], half  # state PARTIAL=2
            )
            self.add_deal(deal_ticket, ticket, volume=half, price=request["price"])
            return SimpleNamespace(
                retcode=10010, order=ticket, deal=deal_ticket, volume=half,
                price=request["price"],
            )
        ticket = self._next; self._next += 1
        deal_ticket = self._next; self._next += 1
        order = self._mk_order(
            ticket, request, ORDER_STATE_FILLED, request["volume"], 0.0
        )
        self._orders[ticket] = order
        self.add_deal(deal_ticket, ticket, volume=request["volume"],
                      price=request["price"])
        if self.accept_but_lose_response:
            return None
        return SimpleNamespace(
            retcode=10009, order=ticket, deal=deal_ticket,
            volume=request["volume"], price=request["price"],
        )

    def _send_remove(self, request):
        if self.remove_mode == "raise":
            raise RuntimeError("transporte")
        if self.remove_mode == "none":
            return None
        if self.remove_mode == "ambiguous":
            return SimpleNamespace(
                retcode=next(iter(_AMBIGUOUS_RETCODES)), order=0, deal=0,
                volume=0.0, price=0.0,
            )
        if self.remove_mode == "reject":
            return SimpleNamespace(
                retcode=10006, order=0, deal=0, volume=0.0, price=0.0
            )
        ticket = int(request["order"])
        order = self._open_orders.pop(ticket, None)
        if order is None:
            return SimpleNamespace(
                retcode=10006, order=0, deal=0, volume=0.0, price=0.0
            )
        order.state = ORDER_STATE_CANCELED
        self._orders[ticket] = order
        return SimpleNamespace(
            retcode=10009, order=ticket, deal=0, volume=0.0, price=0.0
        )

    def orders_get(self, ticket=None):
        if self.none_orders_get:
            self.last_error_value = (1, "orders_get failed")
            return None
        if self.fail_orders_get:
            raise RuntimeError("orders_get transport")
        rows = list(self._open_orders.values())
        if ticket is not None:
            rows = [o for o in rows if o.ticket == ticket]
        return tuple(rows)

    def positions_get(self, symbol=None, ticket=None):
        if self.none_positions_get:
            self.last_error_value = (4, "positions_get failed")
            return None
        rows = list(self._positions.values())
        if symbol is not None:
            rows = [p for p in rows if p.symbol == symbol]
        if ticket is not None:
            rows = [p for p in rows if p.ticket == ticket]
        return tuple(rows)

    def history_orders_get(self, ticket=None):
        if self.none_history_orders_get:
            self.last_error_value = (2, "history_orders_get failed")
            return None
        if self.fail_history_orders_get:
            raise RuntimeError("history_orders_get transport")
        rows = list(self._orders.values())
        if ticket is not None:
            rows = [o for o in rows if o.ticket == ticket]
        return tuple(rows)

    def history_deals_get(self, ticket=None):
        # Firma REAL de la API MT5: `ticket` filtra por ticket de ORDEN
        # (DEAL_ORDER). No se admite `order=` (rechazado por TypeError).
        self.last_deals_kwargs = {"ticket": ticket}
        if self.none_history_deals_get:
            self.last_error_value = (3, "history_deals_get failed")
            return None
        if self.fail_deals:
            raise RuntimeError("history_deals_get transport")
        rows = list(self._deals.values())
        if ticket is not None:
            rows = [d for d in rows if d.order == ticket]
        return tuple(rows)


def _config(**over) -> MT5Config:
    params = dict(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={ATLAS_INSTRUMENT: MT5_SYMBOL},
    )
    params.update(over)
    return MT5Config(**params)


def _credentials() -> MT5Credentials:
    return MT5Credentials(login=123456, password=PASSWORD_CANARY, server="demo")


def _adapter(fake: FakeMT5 | None = None, **cfg) -> tuple[MT5BrokerAdapter, FakeMT5]:
    fake = fake or FakeMT5()
    if MT5_SYMBOL not in fake.symbols:
        fake.add_symbol(MT5_SYMBOL)
    if MT5_SYMBOL not in fake.ticks:
        fake.add_tick(MT5_SYMBOL, bid=49_999.0, ask=50_001.0)
    adapter = MT5BrokerAdapter(_config(**cfg), _credentials(), mt5=fake, clock=lambda: NOW)
    return adapter, fake


def _order(cid="C-1", *, side=OrderSide.BUY, quantity=0.1,
           order_type=OrderType.MARKET, instrument=ATLAS_INSTRUMENT,
           reduce_only=False) -> Order:
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="MT5-1",
        instrument=instrument, side=side, quantity=quantity,
        order_type=order_type, client_order_id=cid, reduce_only=reduce_only,
    )


def _ready(**cfg) -> tuple[MT5BrokerAdapter, FakeMT5]:
    adapter, fake = _adapter(**cfg)
    adapter.connect()
    return adapter, fake


# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------


class TestConfig:
    def test_default_environment_is_demo(self):
        assert MT5Config().environment is MT5Environment.DEMO

    def test_unknown_environment_fails_closed(self):
        with pytest.raises(MT5ConfigError):
            MT5Config.from_mapping({"environment": "prod"})

    def test_provider_must_be_mt5(self):
        with pytest.raises(MT5ConfigError):
            MT5Config.from_mapping({"provider": "ibkr", "environment": "demo"})

    def test_credentials_missing_fails_closed(self):
        with pytest.raises(BrokerAuthError):
            MT5Credentials.from_env({})

    def test_credentials_from_env_ok(self):
        creds = MT5Credentials.from_env(
            {"MT5_LOGIN": "1", "MT5_PASSWORD": "x", "MT5_SERVER": "demo"}
        )
        assert creds.login == 1 and creds.server == "demo"

    def test_credentials_repr_hides_secret(self):
        text = repr(_credentials())
        assert PASSWORD_CANARY not in text
        assert "***" in text

    def test_live_environment_cannot_connect(self):
        adapter, _ = _adapter(environment=MT5Environment.LIVE)
        with pytest.raises(BrokerPermissionError):
            adapter.connect()
        assert adapter.health() is False

    def test_live_environment_cannot_submit(self):
        adapter, _ = _adapter(environment=MT5Environment.LIVE)
        # Aunque se forzara el estado interno, el submit queda prohibido.
        adapter._state = "READY"
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(_order())


# --------------------------------------------------------------------------
# CONNECT / HEALTH
# --------------------------------------------------------------------------


class TestConnect:
    def test_health_false_initially(self):
        adapter, _ = _adapter()
        assert adapter.health() is False

    def test_connect_success(self):
        adapter, fake = _ready()
        assert adapter.health() is True
        assert fake.initialize_kwargs["login"] == 123456
        assert PASSWORD_CANARY not in repr(adapter.config)

    def test_initialize_failure_auth(self):
        fake = FakeMT5()
        fake.initialize_ok = False
        fake.last_error_value = (-6, "auth")
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerAuthError):
            adapter.connect()
        assert adapter.health() is False

    def test_initialize_failure_connection(self):
        fake = FakeMT5()
        fake.initialize_ok = False
        fake.last_error_value = (-1, "generic")
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerConnectionError):
            adapter.connect()

    def test_auth_failure_when_no_account(self):
        fake = FakeMT5()
        fake.account = None
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerAuthError):
            adapter.connect()

    def test_disconnect(self):
        adapter, _ = _ready()
        adapter.disconnect()
        assert adapter.health() is False

    def test_query_before_connect_raises(self):
        adapter, _ = _adapter()
        with pytest.raises(BrokerConnectionError):
            adapter.get_account_state()
        with pytest.raises(BrokerConnectionError):
            adapter.get_positions()


# --------------------------------------------------------------------------
# ACCOUNT
# --------------------------------------------------------------------------


class TestAccount:
    def test_account_mapping(self):
        adapter, _ = _ready()
        state = adapter.get_account_state()
        assert state.account_id == "123456"
        assert state.cash == 10_000.0
        assert state.equity == 10_000.0
        assert state.buying_power == 9_000.0  # margin_free
        assert state.currency == "USD"
        assert state.as_of == NOW

    def test_account_info_unavailable_failsafe(self):
        adapter, fake = _ready()
        fake.account = None
        with pytest.raises(BrokerConnectionError):
            adapter.get_account_state()

    def test_account_invalid_numeric(self):
        adapter, fake = _ready()
        fake.account.balance = float("nan")
        with pytest.raises(BrokerError):
            adapter.get_account_state()


# --------------------------------------------------------------------------
# SYMBOL / INSTRUMENT DISCOVERY
# --------------------------------------------------------------------------


class TestSymbol:
    def test_symbol_found_metadata(self):
        adapter, _ = _ready()
        spec = adapter._symbol_spec(ATLAS_INSTRUMENT)
        assert spec.name == MT5_SYMBOL
        assert spec.volume_min == 0.01
        assert spec.volume_step == 0.01
        assert spec.volume_max == 100.0
        assert spec.currency_profit == "USD"
        assert spec.trade_mode == 4

    def test_symbol_missing_fails(self):
        adapter, fake = _ready()
        fake.symbols.pop(MT5_SYMBOL)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order())

    def test_symbol_not_operable_fails(self):
        adapter, fake = _ready()
        fake.symbols[MT5_SYMBOL].trade_mode = 0
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order())

    def test_no_explicit_mapping_fails(self):
        adapter, _ = _adapter(symbol_map={})
        adapter.connect()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order())


# --------------------------------------------------------------------------
# SUBMIT / order_check / order_send
# --------------------------------------------------------------------------


class TestSubmit:
    def test_order_check_rejection_no_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 10013
        sub = adapter.submit_order(_order())
        assert sub.status is SubmitStatus.REJECTED
        assert fake.send_count == 0  # nunca llego a order_send

    def test_submit_confirmed_with_fill(self):
        adapter, fake = _ready()
        sub = adapter.submit_order(_order(cid="C-OK"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order is not None
        assert sub.broker_order.state is BrokerOrderState.FILLED
        assert sub.broker_order.filled_quantity == pytest.approx(0.1)
        assert fake.send_count == 1

    def test_send_rejection(self):
        adapter, fake = _ready()
        fake.send_mode = "reject"
        sub = adapter.submit_order(_order())
        assert sub.status is SubmitStatus.REJECTED

    def test_send_none_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "none"
        sub = adapter.submit_order(_order())
        assert sub.status is SubmitStatus.UNKNOWN
        assert sub.broker_order is None

    def test_send_raises_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "raise"
        sub = adapter.submit_order(_order())
        assert sub.status is SubmitStatus.UNKNOWN

    def test_ambiguous_retcode_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "ambiguous"
        sub = adapter.submit_order(_order())
        assert sub.status is SubmitStatus.UNKNOWN

    def test_unsupported_order_type_no_submit(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(order_type=OrderType.LIMIT))
        assert fake.send_count == 0

    def test_volume_below_min_rejected(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(quantity=0.001))
        assert fake.send_count == 0

    def test_volume_not_aligned_rejected(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(_order(quantity=0.015))  # step 0.01
        assert fake.send_count == 0

    def test_no_silent_quantity_mutation(self):
        adapter, fake = _ready()
        adapter.submit_order(_order(quantity=0.10))
        assert fake.last_request["volume"] == pytest.approx(0.10)

    def test_intent_journal_write_ahead_before_submit(self, tmp_path):
        adapter, fake = _ready()
        journal = IntentJournal.initialize(tmp_path / "intents.json")
        seen = []
        real_send = fake.order_send

        def spy(request):
            seen.append(journal.get("CID-WA"))
            return real_send(request)

        fake.order_send = spy
        order = _order(cid="CID-WA")
        journal.begin(OrderIntent.from_order(order))
        adapter.submit_order(order)
        assert seen and seen[0] is not None


# --------------------------------------------------------------------------
# CORRELATION / LOOKUP
# --------------------------------------------------------------------------


class TestCorrelationLookup:
    def test_cid_to_ticket_stable(self):
        adapter, fake = _ready()
        sub = adapter.submit_order(_order(cid="C-CORR"))
        ticket = int(sub.broker_order.broker_order_id)
        assert adapter._cid_to_ticket["C-CORR"] == ticket
        assert adapter.get_order("C-CORR").broker_order_id == str(ticket)

    def test_lookup_found(self):
        adapter, _ = _ready()
        adapter.submit_order(_order(cid="C-FOUND"))
        order = adapter.get_order("C-FOUND")
        assert order is not None
        assert order.state is BrokerOrderState.FILLED
        assert len(order.fills) == 1

    def test_lookup_not_found(self):
        adapter, _ = _ready()
        assert adapter.get_order("NEVER-SENT") is None

    def test_lookup_transport_uncertain_raises(self):
        adapter, fake = _ready()
        fake.fail_orders_get = True
        fake.fail_history_orders_get = True
        with pytest.raises(BrokerConnectionError):
            adapter.get_order("ANY")

    def test_lookup_unknown_state_indeterminate(self):
        adapter, fake = _ready()
        magic = adapter._magic_for("C-UNK")
        comment = adapter._comment_for("C-UNK")
        fake._orders[77] = SimpleNamespace(
            ticket=77, symbol=MT5_SYMBOL, magic=magic, comment=comment,
            type=0, state=99, volume_initial=0.1, volume_current=0.1,
            price_open=50_000.0,
        )
        order = adapter.get_order("C-UNK")
        assert order is not None and order.state is BrokerOrderState.UNKNOWN

    def test_lookup_after_unknown_response(self):
        adapter, fake = _ready()
        fake.accept_but_lose_response = True
        sub = adapter.submit_order(_order(cid="C-LOST"))
        assert sub.status is SubmitStatus.UNKNOWN
        resolved = adapter.get_order("C-LOST")
        assert resolved is not None
        assert resolved.state is BrokerOrderState.FILLED
        # Sin resubmit: solo un order_send.
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# 06I-C1: resolucion de simbolo en ordenes FAIL-CLOSED
# --------------------------------------------------------------------------


class TestOrderSymbolResolutionFailClosed:
    def _inject_order(self, adapter, fake, cid, symbol, ticket):
        magic = adapter._magic_for(cid)
        comment = adapter._comment_for(cid)
        fake._orders[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, magic=magic, comment=comment,
            type=0, state=ORDER_STATE_FILLED, volume_initial=0.1,
            volume_current=0.0, price_open=50_000.0,
        )
        adapter._remember(cid, ticket)

    def test_mapped_symbol_resolves_to_atlas_instrument(self):
        adapter, fake = _ready()
        self._inject_order(adapter, fake, "C-MAP", MT5_SYMBOL, 71)
        order = adapter.get_order("C-MAP")
        assert order.instrument == ATLAS_INSTRUMENT
        assert order.instrument != MT5_SYMBOL

    def test_unmapped_symbol_fails_closed(self):
        adapter, fake = _ready()
        self._inject_order(adapter, fake, "C-GHOST", "GHOST.raw", 72)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.get_order("C-GHOST")

    def test_no_silent_fallback_to_raw_mt5_symbol(self):
        # El nombre MT5 crudo NUNCA debe devolverse como instrumento Atlas.
        adapter, fake = _ready()
        self._inject_order(adapter, fake, "C-RAW", "EURUSD.raw", 73)
        with pytest.raises(BrokerInvalidOrderError) as exc:
            adapter.get_order("C-RAW")
        assert "EURUSD.raw" in str(exc.value)

    def test_failure_occurs_before_any_execution_call(self):
        adapter, fake = _ready()
        self._inject_order(adapter, fake, "C-SAFE", "GHOST.raw", 74)
        sends = fake.send_count
        with pytest.raises(BrokerInvalidOrderError):
            adapter.get_order("C-SAFE")
        assert fake.send_count == sends

    def test_explicit_identity_mapping_is_valid(self):
        adapter, fake = _ready(symbol_map={"IDENT": "IDENT"})
        self._inject_order(adapter, fake, "C-ID", "IDENT", 75)
        assert adapter.get_order("C-ID").instrument == "IDENT"

    def test_invalid_non_injective_config_still_rejected(self):
        with pytest.raises(MT5SymbolMappingError):
            _adapter(symbol_map={"A": "SAME", "B": "SAME"})


# --------------------------------------------------------------------------
# FILLS
# --------------------------------------------------------------------------


class TestFills:
    def test_single_deal(self):
        adapter, _ = _ready()
        adapter.submit_order(_order(cid="F1"))
        order = adapter.get_order("F1")
        assert len(order.fills) == 1
        assert order.fills[0].quantity == pytest.approx(0.1)

    def test_multiple_deals_stable_sequence(self):
        adapter, fake = _ready()
        # Inyecta una orden con dos deals.
        magic = adapter._magic_for("F2")
        comment = adapter._comment_for("F2")
        fake._orders[50] = SimpleNamespace(
            ticket=50, symbol=MT5_SYMBOL, magic=magic, comment=comment,
            type=0, state=ORDER_STATE_FILLED, volume_initial=2.0,
            volume_current=0.0, price_open=50_000.0,
        )
        fake.add_deal(900, 50, volume=1.0, price=50_000.0, time=100)
        fake.add_deal(901, 50, volume=1.0, price=50_100.0, time=200)
        adapter._remember("F2", 50)
        order = adapter.get_order("F2")
        seqs = [f.fill_sequence for f in order.fills]
        assert seqs == [900, 901]  # identidad estable = ticket del deal
        assert order.filled_quantity == pytest.approx(2.0)

    def test_commission_preserved(self):
        adapter, fake = _ready()
        magic = adapter._magic_for("F3")
        comment = adapter._comment_for("F3")
        fake._orders[60] = SimpleNamespace(
            ticket=60, symbol=MT5_SYMBOL, magic=magic, comment=comment,
            type=0, state=ORDER_STATE_FILLED, volume_initial=1.0,
            volume_current=0.0, price_open=50_000.0,
        )
        fake.add_deal(910, 60, volume=1.0, price=50_000.0, commission=1.25)
        adapter._remember("F3", 60)
        fill = adapter.get_order("F3").fills[0]
        assert fill.commission == pytest.approx(1.25)

    def test_duplicate_poll_no_duplication(self):
        adapter, _ = _ready()
        adapter.submit_order(_order(cid="F4"))
        a = adapter.get_order("F4").fills
        b = adapter.get_order("F4").fills
        assert [f.fill_sequence for f in a] == [f.fill_sequence for f in b]


# --------------------------------------------------------------------------
# OPEN ORDERS / POSITIONS
# --------------------------------------------------------------------------


class TestOpenOrdersPositions:
    def test_open_orders_mapping(self):
        adapter, fake = _ready()
        magic = adapter._magic_for("O-1")
        fake.add_open_order(70, magic=magic, comment=adapter._comment_for("O-1"))
        orders = adapter.get_open_orders()
        assert len(orders) == 1
        assert orders[0].state in (
            BrokerOrderState.SUBMITTED,
            BrokerOrderState.PARTIALLY_FILLED,
        )

    def test_unknown_broker_order_observed_only(self):
        adapter, fake = _ready()
        fake.add_open_order(71, comment="EXTERNAL-1")
        orders = adapter.get_open_orders()
        assert len(orders) == 1
        assert orders[0].client_order_id == "EXTERNAL-1"
        # No se interna: no hay mapping cid->ticket.
        assert "EXTERNAL-1" not in adapter._cid_to_ticket

    def test_positions_mapping(self):
        adapter, fake = _ready()
        fake.add_position(80, symbol=MT5_SYMBOL, volume=1.5, price_open=50_000.0, type=0)
        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].instrument == ATLAS_INSTRUMENT
        assert positions[0].quantity == pytest.approx(1.5)
        assert positions[0].avg_entry_price == pytest.approx(50_000.0)

    def test_position_unexpected_symbol_not_invented(self):
        # 06E: un simbolo MT5 NO mapeado no se convierte en un identificador
        # canonico por fallback; se conserva la procedencia y `instrument` queda
        # None (fail-closed).
        adapter, fake = _ready()
        fake.add_position(81, symbol="EURUSD", volume=1.0, price_open=1.1, type=1)
        positions = adapter.get_positions()
        assert positions[0].instrument is None
        assert positions[0].mt5_symbol == "EURUSD"
        assert positions[0].quantity == pytest.approx(-1.0)

    def test_hedging_multiple_positions_failclosed(self):
        adapter, fake = _ready()
        fake.account.margin_mode = 2  # RETAIL_HEDGING
        fake.add_position(82, symbol=MT5_SYMBOL, volume=1.0)
        fake.add_position(83, symbol=MT5_SYMBOL, volume=1.0)
        with pytest.raises(BrokerError):
            adapter.get_positions()


# --------------------------------------------------------------------------
# CANCEL
# --------------------------------------------------------------------------


class TestCancel:
    def test_cancel_confirmed(self):
        adapter, fake = _ready()
        magic = adapter._magic_for("CAN-1")
        fake.add_open_order(90, magic=magic, comment=adapter._comment_for("CAN-1"))
        adapter._remember("CAN-1", 90)
        cancelled = adapter.cancel_order("CAN-1")
        assert cancelled.state is BrokerOrderState.CANCELLED

    def test_cancel_rejected(self):
        adapter, fake = _ready()
        fake.add_open_order(91, comment=adapter._comment_for("CAN-2"))
        adapter._remember("CAN-2", 91)
        fake.remove_mode = "reject"
        with pytest.raises(OrderRejectedError):
            adapter.cancel_order("CAN-2")

    def test_cancel_uncertain(self):
        adapter, fake = _ready()
        fake.add_open_order(92, comment=adapter._comment_for("CAN-3"))
        adapter._remember("CAN-3", 92)
        fake.remove_mode = "none"
        with pytest.raises(BrokerConnectionError):
            adapter.cancel_order("CAN-3")

    def test_cancel_not_found(self):
        adapter, _ = _ready()
        with pytest.raises(OrderNotFoundError):
            adapter.cancel_order("NO-EXISTE")


# --------------------------------------------------------------------------
# SAFETY / RECONCILIATION
# --------------------------------------------------------------------------


class TestSafetyReconciliation:
    def test_submit_before_ready_raises(self):
        adapter, fake = _adapter()
        with pytest.raises(BrokerConnectionError):
            adapter.submit_order(_order())
        assert fake.send_count == 0

    def test_unknown_then_no_automatic_resubmit(self):
        adapter, fake = _ready()
        fake.accept_but_lose_response = True
        adapter.submit_order(_order(cid="S-1"))
        adapter.get_order("S-1")
        assert fake.send_count == 1

    def test_broker_state_from_adapter_account(self):
        adapter, fake = _ready()
        fake.add_position(95, symbol=MT5_SYMBOL, volume=0.5, price_open=50_000.0)
        snapshot = broker_state_from_adapter(adapter)
        assert snapshot.available is True
        assert snapshot.account is not None
        assert snapshot.account.cash == 10_000.0
        assert snapshot.account.currency == "USD"
        assert any(p.instrument == ATLAS_INSTRUMENT for p in snapshot.positions)

    def test_adapter_does_not_import_ledger(self):
        import brokers.mt5 as mt5_module

        assert "papertrading" not in mt5_module.__dict__
        assert not hasattr(MT5BrokerAdapter, "ledger")

    def test_abstract_methods_implemented(self):
        from brokers.base import BrokerAdapter

        assert not MT5BrokerAdapter.__abstractmethods__
        assert issubclass(MT5BrokerAdapter, BrokerAdapter)


class TestReadinessCapacity:
    """READY exige capacidad real de trading (trade_allowed + trade_expert)."""

    def test_ready_when_all_capabilities_true(self):
        adapter, _ = _ready()
        assert adapter.health() is True
        assert adapter.state == "READY"

    def test_trade_allowed_false_not_ready(self):
        fake = FakeMT5()
        fake.account.trade_allowed = False
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerPermissionError):
            adapter.connect()
        assert adapter.health() is False

    def test_trade_expert_false_not_ready(self):
        fake = FakeMT5()
        fake.account.trade_expert = False
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerPermissionError):
            adapter.connect()
        assert adapter.health() is False

    def test_valid_account_but_no_trade_submit_impossible(self):
        fake = FakeMT5()
        fake.account.trade_allowed = False
        adapter, _ = _adapter(fake=fake)
        with pytest.raises(BrokerPermissionError):
            adapter.connect()
        with pytest.raises(BrokerConnectionError):
            adapter.submit_order(_order())
        assert fake.send_count == 0


class TestReduceOnly:
    def test_long_position_reduce_only_sell_request(self):
        adapter, fake = _ready()
        fake.add_position(80, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        sub = adapter.submit_order(
            _order(cid="R-LONG", side=OrderSide.SELL, quantity=1.0, reduce_only=True)
        )
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.last_request["type"] == ORDER_TYPE_SELL
        assert fake.last_request["position"] == 80
        assert fake.last_request["volume"] == pytest.approx(1.0)

    def test_short_position_reduce_only_buy_request(self):
        adapter, fake = _ready()
        fake.add_position(81, symbol=MT5_SYMBOL, volume=2.0, type=ORDER_TYPE_SELL)
        sub = adapter.submit_order(
            _order(cid="R-SHORT", side=OrderSide.BUY, quantity=2.0, reduce_only=True)
        )
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.last_request["type"] == ORDER_TYPE_BUY
        assert fake.last_request["position"] == 81

    def test_reduce_only_without_position_rejected_no_send(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(
                _order(side=OrderSide.SELL, quantity=1.0, reduce_only=True)
            )
        assert fake.send_count == 0

    def test_reduce_only_volume_exceeds_position_rejected_no_send(self):
        adapter, fake = _ready()
        fake.add_position(82, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(
                _order(side=OrderSide.SELL, quantity=2.0, reduce_only=True)
            )
        assert fake.send_count == 0

    def test_reduce_only_cannot_invert_position(self):
        adapter, fake = _ready()
        fake.add_position(83, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        # BUY (mismo sentido) reduce_only => intentaria invertir/abrir.
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(
                _order(side=OrderSide.BUY, quantity=1.0, reduce_only=True)
            )
        assert fake.send_count == 0

    def test_hedging_reduce_only_rejected_no_send(self):
        adapter, fake = _ready()
        fake.account.margin_mode = 2  # RETAIL_HEDGING
        fake.add_position(84, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(
                _order(side=OrderSide.SELL, quantity=1.0, reduce_only=True)
            )
        assert fake.send_count == 0

    def test_normal_entry_has_no_position_field(self):
        adapter, fake = _ready()
        adapter.submit_order(_order(cid="R-ENTRY"))
        assert "position" not in fake.last_request
        assert fake.last_request["type"] == ORDER_TYPE_BUY


class TestOrderResultMapping:
    def test_done_confirmed_filled(self):
        adapter, fake = _ready()
        sub = adapter.submit_order(_order(cid="M-DONE"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.FILLED

    def test_placed_confirmed_submitted_no_fill(self):
        adapter, fake = _ready()
        fake.send_mode = "placed"
        sub = adapter.submit_order(_order(cid="M-PLACED"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.SUBMITTED
        assert sub.broker_order.fills == ()

    def test_done_partial_confirmed_partially_filled(self):
        adapter, fake = _ready()
        fake.send_mode = "partial"
        sub = adapter.submit_order(_order(cid="M-PARTIAL", quantity=1.0))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.PARTIALLY_FILLED
        assert len(sub.broker_order.fills) >= 1

    def test_timeout_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "timeout"
        sub = adapter.submit_order(_order(cid="M-TIMEOUT"))
        assert sub.status is SubmitStatus.UNKNOWN

    def test_connection_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "ambiguous"  # retcode 10031 CONNECTION
        sub = adapter.submit_order(_order(cid="M-CONN"))
        assert sub.status is SubmitStatus.UNKNOWN


class TestOrderCheck:
    def test_order_check_none_connection_error_no_send(self):
        adapter, fake = _ready()
        fake.order_check = lambda request: None
        with pytest.raises(BrokerConnectionError):
            adapter.submit_order(_order(cid="K-NONE"))
        assert fake.send_count == 0

    def test_order_check_nonzero_rejected_no_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 10014  # invalid volume
        sub = adapter.submit_order(_order(cid="K-REJ"))
        assert sub.status is SubmitStatus.REJECTED
        assert fake.send_count == 0

    def test_order_check_zero_proceeds_to_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 0
        sub = adapter.submit_order(_order(cid="K-OK"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1


class TestPaperDefaultUnchanged:
    def test_paper_broker_still_default_adapter(self):
        from brokers import PaperBrokerAdapter

        assert PaperBrokerAdapter is not MT5BrokerAdapter
        assert hasattr(PaperBrokerAdapter, "initial_cash")

    def test_mt5_adapter_has_no_price_source(self):
        # El engine paper exige `price_source`: MT5 nunca entra como paper.
        assert not hasattr(MT5BrokerAdapter, "price_source")


# --------------------------------------------------------------------------
# 03A: historial explicito (COMPLETE / EMPTY_CONFIRMED / INCOMPLETE)
# --------------------------------------------------------------------------


class TestOrderHistoryLookup:
    def test_history_complete(self):
        adapter, _ = _ready()
        adapter.submit_order(_order(cid="H-OK"))
        result = adapter.lookup_order_history("H-OK")
        assert result.status == HISTORY_COMPLETE
        assert result.order is not None
        assert result.order.state is BrokerOrderState.FILLED
        assert len(result.order.fills) == 1

    def test_history_empty_confirmed(self):
        adapter, _ = _ready()
        result = adapter.lookup_order_history("H-NONE")
        assert result.status == HISTORY_EMPTY_CONFIRMED
        assert result.order is None

    def test_history_error_is_not_empty_confirmed(self):
        # Un fallo de consulta historica NO se convierte en EMPTY_CONFIRMED.
        adapter, fake = _ready()
        fake.fail_history_orders_get = True
        result = adapter.lookup_order_history("H-ERR")
        assert result.status == HISTORY_INCOMPLETE
        assert result.status != HISTORY_EMPTY_CONFIRMED

    def test_get_order_raises_on_history_transport(self):
        # get_order NUNCA interpreta un fallo de historial como NOT_FOUND.
        adapter, fake = _ready()
        fake.fail_history_orders_get = True
        with pytest.raises(BrokerConnectionError):
            adapter.get_order("H-ERR2")

    def test_history_orders_none_is_incomplete_not_empty(self):
        # MetaTrader5 devuelve None en error (detalle en last_error()).
        adapter, fake = _ready()
        fake.none_history_orders_get = True
        result = adapter.lookup_order_history("H-NONE-ERR")
        assert result.status == HISTORY_INCOMPLETE
        assert result.status != HISTORY_EMPTY_CONFIRMED

    def test_history_orders_empty_is_empty_confirmed(self):
        # Coleccion vacia SIN error = sin resultados (si, ademas, no hay orden).
        adapter, _ = _ready()
        result = adapter.lookup_order_history("H-EMPTY")
        assert result.status == HISTORY_EMPTY_CONFIRMED

    def test_orders_get_none_not_silenced(self):
        # Un fallo de consulta ABIERTA que impide el discovery no se silencia.
        adapter, fake = _ready()
        fake.none_orders_get = True
        result = adapter.lookup_order_history("H-OPEN-ERR")
        assert result.status == HISTORY_INCOMPLETE
        with pytest.raises(BrokerConnectionError):
            adapter.get_order("H-OPEN-ERR")

    def test_deals_none_for_filled_is_incomplete(self):
        adapter, fake = _ready()
        adapter.submit_order(_order(cid="D-NONE"))  # FILLED con 1 deal
        fake.none_history_deals_get = True
        result = adapter.lookup_order_history("D-NONE")
        assert result.status == HISTORY_INCOMPLETE

    def test_deals_empty_for_filled_is_incomplete(self):
        # FILLED sin detalle de deals necesario para contabilizar => incompleto.
        adapter, fake = _ready()
        adapter._remember("D-EMPTY", 4242)
        fake._orders[4242] = SimpleNamespace(
            ticket=4242, symbol=MT5_SYMBOL, magic=adapter._magic_for("D-EMPTY"),
            comment=adapter._comment_for("D-EMPTY"), type=0, state=ORDER_STATE_FILLED,
            volume_initial=0.1, volume_current=0.0, price_open=50_000.0,
        )
        result = adapter.lookup_order_history("D-EMPTY")
        assert result.status == HISTORY_INCOMPLETE

    def test_last_error_text_is_sanitized(self):
        adapter, fake = _ready()
        fake.last_error_value = (4756, "Invalid params")
        text = adapter._last_error_text()
        assert "4756" in text
        assert "password" not in text.lower()

    def test_positions_get_none_not_flat(self):
        # positions_get() None (error) NO se interpreta como "sin posiciones".
        adapter, fake = _ready()
        fake.none_positions_get = True
        with pytest.raises(BrokerConnectionError):
            adapter.get_positions()

    def test_deals_for_uses_ticket_kwarg(self):
        # _deals_for DEBE invocar history_deals_get(ticket=...) (firma real) y
        # recuperar los deals de la orden.
        adapter, fake = _ready()
        adapter.submit_order(_order(cid="DK-1"))
        fake.last_deals_kwargs = None
        order = adapter.get_order("DK-1")
        assert len(order.fills) == 1
        assert fake.last_deals_kwargs is not None
        assert set(fake.last_deals_kwargs) == {"ticket"}
        assert fake.last_deals_kwargs["ticket"] == int(order.broker_order_id)

    def test_fake_rejects_order_kwarg(self):
        # El fake NO admite `order=` (ni **kwargs): reproduce la firma real.
        _, fake = _ready()
        with pytest.raises(TypeError):
            fake.history_deals_get(order=1)
