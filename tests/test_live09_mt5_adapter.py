"""Tests LIVE-09: MT5BrokerAdapter (DEMO-first, sin terminal MT5 real).

El adapter aisla la dependencia `MetaTrader5`: estos tests inyectan un fake
determinista (sin red, sin terminal, sin credenciales reales). No se envia
ninguna orden real.

Cubre: CONFIG, CONNECT, ACCOUNT, SYMBOL, SUBMIT/order_check/order_send, UNKNOWN,
correlacion cid<->ticket, LOOKUP, FILLS, OPEN ORDERS, POSITIONS, CANCEL, SAFETY,
RECONCILIATION. No usa sleeps.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from execution.execution_instance_lock import ExecutionInstanceLock
from execution.execution_authority import (
    BrokerScope,
    ExecutionAuthorizationInvalidError,
    ExecutionAuthorizationRequiredError,
    ExecutionAuthorizationReusedError,
    ExecutionAuthority,
    ExecutionIntent,
)
from execution.intent_journal import IntentJournal, OrderIntent
from execution.order import PURPOSE_REDUCE, Order, OrderSide, OrderType
from reconciliation.orchestrator import broker_state_from_adapter

from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    HISTORY_INCOMPLETE,
    MT5AccountObservationError,
    MT5AccountSnapshot,
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
    Position,
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
            server="MetaQuotes-Demo", credit=0.0,
        )
        self.last_error_value = (0, "ok")
        self.symbols: dict = {}
        self.ticks: dict = {}
        self.check_retcode = 0
        self.check_count = 0
        self.send_mode = "ok"        # ok | reject | none | raise | ambiguous
        self.remove_mode = "ok"      # ok | reject | none | raise | ambiguous
        self.accept_but_lose_response = False
        self.send_count = 0
        self.cancel_count = 0
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
        self.check_count += 1
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
        self.cancel_count += 1
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
    return MT5Credentials(
        login=123456, password=PASSWORD_CANARY, server="MetaQuotes-Demo"
    )


def _adapter(
    fake: FakeMT5 | None = None,
    *,
    clock=lambda: NOW,
    monotonic=lambda: 0.0,
    **cfg,
) -> tuple[MT5BrokerAdapter, FakeMT5]:
    fake = fake or FakeMT5()
    if MT5_SYMBOL not in fake.symbols:
        fake.add_symbol(MT5_SYMBOL)
    if MT5_SYMBOL not in fake.ticks:
        fake.add_tick(MT5_SYMBOL, bid=49_999.0, ask=50_001.0)
    adapter = MT5BrokerAdapter(
        _config(**cfg), _credentials(), mt5=fake, clock=clock, monotonic=monotonic
    )
    return adapter, fake


class _TestGate:
    """Gate de prueba expl?cito; no representa autorizaci?n de producci?n."""

    def __init__(self, *, allow=True):
        self.allow = bool(allow)
        self.calls: list[tuple] = []

    def check(self, client_order_id):
        self.calls.append(("check", str(client_order_id)))
        if not self.allow:
            raise BrokerPermissionError("test gate denied")

    def check_operation(self, operation, *, op_id, target):
        self.calls.append(("check_operation", str(operation), str(op_id), str(target)))
        if not self.allow:
            raise BrokerPermissionError("test gate denied")


def _bind_test_gate(adapter, gate=None):
    gate = gate or _TestGate()
    adapter.bind_execution_gate(gate)
    if getattr(adapter, "_execution_authority", None) is None:
        authority = ExecutionAuthority(ttl_s=3600.0)
        adapter._test_authority = authority
        adapter.bind_execution_authority(authority.verifier())
    if getattr(adapter, "_execution_lock", None) is None:
        lock_dir = tempfile.mkdtemp(prefix="atlas-live09-lock-")
        lock = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        lock.acquire()
        adapter._test_lock_dir = lock_dir
        adapter.bind_execution_lock(lock)
    return gate


def _order(cid="C-1", *, side=OrderSide.BUY, quantity=0.1,
           order_type=OrderType.MARKET, instrument=ATLAS_INSTRUMENT,
           reduce_only=False, purpose="OPEN", account_id="MT5-1",
           target_position_id=None, limit_price=None, stop_price=None) -> Order:
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id=account_id,
        instrument=instrument, side=side, quantity=quantity,
        order_type=order_type, client_order_id=cid, reduce_only=reduce_only,
        purpose=purpose, target_position_id=target_position_id,
        limit_price=limit_price, stop_price=stop_price,
    )


def _test_authority(adapter):
    authority = getattr(adapter, "_test_authority", None)
    if authority is None:
        authority = ExecutionAuthority(ttl_s=3600.0)
        adapter._test_authority = authority
    return authority


def _submit(adapter, order, **kwargs):
    authorization = _test_authority(adapter).issue(
        ExecutionIntent.from_order(order),
        broker_scope=adapter.expected_execution_scope(),
    )
    return adapter.submit_order(order, authorization=authorization, **kwargs)


def _cancel(adapter, client_order_id, *, operation_id=None,
            instrument=ATLAS_INSTRUMENT, account_id="MT5-1"):
    op_id = str(operation_id) if operation_id else f"CANCEL:{client_order_id}"
    ticket = adapter._cid_to_ticket.get(client_order_id)
    authorization = _test_authority(adapter).issue(
        ExecutionIntent.for_cancel(
            op_id=op_id,
            target_client_order_id=str(client_order_id),
            instrument=str(instrument),
            account_id=str(account_id),
            target_broker_order_id=str(ticket) if ticket is not None else "UNRESOLVED",
        ),
        broker_scope=adapter.expected_execution_scope(),
    )
    return adapter.cancel_order(
        client_order_id,
        operation_id=operation_id,
        instrument=instrument,
        account_id=account_id,
        authorization=authorization,
    )


def _ready(**cfg) -> tuple[MT5BrokerAdapter, FakeMT5]:
    adapter, fake = _adapter(**cfg)
    _bind_test_gate(adapter)
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
            _submit(adapter, _order())


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
# 06I-B4: metadatos economicos observados de la cuenta (profit/assets/...)
# --------------------------------------------------------------------------


_ACCT_FIELDS = ("profit", "assets", "liabilities", "commission_blocked")


def _account_property_raising(base, name, exc):
    """`account_info` double con descriptor `name` DEFINIDO que lanza `exc`."""

    def _getter(self):
        raise exc

    cls = type("_AcctProp", (), {name: property(_getter)})
    obj = cls()
    obj.__dict__.update(vars(base))
    return obj


class _AcctDynamic:
    """`account_info` double cuyo `name` procede de `__getattr__`."""

    def __init__(self, base, name, value):
        self.__dict__.update(vars(base))
        self._name = name
        self._value = value

    def __getattr__(self, item):
        if item == self._name:
            return self._value
        raise AttributeError(item)


class TestAccountFinancialMetadata:
    def _base(self, fake):
        if not hasattr(fake.account, "server"):
            fake.account.server = "MetaQuotes-Demo"
        if not hasattr(fake.account, "credit"):
            fake.account.credit = 0.0
        return fake.account

    def _snap(self, adapter, fake, **fields):
        self._base(fake)
        for name, value in fields.items():
            setattr(fake.account, name, value)
        return adapter.read_account_snapshot()

    def test_distinct_values_captured(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake, profit=12.5, assets=100.0,
                          liabilities=7.0, commission_blocked=3.25)
        assert snap.profit == pytest.approx(12.5)
        assert snap.assets == pytest.approx(100.0)
        assert snap.liabilities == pytest.approx(7.0)
        assert snap.commission_blocked == pytest.approx(3.25)

    def test_absent_all_none(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake)
        assert all(getattr(snap, f) is None for f in _ACCT_FIELDS)

    def test_present_none_is_none(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake, **{f: None for f in _ACCT_FIELDS})
        assert all(getattr(snap, f) is None for f in _ACCT_FIELDS)

    def test_explicit_zero_preserved_not_absence(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake, **{f: 0.0 for f in _ACCT_FIELDS})
        for f in _ACCT_FIELDS:
            assert getattr(snap, f) == 0.0
            assert getattr(snap, f) is not None

    def test_partial_present_others_none(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake, profit=-5.0, assets=50.0)
        assert snap.profit == pytest.approx(-5.0)
        assert snap.assets == pytest.approx(50.0)
        assert snap.liabilities is None
        assert snap.commission_blocked is None

    @pytest.mark.parametrize("field", _ACCT_FIELDS)
    @pytest.mark.parametrize(
        "value", [True, False, "1.0", "x", object(), float("nan"),
                  float("inf"), float("-inf"), 10 ** 400]
    )
    def test_invalid_value_typed_error(self, field, value):
        adapter, fake = _ready()
        self._base(fake)
        setattr(fake.account, field, value)
        with pytest.raises(MT5AccountObservationError):
            adapter.read_account_snapshot()

    @pytest.mark.parametrize("field", _ACCT_FIELDS)
    @pytest.mark.parametrize(
        "exc", [AttributeError("x"), RuntimeError("x"), ValueError("x")]
    )
    def test_defective_getter_typed_error(self, field, exc):
        adapter, fake = _ready()
        base = self._base(fake)
        fake.account = _account_property_raising(base, field, exc)
        with pytest.raises(MT5AccountObservationError):
            adapter.read_account_snapshot()

    @pytest.mark.parametrize("field", _ACCT_FIELDS)
    def test_dynamic_valid_preserved(self, field):
        adapter, fake = _ready()
        base = self._base(fake)
        fake.account = _AcctDynamic(base, field, 4.5)
        snap = adapter.read_account_snapshot()
        assert getattr(snap, field) == pytest.approx(4.5)

    def test_original_fields_unchanged(self):
        adapter, fake = _ready()
        snap = self._snap(adapter, fake, profit=1.0, assets=2.0,
                          liabilities=3.0, commission_blocked=4.0)
        assert snap.balance == pytest.approx(10_000.0)
        assert snap.equity == pytest.approx(10_000.0)
        assert snap.credit == pytest.approx(0.0)
        assert snap.currency == "USD"
        assert snap.margin_mode == 0

    def test_constructors_back_compat(self):
        kwargs = dict(
            server="s", login="1", login_masked="***1", currency="USD",
            margin_mode=0, balance=1.0, credit=0.5, equity=1.5,
        )
        snap_kw = MT5AccountSnapshot(**kwargs)
        assert all(getattr(snap_kw, f) is None for f in _ACCT_FIELDS)
        snap_pos = MT5AccountSnapshot(
            "s", "1", "***1", "USD", 0, 1.0, 0.5, 1.5
        )
        assert all(getattr(snap_pos, f) is None for f in _ACCT_FIELDS)


class TestAccountTemporalMetadata:
    def test_observed_at_uses_injected_clock_after_account_info(self):
        observed = datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)
        events: list[tuple[str, object]] = []
        mono_values = iter([100.0, 100.5])

        def monotonic():
            value = next(mono_values)
            events.append(("monotonic", value))
            return value

        def clock():
            events.append(("clock", None))
            return observed

        fake = FakeMT5()
        original = fake.account_info

        def wrapped_account_info():
            events.append(("account_info", None))
            return original()

        fake.account_info = wrapped_account_info
        adapter, _ = _adapter(fake=fake, clock=clock, monotonic=monotonic)
        adapter.connect()
        events.clear()
        snap = adapter.read_account_snapshot()
        assert events == [
            ("monotonic", 100.0),
            ("account_info", None),
            ("monotonic", 100.5),
            ("clock", None),
        ]
        assert snap.read_started_monotonic == 100.0
        assert snap.read_finished_monotonic == 100.5
        assert snap.observed_at == observed

    def test_observed_at_is_not_account_state_as_of(self):
        state_time = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        snapshot_time = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)
        values = iter([state_time, snapshot_time])
        adapter, _ = _adapter(clock=lambda: next(values))
        adapter.connect()
        state = adapter.get_account_state()
        snap = adapter.read_account_snapshot()
        assert state.as_of == state_time
        assert snap.observed_at == snapshot_time
        assert snap.observed_at != state.as_of

    @pytest.mark.parametrize(
        "clock",
        [
            lambda: None,
            lambda: "not-a-datetime",
            lambda: datetime(2026, 1, 1, 12, 0, 0),  # naive
        ],
    )
    def test_invalid_wall_clock_keeps_observed_at_none(self, clock):
        adapter, _ = _adapter(clock=clock)
        adapter.connect()
        snap = adapter.read_account_snapshot()
        assert snap.observed_at is None
        assert snap.balance == pytest.approx(10_000.0)
        assert snap.credit == pytest.approx(0.0)
        assert snap.equity == pytest.approx(10_000.0)

    def test_raising_wall_clock_keeps_observed_at_none(self):
        def clock():
            raise RuntimeError("wall clock failure")

        calls = []
        fake = FakeMT5()
        original = fake.account_info

        def wrapped():
            calls.append("account_info")
            return original()

        fake.account_info = wrapped
        adapter, _ = _adapter(fake=fake, clock=clock)
        adapter.connect()
        calls.clear()
        snap = adapter.read_account_snapshot()
        assert calls == ["account_info"]
        assert snap.observed_at is None
        assert snap.balance == pytest.approx(10_000.0)

    @pytest.mark.parametrize(
        "values",
        [
            (None, None),
            (float("nan"), float("inf")),
            ("x", object()),
        ],
    )
    def test_invalid_monotonic_readings_kept_none(self, values):
        seq = iter(values)
        adapter, _ = _adapter(monotonic=lambda: next(seq))
        adapter.connect()
        snap = adapter.read_account_snapshot()
        assert snap.read_started_monotonic is None
        assert snap.read_finished_monotonic is None
        assert snap.observed_at == NOW

    def test_monotonic_exception_kept_none(self):
        def monotonic():
            raise RuntimeError("monotonic failure")

        adapter, _ = _adapter(monotonic=monotonic)
        adapter.connect()
        snap = adapter.read_account_snapshot()
        assert snap.read_started_monotonic is None
        assert snap.read_finished_monotonic is None
        assert snap.observed_at == NOW

    def test_monotonic_integer_overflow_keeps_snapshot(self):
        calls = []
        fake = FakeMT5()
        original = fake.account_info

        def wrapped():
            calls.append("account_info")
            return original()

        fake.account_info = wrapped
        adapter, _ = _adapter(
            fake=fake, monotonic=lambda: 10**400
        )
        adapter.connect()
        calls.clear()
        snap = adapter.read_account_snapshot()
        assert calls == ["account_info"]
        assert snap.read_started_monotonic is None
        assert snap.read_finished_monotonic is None
        assert snap.observed_at == NOW
        assert snap.balance == pytest.approx(10_000.0)
        assert snap.credit == pytest.approx(0.0)
        assert snap.equity == pytest.approx(10_000.0)

    def test_monotonic_exception_at_start_keeps_snapshot(self):
        calls = []
        first = True
        fake = FakeMT5()
        original = fake.account_info

        def wrapped():
            calls.append("account_info")
            return original()

        def monotonic():
            nonlocal first
            if first:
                first = False
                raise RuntimeError("start clock failure")
            return 100.0

        fake.account_info = wrapped
        adapter, _ = _adapter(
            fake=fake, monotonic=monotonic
        )
        adapter.connect()
        calls.clear()
        snap = adapter.read_account_snapshot()
        assert calls == ["account_info"]
        assert snap.read_started_monotonic is None
        assert snap.read_finished_monotonic == 100.0
        assert snap.observed_at == NOW
        assert snap.balance == pytest.approx(10_000.0)

    def test_monotonic_exception_at_finish_keeps_snapshot(self):
        calls = []
        count = 0
        fake = FakeMT5()
        original = fake.account_info

        def wrapped():
            calls.append("account_info")
            return original()

        def monotonic():
            nonlocal count
            count += 1
            if count == 1:
                return 100.0
            raise RuntimeError("finish clock failure")

        fake.account_info = wrapped
        adapter, _ = _adapter(
            fake=fake, monotonic=monotonic
        )
        adapter.connect()
        calls.clear()
        snap = adapter.read_account_snapshot()
        assert calls == ["account_info"]
        assert snap.read_started_monotonic == 100.0
        assert snap.read_finished_monotonic is None
        assert snap.observed_at == NOW
        assert snap.balance == pytest.approx(10_000.0)

    def test_defective_datetime_utcoffset_runtime_error_yields_none(self):
        class BadDatetime(datetime):
            def utcoffset(self):
                raise RuntimeError("utcoffset failure")

        value = BadDatetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        calls = []
        fake = FakeMT5()
        original = fake.account_info

        def wrapped():
            calls.append("account_info")
            return original()

        fake.account_info = wrapped
        adapter, _ = _adapter(fake=fake, clock=lambda: value)
        adapter.connect()
        calls.clear()
        snap = adapter.read_account_snapshot()
        assert calls == ["account_info"]
        assert snap.observed_at is None
        assert snap.balance == pytest.approx(10_000.0)
        assert snap.credit == pytest.approx(0.0)
        assert snap.equity == pytest.approx(10_000.0)

    def test_defective_datetime_utcoffset_wrong_type_yields_none(self):
        class BadDatetime(datetime):
            def utcoffset(self):
                return "not-a-timedelta"

        value = BadDatetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        adapter, _ = _adapter(clock=lambda: value)
        adapter.connect()
        snap = adapter.read_account_snapshot()
        assert snap.observed_at is None
        assert snap.balance == pytest.approx(10_000.0)

    def test_timezone_aware_datetime_normalizes_to_utc(self):
        source = datetime(
            2026, 9, 1, 14, 0, 0,
            tzinfo=timezone(timedelta(hours=2)),
        )
        adapter, _ = _adapter(clock=lambda: source)
        adapter.connect()
        snap = adapter.read_account_snapshot()
        assert snap.observed_at == datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    def test_default_metadata_does_not_change_account_values(self):
        adapter, _ = _ready()
        snap = adapter.read_account_snapshot()
        assert snap.observed_at == NOW
        assert snap.read_started_monotonic == 0.0
        assert snap.read_finished_monotonic == 0.0
        assert snap.balance == pytest.approx(10_000.0)
        assert snap.credit == pytest.approx(0.0)
        assert snap.equity == pytest.approx(10_000.0)
        assert snap.currency == "USD"
        assert snap.margin_mode == 0

    def test_absent_account_response_still_raises(self):
        adapter, fake = _ready()
        fake.account = None
        with pytest.raises(BrokerConnectionError):
            adapter.read_account_snapshot()


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
            _submit(adapter, _order())

    def test_symbol_not_operable_fails(self):
        adapter, fake = _ready()
        fake.symbols[MT5_SYMBOL].trade_mode = 0
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order())

    def test_no_explicit_mapping_fails(self):
        adapter, _ = _adapter(symbol_map={})
        _bind_test_gate(adapter)
        adapter.connect()
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order())


# --------------------------------------------------------------------------
# SUBMIT / order_check / order_send
# --------------------------------------------------------------------------


class TestExecutionGateRequired:
    def test_submit_without_gate_is_rejected_before_broker(self):
        adapter, fake = _adapter()
        adapter.connect()
        with pytest.raises(BrokerPermissionError):
            _submit(adapter, _order(cid="NO-GATE-SUBMIT"))
        assert fake.check_count == 0
        assert fake.send_count == 0

    def test_cancel_without_gate_is_rejected_before_broker(self):
        adapter, fake = _adapter()
        adapter.connect()
        with pytest.raises(BrokerPermissionError):
            _cancel(adapter, "NO-GATE-CANCEL")
        assert fake.cancel_count == 0
        assert fake.send_count == 0

    @pytest.mark.parametrize(
        "gate",
        [
            None,
            SimpleNamespace(check=lambda cid: None),
            SimpleNamespace(check_operation=lambda *args, **kwargs: None),
            object(),
        ],
    )
    def test_bind_rejects_incomplete_gate(self, gate):
        adapter, _ = _adapter()
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(gate)
        assert adapter._execution_gate is None

    def test_valid_gate_enforces_open_cancel_and_denial(self):
        adapter, fake = _adapter()
        gate = _TestGate()
        _bind_test_gate(adapter, gate)
        adapter.connect()

        sub = _submit(adapter, _order(cid="GATE-OPEN"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1
        assert gate.calls[0] == ("check", "GATE-OPEN")

        fake.add_open_order(
            901, magic=adapter._magic_for("GATE-CANCEL"),
            comment=adapter._comment_for("GATE-CANCEL"),
        )
        adapter._remember("GATE-CANCEL", 901)
        cancelled = _cancel(adapter,
            "GATE-CANCEL", operation_id="CANCEL:GATE-CANCEL"
        )
        assert cancelled.state is BrokerOrderState.CANCELLED
        assert any(
            call[0] == "check_operation" and call[1] == "CANCEL"
            for call in gate.calls
        )
        assert fake.cancel_count == 1

        denied, denied_fake = _adapter()
        _bind_test_gate(denied, _TestGate(allow=False))
        denied.connect()
        with pytest.raises(BrokerPermissionError):
            _submit(denied, _order(cid="DENIED-OPEN"))
        assert denied_fake.check_count == 0
        assert denied_fake.send_count == 0


class TestExecutionAuthorityBinding:
    def test_bind_rejects_none_and_foreign_object(self):
        adapter, _ = _adapter()
        for verifier in (None, object(), SimpleNamespace(consume=lambda *a, **k: None)):
            with pytest.raises(BrokerError):
                adapter.bind_execution_authority(verifier)
        assert adapter._execution_authority is None

    def test_second_bind_fails_without_replacing(self):
        adapter, _ = _ready()
        first = adapter._execution_authority
        assert first is not None
        with pytest.raises(BrokerError):
            adapter.bind_execution_authority(
                ExecutionAuthority().verifier()
            )
        assert adapter._execution_authority is first


class TestExecutionLockBinding:
    def _adapter_without_lock(self):
        adapter, fake = _adapter()
        adapter.bind_execution_gate(_TestGate())
        authority = ExecutionAuthority(ttl_s=3600.0)
        adapter.bind_execution_authority(authority.verifier())
        adapter.connect()
        return adapter, fake, authority

    def test_submit_without_lock_rejected(self):
        adapter, fake, authority = self._adapter_without_lock()
        order = _order(cid="NO-LOCK-SUBMIT")
        authorization = authority.issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_cancel_without_lock_rejected(self):
        adapter, fake, authority = self._adapter_without_lock()
        fake.add_open_order(
            906, magic=adapter._magic_for("NO-LOCK-CANCEL"),
            comment=adapter._comment_for("NO-LOCK-CANCEL"),
        )
        adapter._remember("NO-LOCK-CANCEL", 906)
        authorization = authority.issue(
            ExecutionIntent.for_cancel(
                op_id="CANCEL:NO-LOCK-CANCEL",
                target_client_order_id="NO-LOCK-CANCEL",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                target_broker_order_id="906",
            ),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(BrokerPermissionError):
            adapter.cancel_order(
                "NO-LOCK-CANCEL",
                operation_id="CANCEL:NO-LOCK-CANCEL",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                authorization=authorization,
            )
        assert fake.cancel_count == 0

    def test_bind_lock_none_rejected(self):
        adapter, _ = _adapter()
        with pytest.raises(BrokerError):
            adapter.bind_execution_lock(None)
        assert adapter._execution_lock is None

    def test_bind_lock_wrong_scope_rejected(self):
        adapter, _ = _adapter()
        lock_dir = tempfile.mkdtemp(prefix="atlas-lock-scope-")
        lock = ExecutionInstanceLock.for_scope(
            BrokerScope("demo", "MetaQuotes-Demo", "999999"),
            lock_dir=lock_dir,
        )
        lock.acquire()
        try:
            with pytest.raises(BrokerError):
                adapter.bind_execution_lock(lock)
        finally:
            lock.release()
        assert adapter._execution_lock is None

    def test_second_lock_bind_rejected(self):
        adapter, _ = _ready()
        first = adapter._execution_lock
        assert first is not None
        other = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(),
            lock_dir=tempfile.mkdtemp(prefix="atlas-lock-second-"),
        )
        other.acquire()
        try:
            with pytest.raises(BrokerError):
                adapter.bind_execution_lock(other)
        finally:
            other.release()
        assert adapter._execution_lock is first


class TestExecutionAuthorizationRequired:
    def test_submit_without_authorization_rejected(self):
        adapter, fake = _ready()
        with pytest.raises(ExecutionAuthorizationRequiredError):
            adapter.submit_order(_order(cid="AUTH-NONE"))
        # order_check puede ocurrir antes del consumo; order_send NO.
        assert fake.send_count == 0

    def test_submit_with_forged_authorization_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="AUTH-FORGE")
        forged = ExecutionAuthority().issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=forged)
        assert fake.send_count == 0

    def test_authorization_bound_to_other_order_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="AUTH-BIND")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(_order(cid="OTRO")),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_replayed_authorization_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="AUTH-REPLAY")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        first = adapter.submit_order(order, authorization=authorization)
        assert first.status is SubmitStatus.CONFIRMED
        with pytest.raises(ExecutionAuthorizationReusedError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 1

    def test_cancel_without_authorization_rejected(self):
        adapter, fake = _ready()
        fake.add_open_order(
            905, magic=adapter._magic_for("AUTH-CANCEL"),
            comment=adapter._comment_for("AUTH-CANCEL"),
        )
        adapter._remember("AUTH-CANCEL", 905)
        with pytest.raises(ExecutionAuthorizationRequiredError):
            adapter.cancel_order(
                "AUTH-CANCEL",
                operation_id="CANCEL:AUTH-CANCEL",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
            )
        assert fake.cancel_count == 0


def _live_scope(**over) -> BrokerScope:
    values = dict(
        environment="demo", server="MetaQuotes-Demo", login="123456"
    )
    values.update(over)
    return BrokerScope(**values)


class TestBrokerScopeBinding:
    def test_authorization_for_other_login_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="SCOPE-LOGIN")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=_live_scope(login="999999"),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_authorization_for_other_server_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="SCOPE-SERVER")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=_live_scope(server="Other-Server"),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_authorization_for_other_environment_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="SCOPE-ENV")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=_live_scope(environment="live"),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    @pytest.mark.parametrize("field", ["server", "login", "trade_mode"])
    def test_incomplete_account_identity_fails_closed(self, field):
        adapter, fake = _ready()
        setattr(fake.account, field, None)
        order = _order(cid=f"SCOPE-INCOMPLETE-{field}")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_non_demo_trade_mode_rejected(self):
        adapter, fake = _ready()
        fake.account.trade_mode = 2  # REAL
        order = _order(cid="SCOPE-REAL")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_account_change_before_late_check_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="SCOPE-CHANGE")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        original = fake.order_check

        def order_check(request):
            fake.account.login = 999999
            return original(request)

        fake.order_check = order_check
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_cancel_authorization_for_other_login_rejected(self):
        adapter, fake = _ready()
        fake.add_open_order(
            907, magic=adapter._magic_for("SCOPE-CANCEL"),
            comment=adapter._comment_for("SCOPE-CANCEL"),
        )
        adapter._remember("SCOPE-CANCEL", 907)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.for_cancel(
                op_id="CANCEL:SCOPE-CANCEL",
                target_client_order_id="SCOPE-CANCEL",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                target_broker_order_id="907",
            ),
            broker_scope=_live_scope(login="999999"),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.cancel_order(
                "SCOPE-CANCEL",
                operation_id="CANCEL:SCOPE-CANCEL",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                authorization=authorization,
            )
        assert fake.cancel_count == 0

    def test_cancel_rejects_account_change_before_late_check(self):
        adapter, fake = _ready()
        fake.add_open_order(
            908, magic=adapter._magic_for("SCOPE-CANCEL-2"),
            comment=adapter._comment_for("SCOPE-CANCEL-2"),
        )
        adapter._remember("SCOPE-CANCEL-2", 908)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.for_cancel(
                op_id="CANCEL:SCOPE-CANCEL-2",
                target_client_order_id="SCOPE-CANCEL-2",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                target_broker_order_id="908",
            ),
            broker_scope=adapter.expected_execution_scope(),
        )
        fake.account.login = 999999
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.cancel_order(
                "SCOPE-CANCEL-2",
                operation_id="CANCEL:SCOPE-CANCEL-2",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                authorization=authorization,
            )
        assert fake.cancel_count == 0


class TestAuthorizationEconomicBinding:
    def test_small_sell_authorization_cannot_send_large_sell(self):
        adapter, fake = _ready()
        authorized = _order(cid="ECO-SMALL", side=OrderSide.SELL, quantity=0.01)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(authorized),
            broker_scope=adapter.expected_execution_scope(),
        )
        tampered = _order(cid="ECO-SMALL", side=OrderSide.SELL, quantity=1.0)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(tampered, authorization=authorization)
        assert fake.send_count == 0

    def test_sell_authorization_cannot_send_buy(self):
        adapter, fake = _ready()
        authorized = _order(cid="ECO-SIDE", side=OrderSide.SELL, quantity=0.01)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(authorized),
            broker_scope=adapter.expected_execution_scope(),
        )
        tampered = _order(cid="ECO-SIDE", side=OrderSide.BUY, quantity=0.01)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(tampered, authorization=authorization)
        assert fake.send_count == 0

    def test_authorization_cannot_change_limit_or_stop_price(self):
        adapter, fake = _ready()
        authorized = _order(cid="ECO-PRICE", limit_price=1.0, stop_price=0.5)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(authorized),
            broker_scope=adapter.expected_execution_scope(),
        )
        tampered = _order(cid="ECO-PRICE", limit_price=2.0, stop_price=0.5)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(tampered, authorization=authorization)
        assert fake.send_count == 0

    def test_open_with_reduce_only_is_rejected(self):
        adapter, fake = _ready()
        order = _order(cid="ECO-OPEN-RO", reduce_only=True)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.submit_order(order)
        assert fake.send_count == 0

    def test_cancel_authorization_bound_to_ticket(self):
        adapter, fake = _ready()
        fake.add_open_order(
            903, magic=adapter._magic_for("CANCEL-T"),
            comment=adapter._comment_for("CANCEL-T"),
        )
        adapter._remember("CANCEL-T", 903)
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.for_cancel(
                op_id="CANCEL:CANCEL-T",
                target_client_order_id="CANCEL-T",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                target_broker_order_id="999",
            ),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.cancel_order(
                "CANCEL-T",
                operation_id="CANCEL:CANCEL-T",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                authorization=authorization,
            )
        assert fake.cancel_count == 0

    def test_foreign_verifier_key_cannot_authorize(self):
        adapter, fake = _ready()
        order = _order(cid="ECO-FOREIGN")
        authorization = ExecutionAuthority().issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            adapter.submit_order(order, authorization=authorization)
        assert fake.send_count == 0

    def test_adapter_verifier_exposes_no_signing_capability(self):
        adapter, _ = _ready()
        verifier = adapter._execution_authority
        assert not hasattr(verifier, "issue")
        assert not hasattr(verifier, "_ExecutionVerifier__authority")
        assert "private" not in " ".join(verifier.__slots__).lower()


class TestAuthorizationConsumptionOrder:
    def test_failed_order_check_does_not_consume_authorization(self):
        adapter, fake = _ready()
        fake.check_retcode = 10014
        order = _order(cid="ORD-CHECK")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        result = adapter.submit_order(order, authorization=authorization)
        assert result.status is SubmitStatus.REJECTED
        assert adapter._execution_authority.consumed_count == 0
        assert fake.send_count == 0

    def test_failed_ticket_resolution_does_not_consume_cancel_authorization(self):
        adapter, fake = _ready()
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.for_cancel(
                op_id="CANCEL:NOPE",
                target_client_order_id="NOPE",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                target_broker_order_id="999",
            ),
            broker_scope=adapter.expected_execution_scope(),
        )
        with pytest.raises(OrderNotFoundError):
            adapter.cancel_order(
                "NOPE",
                operation_id="CANCEL:NOPE",
                instrument=ATLAS_INSTRUMENT,
                account_id="MT5-1",
                authorization=authorization,
            )
        assert adapter._execution_authority.consumed_count == 0
        assert fake.cancel_count == 0

    def test_authorization_is_consumed_before_order_send(self):
        adapter, fake = _ready()
        seen: dict[str, int] = {}
        original = fake.order_send

        def order_send(request):
            seen["consumed"] = adapter._execution_authority.consumed_count
            return original(request)

        fake.order_send = order_send
        _submit(adapter, _order(cid="ORD-ORDER"))
        assert seen["consumed"] == 1
        assert fake.send_count == 1

    def test_nonce_lock_is_released_before_order_send(self):
        import threading

        adapter, fake = _ready()
        authority = _test_authority(adapter)
        original = fake.order_send

        def order_send(request):
            done = threading.Event()

            def consume_other():
                other_order = _order(cid="ORD-LOCK-OTHER")
                other_intent = ExecutionIntent.from_order(other_order)
                other_auth = authority.issue(
                    other_intent,
                    broker_scope=adapter.expected_execution_scope(),
                )
                adapter._execution_authority.consume(
                    other_auth,
                    intent=other_intent,
                    broker_scope=adapter.expected_execution_scope(),
                )
                done.set()

            worker = threading.Thread(target=consume_other)
            worker.start()
            worker.join(timeout=2.0)
            assert done.is_set()
            return original(request)

        fake.order_send = order_send
        _submit(adapter, _order(cid="ORD-LOCK"))
        assert fake.send_count == 1

    def test_concurrent_adapter_submit_uses_authorization_once(self):
        import threading

        class _BarrierFake(FakeMT5):
            def __init__(self):
                super().__init__()
                self.barrier = threading.Barrier(2)

            def order_check(self, request):
                self.barrier.wait(timeout=5)
                return super().order_check(request)

        fake = _BarrierFake()
        adapter, fake = _adapter(fake)
        _bind_test_gate(adapter)
        adapter.connect()
        order = _order(cid="ORD-CONC")
        authorization = _test_authority(adapter).issue(
            ExecutionIntent.from_order(order),
            broker_scope=adapter.expected_execution_scope(),
        )
        results: list[str] = []
        lock = threading.Lock()

        def attempt():
            try:
                adapter.submit_order(order, authorization=authorization)
                outcome = "ok"
            except ExecutionAuthorizationReusedError:
                outcome = "reused"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count("ok") == 1
        assert results.count("reused") == 1
        assert fake.send_count == 1


class TestSymbolVolumeMetadata:
    def _spec_or_raise(self, fake):
        adapter, _ = _adapter(fake)
        adapter.connect()
        return adapter._symbol_spec(ATLAS_INSTRUMENT)

    @pytest.mark.parametrize("field", ["volume_min", "volume_step", "volume_max"])
    def test_missing_required_volume_field_is_typed(self, field):
        fake = FakeMT5()
        fake.add_symbol(MT5_SYMBOL)
        delattr(fake.symbols[MT5_SYMBOL], field)
        with pytest.raises(BrokerInvalidOrderError):
            self._spec_or_raise(fake)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("volume_min", True),
            ("volume_step", False),
            ("volume_max", "100"),
            ("volume_min", None),
            ("volume_min", float("nan")),
            ("volume_step", float("inf")),
            ("volume_max", float("-inf")),
        ],
    )
    def test_invalid_volume_metadata_type_or_value_is_typed(self, field, value):
        fake = FakeMT5()
        fake.add_symbol(MT5_SYMBOL)
        setattr(fake.symbols[MT5_SYMBOL], field, value)
        with pytest.raises(BrokerInvalidOrderError):
            self._spec_or_raise(fake)

    @pytest.mark.parametrize(
        "over",
        [
            {"volume_min": 0.0},
            {"volume_min": -0.01},
            {"volume_step": 0.0},
            {"volume_step": -0.01},
            {"volume_min": 10.0, "volume_max": 1.0},
        ],
    )
    def test_incoherent_volume_metadata_is_typed(self, over):
        fake = FakeMT5()
        fake.add_symbol(MT5_SYMBOL, **over)
        with pytest.raises(BrokerInvalidOrderError):
            self._spec_or_raise(fake)

    def test_volume_getter_exception_is_typed(self):
        class BadInfo(SimpleNamespace):
            @property
            def volume_min(self):
                raise RuntimeError("getter failure")

        fake = FakeMT5()
        fake.add_symbol(MT5_SYMBOL)
        base = fake.symbols[MT5_SYMBOL]
        fake.symbols[MT5_SYMBOL] = BadInfo(**vars(base))
        with pytest.raises(BrokerInvalidOrderError):
            self._spec_or_raise(fake)

    def test_symbol_info_exception_is_typed(self):
        fake = FakeMT5()
        fake.add_symbol(MT5_SYMBOL)

        def fail(name):
            raise RuntimeError("symbol_info failure")

        fake.symbol_info = fail
        with pytest.raises(BrokerConnectionError):
            self._spec_or_raise(fake)


class TestSubmit:
    def test_order_check_rejection_no_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 10013
        sub = _submit(adapter, _order())
        assert sub.status is SubmitStatus.REJECTED
        assert fake.send_count == 0  # nunca llego a order_send

    def test_submit_confirmed_with_fill(self):
        adapter, fake = _ready()
        sub = _submit(adapter, _order(cid="C-OK"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order is not None
        assert sub.broker_order.state is BrokerOrderState.FILLED
        assert sub.broker_order.filled_quantity == pytest.approx(0.1)
        assert fake.send_count == 1

    def test_send_rejection(self):
        adapter, fake = _ready()
        fake.send_mode = "reject"
        sub = _submit(adapter, _order())
        assert sub.status is SubmitStatus.REJECTED

    def test_send_none_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "none"
        sub = _submit(adapter, _order())
        assert sub.status is SubmitStatus.UNKNOWN
        assert sub.broker_order is None

    def test_send_raises_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "raise"
        sub = _submit(adapter, _order())
        assert sub.status is SubmitStatus.UNKNOWN

    def test_ambiguous_retcode_is_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "ambiguous"
        sub = _submit(adapter, _order())
        assert sub.status is SubmitStatus.UNKNOWN

    def test_unsupported_order_type_no_submit(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order(order_type=OrderType.LIMIT))
        assert fake.send_count == 0

    def test_volume_below_min_rejected(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order(quantity=0.001))
        assert fake.send_count == 0

    @pytest.mark.parametrize(
        "quantity",
        [True, "0.1", float("nan"), float("inf"), float("-inf")],
    )
    def test_volume_non_numeric_or_non_finite_rejected(self, quantity):
        adapter, fake = _ready()
        spec = adapter._symbol_spec(ATLAS_INSTRUMENT)
        with pytest.raises(BrokerInvalidOrderError):
            adapter._validate_volume(spec, quantity)
        assert fake.send_count == 0

    def test_volume_above_max_rejected(self):
        adapter, fake = _ready()
        fake.symbols[MT5_SYMBOL].volume_max = 0.10
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order(quantity=0.11))
        assert fake.send_count == 0

    def test_volume_not_aligned_rejected(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter, _order(quantity=0.015))  # step 0.01
        assert fake.send_count == 0

    def test_no_silent_quantity_mutation(self):
        adapter, fake = _ready()
        _submit(adapter, _order(quantity=0.10))
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
        _submit(adapter, order)
        assert seen and seen[0] is not None


# --------------------------------------------------------------------------
# CORRELATION / LOOKUP
# --------------------------------------------------------------------------


class TestCorrelationLookup:
    def test_cid_to_ticket_stable(self):
        adapter, fake = _ready()
        sub = _submit(adapter, _order(cid="C-CORR"))
        ticket = int(sub.broker_order.broker_order_id)
        assert adapter._cid_to_ticket["C-CORR"] == ticket
        assert adapter.get_order("C-CORR").broker_order_id == str(ticket)

    def test_lookup_found(self):
        adapter, _ = _ready()
        _submit(adapter, _order(cid="C-FOUND"))
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
        sub = _submit(adapter, _order(cid="C-LOST"))
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
# 06I-C1: consumidores (lookup/polling) y fallo POST-SEND fail-closed
# --------------------------------------------------------------------------


_UNMAPPED_MT5_SYMBOL = "GHOST.raw"


class _UnmappedSymbolAfterSend(FakeMT5):
    """FakeMT5 cuya orden ENVIADA queda registrada con un simbolo NO mapeado.

    Simula un broker que responde DONE pero refleja la orden con un simbolo
    distinto al solicitado (anomalia): la resolucion MT5->Atlas debe fallar.
    """

    def __init__(self, unmapped: str = _UNMAPPED_MT5_SYMBOL) -> None:
        super().__init__()
        self._unmapped = unmapped

    def order_send(self, request):
        result = super().order_send(request)
        ticket = getattr(result, "order", None)
        if ticket and int(ticket) in self._orders:
            self._orders[int(ticket)].symbol = self._unmapped
        return result


class TestOrderSymbolConsumerRegression:
    # ---- lookup_order_history -------------------------------------------
    def _history_order(self, adapter, cid, ticket, symbol):
        magic = adapter._magic_for(cid)
        comment = adapter._comment_for(cid)
        return SimpleNamespace(
            ticket=ticket, symbol=symbol, magic=magic, comment=comment,
            type=ORDER_TYPE_BUY, state=ORDER_STATE_FILLED,
            volume_initial=0.1, volume_current=0.0, price_open=50_000.0,
        )

    def test_lookup_history_mapped_symbol_ok(self):
        adapter, fake = _ready()
        fake._orders[201] = self._history_order(adapter, "H-MAP", 201, MT5_SYMBOL)
        fake.add_deal(1201, 201, volume=0.1, price=50_000.0)
        adapter._remember("H-MAP", 201)
        result = adapter.lookup_order_history("H-MAP")
        assert result.status == HISTORY_COMPLETE
        assert result.order.instrument == ATLAS_INSTRUMENT

    def test_lookup_history_unmapped_symbol_raises(self):
        adapter, fake = _ready()
        fake._orders[202] = self._history_order(
            adapter, "H-GHOST", 202, _UNMAPPED_MT5_SYMBOL
        )
        adapter._remember("H-GHOST", 202)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.lookup_order_history("H-GHOST")

    def test_lookup_history_unmapped_not_empty_or_incomplete(self):
        # El fallo de resolucion NO puede degradarse a EMPTY_CONFIRMED ni a un
        # INCOMPLETE "silencioso": debe ser el error tipado, nunca un resultado.
        adapter, fake = _ready()
        fake._orders[203] = self._history_order(
            adapter, "H-RAW", 203, _UNMAPPED_MT5_SYMBOL
        )
        adapter._remember("H-RAW", 203)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.lookup_order_history("H-RAW")

    # ---- get_open_orders -------------------------------------------------
    def test_get_open_orders_all_mapped_ok(self):
        adapter, fake = _ready()
        fake.add_open_order(301, symbol=MT5_SYMBOL)
        orders = adapter.get_open_orders()
        assert len(orders) == 1
        assert orders[0].instrument == ATLAS_INSTRUMENT

    def test_get_open_orders_unmapped_raises(self):
        adapter, fake = _ready()
        fake.add_open_order(302, symbol=_UNMAPPED_MT5_SYMBOL)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.get_open_orders()

    def test_get_open_orders_mixed_has_no_partial_snapshot(self):
        adapter, fake = _ready()
        fake.add_open_order(303, symbol=MT5_SYMBOL)
        fake.add_open_order(304, symbol=_UNMAPPED_MT5_SYMBOL)
        with pytest.raises(BrokerInvalidOrderError):
            adapter.get_open_orders()  # NO devuelve una lista parcial

    # ---- fallo POST-SEND (efecto externo ya ocurrido) --------------------
    def test_submit_post_send_unmapped_returns_unknown_no_order(self):
        adapter, fake = _adapter(_UnmappedSymbolAfterSend())
        _bind_test_gate(adapter)
        adapter.connect()
        sub = _submit(adapter, _order(cid="S-UNMAP"))
        assert sub.status is SubmitStatus.UNKNOWN
        assert sub.broker_order is None
        assert fake.send_count == 1  # order_send SI ocurrio (efecto externo)

    def test_cancel_post_send_unmapped_is_ambiguous_no_order(self):
        adapter, fake = _ready()
        cid = "CANC-UNMAP"
        fake.add_open_order(
            401, symbol=_UNMAPPED_MT5_SYMBOL,
            magic=adapter._magic_for(cid), comment=adapter._comment_for(cid),
        )
        adapter._remember(cid, 401)
        with pytest.raises(BrokerConnectionError):
            _cancel(adapter, cid, operation_id=f"CANCEL:{cid}")
        # La cancelacion SI se ejecuto en el broker antes del fallo de resolucion.
        assert fake._orders[401].state == ORDER_STATE_CANCELED


# --------------------------------------------------------------------------
# FILLS
# --------------------------------------------------------------------------


class TestFills:
    def test_single_deal(self):
        adapter, _ = _ready()
        _submit(adapter, _order(cid="F1"))
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
        _submit(adapter, _order(cid="F4"))
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
# 06I-B3: captura estricta del SWAP acumulado de posiciones abiertas
# --------------------------------------------------------------------------


_SWAP_NOT_SET = object()


class _PosSwapRaiser:
    """Posicion MT5 con un descriptor `swap` DEFINIDO que lanza `exc`."""

    def __init__(self, base, exc):
        self.__dict__.update(vars(base))
        self._exc = exc

    @property
    def swap(self):
        raise self._exc


class _PosSwapDynamic:
    """Posicion MT5 cuyo `swap` procede de `__getattr__` (atributo dinamico)."""

    def __init__(self, base, value):
        self.__dict__.update(vars(base))
        self._value = value

    def __getattr__(self, item):
        if item == "swap":
            return self._value
        raise AttributeError(item)


class TestPositionSwapCapture:
    def _view(self, adapter, fake, ticket, *, swap=_SWAP_NOT_SET, **kw):
        fake.add_position(ticket, symbol=MT5_SYMBOL, **kw)
        if swap is not _SWAP_NOT_SET:
            setattr(fake._positions[ticket], "swap", swap)
        return adapter.get_positions()[0]

    def test_positive_swap_preserved(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 90, swap=1.25)
        assert pos.swap == pytest.approx(1.25)

    def test_negative_swap_preserved(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 91, swap=-2.5)
        assert pos.swap == pytest.approx(-2.5)

    def test_absent_attribute_is_none(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 92)
        assert pos.swap is None

    def test_present_none_is_none(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 93, swap=None)
        assert pos.swap is None

    def test_absent_is_none_not_zero(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 94)
        assert pos.swap is None
        assert pos.swap != 0.0

    @pytest.mark.parametrize(
        "value", [True, False, "1.0", "x", object(), float("nan"),
                  float("inf"), float("-inf"), 10 ** 400]
    )
    def test_invalid_swap_typed_error(self, value):
        adapter, fake = _ready()
        fake.add_position(95, symbol=MT5_SYMBOL)
        setattr(fake._positions[95], "swap", value)
        with pytest.raises(BrokerError):
            adapter.get_positions()

    @pytest.mark.parametrize(
        "exc", [AttributeError("x"), RuntimeError("x"), ValueError("x")]
    )
    def test_defined_getter_raises_typed_error(self, exc):
        adapter, fake = _ready()
        fake.add_position(96, symbol=MT5_SYMBOL)
        base = fake._positions[96]
        fake._positions[96] = _PosSwapRaiser(base, exc)
        with pytest.raises(BrokerError):
            adapter.get_positions()

    def test_dynamic_valid_swap_preserved(self):
        adapter, fake = _ready()
        fake.add_position(97, symbol=MT5_SYMBOL)
        base = fake._positions[97]
        fake._positions[97] = _PosSwapDynamic(base, 0.75)
        assert adapter.get_positions()[0].swap == pytest.approx(0.75)

    def test_other_fields_unchanged_with_swap(self):
        adapter, fake = _ready()
        pos = self._view(adapter, fake, 98, swap=3.0, volume=1.5,
                         price_open=50_000.0, type=1)
        assert pos.swap == pytest.approx(3.0)
        assert pos.quantity == pytest.approx(-1.5)
        assert pos.avg_entry_price == pytest.approx(50_000.0)
        assert pos.instrument == ATLAS_INSTRUMENT
        assert pos.mt5_symbol == MT5_SYMBOL

    def test_position_constructors_back_compat(self):
        old_kw = Position(
            instrument="EURUSD", quantity=1.0, avg_entry_price=1.1,
            updated_at=NOW,
        )
        assert old_kw.swap is None
        old_pos = Position("EURUSD", 1.0, 1.1, NOW)  # posicional antiguo
        assert old_pos.swap is None


# --------------------------------------------------------------------------
# CANCEL
# --------------------------------------------------------------------------


class TestCancel:
    def test_cancel_confirmed(self):
        adapter, fake = _ready()
        magic = adapter._magic_for("CAN-1")
        fake.add_open_order(90, magic=magic, comment=adapter._comment_for("CAN-1"))
        adapter._remember("CAN-1", 90)
        cancelled = _cancel(adapter, "CAN-1", operation_id="CANCEL:CAN-1")
        assert cancelled.state is BrokerOrderState.CANCELLED

    def test_cancel_rejected(self):
        adapter, fake = _ready()
        fake.add_open_order(91, comment=adapter._comment_for("CAN-2"))
        adapter._remember("CAN-2", 91)
        fake.remove_mode = "reject"
        with pytest.raises(OrderRejectedError):
            _cancel(adapter, "CAN-2", operation_id="CANCEL:CAN-2")

    def test_cancel_uncertain(self):
        adapter, fake = _ready()
        fake.add_open_order(92, comment=adapter._comment_for("CAN-3"))
        adapter._remember("CAN-3", 92)
        fake.remove_mode = "none"
        with pytest.raises(BrokerConnectionError):
            _cancel(adapter, "CAN-3", operation_id="CANCEL:CAN-3")

    def test_cancel_not_found(self):
        adapter, _ = _ready()
        with pytest.raises(OrderNotFoundError):
            _cancel(adapter, "NO-EXISTE", operation_id="CANCEL:NO-EXISTE")


# --------------------------------------------------------------------------
# SAFETY / RECONCILIATION
# --------------------------------------------------------------------------


class TestSafetyReconciliation:
    def test_submit_before_ready_raises(self):
        adapter, fake = _adapter()
        with pytest.raises(BrokerConnectionError):
            _submit(adapter, _order())
        assert fake.send_count == 0

    def test_unknown_then_no_automatic_resubmit(self):
        adapter, fake = _ready()
        fake.accept_but_lose_response = True
        _submit(adapter, _order(cid="S-1"))
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
            _submit(adapter, _order())
        assert fake.send_count == 0


class TestReduceOnly:
    def test_long_position_reduce_only_sell_request(self):
        adapter, fake = _ready()
        fake.add_position(80, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        sub = _submit(adapter,
            _order(cid="R-LONG", side=OrderSide.SELL, quantity=1.0, reduce_only=True, purpose=PURPOSE_REDUCE)
        )
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.last_request["type"] == ORDER_TYPE_SELL
        assert fake.last_request["position"] == 80
        assert fake.last_request["volume"] == pytest.approx(1.0)

    def test_short_position_reduce_only_buy_request(self):
        adapter, fake = _ready()
        fake.add_position(81, symbol=MT5_SYMBOL, volume=2.0, type=ORDER_TYPE_SELL)
        sub = _submit(adapter,
            _order(cid="R-SHORT", side=OrderSide.BUY, quantity=2.0, reduce_only=True, purpose=PURPOSE_REDUCE)
        )
        assert sub.status is SubmitStatus.CONFIRMED
        assert fake.last_request["type"] == ORDER_TYPE_BUY
        assert fake.last_request["position"] == 81

    def test_reduce_only_without_position_rejected_no_send(self):
        adapter, fake = _ready()
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter,
                _order(side=OrderSide.SELL, quantity=1.0, reduce_only=True, purpose=PURPOSE_REDUCE)
            )
        assert fake.send_count == 0

    def test_reduce_only_volume_exceeds_position_rejected_no_send(self):
        adapter, fake = _ready()
        fake.add_position(82, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter,
                _order(side=OrderSide.SELL, quantity=2.0, reduce_only=True, purpose=PURPOSE_REDUCE)
            )
        assert fake.send_count == 0

    def test_reduce_only_cannot_invert_position(self):
        adapter, fake = _ready()
        fake.add_position(83, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        # BUY (mismo sentido) reduce_only => intentaria invertir/abrir.
        with pytest.raises(BrokerInvalidOrderError):
            _submit(adapter,
                _order(side=OrderSide.BUY, quantity=1.0, reduce_only=True, purpose=PURPOSE_REDUCE)
            )
        assert fake.send_count == 0

    def test_hedging_reduce_only_rejected_no_send(self):
        adapter, fake = _ready()
        fake.account.margin_mode = 2  # RETAIL_HEDGING
        fake.add_position(84, symbol=MT5_SYMBOL, volume=1.0, type=ORDER_TYPE_BUY)
        with pytest.raises(BrokerPermissionError):
            _submit(adapter,
                _order(side=OrderSide.SELL, quantity=1.0, reduce_only=True, purpose=PURPOSE_REDUCE)
            )
        assert fake.send_count == 0

    def test_normal_entry_has_no_position_field(self):
        adapter, fake = _ready()
        _submit(adapter, _order(cid="R-ENTRY"))
        assert "position" not in fake.last_request
        assert fake.last_request["type"] == ORDER_TYPE_BUY


class TestOrderResultMapping:
    def test_done_confirmed_filled(self):
        adapter, fake = _ready()
        sub = _submit(adapter, _order(cid="M-DONE"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.FILLED

    def test_placed_confirmed_submitted_no_fill(self):
        adapter, fake = _ready()
        fake.send_mode = "placed"
        sub = _submit(adapter, _order(cid="M-PLACED"))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.SUBMITTED
        assert sub.broker_order.fills == ()

    def test_done_partial_confirmed_partially_filled(self):
        adapter, fake = _ready()
        fake.send_mode = "partial"
        sub = _submit(adapter, _order(cid="M-PARTIAL", quantity=1.0))
        assert sub.status is SubmitStatus.CONFIRMED
        assert sub.broker_order.state is BrokerOrderState.PARTIALLY_FILLED
        assert len(sub.broker_order.fills) >= 1

    def test_timeout_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "timeout"
        sub = _submit(adapter, _order(cid="M-TIMEOUT"))
        assert sub.status is SubmitStatus.UNKNOWN

    def test_connection_unknown(self):
        adapter, fake = _ready()
        fake.send_mode = "ambiguous"  # retcode 10031 CONNECTION
        sub = _submit(adapter, _order(cid="M-CONN"))
        assert sub.status is SubmitStatus.UNKNOWN


class TestOrderCheck:
    def test_order_check_none_connection_error_no_send(self):
        adapter, fake = _ready()
        fake.order_check = lambda request: None
        with pytest.raises(BrokerConnectionError):
            _submit(adapter, _order(cid="K-NONE"))
        assert fake.send_count == 0

    def test_order_check_nonzero_rejected_no_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 10014  # invalid volume
        sub = _submit(adapter, _order(cid="K-REJ"))
        assert sub.status is SubmitStatus.REJECTED
        assert fake.send_count == 0

    def test_order_check_zero_proceeds_to_send(self):
        adapter, fake = _ready()
        fake.check_retcode = 0
        sub = _submit(adapter, _order(cid="K-OK"))
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
        _submit(adapter, _order(cid="H-OK"))
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
        _submit(adapter, _order(cid="D-NONE"))  # FILLED con 1 deal
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
        _submit(adapter, _order(cid="DK-1"))
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

class _CancelAckButActiveFake(FakeMT5):
    def _send_remove(self, request):
        ticket = int(request["order"])
        return SimpleNamespace(retcode=10009, order=ticket, deal=0, volume=0.0, price=0.0)


class TestCancelAckNoInference:
    def test_ack_active_returns_observed_state(self):
        fake = _CancelAckButActiveFake()
        fake.add_open_order(
            990, symbol=MT5_SYMBOL, magic=0, comment=""
        )
        adapter, fake = _adapter(fake)
        _bind_test_gate(adapter)
        adapter.connect()
        adapter._remember("CANCEL-ACK-ACTIVE", 990)
        result = _cancel(adapter, "CANCEL-ACK-ACTIVE", operation_id="CANCEL:CANCEL-ACK-ACTIVE")
        assert result.state is BrokerOrderState.SUBMITTED
        assert fake._open_orders[990].state == ORDER_STATE_PLACED

class TestDealFillPositionIdentity:
    @staticmethod
    def _deal(**over):
        values = dict(
            order=123, ticket=456, volume=0.1, price=1.1,
            time=1_700_000_000, commission=0.5, position_id=900,
        )
        values.update(over)
        return SimpleNamespace(**values)

    def test_deal_position_id_captured_with_order_identity(self):
        fill = MT5BrokerAdapter._deal_to_fill(self._deal())
        assert fill.broker_order_id == "123"
        assert fill.fill_sequence == 456
        assert fill.position_id == "900"

    def test_missing_deal_position_id_stays_none(self):
        deal = self._deal()
        del deal.position_id
        fill = MT5BrokerAdapter._deal_to_fill(deal)
        assert fill.position_id is None

    @pytest.mark.parametrize(
        "value", [None, 0, -1, True, 1.5, "900", object()]
    )
    def test_invalid_deal_position_id_stays_none(self, value):
        fill = MT5BrokerAdapter._deal_to_fill(
            self._deal(position_id=value)
        )
        assert fill.position_id is None

    def test_broker_fill_constructor_back_compat(self):
        from brokers.models import BrokerFill
        fill = BrokerFill(
            "123", 0.1, 1.1, datetime.fromtimestamp(1_700_000_000, tz=UTC),
            0.5, 456,
        )
        assert fill.position_id is None
