"""Tests LIVE-08: persistencia write-ahead del intent de orden (crash-safe).

Contrato LIVE-08 (GAP-2 / GAP-2a):

    crear orden -> client_order_id estable -> persistir intent (durable)
      -> submit -> crash -> restart -> load intents -> lookup/reconciliacion
      -> NUNCA resubmit ciego

Garantia: write-ahead intent + stable identity + reconciliation + idempotency.
NO es una transaccion atomica broker+storage. NO reabre F-06-RECOVERY; el
journal NO usa `PaperBrokerAdapter.snapshot/load_snapshot` ni sustituye al
replay de `bars.jsonl`.

Determinista: sin sleeps ni red. No usa `pickle`.
"""

from __future__ import annotations

import json

from execution.intent_journal import (
    SCHEMA_VERSION,
    IntentJournal,
    IntentJournalCorruptionError,
    IntentJournalDuplicateError,
    IntentJournalError,
    IntentJournalNotInitializedError,
    IntentJournalSchemaError,
    IntentJournalStorageAmbiguousError,
    OrderIntent,
    recover_pending_intents,
)
from execution.order import Order, OrderSide, OrderType
from brokers.models import BrokerConnectionError, BrokerOrder, BrokerOrderState

from tests.test_paper_trading import (  # noqa: E402
    HAPPY,
    INSTRUMENT,
    LEVEL,
    START,
    _LevelLong,
    _drive,
    _stack,
)

ENTRY_CID = f"TEST-LONG:{INSTRUMENT}:{START + 3600}"


def _order(cid: str, *, quantity: float = 1.0, order_type=OrderType.MARKET) -> Order:
    return Order(
        strategy_id="s",
        strategy_version="1.0.0",
        account_id="PAPER-01",
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=order_type,
        client_order_id=cid,
        limit_price=49_000.0 if order_type is OrderType.LIMIT else None,
    )


def _journal(tmp_path, name: str = "intents.json") -> IntentJournal:
    """Primera inicializacion explicita y durable del journal."""
    return IntentJournal.initialize(tmp_path / name)


def _env(tmp_path, **kw):
    j = kw.pop("journal", None) or _journal(tmp_path)
    env = _stack(strategy=_LevelLong(level=LEVEL), intent_journal=j, **kw)
    return env, j


# --------------------------------------------------------------------------
# PRE-SUBMIT: write-ahead ordering
# --------------------------------------------------------------------------


class TestWriteAhead:
    def test_intent_persisted_before_submit(self, tmp_path):
        env, j = _env(tmp_path)
        seen = []
        real = env.broker.submit_order

        def spy(order):
            # En el instante del submit el intent ya debe estar DURABLE.
            seen.append(
                (j.path.exists(), j.get(order.client_order_id))
            )
            return real(order)

        env.broker.submit_order = spy
        _drive(env, HAPPY[:3])
        assert seen, "no se ejecuto submit"
        existed, intent = seen[0]
        assert existed is True  # fichero ya escrito
        assert intent is not None and intent.client_order_id == ENTRY_CID

    def test_persist_failure_means_no_submit(self, tmp_path):
        env, j = _env(tmp_path)

        def boom(intent):
            raise IntentJournalError("fallo de persistencia simulado")

        j.begin = boom
        _drive(env, HAPPY[:3])
        # Fail-closed: NUNCA se envio la orden al broker.
        assert env.broker._orders == {}
        assert env.engine.position is None

    def test_client_order_id_stable_across_reload(self, tmp_path):
        env, j = _env(tmp_path)
        _drive(env, HAPPY[:3])
        reloaded = IntentJournal(j.path)
        intent = reloaded.get(ENTRY_CID)
        assert intent is not None
        assert intent.client_order_id == ENTRY_CID
        assert ENTRY_CID in {i.client_order_id for i in reloaded.all()}


# --------------------------------------------------------------------------
# CRASH WINDOWS A-E
# --------------------------------------------------------------------------


