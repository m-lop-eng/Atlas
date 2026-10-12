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
    IntentJournal,
    IntentJournalError,
    IntentJournalNotInitializedError,
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
from execution.order import Order, OrderSide, OrderType
from execution.order_manager import OrderManager
from risk.types import AccountRiskState

from brokers.models import BrokerError, SubmitStatus
from brokers.mt5 import (
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
    ORDER_STATE_FILLED,
    ORDER_TYPE_BUY,
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
        self.send_count = 0
        self.check_count = 0
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
        self.send_count += 1
        if request.get("action") == TRADE_ACTION_REMOVE:
            return SimpleNamespace(retcode=10009, order=0, deal=0, volume=0.0, price=0.0)
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

    def history_deals_get(self, order=None):
        rows = list(self._deals.values())
        if order is not None:
            rows = [d for d in rows if d.order == order]
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
        instrument_spec=_spec(quote=quote),
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
