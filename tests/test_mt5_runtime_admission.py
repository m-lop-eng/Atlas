"""ATLAS-MT5-ACCOUNTING-05G: gates de admisión contable MT5 en runtime (offline).

Brokers, store, journal y kill switch SIMULADOS. Sin MT5 real, sin credenciales,
sin órdenes. Ejercita la autoridad REAL (`MT5AccountingAdmission`) contra un
broker fake que expone identidad observable, además del cableado del runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.test_mt5_execution_foundation import (
    CLOSE_TIME,
    INSTRUMENT,
    NOW,
    OPEN_TIME,
    FakeSessions,
    _spec,
)

from execution.intent_journal import IntentJournal, OrderIntent
from execution.ledger_applier import CommissionPolicy
from execution.ledger_store import LedgerStore
from execution.live_execution import MarketDataReference
from execution.live_runtime import LiveExecutionRuntime, RuntimeNotReadyError
from execution.mt5_accounting import (
    MT5_ACCOUNTING_SCHEMA_VERSION,
    MT5AccountBaseline,
    MT5AccountingError,
    MT5Deal,
    account_fingerprint,
)
from execution.mt5_accounting_store import MT5AccountingStore
from execution.mt5_admission import (
    MT5AccountingAdmission,
    MT5AdmissionBlockedError,
    MT5AdmissionState,
)
from execution.order_manager import OrderManager

from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    HISTORY_INCOMPLETE,
    DealHistoryResult,
    MT5AccountSnapshot,
    MT5Environment,
    OrderHistoryLookup,
)
from brokers.models import AccountState, OrderNotFoundError, Position

from killswitches.coordinator import KillSwitchCoordinator
from killswitches.models import KillSource, KillSwitchState
from papertrading.ledger import AccountLedger
from risk.types import AccountRiskState

UTC = timezone.utc
AT = datetime(2026, 1, 1, tzinfo=UTC)
TO = AT + timedelta(days=1)
SERVER = "Demo-Server"
LOGIN = 42
FP = account_fingerprint(server=SERVER, account_id=LOGIN)


# --------------------------------------------------------------------------
# Broker fake con observación de cuenta + superficie de runtime
# --------------------------------------------------------------------------


class _FakeBroker:
    """Broker de test: superficie mínima del runtime + identidad observable."""

    requires_accounting_admission = True

    def __init__(
        self,
        *,
        balance=10_000.0,
        credit=0.0,
        equity=10_000.0,
        currency="USD",
        server=SERVER,
        login=LOGIN,
        margin_mode=0,
        healthy=True,
        account_id="DEMO-1",
    ) -> None:
        self.config = SimpleNamespace(symbol_map={INSTRUMENT: INSTRUMENT})
        self.environment = MT5Environment.DEMO
        self._balance = balance
        self._credit = credit
        self._equity = equity
        self._currency = currency
        self._server = server
        self._login = login
        self._margin_mode = margin_mode
        self._healthy = healthy
        self._account_id = account_id
        self._positions: tuple = ()
        self._execution_gate = None

    def bind_execution_gate(self, gate) -> None:
        self._execution_gate = gate

    def remember_identity(self, client_order_id, broker_order_id) -> None:
        return None

    # --- observación (admisión)
    def read_account_snapshot(self) -> MT5AccountSnapshot:
        return MT5AccountSnapshot(
            server=self._server,
            login=str(self._login),
            login_masked="***" + str(self._login)[-4:],
            currency=self._currency,
            margin_mode=self._margin_mode,
            balance=self._balance,
            credit=self._credit,
            equity=self._equity,
        )

    # --- superficie del runtime (reconciliación / health)
    def health(self) -> bool:
        return self._healthy

    def get_open_orders(self):
        return ()

    def get_order(self, client_order_id):
        raise OrderNotFoundError(client_order_id)

    def lookup_order_history(self, client_order_id):
        return OrderHistoryLookup(client_order_id, HISTORY_EMPTY_CONFIRMED)

    def get_positions(self):
        return tuple(self._positions)

    def get_account_state(self) -> AccountState:
        return AccountState(
            self._account_id, self._balance, self._equity, 0.0, self._currency, NOW
        )


class _GenericBroker(_FakeBroker):
    """Broker NO MT5: sin marcador de admisión contable."""

    requires_accounting_admission = False


# --------------------------------------------------------------------------
# Helpers de snapshot/admisión/runtime
# --------------------------------------------------------------------------


def _baseline(*, balance=10_000.0, credit=0.0, server=SERVER, login=LOGIN,
              currency="USD") -> MT5AccountBaseline:
    return MT5AccountBaseline(
        schema_version=MT5_ACCOUNTING_SCHEMA_VERSION,
        account_fingerprint=account_fingerprint(server=server, account_id=login),
        server=server,
        account_currency=currency,
        baseline_at=AT,
        coverage_from=AT,
        balance=balance,
        credit=credit,
        login_masked="***" + str(login)[-4:],
        leverage=100,
        trade_mode=0,
        margin_mode=0,
    )


def _store(tmp_path, monkeypatch, *, durable=True, create=True, baseline=None):
    store = MT5AccountingStore(tmp_path / "mt5.json")
    monkeypatch.setattr(store, "_sync_dir", lambda: durable)
    if create:
        store.create(baseline or _baseline())
    return store


def _provider(status=HISTORY_EMPTY_CONFIRMED, deals=()):
    return lambda: DealHistoryResult(AT, TO, status, tuple(deals))


def _admission(store, *, status=HISTORY_EMPTY_CONFIRMED, deals=()):
    return MT5AccountingAdmission(store=store, history_provider=_provider(status, deals))


def _make_runtime(
    tmp_path,
    *,
    broker,
    admission=None,
    account_mode=0,
    kill_switch=None,
    allow_hedging=False,
):
    journal = IntentJournal.initialize(tmp_path / "j.json")
    ledger = AccountLedger(initial_cash=10_000.0, account_currency="USD")
    ledger.register_instrument(_spec())
    rt = LiveExecutionRuntime(
        broker=broker,
        order_manager=OrderManager(),
        journal=journal,
        ledger=ledger,
        ledger_store=LedgerStore(tmp_path / "ledger.json"),
        sessions=FakeSessions(),
        kill_switch=kill_switch or KillSwitchCoordinator(),
        instrument=INSTRUMENT,
        instrument_spec=_spec(),
        account_id="DEMO-1",
        account_currency="USD",
        max_open_positions=3,
        risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
        market_data_provider=lambda: MarketDataReference(
            INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME
        ),
        data_max_age_s=120.0,
        account_mode_provider=lambda: account_mode,
        marks_provider=lambda: {INSTRUMENT: 1.1},
        commission_policy=CommissionPolicy("USD"),
        clock=lambda: NOW,
        run_id="ADM",
        mt5_accounting=admission,
        allow_restricted_hedging=allow_hedging,
    )
    return rt, ledger, journal


# --------------------------------------------------------------------------
# 1-2. Gate obligatorio vs broker genérico
# --------------------------------------------------------------------------


class TestRequiredGate:
    def test_mt5_adapter_without_admission_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, create=False)
        rt, _, _ = _make_runtime(tmp_path, broker=_FakeBroker(), admission=None)
        rep = rt.initialize()
        assert rep.ready is False
        assert rep.stage == "mt5_accounting"
        assert rt.ready is False

    def test_generic_broker_without_admission_unchanged(self, tmp_path, monkeypatch):
        rt, _, _ = _make_runtime(tmp_path, broker=_GenericBroker(), admission=None)
        rep = rt.initialize()
        assert rep.ready is True and rep.stage == "ready"


# --------------------------------------------------------------------------
# 3. Snapshot ausente / corrupto / otra cuenta / versión incompatible
# --------------------------------------------------------------------------


class TestSnapshotBlocking:
    def test_snapshot_absent_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, create=False)
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "snapshot"

    def test_snapshot_corrupt_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        store.path.write_text("{not json", encoding="utf-8")
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "snapshot"

    def test_snapshot_other_account_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["accounting"]["baseline"]["account_fingerprint"] = "a" * 32
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "identity"

    def test_snapshot_version_incompatible_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["accounting"]["state_version"] = 3
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "snapshot"


# --------------------------------------------------------------------------
# 4. Identidad / divisa discrepantes
# --------------------------------------------------------------------------


class TestIdentityCurrency:
    def test_identity_mismatch_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        broker = _FakeBroker(login=9999)
        rep = _admission(store).initialize(broker)
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "identity"

    def test_currency_mismatch_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        broker = _FakeBroker(currency="EUR")
        rep = _admission(store).initialize(broker)
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "currency"


# --------------------------------------------------------------------------
# 5-6. Durabilidad no confirmada / fallo de confirm_durability
# --------------------------------------------------------------------------


class TestDurability:
    def test_unconfirmed_durability_blocks_at_init(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, durable=False)
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "durability"

    def test_unconfirmed_durability_blocks_after_restart(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, durable=False)
        _admission(store).initialize(_FakeBroker())
        # "Reinicio": nueva autoridad sobre el MISMO store.
        rep2 = _admission(store).initialize(_FakeBroker())
        assert rep2.state is MT5AdmissionState.BLOCKED and rep2.stage == "durability"

    def test_confirm_durability_failure_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)

        def boom(*_a, **_k):
            raise MT5AccountingError("save fallo simulado")

        monkeypatch.setattr(store, "save", boom)
        rep = _admission(store).initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "durability"


# --------------------------------------------------------------------------
# 7. Ledger incompleto
# --------------------------------------------------------------------------


class TestIncompleteLedger:
    def _incomplete_store(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        ledger = store.load()
        bad = MT5Deal(
            account_fingerprint=FP, ticket=1, order=0, type=99, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=0.0,
            commission=0.0, swap=0.0, fee=0.0, time=AT,
            time_msc=int(AT.timestamp()) * 1000,
        )
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(bad)
        store.save(ledger, overwrite=True)
        return store, ledger

    def test_incomplete_ledger_blocks_and_preserves_reason(self, tmp_path, monkeypatch):
        store, ledger = self._incomplete_store(tmp_path, monkeypatch)
        reason = ledger.incomplete_reason
        admission = _admission(store)
        rep = admission.initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "incomplete"
        assert admission.ingestor.ledger.is_complete is False
        assert admission.ingestor.ledger.incomplete_reason == reason
        assert admission.ingestor.ledger.ingestion.has_successful_scan is False


# --------------------------------------------------------------------------
# 8-9. Ingestión: consulta inicial, historial incompleto
# --------------------------------------------------------------------------


class TestIngestion:
    def test_valid_initial_query_admits(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store, status=HISTORY_EMPTY_CONFIRMED)
        rep = admission.initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.ADMITTED
        assert admission.ingestor.ledger.ingestion.has_successful_scan is True

    def test_history_incomplete_blocks_without_cursor(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store, status=HISTORY_INCOMPLETE)
        rep = admission.initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "history"
        assert admission.ingestor.ledger.ingestion.has_successful_scan is False

    def test_malformed_result_blocks_without_cursor(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)

        def provider():
            return DealHistoryResult(AT, TO, HISTORY_COMPLETE, None)  # deals invalido

        admission = MT5AccountingAdmission(store=store, history_provider=provider)
        rep = admission.initialize(_FakeBroker())
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "ingestion"
        assert admission.ingestor.ledger.ingestion.has_successful_scan is False


# --------------------------------------------------------------------------
# 11-13. Ownership, reconciliación y kill switch
# --------------------------------------------------------------------------


class TestOtherGates:
    def test_external_position_blocks_ownership(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store)
        broker = _FakeBroker()
        broker._positions = (
            Position(
                instrument=INSTRUMENT, quantity=0.2, avg_entry_price=1.1,
                updated_at=NOW, ticket="9001", position_id="9001",
            ),
        )
        rt, _, _ = _make_runtime(
            tmp_path, broker=broker, admission=admission, allow_hedging=True
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "ownership"

    def test_reconciliation_mismatch_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, baseline=_baseline(balance=10_000.0))
        broker = _FakeBroker(balance=9_999.0)  # divergencia de balance
        rep = _admission(store).initialize(broker)
        assert rep.state is MT5AdmissionState.BLOCKED and rep.stage == "reconciliation"

    def test_kill_switch_active_blocks(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store)
        ks = KillSwitchCoordinator()
        ks.set_source(KillSource.MANUAL, KillSwitchState.HALT, "manual")
        rt, _, _ = _make_runtime(
            tmp_path, broker=_FakeBroker(), admission=admission, kill_switch=ks
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "kill_switch"


# --------------------------------------------------------------------------
# 10. Gate de recovery existente (intents sin resolver)
# --------------------------------------------------------------------------


class TestRecoveryGatePreserved:
    def test_ambiguous_intent_blocks_recovery(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store)
        rt, _, journal = _make_runtime(
            tmp_path, broker=_FakeBroker(), admission=admission
        )
        journal.begin(OrderIntent(
            client_order_id="U-1", order_id="ORD-U-1", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        journal.mark_state("U-1", "SUBMITTING")
        rt.broker.lookup_order_history = (
            lambda cid: OrderHistoryLookup(cid, "LOOKUP_UNCERTAIN")
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "recovery"


# --------------------------------------------------------------------------
# 14-16. Admisión válida, fallo tras operación y caminos alternativos
# --------------------------------------------------------------------------


class RecordingAdmission:
    """Stub que registra refresh y puede bloquearse (para el cableado)."""

    class _Report:
        def __init__(self, admitted, stage="admitted", reason="stub"):
            self.admitted = admitted
            self.stage = stage
            self.reason = reason

    def __init__(self):
        self.admitted = True
        self.refreshed = 0

    def initialize(self, broker):
        return self._Report(True)

    def refresh(self, broker):
        self.refreshed += 1
        self.admitted = False
        return self._Report(False, "reconciliation", "forzado")

    def ensure_admitted(self):
        if not self.admitted:
            raise MT5AdmissionBlockedError("bloqueado por stub")


class TestAdmissionWiring:
    def test_all_gates_pass_admits(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        admission = _admission(store)
        rt, _, _ = _make_runtime(
            tmp_path, broker=_FakeBroker(), admission=admission
        )
        rep = rt.initialize()
        assert rep.ready is True and rep.stage == "ready"
        assert admission.admitted is True

    def test_post_operation_refresh_blocks_next(self, tmp_path, monkeypatch):
        admission = RecordingAdmission()
        rt, _, _ = _make_runtime(
            tmp_path, broker=_FakeBroker(), admission=admission
        )
        assert rt.initialize().ready is True
        # Simula el retorno de una operación (sin submission) => refresh contable.
        rt._post_operation(SimpleNamespace(submission=None, order=None))
        assert admission.refreshed == 1
        with pytest.raises(RuntimeNotReadyError):
            rt._ensure_operable()

    def test_no_alternative_path_bypasses_gate(self, tmp_path, monkeypatch):
        admission = RecordingAdmission()
        admission.admitted = False  # bloqueado
        rt, _, _ = _make_runtime(
            tmp_path, broker=_FakeBroker(), admission=admission
        )
        rt._ready = True  # fuerza el estado "preparado" obsoleto
        for call in (
            lambda: rt.submit(strategy_id="s", strategy_version="1", client_order_id="A0"),
            lambda: rt.cancel_pending(client_order_id="A1"),
            lambda: rt.close_position(),
            lambda: rt.reduce_position(quantity=0.1),
        ):
            with pytest.raises(RuntimeNotReadyError):
                call()

    def test_mt5_adapter_without_component_blocks_all_paths(self, tmp_path, monkeypatch):
        rt, _, _ = _make_runtime(tmp_path, broker=_FakeBroker(), admission=None)
        rt._ready = True  # aunque marquen ready, el gate debe bloquear
        for call in (
            lambda: rt.submit(strategy_id="s", strategy_version="1", client_order_id="B0"),
            lambda: rt.cancel_pending(client_order_id="B1"),
            lambda: rt.close_position(),
            lambda: rt.reduce_position(quantity=0.1),
        ):
            with pytest.raises(RuntimeNotReadyError):
                call()


# --------------------------------------------------------------------------
# 17. Aislamiento: sin MT5 real
# --------------------------------------------------------------------------


class TestIsolation:
    def test_no_real_mt5_imported(self):
        import sys

        assert "MetaTrader5" not in sys.modules
