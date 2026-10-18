"""Tests MT5-DEMO-LIVE-READINESS-03A: runtime coordinado, fills y reconciliacion.

Exclusivamente FakeMT5/FakeBroker: sin terminal real, sin credenciales, sin
ordenes reales. Verifica ledger, cash, fills, journal, snapshots, kill switch y
llamadas al broker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_mt5_execution_foundation import (
    CLOSE_TIME,
    INSTRUMENT,
    NOW,
    OPEN_TIME,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    FakeMT5,
    FakeSessions,
    passing_admission,
)

from execution.instrument import InstrumentSpec
from execution.intent_journal import (
    OPERATION_CLOSE,
    OPERATION_OPEN,
    IntentJournal,
    OrderIntent,
)
from execution.ledger_applier import (
    ApplyOutcome,
    CommissionPolicy,
    LedgerApplier,
)
from execution.ledger_store import LedgerStore, LedgerStoreError
from execution.live_execution import ExecutionBlockedError, MarketDataReference
from execution.live_runtime import LiveExecutionRuntime, RuntimeNotReadyError
from execution.order import Order, OrderSide, OrderType
from execution.order_manager import OrderManager

from papertrading.ledger import AccountLedger
from killswitches.coordinator import KillSwitchCoordinator
from risk.types import AccountRiskState

from brokers.mt5 import (
    HISTORY_INCOMPLETE,
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
    ORDER_STATE_FILLED,
    ORDER_STATE_PLACED,
    OrderHistoryLookup,
    TRADE_ACTION_REMOVE,
)
from brokers.models import (
    BrokerConnectionError,
    BrokerFill,
    BrokerInvalidOrderError,
    BrokerOrder,
    BrokerOrderState,
)

UTC = timezone.utc
CONTRACT_SIZE = 100000.0
ASK = 1.1001
BID = 1.0999
NOTIONAL = 0.1 * ASK * CONTRACT_SIZE  # 11001.0
PASSWORD_CANARY = "not-a-real-password"


# --------------------------------------------------------------------------
# FakeMT5 con contabilidad (espeja el modelo del AccountLedger)
# --------------------------------------------------------------------------


class AccountingFakeMT5(FakeMT5):
    """FakeMT5 que refleja fills en balance/equity/posiciones (para reconciliar)."""

    def __init__(self) -> None:
        super().__init__()
        self._net: dict[str, list] = {}  # symbol -> [qty, avg]
        self._pos_ticket: dict[str, int] = {}
        self._mark = ASK
        self._next_pos = 5000

    def _cs(self, symbol: str) -> float:
        return float(getattr(self.symbols[symbol], "trade_contract_size", 1.0))

    def order_send(self, request):
        result = super().order_send(request)
        if request.get("action") == TRADE_ACTION_REMOVE:
            return result
        if self.send_mode != "ok" or result is None:
            return result
        if int(getattr(result, "retcode", -1)) != 10009:
            return result
        symbol = request["symbol"]
        volume = float(request["volume"])
        price = float(request["price"])
        signed = volume if int(request["type"]) == ORDER_TYPE_BUY else -volume
        self._account_fill(symbol, signed, price)
        return result

    def _account_fill(self, symbol: str, signed: float, price: float) -> None:
        cs = self._cs(symbol)
        qty, avg = self._net.get(symbol, [0.0, price])
        new = qty + signed
        self.account.balance -= signed * price * cs
        if abs(new) <= 1e-12:
            self._net.pop(symbol, None)
        elif qty == 0.0:
            self._net[symbol] = [new, price]
        elif (qty > 0) == (signed > 0):
            self._net[symbol] = [new, (qty * avg + signed * price) / new]
        else:
            self._net[symbol] = [new, avg]  # reduccion: conserva avg
        self._mark = price
        self.account.equity = self.account.balance + sum(
            q * self._mark * self._cs(s) for s, (q, _) in self._net.items()
        )

    def positions_get(self, symbol=None, ticket=None):
        rows = []
        for sym, (qty, avg) in self._net.items():
            if symbol is not None and sym != symbol:
                continue
            if abs(qty) <= 1e-12:
                continue
            if sym not in self._pos_ticket:
                self._next_pos += 1
                self._pos_ticket[sym] = self._next_pos
            rows.append(
                SimpleNamespace(
                    ticket=self._pos_ticket[sym], symbol=sym, volume=abs(qty),
                    price_open=avg, type=(ORDER_TYPE_BUY if qty > 0 else ORDER_TYPE_SELL),
                    time=1_700_000_000,
                )
            )
        return tuple(rows)


def _spec() -> InstrumentSpec:
    return InstrumentSpec(INSTRUMENT, contract_size=CONTRACT_SIZE, quote_currency="USD")


def _adapter(fake, *, symbol_map=None) -> MT5BrokerAdapter:
    cfg = MT5Config(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={INSTRUMENT: INSTRUMENT} if symbol_map is None else symbol_map,
    )
    creds = MT5Credentials(login=42, password=PASSWORD_CANARY, server="demo")
    adapter = MT5BrokerAdapter(cfg, creds, mt5=fake, clock=lambda: NOW)
    adapter.connect()
    return adapter


def _ledger() -> AccountLedger:
    led = AccountLedger(initial_cash=10_000.0, account_currency="USD")
    led.register_instrument(_spec())
    return led


def _runtime(tmp_path, *, fake=None, journal=None, ledger=None, account_mode=0,
             symbol_map=None, commission_policy=CommissionPolicy("USD"),
             ledger_store=None, adapter=None, kill_switch=None, maker=True):
    fake = fake or AccountingFakeMT5()
    if maker:
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
    adapter = adapter or _adapter(fake, symbol_map=symbol_map)
    journal = journal or IntentJournal.initialize(tmp_path / "j.json")
    ledger = ledger or _ledger()
    store = ledger_store or LedgerStore(tmp_path / "ledger.json")
    ks = kill_switch or KillSwitchCoordinator()
    rt = LiveExecutionRuntime(
        broker=adapter, order_manager=OrderManager(), journal=journal, ledger=ledger,
        ledger_store=store, sessions=FakeSessions(), kill_switch=ks,
        instrument=INSTRUMENT, instrument_spec=_spec(), account_id="DEMO-1",
        account_currency="USD", max_open_positions=3,
        risk_state_provider=lambda: AccountRiskState(equity=10_000.0, open_positions=0),
        market_data_provider=lambda: MarketDataReference(
            INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME
        ),
        data_max_age_s=120.0, account_mode_provider=lambda: account_mode,
        marks_provider=lambda: {INSTRUMENT: fake._mark},
        commission_policy=commission_policy, clock=lambda: NOW, run_id="RT",
        mt5_accounting=passing_admission(),
    )
    return rt, adapter, fake, ledger, journal, store


def _open_order(cid: str, *, side=OrderSide.BUY, quantity=0.1, purpose="OPEN") -> Order:
    from execution.order import PURPOSE_OPEN
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="DEMO-1",
        instrument=INSTRUMENT, side=side, quantity=quantity,
        order_type=OrderType.MARKET, client_order_id=cid,
        purpose=PURPOSE_OPEN if purpose == "OPEN" else purpose,
    )


# --------------------------------------------------------------------------
# 1-3. Arranque
# --------------------------------------------------------------------------


class TestStartup:
    def test_initialize_ready_flat(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rep = rt.initialize()
        assert rep.ready is True and rep.stage == "ready"
        assert rt.ready is True
        assert rep.reconciliation.status.value == "RECONCILIATION_OK"
        # secuencia: journal migrado + ledger persistido
        assert store.exists

    def test_initialize_rejects_missing_mapping(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, symbol_map={})
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "config"
        assert fake.send_count == 0

    def test_initialize_rejects_non_netting(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, account_mode=2)
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "account_mode"

    def test_initialize_uninitialized_journal_raises(self, tmp_path):
        fake = AccountingFakeMT5()
        with pytest.raises(Exception):
            IntentJournal(tmp_path / "never.json")  # sin create=True
        assert fake.send_count == 0

    def test_initialize_corrupt_journal_raises(self, tmp_path):
        p = tmp_path / "j.json"
        p.write_text("{ corrupto", encoding="utf-8")
        (tmp_path / "j.json.init").write_text("{}", encoding="utf-8")
        with pytest.raises(Exception):
            IntentJournal(p)

    def test_initialize_ambiguous_intent_not_ready(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent.from_order(_open_order("O-AMB")))
        rt, adapter, fake, ledger, journal2, store = _runtime(tmp_path, journal=journal)
        # El broker no conoce O-AMB => NOT_FOUND (ambiguo) => no preparado.
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "recovery"
        assert fake.send_count == 0

    def test_initialize_adopts_prior_broker_position_blocks(self, tmp_path):
        # Posicion previa en el broker que el ledger no conoce => BLOCKED.
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        fake._net[INSTRUMENT] = [0.5, ASK]
        fake.account.balance = 9_000.0
        fake.account.equity = 9_000.0 + 0.5 * ASK * CONTRACT_SIZE
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, fake=fake, maker=False)
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "reconciliation"


# --------------------------------------------------------------------------
# 4-7. Fills -> ledger (OPEN/CLOSE/REDUCE)
# --------------------------------------------------------------------------


class TestFillsLedger:
    def test_open_confirmed_fills_ledger(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        out = rt.submit(
            side=OrderSide.BUY, quantity=0.1, strategy_id="s",
            strategy_version="1.0.0", client_order_id="O-1",
        )
        assert out.allowed and fake.send_count == 1
        pos = ledger.position(INSTRUMENT)
        assert pos is not None and pos.quantity == pytest.approx(0.1)
        assert ledger.cash == pytest.approx(10_000.0 - NOTIONAL)
        assert journal.get("O-1").state == "FILLED"
        assert rt.reconcile_now().status.value == "RECONCILIATION_OK"

    def test_close_total_pnl_and_flat(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        out = rt.close_position()
        assert out.allowed
        assert ledger.position(INSTRUMENT) is None
        # P&L realizado = (bid - ask) * qty * cs (spread) => perdida pequena
        expected_pnl = (BID - ASK) * 0.1 * CONTRACT_SIZE
        assert ledger.realized_pnl == pytest.approx(expected_pnl, abs=1e-6)
        assert rt.reconcile_now().status.value == "RECONCILIATION_OK"

    def test_reduce_partial_residual(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.3, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        out = rt.reduce_position(quantity=0.1)
        assert out.allowed
        pos = ledger.position(INSTRUMENT)
        assert pos is not None and pos.quantity == pytest.approx(0.2)
        assert pos.avg_entry_price == pytest.approx(ASK)
        assert rt.reconcile_now().status.value == "RECONCILIATION_OK"

    def test_reduce_exceeding_exposure_blocked(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        with pytest.raises(ExecutionBlockedError):
            rt.reduce_position(quantity=0.5)
        assert ledger.position(INSTRUMENT).quantity == pytest.approx(0.1)


# --------------------------------------------------------------------------
# 11-13. Idempotencia, durabilidad, comisiones
# --------------------------------------------------------------------------


class TestIdempotencyDurability:
    def test_fill_redelivery_not_applied_twice(self, tmp_path):
        ledger = _ledger()
        applier = LedgerApplier(ledger=ledger, account_currency="USD",
                                commission_policy=CommissionPolicy("USD"))
        bro = BrokerOrder(
            client_order_id="O-1", broker_order_id="9", instrument=INSTRUMENT,
            side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=0.1,
            state=BrokerOrderState.FILLED, filled_quantity=0.1, avg_fill_price=ASK,
            fills=(BrokerFill("9", 0.1, ASK, NOW, 0.0, 0),),
        )
        first = applier.apply_broker_order("O-1", bro)
        second = applier.apply_broker_order("O-1", bro)
        assert first.changed and not second.changed
        assert ledger.cash == pytest.approx(10_000.0 - NOTIONAL)

    def test_durability_reconstruct_from_broker(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        expected_cash = ledger.cash
        # Simula fallo entre aplicar el fill y persistir: se borra el snapshot.
        store.path.unlink()
        # Nuevo runtime con ledger FRESCO, mismo journal y MISMO terminal fake
        # (nuevo adapter: el gate es de una sola asignacion por adapter).
        adapter2 = _adapter(fake)
        rt2 = LiveExecutionRuntime(
            broker=adapter2, order_manager=OrderManager(), journal=journal,
            ledger=_ledger(), ledger_store=store, sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
            instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
            data_max_age_s=120.0, account_mode_provider=lambda: 0,
            marks_provider=lambda: {INSTRUMENT: fake._mark},
            commission_policy=CommissionPolicy("USD"), clock=lambda: NOW, run_id="RT2",
            mt5_accounting=passing_admission(),
        )
        rep = rt2.initialize()
        assert rep.ready is True
        # El ledger se reconstruyo desde el broker exactamente una vez.
        assert rt2.ledger.position(INSTRUMENT).quantity == pytest.approx(0.1)
        assert rt2.ledger.cash == pytest.approx(expected_cash)
        # Segundo arranque: no re-aplica (idempotente).
        rt2.initialize()
        assert rt2.ledger.cash == pytest.approx(expected_cash)

    def test_commission_unverified_not_applied(self, tmp_path):
        ledger = _ledger()
        applier = LedgerApplier(
            ledger=ledger, account_currency="USD",
            commission_policy=CommissionPolicy(None),  # moneda no verificada
        )
        bro = BrokerOrder(
            client_order_id="O-1", broker_order_id="9", instrument=INSTRUMENT,
            side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=0.1,
            state=BrokerOrderState.FILLED, filled_quantity=0.1, avg_fill_price=ASK,
            fills=(BrokerFill("9", 0.1, ASK, NOW, 5.0, 0),),
        )
        out = applier.apply_broker_order("O-1", bro)
        assert out.unverified_commission == pytest.approx(5.0)
        assert out.applied_commission == 0.0
        # La comision (5.0) NO se resta: fail-safe, discrepancia visible.
        assert ledger.cash == pytest.approx(10_000.0 - NOTIONAL)

    def test_commission_wrong_currency_not_resolvable(self):
        pol = CommissionPolicy("EUR")
        assert pol.resolvable(account_currency="USD") is False

    def test_commission_applied_once_in_account_currency(self):
        ledger = _ledger()
        applier = LedgerApplier(ledger=ledger, account_currency="USD",
                                commission_policy=CommissionPolicy("USD"))
        bro = BrokerOrder(
            client_order_id="O-1", broker_order_id="9", instrument=INSTRUMENT,
            side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=0.1,
            state=BrokerOrderState.FILLED, filled_quantity=0.1, avg_fill_price=ASK,
            fills=(BrokerFill("9", 0.1, ASK, NOW, 5.0, 0),),
        )
        applier.apply_broker_order("O-1", bro)
        applier.apply_broker_order("O-1", bro)  # redelivery
        assert ledger.cash == pytest.approx(10_000.0 - NOTIONAL - 5.0)


# --------------------------------------------------------------------------
# 14-15. Reconciliacion pre/post bloquea
# --------------------------------------------------------------------------


class TestReconciliationGates:
    def test_post_account_divergence_blocks_next(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        # Divergencia inyectada en el broker.
        fake.account.balance += 100.0
        fake.account.equity += 100.0
        rep = rt.reconcile_now()
        assert rep.status.value != "RECONCILIATION_OK"
        with pytest.raises(RuntimeNotReadyError):
            rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                      strategy_version="1.0.0", client_order_id="O-2")
        assert fake.send_count == 1  # el segundo envio no ocurrio

    def test_cannot_operate_before_initialize(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        with pytest.raises(RuntimeNotReadyError):
            rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                      strategy_version="1.0.0", client_order_id="O-1")
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 8. CANCEL parcial preserva fills
# --------------------------------------------------------------------------


class TestCancelPartial:
    def test_cancel_partial_preserves_fills(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent.from_order(_open_order("O-P", quantity=0.3)))
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, journal=journal)
        # Orden PENDIENTE parcialmente ejecutada en el broker.
        fake.add_open_order(
            700, magic=adapter._magic_for("O-P"), comment=adapter._comment_for("O-P"),
            volume=0.3, price=ASK,
        )
        fake.add_deal(7001, 700, volume=0.1, price=ASK)
        adapter._remember("O-P", 700)
        fake._account_fill(INSTRUMENT, 0.1, ASK)  # el broker ya ejecuto 0.1
        rep = rt.initialize()
        assert rep.ready is True
        # El fill parcial ya ejecutado se contabilizo (idempotente).
        assert ledger.position(INSTRUMENT).quantity == pytest.approx(0.1)
        out = rt.cancel_pending(client_order_id="O-P")
        assert out.allowed and fake.cancel_count == 1
        # fills preservados; solo se cancela el remanente
        assert ledger.position(INSTRUMENT).quantity == pytest.approx(0.1)
        assert journal.get("O-P").state == "CANCELLED"


# --------------------------------------------------------------------------
# 16. Compatibilidad: OPEN/CANCEL/JOURNAL/LIVE-02/07/08/09
# --------------------------------------------------------------------------


class TestHistoryIntegrity:
    def test_snapshot_corrupt_fails_closed(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        store.path.write_text("{ corrupto", encoding="utf-8")
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "ledger"
        assert fake.send_count == 0

    def test_history_incomplete_without_snapshot_not_ready(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent.from_order(_open_order("O-H")))
        journal.mark_state("O-H", "FILLED")  # terminal: no bloquea recovery
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, journal=journal)
        # El historial agregado del broker falla: NO debe convertirse en [].
        monkeypatch.setattr(
            adapter, "_safe_history_orders_get",
            lambda: (_ for _ in ()).throw(BrokerConnectionError("transport")),
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "history"
        assert fake.send_count == 0

    def test_valid_snapshot_recovery_same_result_no_double(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        cash = ledger.cash
        # Nuevo runtime: snapshot PRESENTE + historial COMPLETO => mismo estado.
        adapter2 = _adapter(fake)
        ledger2 = _ledger()
        rt2 = LiveExecutionRuntime(
            broker=adapter2, order_manager=OrderManager(), journal=journal,
            ledger=ledger2, ledger_store=store, sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
            instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
            data_max_age_s=120.0, account_mode_provider=lambda: 0,
            marks_provider=lambda: {INSTRUMENT: fake._mark},
            commission_policy=CommissionPolicy("USD"), clock=lambda: NOW, run_id="RT2",
            mt5_accounting=passing_admission(),
        )
        rep = rt2.initialize()
        assert rep.ready is True
        assert rt2.ledger.cash == pytest.approx(cash)
        assert rt2.ledger.position(INSTRUMENT).quantity == pytest.approx(0.1)
        # Repetir recuperacion no duplica (snapshot integro).
        rt2.initialize()
        assert rt2.ledger.cash == pytest.approx(cash)

    def test_history_none_not_ready(self, tmp_path):
        # None de history_orders_get() == fallo de API, NUNCA historial vacio.
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent.from_order(_open_order("O-H")))
        journal.mark_state("O-H", "FILLED")
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, journal=journal)
        fake.history_orders_get = lambda ticket=None: None
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "history"
        assert fake.send_count == 0

    def test_deals_none_for_filled_not_ready(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent.from_order(_open_order("O-D")))
        journal.mark_state("O-D", "FILLED")
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path, journal=journal)
        ticket = 810
        fake._orders[ticket] = SimpleNamespace(
            ticket=ticket, symbol=INSTRUMENT, magic=adapter._magic_for("O-D"),
            comment=adapter._comment_for("O-D"), type=ORDER_TYPE_BUY,
            state=ORDER_STATE_FILLED, volume_initial=0.1, volume_current=0.0,
            price_open=ASK,
        )
        adapter._remember("O-D", ticket)
        fake.history_deals_get = lambda order=None: None
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "history"

    def test_history_incomplete_with_snapshot_not_ready(self, tmp_path, monkeypatch):
        # La existencia de un snapshot NO demuestra cobertura de fills
        # posteriores: historial incompleto => no preparado aunque haya snapshot.
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()  # snapshot persistido
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        assert store.exists
        monkeypatch.setattr(
            adapter, "lookup_order_history",
            lambda cid: OrderHistoryLookup(cid, HISTORY_INCOMPLETE),
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "history"

    def test_incomplete_lookup_is_not_empty_confirmed(self, tmp_path, monkeypatch):
        # El adapter expone estado explicito; INCOMPLETE != EMPTY_CONFIRMED.
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = _adapter(fake)
        monkeypatch.setattr(
            adapter, "_safe_history_orders_get",
            lambda: (_ for _ in ()).throw(BrokerConnectionError("transport")),
        )
        result = adapter.lookup_order_history("ANY")
        assert result.status == HISTORY_INCOMPLETE
        assert result.status != "EMPTY_CONFIRMED"


class TestCompatibility:
    def test_close_reduce_keep_distinct_operation_types(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        rt.initialize()
        rt.submit(side=OrderSide.BUY, quantity=0.3, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="O-1")
        rt.close_position()
        assert journal.get("CLOSE:EURUSD").operation == OPERATION_CLOSE
        assert journal.get("O-1").operation == OPERATION_OPEN


# --------------------------------------------------------------------------
# 06I-C1: fallo de resolucion POST-SEND (efecto externo ya ocurrido)
# --------------------------------------------------------------------------


class UnmappedSymbolAfterSendFakeMT5(AccountingFakeMT5):
    """La orden ENVIADA queda registrada con un simbolo MT5 NO mapeado.

    Simula un broker que responde DONE pero refleja la orden con un simbolo ajeno
    a la configuracion: la resolucion MT5->Atlas del adaptador falla DESPUES de
    que `order_send` ya incremento el contador (efecto externo simulado).
    """

    unmapped_symbol = "GHOST.raw"

    def order_send(self, request):
        result = super().order_send(request)
        ticket = getattr(result, "order", None)
        if ticket and int(ticket) in self._orders:
            self._orders[int(ticket)].symbol = self.unmapped_symbol
        return result


class TestPostSendSymbolResolutionFailure:
    def test_post_send_failure_propagates_and_blocks_next_operation(self, tmp_path):
        fake = UnmappedSymbolAfterSendFakeMT5()
        rt, adapter, fake2, ledger, journal, store = _runtime(
            tmp_path, fake=fake, maker=True
        )
        assert rt.initialize().ready is True

        # 1) La operacion SI llego al broker (efecto externo) pero la resolucion
        #    posterior falla: el error tipado se propaga (no es exito).
        with pytest.raises(BrokerInvalidOrderError):
            rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                      strategy_version="1.0.0", client_order_id="P-1")
        assert fake.send_count == 1

        # 2) El runtime NO autoriza una nueva operacion hasta la recuperacion:
        #    el intent WRITE-AHEAD previo queda sin resolver y bloquea el envio.
        sends = fake.send_count
        with pytest.raises(ExecutionBlockedError):
            rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                      strategy_version="1.0.0", client_order_id="P-2")
        assert fake.send_count == sends  # sin segundo order_send
        assert len(journal.unresolved()) == 1

        # 3) La recuperacion NO limpia la incertidumbre en silencio.
        assert rt.initialize().ready is False
        assert len(journal.unresolved()) == 1