class TestCrashWindows:
    def test_crash_after_persist_before_submit(self, tmp_path):
        j = _journal(tmp_path)
        intent = OrderIntent.from_order(_order("C-A"))
        j.begin(intent)
        # submit nunca ejecutado; el intent sigue unresolved.
        unresolved = j.unresolved()
        assert [i.client_order_id for i in unresolved] == ["C-A"]
        assert j.get("C-A").state == "CREATED"

    def test_crash_after_broker_accept_response_lost(self, tmp_path):
        env, j = _env(tmp_path)
        env.broker.simulate_response_loss(ENTRY_CID)
        # Simula "crash antes de resolver": el lookup no esta disponible, de modo
        # que el estado UNKNOWN persiste (no se resuelve en la misma barra).
        def unavailable(cid):
            raise BrokerConnectionError("crash antes de resolver")

        env.broker.get_order = unavailable
        _drive(env, HAPPY[:3])
        assert j.get(ENTRY_CID).state == "UNKNOWN"
        assert ENTRY_CID in {i.client_order_id for i in j.unresolved()}

    def test_state_update_lost_recovery_resolves(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "lost.json")
        cid = "C-LOST"
        # El broker ya posee la orden (submit aceptado antes del crash)...
        env.broker.submit_order(_order(cid))
        # ... pero el proceso murio antes de registrar el resultado.
        j.begin(OrderIntent.from_order(_order(cid)))
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "RESOLVED_FOUND"
        assert j.get(cid).state == "FILLED"
        assert j.get(cid).broker_order_id is not None

    def test_partial_fill_crash_then_resolve(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "partial.json")
        cid = "C-PART"
        env.broker.submit_order(_order(cid, quantity=3.0, order_type=OrderType.LIMIT))
        env.broker.simulate_fill(cid, 1.0, 50_000.0)
        j.begin(OrderIntent.from_order(_order(cid, quantity=3.0)))
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "RESOLVED_FOUND"
        assert j.get(cid).state == "PARTIALLY_FILLED"
        assert j.get(cid).fills_applied == (0,)

    def test_terminal_fill_crash_then_resolve(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "terminal.json")
        cid = "C-TERM"
        env.broker.submit_order(_order(cid))
        j.begin(OrderIntent.from_order(_order(cid)))
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "RESOLVED_FOUND"
        assert j.get(cid).state == "FILLED"
        assert j.get(cid).terminal is True


# --------------------------------------------------------------------------
# RECOVERY: lookup-only, sin resubmit
# --------------------------------------------------------------------------


class TestRecoveryLookup:
    def test_found_resolves_and_records_identity(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "found.json")
        cid = "C-FOUND"
        env.broker.submit_order(_order(cid))
        j.begin(OrderIntent.from_order(_order(cid)))
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "RESOLVED_FOUND"
        assert out[0].broker_order_id == env.broker._orders[cid].broker_order_id

    def test_not_found_stays_unresolved(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "nf.json")
        j.begin(OrderIntent.from_order(_order("C-NF")))
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "NOT_FOUND"
        assert j.get("C-NF").state == "CREATED"
        assert "C-NF" in {i.client_order_id for i in j.unresolved()}

    def test_lookup_uncertain_stays_unresolved(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "unc.json")
        j.begin(OrderIntent.from_order(_order("C-UNC")))
        env.broker.disconnect()
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "LOOKUP_UNCERTAIN"
        assert "C-UNC" in {i.client_order_id for i in j.unresolved()}

    def test_broker_unknown_stays_unresolved(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "bunknown.json")
        j.begin(OrderIntent.from_order(_order("C-BU")))

        def fake_get(cid):
            return BrokerOrder(
                client_order_id=cid,
                broker_order_id="BRK-U",
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=1.0,
                state=BrokerOrderState.UNKNOWN,
            )

        env.broker.get_order = fake_get
        out = recover_pending_intents(j, env.broker)
        assert out[0].result == "BROKER_UNKNOWN"
        assert j.get("C-BU").state == "CREATED"

    def test_recovery_never_resubmits(self, tmp_path):
        env, _ = _env(tmp_path)
        j = _journal(tmp_path, "noresub.json")
        j.begin(OrderIntent.from_order(_order("C-NORESUB")))
        before = dict(env.broker._orders)
        recover_pending_intents(j, env.broker)
        # NOT_FOUND => sin resubmit: el broker sigue igual.
        assert env.broker._orders == before


