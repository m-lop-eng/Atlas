"""Tests MT5-DEMO-EXECUTION-FOUNDATION-01: ruta segura de ejecucion.

Exclusivamente FakeMT5: sin terminal real, sin credenciales, sin ordenes reales.
Comprueban tanto los resultados como la AUSENCIA de llamadas indebidas
(`order_send`/`order_check` no invocados cuando un gate bloquea).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from execution.instrument import InstrumentSpec
from execution.intent_journal import (
    OPERATION_CANCEL,
    OPERATION_CLOSE,
    OPERATION_REDUCE,
    IntentJournal,
    IntentJournalError,
    IntentJournalNotInitializedError,
    OrderIntent,
    recover_pending_cancels,
    recover_pending_intents,
)
from execution.live_execution import (
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    ExecutionBlockedError,
    ExecutionGate,
    ExecutionNotAuthorizedError,
    LiveExecutionCoordinator,
    MarketDataReference,
    UnresolvedIntentsError,
    UnsupportedAccountModeError,
)
from execution.order import (
    PURPOSE_CLOSE,
    PURPOSE_REDUCE,
    Order,
    OrderSide,
    OrderType,
)
from execution.order_manager import OrderManager
from risk.types import AccountRiskState

from brokers.models import (
    BrokerConnectionError,
    BrokerError,
    BrokerOrderState,
    SubmitStatus,
)
from brokers.mt5 import (
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
    ORDER_STATE_CANCELED,
    ORDER_STATE_FILLED,
    ORDER_STATE_PLACED,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_REMOVE,
)
from reconciliation.models import ReconciliationStatus

UTC = timezone.utc
NOW = datetime.fromtimestamp(1_788_264_000, tz=UTC)  # 2026-09-01T12:00Z (alineado 1h)
OPEN_TIME = 1_788_260_400
CLOSE_TIME = 1_788_264_000
INSTRUMENT = "EURUSD"
PASSWORD_CANARY = "not-a-real-password"


# --------------------------------------------------------------------------
# FakeMT5 minimo (sin terminal real)
# --------------------------------------------------------------------------


class FakeMT5:
    def __init__(self) -> None:
        self.initialize_ok = True
        self.terminal = SimpleNamespace(connected=True)
        self.account = SimpleNamespace(
            login=42, balance=10_000.0, equity=10_000.0, margin_free=9_000.0,
            currency="USD", margin_mode=0, trade_allowed=True, trade_expert=True,
            trade_mode=0,
        )
        self.last_error_value = (0, "ok")
        self.symbols: dict = {}
        self.ticks: dict = {}
        self.check_retcode = 0
        self.send_mode = "ok"  # ok | none | raise | reject | ambiguous
        self.remove_mode = "ok"  # ok | none | raise | reject | ambiguous
        self.send_count = 0
        self.cancel_count = 0
        self.check_count = 0
        self.last_request: dict | None = None
        self._orders: dict = {}
        self._open_orders: dict = {}
        self._deals: dict = {}
        self._positions: dict = {}
        self._next = 100

    def add_symbol(self, name: str, **over) -> None:
        fields = dict(
            name=name, visible=True, volume_min=0.01, volume_step=0.01,
            volume_max=100.0, point=0.00001, trade_tick_size=0.00001,
            trade_tick_value=1.0, trade_contract_size=100000.0,
            currency_base="EUR", currency_profit="USD", currency_margin="USD",
            trade_mode=4, filling_mode=1, digits=5,
        )
        fields.update(over)
        self.symbols[name] = SimpleNamespace(**fields)

    def add_tick(self, name: str, bid: float, ask: float) -> None:
        self.ticks[name] = SimpleNamespace(bid=bid, ask=ask)

    def add_open_order(self, ticket, *, symbol=INSTRUMENT, magic=0, comment="",
                       type=ORDER_TYPE_BUY, volume=0.1, price=1.1,
                       state=ORDER_STATE_PLACED) -> None:
        self._open_orders[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, magic=magic, comment=comment,
            type=type, state=state, volume_initial=volume, volume_current=volume,
            price_open=price,
        )

    def add_deal(self, ticket, order, *, volume=1.0, price=1.1, commission=0.0,
                 time=1_700_000_000) -> None:
        self._deals[ticket] = SimpleNamespace(
            ticket=ticket, order=order, volume=volume, price=price,
            commission=commission, time=time,
        )

    def add_position(self, ticket, *, symbol=INSTRUMENT, volume=0.1, price_open=1.1,
                     type=ORDER_TYPE_BUY, time=1_700_000_000) -> None:
        self._positions[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, volume=volume, price_open=price_open,
            type=type, time=time,
        )

    # --- API MT5 simulada
    def initialize(self, **kwargs):
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
        return True

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
        self.last_request = dict(request)
        if request.get("action") == TRADE_ACTION_REMOVE:
            return self._send_remove(request)
        self.send_count += 1
        if self.send_mode == "none":
            return None
        if self.send_mode == "raise":
            raise RuntimeError("transporte")
        if self.send_mode == "ambiguous":
            return SimpleNamespace(retcode=10012, order=0, deal=0, volume=0.0, price=0.0)
        if self.send_mode == "reject":
            return SimpleNamespace(retcode=10006, order=0, deal=0, volume=0.0, price=0.0)
        ticket = self._next; self._next += 1
        deal_ticket = self._next; self._next += 1
        self._orders[ticket] = self._mk_order(
            ticket, request, ORDER_STATE_FILLED, request["volume"], 0.0
        )
        self._deals[deal_ticket] = SimpleNamespace(
            ticket=deal_ticket, order=ticket, volume=request["volume"],
            price=request["price"], commission=0.0, time=1_700_000_000,
        )
        return SimpleNamespace(
            retcode=10009, order=ticket, deal=deal_ticket,
            volume=request["volume"], price=request["price"],
        )

    def _send_remove(self, request):
        self.cancel_count += 1
        if self.remove_mode == "none":
            return None
        if self.remove_mode == "raise":
            raise RuntimeError("transporte")
        if self.remove_mode == "ambiguous":
            return SimpleNamespace(retcode=10012, order=0, deal=0, volume=0.0, price=0.0)
        if self.remove_mode == "reject":
            return SimpleNamespace(retcode=10006, order=0, deal=0, volume=0.0, price=0.0)
        ticket = int(request["order"])
        order = self._open_orders.pop(ticket, None)
        if order is None:
            return SimpleNamespace(retcode=10006, order=0, deal=0, volume=0.0, price=0.0)
        order.state = ORDER_STATE_CANCELED
        self._orders[ticket] = order
        return SimpleNamespace(retcode=10009, order=ticket, deal=0, volume=0.0, price=0.0)

    def orders_get(self, ticket=None):
        rows = list(self._open_orders.values())
        if ticket is not None:
            rows = [o for o in rows if o.ticket == ticket]
        return tuple(rows)

    def history_orders_get(self, ticket=None):
        rows = list(self._orders.values())
        if ticket is not None:
            rows = [o for o in rows if o.ticket == ticket]
        return tuple(rows)

    def history_deals_get(self, ticket=None):
        # Firma REAL de la API MT5: `ticket` = ticket de ORDEN (DEAL_ORDER).
        rows = list(self._deals.values())
        if ticket is not None:
            rows = [d for d in rows if d.order == ticket]
        return tuple(rows)

    def positions_get(self, symbol=None, ticket=None):
        rows = list(self._positions.values())
        if symbol is not None:
            rows = [p for p in rows if p.symbol == symbol]
        return tuple(rows)


# --------------------------------------------------------------------------
# Fakes de gates
# --------------------------------------------------------------------------


class FakeSessions:
    def __init__(self, *, can_open=True, can_hold=True, state="OPEN"):
        self._can_open = can_open
        self._can_hold = can_hold
        self._state = state
        self.calls = 0

    def get_session(self, instrument, timestamp):
        self.calls += 1
        return SimpleNamespace(
            state=SimpleNamespace(value=self._state),
            can_open_positions=self._can_open,
            can_hold_positions=self._can_hold,
            reason="fake",
        )


class FakeKillSwitch:
    def __init__(self, *, can_trade=True, state="ALLOW"):
        self._can_trade = can_trade
        self._state = state
        self.calls = 0

    def status(self):
        self.calls += 1
        return SimpleNamespace(
            can_trade=self._can_trade, state=SimpleNamespace(value=self._state)
        )


class Recorder:
    def __init__(self, log, label):
        self._log = log
        self._label = label

    def __call__(self, *a, **k):
        self._log.append(self._label)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _spec(quote="USD"):
    return InstrumentSpec("EURUSD", contract_size=100000.0, quote_currency=quote)


def _fake() -> FakeMT5:
    fake = FakeMT5()
    fake.add_symbol(INSTRUMENT)
    fake.add_tick(INSTRUMENT, bid=1.0999, ask=1.1001)
    return fake


def _adapter(fake: FakeMT5) -> MT5BrokerAdapter:
    cfg = MT5Config(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={INSTRUMENT: INSTRUMENT},
    )
    creds = MT5Credentials(login=42, password=PASSWORD_CANARY, server="demo")
    adapter = MT5BrokerAdapter(cfg, creds, mt5=fake, clock=lambda: NOW)
    adapter.connect()
    return adapter


def _ref(instrument=INSTRUMENT, interval=3600, open_time=OPEN_TIME, close_time=CLOSE_TIME):
    return MarketDataReference(instrument, interval, open_time, close_time)


def _coord(
    *,
    fake=None,
    journal,
    sessions=None,
    kill_switch=None,
    account_mode=0,
    open_positions=0,
    market_ref=None,
    projected=None,
    quote="USD",
    account_currency="USD",
    instrument=INSTRUMENT,
    adapter=None,
    spec=None,
):
    fake = fake or _fake()
    adapter = adapter or _adapter(fake)
    sessions = sessions or FakeSessions()
    kill_switch = kill_switch or FakeKillSwitch()
    risk = lambda: AccountRiskState(equity=10_000.0, open_positions=open_positions)
    ref = market_ref if market_ref is not None else _ref()
    coord = LiveExecutionCoordinator(
        broker=adapter,
        order_manager=OrderManager(),
        journal=journal,
        sessions=sessions,
        kill_switch=kill_switch,
        instrument=instrument,
        instrument_spec=spec or _spec(quote=quote),
        account_id="MT5-DEMO-1",
        account_currency=account_currency,
        max_open_positions=3,
        risk_state_provider=risk,
        market_data_provider=lambda: ref,
        data_max_age_s=120.0,
        account_mode_provider=lambda: account_mode,
        projected_exposure_gate=projected,
        clock=lambda: NOW,
        run_id="TEST",
    )
    return coord, adapter, fake


def _submit(coord, cid="C-1", *, side=OrderSide.BUY, quantity=0.1, is_entry=True,
            reduce_only=False):
    return coord.submit(
        side=side, quantity=quantity, strategy_id="s", strategy_version="1.0.0",
        client_order_id=cid, order_type=OrderType.MARKET, is_entry=is_entry,
        reduce_only=reduce_only,
    )


# --------------------------------------------------------------------------
# 1. Journal obligatorio
# --------------------------------------------------------------------------


class TestJournalMandatory:
    def test_coordinator_requires_journal(self, tmp_path):
        fake = _fake()
        adapter = _adapter(fake)
        with pytest.raises(TypeError):
            LiveExecutionCoordinator(
                broker=adapter, order_manager=OrderManager(), journal=None,
                sessions=FakeSessions(), kill_switch=FakeKillSwitch(),
                instrument=INSTRUMENT, instrument_spec=_spec(), account_id="A",
                account_currency="USD", max_open_positions=3,
                risk_state_provider=lambda: AccountRiskState(equity=1.0),
                market_data_provider=lambda: _ref(), data_max_age_s=120.0,
                account_mode_provider=lambda: 0,
            )

    def test_uninitialized_journal_refuses(self, tmp_path):
        with pytest.raises(IntentJournalNotInitializedError):
            IntentJournal(tmp_path / "nope.json")

    def test_no_send_without_gate_authorization(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        # El adapter quedo gate-bound por el coordinador: un submit directo de un
        # cid NO autorizado debe fallar y no tocar el broker.
        order = Order(
            strategy_id="s", strategy_version="1", account_id="A",
            instrument=INSTRUMENT, side=OrderSide.BUY, quantity=0.1,
            order_type=OrderType.MARKET, client_order_id="UNAPPROVED",
        )
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(order)
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 2. Write-ahead falla => no submit
# --------------------------------------------------------------------------


class TestWriteAhead:
    def test_write_ahead_failure_blocks_send(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)

        def _boom(intent):
            raise IntentJournalError("fallo de persistencia simulado")

        monkeypatch.setattr(journal, "begin", _boom)
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "C-WA")
        assert ei.value.stage == "journal_persist"
        assert fake.send_count == 0
        assert journal.get("C-WA") is None


# --------------------------------------------------------------------------
# 3. Todos los gates antes del envio
# --------------------------------------------------------------------------


class TestAllGatesBeforeSend:
    def test_gate_order_precedes_send(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        log: list[str] = []
        sessions = FakeSessions()
        kill = FakeKillSwitch()
        projected = Recorder(log, "projected")
        coord, adapter, fake = _coord(
            journal=journal, sessions=sessions, kill_switch=kill, projected=projected
        )
        real_send = fake.order_send

        def spy_send(request):
            log.append("SEND")
            return real_send(request)

        fake.order_send = spy_send
        out = _submit(coord, "C-G")
        assert out.allowed is True and out.submission.status is SubmitStatus.CONFIRMED
        assert "SEND" in log
        assert log.index("projected") < log.index("SEND")
        assert sessions.calls >= 1 and kill.calls >= 1
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# 4. Recuperacion tras reinicio conserva identity
# --------------------------------------------------------------------------


class TestRestartRecovery:
    def test_restart_keeps_client_order_id_and_no_resubmit(self, tmp_path):
        path = tmp_path / "j.json"
        fake = _fake()
        journal = IntentJournal.initialize(path)
        coord, adapter, _ = _coord(fake=fake, journal=journal)
        out = _submit(coord, "C-REC")
        ticket = int(out.submission.broker_order.broker_order_id)
        sends_after_first = fake.send_count

        # "Reinicio": nuevo adapter + nuevo OrderManager sobre el MISMO terminal
        # (la verdad vive en el broker/fake), journal recargado de disco.
        adapter2 = _adapter(fake)
        journal2 = IntentJournal(path)
        coord2, _, _ = _coord(fake=fake, journal=journal2, adapter=adapter2)
        primed = coord2.prime_identity_from_journal()
        assert primed >= 1

        resolved = adapter2.get_order("C-REC")
        assert resolved is not None
        assert resolved.client_order_id == "C-REC"
        assert int(resolved.broker_order_id) == ticket
        assert fake.send_count == sends_after_first  # sin reenvio


# --------------------------------------------------------------------------
# 5. Comentario truncado / colision => indeterminado, sin reenvio
# --------------------------------------------------------------------------


class TestAmbiguityNoResubmit:
    def test_collision_is_indeterminate_and_no_resend(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        cid = "C-" + "X" * 40  # comentario "atlas:<cid>" truncado a 31
        magic = adapter._magic_for(cid)
        comment = adapter._comment_for(cid)
        # Dos ordenes historicas con MISMA magic+comment (colision) => ambiguedad.
        for ticket in (700, 701):
            fake._orders[ticket] = SimpleNamespace(
                ticket=ticket, symbol=INSTRUMENT, magic=magic, comment=comment,
                type=ORDER_TYPE_BUY, state=ORDER_STATE_FILLED,
                volume_initial=0.1, volume_current=0.0, price_open=1.1,
            )
        order = adapter.get_order(cid)
        assert order is not None
        assert order.state.value == "UNKNOWN"  # indeterminado, no inventa identidad
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 6. UNKNOWN / timeout / desconexion => sin reenvio
# --------------------------------------------------------------------------


class TestUnknownNoResubmit:
    def test_unknown_marks_and_blocks_next(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.send_mode = "none"  # respuesta perdida
        out = _submit(coord, "C-UNK")
        assert out.allowed is True
        assert out.submission.status is SubmitStatus.UNKNOWN
        assert journal.get("C-UNK").state == "UNKNOWN"
        assert fake.send_count == 1

        # Un intent previo sin resolver bloquea el siguiente envio.
        with pytest.raises(UnresolvedIntentsError):
            _submit(coord, "C-NEXT")
        assert fake.send_count == 1

        # Lookup no reenvia (NOT_FOUND / LOOKUP_UNCERTAIN permanecen pendientes).
        outcomes = coord.resolve_pending()
        assert all(r.result in ("NOT_FOUND", "LOOKUP_UNCERTAIN", "BROKER_UNKNOWN")
                   for r in outcomes)
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# 7. Reconciliacion bloqueada impide siguiente envio
# --------------------------------------------------------------------------


class TestReconciliationGate:
    def test_blocked_reconciliation_stops_send(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        coord.update_reconciliation(ReconciliationStatus.RECONCILIATION_BLOCKED)
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "C-RECON")
        assert ei.value.stage == "reconciliation"
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 8. Limite global de posiciones / exposicion proyectada
# --------------------------------------------------------------------------


class TestRiskGates:
    def test_position_limit_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, open_positions=3)
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "C-LIM")
        assert ei.value.stage == "position_limit"
        assert fake.send_count == 0

    def test_projected_gate_can_block(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")

        def gate(order):
            raise ExecutionBlockedError("projected_exposure", "max_gross")

        coord, adapter, fake = _coord(journal=journal, projected=gate)
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "C-PROJ")
        assert ei.value.stage == "projected_exposure"
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 9. Divisas incompatibles => fail-closed
# --------------------------------------------------------------------------


class TestCurrency:
    def test_quote_mismatch_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, quote="EUR", account_currency="USD")
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "C-FX")
        assert ei.value.stage == "currency"
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 10. HEDGING sin cierre seguro => ninguna apertura
# --------------------------------------------------------------------------


class TestAccountMode:
    def test_hedging_blocks_entry(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(
            journal=journal, account_mode=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        )
        with pytest.raises(UnsupportedAccountModeError):
            _submit(coord, "C-HEDGE", is_entry=True)
        assert fake.send_count == 0

    def test_unverified_mode_blocks_entry(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, account_mode=None)
        with pytest.raises(UnsupportedAccountModeError):
            _submit(coord, "C-NOMODE")
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 11. Camino feliz
# --------------------------------------------------------------------------


class TestHappyPath:
    def test_normal_submit_confirmed_and_journal(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        out = _submit(coord, "C-OK")
        assert out.allowed is True
        assert out.submission.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1
        assert fake.check_count == 1
        intent = journal.get("C-OK")
        assert intent is not None
        assert intent.broker_order_id == out.submission.broker_order.broker_order_id


# --------------------------------------------------------------------------
# 12. H1: bind_execution_gate seguro (una sola asignacion, sin desenlace)
# --------------------------------------------------------------------------


def _new_order(
    client_order_id: str,
    *,
    quantity: float = 0.1,
    order_type: OrderType = OrderType.MARKET,
) -> Order:
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="A",
        instrument=INSTRUMENT, side=OrderSide.BUY, quantity=quantity,
        order_type=order_type, client_order_id=client_order_id,
    )


class TestGateBindingHardening:
    def test_bind_rejects_none(self):
        adapter = _adapter(_fake())
        assert adapter._execution_gate is None
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(None)
        assert adapter._execution_gate is None

    def test_bind_rejects_object_without_check(self):
        adapter = _adapter(_fake())
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(object())
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(SimpleNamespace(check=None))
        assert adapter._execution_gate is None

    def test_second_bind_fails_without_replacing(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, _ = _coord(journal=journal)
        first = adapter._execution_gate
        assert first is not None
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(ExecutionGate("otro-token"))
        assert adapter._execution_gate is first
        assert coord._gate is first

    def test_failed_rebind_keeps_gate_enforced(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        _, adapter, fake = _coord(journal=journal)
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(None)
        with pytest.raises(BrokerError):
            adapter.bind_execution_gate(object())
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(_new_order("UNAPPROVED"))
        assert fake.send_count == 0

    def test_legacy_adapter_without_gate_still_sends(self):
        fake = _fake()
        adapter = _adapter(fake)
        assert adapter._execution_gate is None  # modo LIVE-09 heredado
        out = adapter.submit_order(_new_order("LEGACY-1"))
        assert out.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1

    def test_coordinator_is_authorized_route(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        assert adapter._execution_gate is not None  # gate enlazado por el coord
        out = _submit(coord, "C-AUTH")
        assert out.allowed is True
        assert out.submission.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1

    def test_ambiguous_result_consumes_permission(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        gate = adapter._execution_gate
        fake.send_mode = "ambiguous"  # respuesta incierta => UNKNOWN
        out = _submit(coord, "C-AMB")
        assert out.submission.status is SubmitStatus.UNKNOWN
        assert gate.approved_count == 0  # permiso consumido
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(_new_order("C-AMB"))
        assert fake.send_count == 1

    def test_exception_after_approval_consumes_permission(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        gate = adapter._execution_gate
        # LIMIT pasa el gate y falla DENTRO de submit_order => excepcion.
        with pytest.raises(BrokerError):
            coord.submit(
                side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                strategy_version="1.0.0", client_order_id="C-EXC",
                order_type=OrderType.LIMIT,
            )
        assert gate.approved_count == 0  # permiso consumido pese a la excepcion
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(_new_order("C-EXC"))
        assert fake.send_count == 0


# ==========================================================================
# 13. MT5-DEMO-EXECUTION-CONTROLS-02A: cancelacion coordinada (FakeMT5)
# ==========================================================================


def _seed_open_intent(
    journal,
    cid: str,
    *,
    instrument: str = INSTRUMENT,
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: float = 0.1,
    reduce_only: bool = False,
) -> OrderIntent:
    return journal.begin(
        OrderIntent(
            client_order_id=cid,
            order_id=f"ORD-{cid}",
            instrument=instrument,
            side=side.value,
            order_type=order_type.value,
            quantity=quantity,
            reduce_only=reduce_only,
        )
    )


def _plant_pending(adapter, fake, cid: str, ticket: int, *, volume: float = 0.1,
                   price: float = 1.1, filled: float | None = None) -> int:
    """Planta una orden PENDIENTE en el broker fake y correlaciona cid<->ticket."""
    fake.add_open_order(
        ticket, symbol=INSTRUMENT, magic=adapter._magic_for(cid),
        comment=adapter._comment_for(cid), volume=volume, price=price,
    )
    if filled is not None:
        fake.add_deal(ticket + 10_000, ticket, volume=filled, price=price)
    adapter._remember(cid, ticket)
    return ticket


def _cancel(coord, cid: str):
    return coord.cancel_pending(client_order_id=cid)


class TestCoordinatedCancel:
    def test_cancel_confirmed_and_idempotent(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-1")
        _plant_pending(adapter, fake, "CANC-1", 500)

        out = _cancel(coord, "CANC-1")
        assert out.allowed is True and out.stage == "cancel_confirmed"
        assert fake.cancel_count == 1 and fake.send_count == 0

        # journal: cancelacion resuelta y objetivo resuelto
        ci = journal.get("CANCEL:CANC-1")
        assert ci is not None and ci.operation == OPERATION_CANCEL
        assert ci.state == "CANCELLED"
        assert ci.target_client_order_id == "CANC-1"
        assert ci.target_broker_order_id == "500"
        assert journal.get("CANC-1").state == "CANCELLED"
        assert adapter.get_order("CANC-1").state is BrokerOrderState.CANCELLED

        # idempotencia: repetir la solicitud resuelta NO genera otra cancelacion
        out2 = _cancel(coord, "CANC-1")
        assert out2.allowed is True and out2.stage == "cancel_idempotent"
        assert fake.cancel_count == 1

    def test_cancel_partial_preserves_fills(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-P", quantity=0.5)
        _plant_pending(adapter, fake, "CANC-P", 501, volume=0.5, filled=0.2)

        out = _cancel(coord, "CANC-P")
        assert out.allowed and fake.cancel_count == 1
        bro = adapter.get_order("CANC-P")
        assert bro.state is BrokerOrderState.CANCELLED
        # fills existentes PRESERVADOS; solo se cancela el remanente
        assert bro.filled_quantity == pytest.approx(0.2)
        assert len(bro.fills) == 1
        assert bro.remaining == pytest.approx(0.3, abs=1e-9)
        assert journal.get("CANC-P").state == "CANCELLED"

    def test_cancel_uninitialized_journal_zero_trading(self, tmp_path):
        fake = _fake()
        with pytest.raises(IntentJournalNotInitializedError):
            IntentJournal(tmp_path / "missing.json")
        assert fake.send_count == 0 and fake.cancel_count == 0

    def test_cancel_corrupt_journal_failclosed(self, tmp_path):
        path = tmp_path / "j.json"
        path.write_text("{ no-es-json", encoding="utf-8")
        (tmp_path / "j.json.init").write_text("{}", encoding="utf-8")
        with pytest.raises(IntentJournalError):
            IntentJournal(path)

    def test_cancel_write_ahead_failure_blocks(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-W")
        _plant_pending(adapter, fake, "CANC-W", 502)

        def boom(intent):
            raise IntentJournalError("fallo de persistencia simulado")

        monkeypatch.setattr(journal, "begin", boom)
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-W")
        assert ei.value.stage == "journal_persist"
        assert fake.cancel_count == 0  # NUNCA se llama al broker
        assert journal.get("CANCEL:CANC-W") is None

    def test_direct_cancel_requires_operation_id(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _plant_pending(adapter, fake, "CANC-G", 503)
        with pytest.raises(BrokerError):
            adapter.cancel_order("CANC-G")  # gate enlazado, sin operation_id
        assert fake.cancel_count == 0

    def test_direct_cancel_wrong_operation_id_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _plant_pending(adapter, fake, "CANC-G2", 513)
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.cancel_order("CANC-G2", operation_id="CANCEL:OTRO")
        assert fake.cancel_count == 0

    def test_open_permission_cannot_authorize_cancel(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-OP")
        _plant_pending(adapter, fake, "CANC-OP", 512)

        # un permiso OPEN para el target NO autoriza una cancelacion
        coord._gate.approve("CANC-OP", token=coord._token)
        # y un permiso CANCEL con objetivo INCORRECTO tampoco
        coord._gate.approve_operation(
            OPERATION_CANCEL, op_id="CANCEL:CANC-OP", target="OTRO",
            token=coord._token,
        )
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.cancel_order("CANC-OP", operation_id="CANCEL:CANC-OP")
        assert fake.cancel_count == 0
        # el coordinador (permiso correcto) si puede cancelar
        out = _cancel(coord, "CANC-OP")
        assert out.allowed and fake.cancel_count == 1

    def test_consumed_permission_cannot_be_reused(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-5")
        _plant_pending(adapter, fake, "CANC-5", 505)
        assert _cancel(coord, "CANC-5").allowed
        # el permiso CANCEL correcto fue consumido: un uso posterior no autoriza
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.cancel_order("CANC-5", operation_id="CANCEL:CANC-5")
        assert fake.cancel_count == 1

    def test_cancel_ambiguous_unknown_no_auto_retry(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-A")
        _plant_pending(adapter, fake, "CANC-A", 504)
        fake.remove_mode = "ambiguous"  # resultado incierto
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-A")
        assert ei.value.stage == "cancel_unknown"
        assert fake.cancel_count == 1
        assert journal.get("CANCEL:CANC-A").state == "UNKNOWN"
        # repetir: sin resolver => NO se repite a ciegas
        with pytest.raises(ExecutionBlockedError) as ei2:
            _cancel(coord, "CANC-A")
        assert ei2.value.stage == "cancel_pending"
        assert fake.cancel_count == 1

    def test_cancel_response_lost_unknown(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-N")
        _plant_pending(adapter, fake, "CANC-N", 514)
        fake.remove_mode = "none"  # respuesta perdida
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-N")
        assert ei.value.stage == "cancel_unknown"
        assert journal.get("CANCEL:CANC-N").state == "UNKNOWN"
        assert fake.cancel_count == 1

    def test_cancel_rejected(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-RJ")
        _plant_pending(adapter, fake, "CANC-RJ", 515)
        fake.remove_mode = "reject"
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-RJ")
        assert ei.value.stage == "cancel_rejected"
        assert journal.get("CANCEL:CANC-RJ").state == "REJECTED"

    def test_cancel_target_already_cancelled_idempotent(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-AC")
        fake._orders[516] = SimpleNamespace(
            ticket=516, symbol=INSTRUMENT, magic=adapter._magic_for("CANC-AC"),
            comment=adapter._comment_for("CANC-AC"), type=ORDER_TYPE_BUY,
            state=ORDER_STATE_CANCELED, volume_initial=0.1, volume_current=0.0,
            price_open=1.1,
        )
        adapter._remember("CANC-AC", 516)
        out = _cancel(coord, "CANC-AC")
        assert out.allowed and out.stage == "cancel_already_done"
        assert fake.cancel_count == 0  # ya cancelada: sin segunda llamada
        assert journal.get("CANC-AC").state == "CANCELLED"

    def test_cancel_filled_target_blocked_preserves_fills(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-F")
        fake._orders[517] = SimpleNamespace(
            ticket=517, symbol=INSTRUMENT, magic=adapter._magic_for("CANC-F"),
            comment=adapter._comment_for("CANC-F"), type=ORDER_TYPE_BUY,
            state=ORDER_STATE_FILLED, volume_initial=0.1, volume_current=0.0,
            price_open=1.1,
        )
        fake.add_deal(5170, 517, volume=0.1, price=1.1)
        adapter._remember("CANC-F", 517)
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-F")
        assert ei.value.stage == "cancel_target_filled"
        assert fake.cancel_count == 0
        # no se afirma cancelacion; fills preservados en el broker
        assert len(adapter.get_order("CANC-F").fills) == 1

    def test_kill_switch_does_not_block_cancel_but_blocks_open(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        kill = FakeKillSwitch(can_trade=False, state="BLOCK_NEW_ORDERS")
        coord, adapter, fake = _coord(journal=journal, kill_switch=kill)
        _seed_open_intent(journal, "CANC-K")
        _plant_pending(adapter, fake, "CANC-K", 506)
        out = _cancel(coord, "CANC-K")
        assert out.allowed and fake.cancel_count == 1
        # el veto de nuevas entradas SI bloquea un OPEN
        with pytest.raises(ExecutionBlockedError) as ei:
            _submit(coord, "OPEN-K")
        assert ei.value.stage == "kill_switch"

    def test_reconciliation_blocked_allows_verified_cancel(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-RB")
        _plant_pending(adapter, fake, "CANC-RB", 507)
        coord.update_reconciliation(ReconciliationStatus.RECONCILIATION_BLOCKED)
        out = _cancel(coord, "CANC-RB")
        assert out.allowed and fake.cancel_count == 1

    def test_unhealthy_broker_blocks_cancel(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-UH")
        _plant_pending(adapter, fake, "CANC-UH", 508)
        adapter._state = "DISCONNECTED"  # broker no sano
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "CANC-UH")
        assert ei.value.stage == "broker_health"
        assert fake.cancel_count == 0

    def test_unresolved_open_cancellable_new_opens_stay_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-UN")
        journal.mark_state("CANC-UN", "UNKNOWN")
        _seed_open_intent(journal, "OTHER-UN")
        journal.mark_state("OTHER-UN", "UNKNOWN")
        _plant_pending(adapter, fake, "CANC-UN", 509)
        out = _cancel(coord, "CANC-UN")
        assert out.allowed and fake.cancel_count == 1
        assert journal.get("CANC-UN").state == "CANCELLED"
        # nuevas aperturas siguen bloqueadas por el intent restante sin resolver
        with pytest.raises(UnresolvedIntentsError):
            _submit(coord, "NEW-OPEN")

    def test_cancel_reduce_order_out_of_scope_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "EXIT:X", reduce_only=True)
        _plant_pending(adapter, fake, "EXIT:X", 510)
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "EXIT:X")
        assert ei.value.stage == "cancel_target"
        assert fake.cancel_count == 0

    def test_cancel_without_open_intent_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        _plant_pending(adapter, fake, "NO-INTENT", 511)
        with pytest.raises(ExecutionBlockedError) as ei:
            _cancel(coord, "NO-INTENT")
        assert ei.value.stage == "cancel_target"
        assert fake.cancel_count == 0

    def test_restart_preserves_cancel_identity(self, tmp_path):
        path = tmp_path / "j.json"
        journal = IntentJournal.initialize(path)
        coord, adapter, fake = _coord(journal=journal)
        _seed_open_intent(journal, "CANC-R")
        _plant_pending(adapter, fake, "CANC-R", 505)
        assert _cancel(coord, "CANC-R").allowed

        # "Reinicio": nuevo adapter (mismo terminal fake) + journal recargado.
        adapter2 = _adapter(fake)
        journal2 = IntentJournal(path)
        intent = journal2.get("CANCEL:CANC-R")
        assert intent.operation == OPERATION_CANCEL
        assert intent.target_client_order_id == "CANC-R"
        assert intent.target_broker_order_id == "505"
        assert intent.state == "CANCELLED"
        coord2, _, _ = _coord(fake=fake, journal=journal2, adapter=adapter2)
        out = _cancel(coord2, "CANC-R")  # idempotente tras reinicio
        assert out.stage == "cancel_idempotent"
        assert fake.cancel_count == 1


class TestCoordinatedCancelRecovery:
    def _journal_with_cancel(self, tmp_path, target="T"):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(
            OrderIntent.for_cancel(
                operation_id=f"CANCEL:{target}", target_client_order_id=target,
                instrument=INSTRUMENT, side="BUY", order_type="MARKET",
                quantity=0.1,
            )
        )
        return journal

    def test_recover_not_found_stays_unresolved(self, tmp_path):
        journal = self._journal_with_cancel(tmp_path, "NF")
        adapter = _adapter(_fake())
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "NOT_FOUND"
        assert journal.get("CANCEL:NF").terminal is False

    def test_recover_lookup_uncertain_stays_unresolved(self, tmp_path, monkeypatch):
        journal = self._journal_with_cancel(tmp_path, "LU")
        adapter = _adapter(_fake())

        def boom(cid):
            raise BrokerConnectionError("transporte")

        monkeypatch.setattr(adapter, "get_order", boom)
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "LOOKUP_UNCERTAIN"
        assert journal.get("CANCEL:LU").terminal is False

    def test_recover_broker_unknown_collision_stays_unresolved(self, tmp_path):
        journal = self._journal_with_cancel(tmp_path, "COL")
        fake = _fake()
        adapter = _adapter(fake)
        magic = adapter._magic_for("COL")
        comment = adapter._comment_for("COL")
        for ticket in (700, 701):
            fake._orders[ticket] = SimpleNamespace(
                ticket=ticket, symbol=INSTRUMENT, magic=magic, comment=comment,
                type=ORDER_TYPE_BUY, state=ORDER_STATE_FILLED,
                volume_initial=0.1, volume_current=0.0, price_open=1.1,
            )
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "BROKER_UNKNOWN"
        assert journal.get("CANCEL:COL").terminal is False

    def test_recover_target_active_no_auto_repeat(self, tmp_path):
        journal = self._journal_with_cancel(tmp_path, "ACT")
        fake = _fake()
        adapter = _adapter(fake)
        _plant_pending(adapter, fake, "ACT", 710)
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "TARGET_ACTIVE"
        assert journal.get("CANCEL:ACT").terminal is False
        assert fake.cancel_count == 0  # lookup NUNCA cancela

    def test_recover_confirmed_cancelled_resolved(self, tmp_path):
        journal = self._journal_with_cancel(tmp_path, "RC")
        fake = _fake()
        adapter = _adapter(fake)
        fake._orders[711] = SimpleNamespace(
            ticket=711, symbol=INSTRUMENT, magic=adapter._magic_for("RC"),
            comment=adapter._comment_for("RC"), type=ORDER_TYPE_BUY,
            state=ORDER_STATE_CANCELED, volume_initial=0.1, volume_current=0.0,
            price_open=1.1,
        )
        adapter._remember("RC", 711)
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "RESOLVED_CANCELLED"
        assert journal.get("CANCEL:RC").state == "CANCELLED"
        assert fake.cancel_count == 0

    def test_recover_target_filled_no_false_cancel(self, tmp_path):
        journal = self._journal_with_cancel(tmp_path, "TF")
        fake = _fake()
        adapter = _adapter(fake)
        fake._orders[712] = SimpleNamespace(
            ticket=712, symbol=INSTRUMENT, magic=adapter._magic_for("TF"),
            comment=adapter._comment_for("TF"), type=ORDER_TYPE_BUY,
            state=ORDER_STATE_FILLED, volume_initial=0.1, volume_current=0.0,
            price_open=1.1,
        )
        fake.add_deal(7120, 712, volume=0.1, price=1.1)
        adapter._remember("TF", 712)
        outcomes = recover_pending_cancels(journal, adapter)
        assert outcomes[0].result == "TARGET_FILLED"
        assert journal.get("CANCEL:TF").state == "REJECTED"  # no se afirma cancelacion
        assert len(adapter.get_order("TF").fills) == 1


class TestLegacyCancelCompatibility:
    def test_legacy_cancel_without_gate_still_works(self):
        fake = _fake()
        adapter = _adapter(fake)
        assert adapter._execution_gate is None  # modo LIVE-09 heredado
        fake.add_open_order(
            520, symbol=INSTRUMENT, magic=adapter._magic_for("LEG-C"),
            comment=adapter._comment_for("LEG-C"), volume=0.1, price=1.1,
        )
        adapter._remember("LEG-C", 520)
        out = adapter.cancel_order("LEG-C")
        assert out.state is BrokerOrderState.CANCELLED
        assert fake.cancel_count == 1


# ==========================================================================
# 14. MT5-DEMO-EXECUTION-CONTROLS-02B: CLOSE/REDUCE coordinados (FakeMT5)
# ==========================================================================


def _spec_stepged(step=0.01, lo=0.01, hi=100.0):
    return InstrumentSpec(
        "EURUSD", contract_size=100000.0, quote_currency="USD",
        quantity_min=lo, quantity_step=step, quantity_max=hi,
    )


def _long_order(cid, *, reduce_only=True, purpose=PURPOSE_REDUCE, side=OrderSide.SELL,
                qty=0.1):
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="A",
        instrument=INSTRUMENT, side=side, quantity=qty, order_type=OrderType.MARKET,
        client_order_id=cid, reduce_only=reduce_only, purpose=purpose,
    )


class TestCloseReduce:
    def test_close_total_confirmed_netting_long(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(600, volume=0.5, price_open=1.1, type=ORDER_TYPE_BUY)
        out = coord.close_position()
        assert out.allowed is True and out.stage == "closed"
        assert fake.send_count == 1 and fake.cancel_count == 0
        req = fake.last_request
        assert req["action"] == TRADE_ACTION_DEAL
        assert req["position"] == 600  # no-inversion: cierra la posicion exacta
        assert req["type"] == ORDER_TYPE_SELL
        assert req["volume"] == pytest.approx(0.5)
        ci = journal.get("CLOSE:EURUSD")
        assert ci.operation == OPERATION_CLOSE
        assert ci.reduce_only is True
        assert ci.available_quantity == pytest.approx(0.5)
        # idempotente: repetir no envia de nuevo
        assert coord.close_position().stage == "reduce_idempotent"
        assert fake.send_count == 1

    def test_close_short_uses_buy(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(601, volume=0.3, price_open=1.1, type=ORDER_TYPE_SELL)
        out = coord.close_position()
        assert out.allowed and fake.send_count == 1
        assert fake.last_request["type"] == ORDER_TYPE_BUY

    def test_reduce_partial_confirmed(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(602, volume=0.5, price_open=1.1, type=ORDER_TYPE_BUY)
        out = coord.reduce_position(quantity=0.2)
        assert out.allowed and out.stage == "reduced"
        assert fake.last_request["volume"] == pytest.approx(0.2)
        assert fake.last_request["position"] == 602
        ri = journal.get("REDUCE:EURUSD:0.2")
        assert ri.operation == OPERATION_REDUCE
        assert ri.quantity == pytest.approx(0.2)
        assert ri.available_quantity == pytest.approx(0.5)

    def test_reduce_exceeds_exposure_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(603, volume=0.1, price_open=1.1, type=ORDER_TYPE_BUY)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.reduce_position(quantity=0.5)
        assert ei.value.stage == "quantity"
        assert fake.send_count == 0

    def test_reduce_zero_negative_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(604, volume=0.5, price_open=1.1, type=ORDER_TYPE_BUY)
        for bad in (0, -0.1):
            with pytest.raises(ExecutionBlockedError) as ei:
                coord.reduce_position(quantity=bad)
            assert ei.value.stage == "quantity"
        assert fake.send_count == 0

    def test_reduce_step_mismatch_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, spec=_spec_stepged(step=0.01))
        fake.add_position(605, volume=0.5, price_open=1.1, type=ORDER_TYPE_BUY)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.reduce_position(quantity=0.015)
        assert ei.value.stage == "quantity"
        assert fake.send_count == 0
        # cantidad alineada SI reduce
        assert coord.reduce_position(quantity=0.02).allowed
        assert fake.send_count == 1

    def test_close_without_position_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "position_evidence"
        assert fake.send_count == 0

    def test_ambiguous_positions_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(606, volume=0.2, type=ORDER_TYPE_BUY)
        fake.add_position(607, volume=0.3, type=ORDER_TYPE_BUY)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "position_evidence"
        assert fake.send_count == 0

    def test_hedging_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, account_mode=2)
        fake.add_position(608, volume=0.5, type=ORDER_TYPE_BUY)
        with pytest.raises(UnsupportedAccountModeError):
            coord.close_position()
        assert fake.send_count == 0

    def test_unknown_account_mode_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal, account_mode=None)
        fake.add_position(609, volume=0.5, type=ORDER_TYPE_BUY)
        with pytest.raises(UnsupportedAccountModeError):
            coord.close_position()
        assert fake.send_count == 0

    def test_journal_write_ahead_failure_blocks(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(610, volume=0.5, type=ORDER_TYPE_BUY)

        def boom(intent):
            raise IntentJournalError("fallo de persistencia simulado")

        monkeypatch.setattr(journal, "begin", boom)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "journal_persist"
        assert fake.send_count == 0
        assert journal.get("CLOSE:EURUSD") is None

    def test_kill_switch_matrix(self, tmp_path):
        # ALLOW permite
        for state, can_trade, allowed in (
            ("ALLOW", True, True),
            ("BLOCK_NEW_ORDERS", False, True),
            ("HALT", False, False),
            ("EMERGENCY", False, False),
        ):
            journal = IntentJournal.initialize(tmp_path / f"j-{state}.json")
            kill = FakeKillSwitch(can_trade=can_trade, state=state)
            coord, adapter, fake = _coord(journal=journal, kill_switch=kill)
            fake.add_position(620, volume=0.5, type=ORDER_TYPE_BUY)
            if allowed:
                assert coord.close_position().allowed
                assert fake.send_count == 1
            else:
                with pytest.raises(ExecutionBlockedError) as ei:
                    coord.close_position()
                assert ei.value.stage == "kill_switch"
                assert fake.send_count == 0

    def test_session_closed_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        sessions = FakeSessions(can_open=False, can_hold=True, state="CLOSED")
        coord, adapter, fake = _coord(journal=journal, sessions=sessions)
        fake.add_position(621, volume=0.5, type=ORDER_TYPE_BUY)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "session"
        assert fake.send_count == 0

    def test_reconciliation_blocked_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(622, volume=0.5, type=ORDER_TYPE_BUY)
        coord.update_reconciliation(ReconciliationStatus.RECONCILIATION_BLOCKED)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "reconciliation"
        assert fake.send_count == 0

    def test_stale_market_data_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        stale = MarketDataReference(INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME - 10_000)
        coord, adapter, fake = _coord(journal=journal, market_ref=stale)
        fake.add_position(623, volume=0.5, type=ORDER_TYPE_BUY)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "market_data"
        assert fake.send_count == 0

    def test_unhealthy_broker_blocks(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(624, volume=0.5, type=ORDER_TYPE_BUY)
        adapter._state = "DISCONNECTED"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()
        assert ei.value.stage == "broker_health"
        assert fake.send_count == 0

    def test_gate_open_permission_cannot_authorize_close(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(625, volume=0.5, type=ORDER_TYPE_BUY)
        coord._gate.approve("CLOSE-ADV", token=coord._token)  # permiso OPEN
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(_long_order("CLOSE-ADV", purpose=PURPOSE_CLOSE))
        assert fake.send_count == 0
        # el coordinador (permiso CLOSE correcto) si cierra
        assert coord.close_position().allowed
        assert fake.send_count == 1

    def test_gate_wrong_target_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(626, volume=0.5, type=ORDER_TYPE_BUY)
        coord._gate.approve_operation(
            OPERATION_REDUCE, op_id="R-ADV", target="OTRO", token=coord._token
        )
        with pytest.raises(ExecutionNotAuthorizedError):
            adapter.submit_order(
                _long_order("R-ADV", purpose=PURPOSE_REDUCE)
            )
        assert fake.send_count == 0

    def test_adapter_rejects_inverting_reduce(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(627, volume=0.5, type=ORDER_TYPE_BUY)
        # Mismo sentido que la posicion (BUY sobre long): invertiria.
        coord._gate.approve_operation(
            OPERATION_REDUCE, op_id="R-INV", target=INSTRUMENT, token=coord._token
        )
        with pytest.raises(BrokerError):
            adapter.submit_order(
                _long_order("R-INV", purpose=PURPOSE_REDUCE, side=OrderSide.BUY)
            )
        assert fake.send_count == 0

    def test_reduce_id_reuse_with_other_quantity_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(628, volume=0.5, type=ORDER_TYPE_BUY)
        assert coord.reduce_position(quantity=0.2, operation_id="R-1").allowed
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.reduce_position(quantity=0.3, operation_id="R-1")
        assert ei.value.stage == "identity"

    def test_reduce_unknown_then_lookup_resolves(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        coord, adapter, fake = _coord(journal=journal)
        fake.add_position(629, volume=0.5, type=ORDER_TYPE_BUY)
        fake.send_mode = "none"  # respuesta perdida => UNKNOWN
        out = coord.reduce_position(quantity=0.2, operation_id="R-UNK")
        assert out.submission.status is SubmitStatus.UNKNOWN
        assert journal.get("R-UNK").state == "UNKNOWN"
        # repetir: sin resolver => no repite
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.reduce_position(quantity=0.2, operation_id="R-UNK")
        assert ei.value.stage == "reduce_pending"
        send_after = fake.send_count
        # lookup: el broker SI conoce la orden (se planta el estado real)
        fake._orders[900] = SimpleNamespace(
            ticket=900, symbol=INSTRUMENT, magic=adapter._magic_for("R-UNK"),
            comment=adapter._comment_for("R-UNK"), type=ORDER_TYPE_SELL,
            state=ORDER_STATE_FILLED, volume_initial=0.2, volume_current=0.0,
            price_open=1.1,
        )
        fake.add_deal(9001, 900, volume=0.2, price=1.1)
        outcomes = coord.resolve_pending()
        assert any(o.client_order_id == "R-UNK" for o in outcomes)
        assert journal.get("R-UNK").state == "FILLED"  # adoptado del broker
        assert fake.send_count == send_after  # lookup NO reenvia
        # fills registrados una sola vez (idempotente por (cid, seq))
        assert journal.get("R-UNK").fills_applied == (9001,)
        coord.resolve_pending()
        assert journal.get("R-UNK").fills_applied == (9001,)

    def test_reduce_recovery_not_found_stays_pending(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(
            OrderIntent.for_reduce(
                operation_id="R-NF", instrument=INSTRUMENT, side="SELL",
                quantity=0.1, available_quantity=0.5,
            )
        )
        adapter = _adapter(_fake())
        outcomes = recover_pending_intents(journal, adapter)
        assert outcomes[0].result == "NOT_FOUND"
        assert journal.get("R-NF").terminal is False