# --------------------------------------------------------------------------
# IDEMPOTENCIA
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_intent_no_second_order(self, tmp_path):
        env1, j = _env(tmp_path)
        _drive(env1, HAPPY[:3])
        assert ENTRY_CID in env1.broker._orders

        # Restart-logico: NUEVO engine con el MISMO journal y broker fresco.
        env2 = _stack(strategy=_LevelLong(level=LEVEL), intent_journal=j)
        _drive(env2, HAPPY[:3])
        # El intent persistido bloquea el resubmit: 0 ordenes nuevas.
        assert env2.broker._orders == {}
        assert len([i for i in j.all()]) == 1

    def test_fill_redelivery_records_identity_once(self, tmp_path):
        j = _journal(tmp_path, "fills.json")
        j.begin(OrderIntent.from_order(_order("C-FILL")))
        assert j.record_fill("C-FILL", 0) is True
        assert j.record_fill("C-FILL", 0) is False
        assert j.get("C-FILL").fills_applied == (0,)
        # Sobrevive a reload.
        assert IntentJournal(j.path).get("C-FILL").fills_applied == (0,)


# --------------------------------------------------------------------------
# STORAGE: corrupcion / ambiguedad => fail-closed
# --------------------------------------------------------------------------


class TestInitialization:
    def test_first_initialization_creates_durable_journal(self, tmp_path):
        p = tmp_path / "first.json"
        j = IntentJournal.initialize(p)
        assert p.exists() and j.marker_path.exists()
        assert j.initialized is True
        assert len(j) == 0 and j.all() == ()
        # El journal inicializado vacio es un JSON valido con schema.
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_VERSION
        # Y se puede reabrir como journal ya inicializado.
        assert len(IntentJournal(p)) == 0

    def test_not_initialized_fails_closed(self, tmp_path):
        p = tmp_path / "never.json"
        try:
            IntentJournal(p)  # sin create=True
        except IntentJournalNotInitializedError:
            return
        raise AssertionError("journal no inicializado debe fallar cerrado")

    def test_absent_after_initialization_fails_closed(self, tmp_path):
        p = tmp_path / "gone.json"
        IntentJournal.initialize(p)
        p.unlink()  # journal inicializado desaparecido
        try:
            IntentJournal(p)
        except IntentJournalStorageAmbiguousError:
            return
        raise AssertionError("journal ausente tras init debe ser STORAGE_AMBIGUOUS")

    def test_missing_marker_fails_closed(self, tmp_path):
        p = tmp_path / "nomarker.json"
        j = IntentJournal.initialize(p)
        j.marker_path.unlink()
        try:
            IntentJournal(p)
        except IntentJournalStorageAmbiguousError:
            return
        raise AssertionError("journal sin marcador debe ser STORAGE_AMBIGUOUS")

    def test_corrupt_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        IntentJournal.initialize(p)
        p.write_text("{ not valid json", encoding="utf-8")
        try:
            IntentJournal(p)
        except IntentJournalCorruptionError:
            return
        raise AssertionError("JSON corrupto no debe cargarse")

    def test_truncated_empty_file_raises(self, tmp_path):
        p = tmp_path / "trunc.json"
        IntentJournal.initialize(p)
        p.write_text("", encoding="utf-8")
        try:
            IntentJournal(p)
        except IntentJournalCorruptionError:
            return
        raise AssertionError("journal truncado/vacio no debe cargarse")

    def test_unknown_schema_raises(self, tmp_path):
        p = tmp_path / "schema.json"
        IntentJournal.initialize(p)
        p.write_text(
            json.dumps({"schema_version": 999, "intents": {}}), encoding="utf-8"
        )
        try:
            IntentJournal(p)
        except IntentJournalSchemaError:
            return
        raise AssertionError("schema desconocido debe fallar cerrado")


class TestStorageFailSafe:
    def test_duplicate_cid_conflict_raises(self, tmp_path):
        j = _journal(tmp_path, "dup.json")
        j.begin(OrderIntent.from_order(_order("C-DUP")))
        other = OrderIntent.from_order(_order("C-DUP"))
        # order_id distinto para el mismo cid => conflicto.
        try:
            j.begin(other)
        except IntentJournalDuplicateError:
            return
        raise AssertionError("conflicto de cid debe fallar")

    def test_duplicate_begin_same_identity_is_idempotent(self, tmp_path):
        j = _journal(tmp_path, "dupok.json")
        intent = OrderIntent.from_order(_order("C-IDEM"))
        j.begin(intent)
        j.begin(intent)  # mismo order_id/identidad -> no-op
        assert len(j) == 1

    def test_storage_version_is_written(self, tmp_path):
        j = _journal(tmp_path, "ver.json")
        j.begin(OrderIntent.from_order(_order("C-V")))
        payload = json.loads(j.path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_VERSION


# --------------------------------------------------------------------------
# RETENTION: tombstones conservan identidad de fill (LIVE-06)
# --------------------------------------------------------------------------


class TestRetention:
    def test_cleanup_without_policy_preserves_tombstones(self, tmp_path):
        j = _journal(tmp_path, "cleanup.json")
        j.begin(OrderIntent.from_order(_order("C-OPEN")))
        j.begin(OrderIntent.from_order(_order("C-DONE")))
        j.mark_state("C-DONE", "FILLED")
        assert j.cleanup() == 0  # sin politica explicita no elimina nada
        assert j.get("C-DONE") is not None
        assert j.get("C-OPEN") is not None

    def test_terminal_intent_preserves_fill_identity(self, tmp_path):
        j = _journal(tmp_path, "fillsurv.json")
        j.begin(OrderIntent.from_order(_order("C-FS")))
        j.mark_state("C-FS", "FILLED", broker_order_id="BRK-FS")
        j.record_fill("C-FS", 0)
        j.cleanup()  # no-op
        t = j.get("C-FS")
        assert t.state == "FILLED"
        assert t.broker_order_id == "BRK-FS"
        assert t.fills_applied == (0,)

    def test_restart_redelivery_of_same_fill_is_noop(self, tmp_path):
        j = _journal(tmp_path, "redeliver.json")
        j.begin(OrderIntent.from_order(_order("C-RD")))
        j.record_fill("C-RD", 0)
        j.cleanup()
        # Restart: recargar desde disco.
        reloaded = IntentJournal(j.path)
        assert reloaded.get("C-RD").fills_applied == (0,)
        # Redelivery del mismo fill => no-op.
        assert reloaded.record_fill("C-RD", 0) is False

    def test_cancelled_rejected_preserve_identity(self, tmp_path):
        j = _journal(tmp_path, "term.json")
        j.begin(OrderIntent.from_order(_order("C-CAN")))
        j.mark_state("C-CAN", "CANCELLED", broker_order_id="BRK-CAN")
        j.begin(OrderIntent.from_order(_order("C-REJ")))
        j.mark_state("C-REJ", "REJECTED")
        j.cleanup()
        assert j.get("C-CAN").broker_order_id == "BRK-CAN"
        assert j.get("C-REJ").state == "REJECTED"

    def test_explicit_policy_never_drops_filled_identity(self, tmp_path):
        j = _journal(tmp_path, "policy.json")
        j.begin(OrderIntent.from_order(_order("C-FILLED")))
        j.mark_state("C-FILLED", "FILLED")
        j.record_fill("C-FILLED", 0)
        j.begin(OrderIntent.from_order(_order("C-REJECTED")))
        j.mark_state("C-REJECTED", "REJECTED")
        # Politica explicita que "borraria todo": aun asi NO puede eliminar un
        # intent con fills aplicados (identidad de fill intocable).
        removed = j.cleanup(retention=lambda intent: True)
        assert removed == 1
        assert j.get("C-FILLED") is not None
        assert j.get("C-REJECTED") is None


# --------------------------------------------------------------------------
# A4 / replay: el intent persistido es la autoridad, no bars.jsonl
# --------------------------------------------------------------------------


class TestA4IntentAuthority:
    def test_replay_with_persisted_intent_does_not_duplicate_order(self, tmp_path):
        env1, j = _env(tmp_path)
        _drive(env1, HAPPY[:3])
        assert len(env1.broker._orders) == 1

        # "Replay" sobre un engine/broker nuevos con el mismo journal.
        env2 = _stack(strategy=_LevelLong(level=LEVEL), intent_journal=j)
        _drive(env2, HAPPY[:3])
        # La senal reproducida NO genera una nueva orden (el intent manda).
        assert env2.broker._orders == {}
