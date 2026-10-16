"""ATLAS-MT5-ACCOUNTING-05D: ingestion idempotente + cursor atomico (offline).

Solo directorios temporales, objetos en memoria y `DealHistoryResult`/
`HistoricalDeal` construidos a mano. Sin MT5, sin credenciales, sin runtime, sin
trading. La durabilidad del store se controla con `_sync_dir` (mismo patron que
`test_mt5_accounting_store.py`).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from brokers.models import HistoricalDeal
from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    HISTORY_INCOMPLETE,
    DealHistoryResult,
)
from execution.mt5_accounting import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    DEAL_TYPE_BALANCE,
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    MT5_ACCOUNTING_SCHEMA_VERSION,
    MT5AccountBaseline,
    MT5AccountingError,
    MT5Deal,
    account_fingerprint,
)
from execution.mt5_accounting_store import (
    MT5AccountingStore,
    PersistenceResult,
    SnapshotNotEmptyError,
    SnapshotVersionError,
    SnapshotWriteError,
)
from execution.mt5_ingestion import (
    AccountIdentityError,
    DurabilityState,
    IngestionBlockedError,
    IngestionStatus,
    MT5AccountIngestor,
    ScanRejectedError,
)

UTC = timezone.utc
AT = datetime(2026, 1, 1, tzinfo=UTC)
TO = AT + timedelta(days=1)
SERVER = "Demo-Server"
ACCOUNT_ID = "1000001"
FP = account_fingerprint(server=SERVER, account_id=ACCOUNT_ID)


def _baseline(**over):
    fields = dict(
        schema_version=MT5_ACCOUNTING_SCHEMA_VERSION,
        account_fingerprint=FP,
        server=SERVER,
        account_currency="USD",
        baseline_at=AT,
        coverage_from=AT,
        balance=10000.0,
        credit=0.0,
        login_masked="***89",
        leverage=100,
        trade_mode=0,
        margin_mode=2,
    )
    fields.update(over)
    return MT5AccountBaseline(**fields)


def _hist(
    ticket,
    *,
    time,
    time_msc=None,
    order=1,
    d_type=DEAL_TYPE_BUY,
    entry=DEAL_ENTRY_IN,
    position_id=100,
    symbol="EURUSD",
    volume=1.0,
    price=1.10,
    profit=0.0,
    commission=0.0,
    swap=0.0,
    fee=0.0,
):
    if time_msc is None:
        time_msc = int(time.timestamp()) * 1000
    return HistoricalDeal(
        ticket=ticket,
        order=order,
        time=time,
        time_msc=time_msc,
        type=d_type,
        entry=entry,
        position_id=position_id,
        symbol=symbol,
        volume=volume,
        price=price,
        profit=profit,
        commission=commission,
        swap=swap,
        fee=fee,
    )


def _deposit(ticket, amount, *, time, time_msc=None):
    return _hist(
        ticket,
        time=time,
        time_msc=time_msc,
        order=0,
        d_type=DEAL_TYPE_BALANCE,
        entry=None,
        position_id=None,
        symbol=None,
        volume=0.0,
        price=0.0,
        profit=amount,
    )


def _res(status, deals, frm=AT, to=TO):
    return DealHistoryResult(frm, to, status, tuple(deals))


def _open_position(ticket, *, time, time_msc=None, position_id=100):
    return _hist(
        ticket,
        time=time,
        time_msc=time_msc,
        order=1,
        d_type=DEAL_TYPE_BUY,
        entry=DEAL_ENTRY_IN,
        position_id=position_id,
        symbol="EURUSD",
        volume=1.0,
        price=1.10,
        profit=0.0,
    )


def _setup(tmp_path, monkeypatch, *, durable=True, baseline=None):
    store = MT5AccountingStore(tmp_path / "ledger.json")
    ledger = store.create(baseline or _baseline())
    monkeypatch.setattr(store, "_sync_dir", lambda: durable)
    ingestor = MT5AccountIngestor(store, ledger)
    assert ingestor.confirm_durability() is PersistenceResult.DURABLE
    assert ingestor.durability is DurabilityState.CONFIRMED
    return store, ingestor


def _unsupported_deal(ticket=999):
    """MT5Deal con `type` desconocido: al aplicarlo marca el ledger INCOMPLETO."""
    t_msc = int(AT.timestamp()) * 1000
    return MT5Deal(
        account_fingerprint=FP,
        ticket=ticket,
        order=0,
        type=99,
        entry=None,
        position_id=None,
        symbol=None,
        volume=0.0,
        price=0.0,
        profit=0.0,
        commission=0.0,
        swap=0.0,
        fee=0.0,
        time=AT,
        time_msc=t_msc,
    )


# --------------------------------------------------------------------------
# 1-2. Aplicacion de lote y consulta vacia confirmada
# --------------------------------------------------------------------------


class TestApplyBatch:
    def test_apply_batch_of_deals(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        base_ms = int(AT.timestamp()) * 1000
        deals = [
            _hist(2, time=AT, time_msc=base_ms + 100, d_type=DEAL_TYPE_BUY,
                  entry=DEAL_ENTRY_IN, position_id=100, volume=1.0, price=1.10),
            _hist(1, time=AT, time_msc=base_ms + 200, d_type=DEAL_TYPE_SELL,
                  entry=DEAL_ENTRY_OUT, position_id=100, volume=1.0, price=1.20,
                  profit=100.0),
        ]
        # Entrada inversa (OUT antes que IN): la ingestion DEBE ordenar canonico.
        out = ingestor.apply_scan(
            _res(HISTORY_COMPLETE, list(reversed(deals))), account_fingerprint=FP
        )
        assert out.status is IngestionStatus.APPLIED
        assert out.new_deals == 2
        assert out.total_deals == 2
        assert ingestor.ledger.processed_deal_count() == 2
        assert ingestor.ledger.positions() == ()
        assert ingestor.ledger.realized_pnl == pytest.approx(100.0)
        assert ingestor.ledger.balance == pytest.approx(10100.0)
        assert out.ingestion.status is not None
        assert out.ingestion.last_successful_scan_to == TO

    def test_empty_confirmed_updates_cursor_without_deals(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        out = ingestor.apply_scan(
            _res(HISTORY_EMPTY_CONFIRMED, []), account_fingerprint=FP
        )
        assert out.status is IngestionStatus.NO_NEW_DEALS
        assert out.new_deals == 0
        assert ingestor.ledger.processed_deal_count() == 0
        assert ingestor.ledger.ingestion.has_successful_scan is True
        assert store.load().ingestion.has_successful_scan is True


# --------------------------------------------------------------------------
# 3-4. Solapamiento, duplicado e identidad contradictoria
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_overlapping_scans_do_not_double_count(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        d = _deposit(10, 250.0, time=AT)
        ingestor.apply_scan(_res(HISTORY_COMPLETE, [d]), account_fingerprint=FP)
        assert ingestor.ledger.balance == pytest.approx(10250.0)

        out = ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [d], to=AT + timedelta(days=2)),
            account_fingerprint=FP,
        )
        assert out.status is IngestionStatus.NO_NEW_DEALS
        assert out.new_deals == 0
        assert out.total_deals == 1
        assert ingestor.ledger.balance == pytest.approx(10250.0)

    def test_identical_ticket_is_noop_conflicting_ticket_blocks(
        self, tmp_path, monkeypatch
    ):
        store, ingestor = _setup(tmp_path, monkeypatch)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(10, 100.0, time=AT)]),
            account_fingerprint=FP,
        )
        # Identico: sin doble contabilizacion.
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(10, 100.0, time=AT)]),
            account_fingerprint=FP,
        )
        assert ingestor.ledger.balance == pytest.approx(10100.0)
        # Contradictorio (mismo ticket, distinto importe): se bloquea.
        with pytest.raises(MT5AccountingError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(10, 999.0, time=AT)]),
                account_fingerprint=FP,
            )
        assert ingestor.ledger.balance == pytest.approx(10100.0)


# --------------------------------------------------------------------------
# 5-6. Orden temporal y deals tardios
# --------------------------------------------------------------------------


class TestOrderingAndLateDeals:
    def test_same_second_distinct_time_msc(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        base_ms = int(AT.timestamp()) * 1000
        deps = [
            _deposit(2, 20.0, time=AT, time_msc=base_ms + 500),
            _deposit(1, 10.0, time=AT, time_msc=base_ms + 100),
        ]
        # Se pasan en orden no canonico; el snapshot debe quedar canonico.
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, deps), account_fingerprint=FP
        )
        assert ingestor.ledger.balance == pytest.approx(10030.0)
        order = [d.ticket for d, _ in ingestor.ledger.processed_deals_in_order()]
        assert order == [1, 2]

    def test_late_deal_within_coverage_reconstructed(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        t09 = AT + timedelta(hours=9)
        t10 = AT + timedelta(hours=10)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(100, 10.0, time=t10)], to=t10),
            account_fingerprint=FP,
        )
        out = ingestor.apply_scan(
            _res(
                HISTORY_COMPLETE,
                [_deposit(100, 10.0, time=t10), _deposit(50, 5.0, time=t09)],
                to=t10 + timedelta(hours=1),
            ),
            account_fingerprint=FP,
        )
        assert out.new_deals == 1
        assert ingestor.ledger.balance == pytest.approx(10015.0)
        order = [d.ticket for d, _ in ingestor.ledger.processed_deals_in_order()]
        assert order == [50, 100]

    def test_late_deal_that_breaks_sequence_blocks(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        t09 = AT + timedelta(hours=9)
        t10 = AT + timedelta(hours=10)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_open_position(2, time=t10)], to=t10),
            account_fingerprint=FP,
        )
        before = store.load().processed_deal_count()
        # OUT tardio anterior al IN: reconstruccion imposible => bloqueo seguro.
        late_out = _hist(
            1, time=t09, order=2, d_type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            position_id=100, volume=1.0, price=1.20, profit=50.0,
        )
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(
                _res(
                    HISTORY_COMPLETE,
                    [_open_position(2, time=t10), late_out],
                    to=t10 + timedelta(hours=1),
                ),
                account_fingerprint=FP,
            )
        assert store.load().processed_deal_count() == before
        assert ingestor.ledger.processed_deal_count() == before


# --------------------------------------------------------------------------
# 7-8. Consultas no exitosas e identidad de cuenta
# --------------------------------------------------------------------------


class TestRejections:
    @pytest.mark.parametrize("status", [HISTORY_INCOMPLETE, "WAT", None, ""])
    def test_non_successful_status_does_not_advance(
        self, tmp_path, monkeypatch, status
    ):
        store, ingestor = _setup(tmp_path, monkeypatch)
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(
                _res(status, []), account_fingerprint=FP
            )
        assert ingestor.ledger.ingestion.has_successful_scan is False
        assert store.load().ingestion.has_successful_scan is False

    def test_none_result_rejected(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(None, account_fingerprint=FP)  # type: ignore[arg-type]
        assert ingestor.ledger.ingestion.has_successful_scan is False

    def test_incoherent_status_payloads_rejected(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        with pytest.raises(ScanRejectedError):  # COMPLETE sin deals
            ingestor.apply_scan(_res(HISTORY_COMPLETE, []), account_fingerprint=FP)
        with pytest.raises(ScanRejectedError):  # EMPTY_CONFIRMED con deals
            ingestor.apply_scan(
                _res(HISTORY_EMPTY_CONFIRMED, [_deposit(1, 1.0, time=AT)]),
                account_fingerprint=FP,
            )
        assert ingestor.ledger.ingestion.has_successful_scan is False

    def test_deal_out_of_interval_rejected(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        wrong = _deposit(1, 1.0, time=AT + timedelta(days=3))
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [wrong]), account_fingerprint=FP
            )
        assert ingestor.ledger.ingestion.has_successful_scan is False

    def test_wrong_account_identity_does_not_touch_ledger(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        other = account_fingerprint(server=SERVER, account_id="1000002")
        with pytest.raises(AccountIdentityError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(1, 1.0, time=AT)]),
                account_fingerprint=other,
            )
        assert ingestor.ledger.processed_deal_count() == 0
        assert store.load().processed_deal_count() == 0


# --------------------------------------------------------------------------
# 9-10. Corte de cobertura y ambiguedad
# --------------------------------------------------------------------------


class TestCoverageCutoff:
    def test_event_before_coverage_excluded(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        before = AT - timedelta(minutes=30)
        out = ingestor.apply_scan(
            _res(
                HISTORY_COMPLETE,
                [_deposit(1, 7.0, time=before), _deposit(2, 3.0, time=AT + timedelta(hours=1))],
                frm=AT - timedelta(hours=1),
                to=TO,
            ),
            account_fingerprint=FP,
        )
        assert out.excluded_before_coverage == 1
        assert out.new_deals == 1
        assert ingestor.ledger.balance == pytest.approx(10003.0)

    def test_ambiguous_cutoff_blocks(self, tmp_path, monkeypatch):
        cov = AT + timedelta(microseconds=500)
        store, ingestor = _setup(
            tmp_path, monkeypatch, baseline=_baseline(baseline_at=cov, coverage_from=cov)
        )
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(1, 1.0, time=AT)], frm=AT),
                account_fingerprint=FP,
            )
        assert ingestor.ledger.ingestion.has_successful_scan is False
        assert ingestor.ledger.processed_deal_count() == 0


# --------------------------------------------------------------------------
# 11-12. Fallos de aplicacion y de escritura
# --------------------------------------------------------------------------


class TestFailClosed:
    def test_mid_batch_failure_leaves_no_partial_snapshot(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        good = _deposit(1, 5.0, time=AT)
        bad = _hist(2, time=AT + timedelta(seconds=1), d_type=99)
        with pytest.raises(MT5AccountingError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [good, bad]), account_fingerprint=FP
            )
        assert ingestor.ledger.processed_deal_count() == 0
        assert ingestor.ledger.balance == pytest.approx(10000.0)
        assert store.load().processed_deal_count() == 0
        assert store.load().ingestion.has_successful_scan is False

    def test_write_error_before_publish_preserves_previous(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
            account_fingerprint=FP,
        )
        previous = store.load()
        previous_balance = previous.balance
        previous_to = previous.ingestion.last_successful_scan_to

        def boom(*_a, **_k):
            raise SnapshotWriteError("fallo simulado antes de publicar")

        monkeypatch.setattr(store, "_write_atomic", boom)
        with pytest.raises(SnapshotWriteError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(2, 9.0, time=AT + timedelta(hours=1))]),
                account_fingerprint=FP,
            )
        reloaded = store.load()
        assert reloaded.balance == pytest.approx(previous_balance)
        assert reloaded.processed_deal_count() == 1
        assert reloaded.ingestion.last_successful_scan_to == previous_to
        # El estado activo NO avanzo la posicion nueva.
        assert ingestor.ledger.processed_deal_count() == 1


# --------------------------------------------------------------------------
# 13-15. Durabilidad y recuperacion
# --------------------------------------------------------------------------


class TestDurability:
    def test_unconfirmed_durability_blocks_new_ingestion(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        monkeypatch.setattr(store, "_sync_dir", lambda: False)
        out = ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
            account_fingerprint=FP,
        )
        assert out.persistence is PersistenceResult.REPLACED_DURABILITY_UNCONFIRMED
        assert out.status is IngestionStatus.DURABILITY_UNCONFIRMED
        assert ingestor.durability is DurabilityState.UNCONFIRMED
        with pytest.raises(IngestionBlockedError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(2, 6.0, time=AT + timedelta(hours=1))]),
                account_fingerprint=FP,
            )

    def test_durable_confirmation_releases_block(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        monkeypatch.setattr(store, "_sync_dir", lambda: False)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
            account_fingerprint=FP,
        )
        assert ingestor.durability is DurabilityState.UNCONFIRMED
        # Sigue sin confirmar: no libera el bloqueo.
        assert ingestor.confirm_durability() is (
            PersistenceResult.REPLACED_DURABILITY_UNCONFIRMED
        )
        assert ingestor.durability is DurabilityState.UNCONFIRMED
        with pytest.raises(IngestionBlockedError):
            ingestor.apply_scan(
                _res(HISTORY_EMPTY_CONFIRMED, []), account_fingerprint=FP
            )
        # Confirmacion durable real: libera el bloqueo.
        monkeypatch.setattr(store, "_sync_dir", lambda: True)
        assert ingestor.confirm_durability() is PersistenceResult.DURABLE
        assert ingestor.durability is DurabilityState.CONFIRMED
        out = ingestor.apply_scan(
            _res(HISTORY_EMPTY_CONFIRMED, [], to=TO + timedelta(days=1)),
            account_fingerprint=FP,
        )
        assert out.status is IngestionStatus.NO_NEW_DEALS

    def test_restart_requires_reconfirmation_and_dedups(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        d = _deposit(1, 5.0, time=AT)
        ingestor.apply_scan(_res(HISTORY_COMPLETE, [d]), account_fingerprint=FP)

        # "Reinicio": nueva instancia desde el snapshot persistido.
        resumed = MT5AccountIngestor.from_store(store)
        assert resumed.durability is DurabilityState.UNCONFIRMED
        with pytest.raises(IngestionBlockedError):
            resumed.apply_scan(_res(HISTORY_COMPLETE, [d]), account_fingerprint=FP)
        assert resumed.confirm_durability() is PersistenceResult.DURABLE
        out = resumed.apply_scan(
            _res(HISTORY_COMPLETE, [d], to=AT + timedelta(days=2)),
            account_fingerprint=FP,
        )
        assert out.status is IngestionStatus.NO_NEW_DEALS
        assert resumed.ledger.balance == pytest.approx(10005.0)


# --------------------------------------------------------------------------
# 16-18. Esquema, orden canonico y _snapshot_is_empty
# --------------------------------------------------------------------------


class TestSchemaOrderAndEmpty:
    def test_old_state_version_rejected(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["accounting"]["state_version"] = 3
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotVersionError):
            store.load()

    def test_canonical_order_persisted(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        base_ms = int(AT.timestamp()) * 1000
        deps = [
            _deposit(3, 1.0, time=AT + timedelta(seconds=2)),
            _deposit(1, 1.0, time=AT, time_msc=base_ms + 10),
            _deposit(2, 1.0, time=AT, time_msc=base_ms + 20),
        ]
        ingestor.apply_scan(_res(HISTORY_COMPLETE, deps), account_fingerprint=FP)
        reloaded = store.load()
        order = [d.ticket for d, _ in reloaded.processed_deals_in_order()]
        assert order == [1, 2, 3]
        snap = reloaded.to_snapshot()
        assert [p["deal"]["ticket"] for p in snap["processed"]] == [1, 2, 3]

    def test_snapshot_is_empty_protects_ingestion_progress(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        # Sin progreso: la sobrescritura no destructiva sigue permitida.
        store.create(_baseline(), overwrite=True)
        # Con progreso de ingestion (consulta vacia) y sin deals: NO es vacio.
        ingestor.apply_scan(_res(HISTORY_EMPTY_CONFIRMED, []), account_fingerprint=FP)
        assert store.load().ingestion.has_successful_scan is True
        with pytest.raises(SnapshotNotEmptyError):
            store.create(_baseline(), overwrite=True)
        # El snapshot previo permanece intacto.
        assert store.load().ingestion.has_successful_scan is True
        # Con autorizacion destructiva si se puede reemplazar.
        store.create(_baseline(), overwrite=True, destructive=True)
        assert store.load().ingestion.has_successful_scan is False


# --------------------------------------------------------------------------
# 19. Aislamiento: sin MT5 real, sin API publica de avance de cursor
# --------------------------------------------------------------------------


class TestIsolation:
    def test_no_public_independent_cursor_advance(self):
        for name in ("advance_cursor", "set_cursor", "record_scan", "apply_deals"):
            assert not hasattr(MT5AccountIngestor, name)
        assert hasattr(MT5AccountIngestor, "apply_scan")

    def test_no_real_mt5_imported(self, tmp_path, monkeypatch):
        assert "MetaTrader5" not in sys.modules
        store, ingestor = _setup(tmp_path, monkeypatch)
        ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
            account_fingerprint=FP,
        )
        assert "MetaTrader5" not in sys.modules

    def test_confirm_durability_does_not_advance_cursor(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        assert ingestor.ledger.ingestion.has_successful_scan is False
        ingestor.confirm_durability()
        assert ingestor.ledger.ingestion.has_successful_scan is False


# --------------------------------------------------------------------------
# 05F. Precondicion: ledger INCOMPLETO bloquea toda nueva consulta
# --------------------------------------------------------------------------


class TestIncompleteLedgerPrecondition:
    def _incomplete(self, tmp_path, monkeypatch):
        """Genera un ledger INCOMPLETO por una via REAL del modelo y lo persiste."""
        store = MT5AccountingStore(tmp_path / "ledger.json")
        ledger = store.create(_baseline())
        monkeypatch.setattr(store, "_sync_dir", lambda: True)
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(_unsupported_deal())
        assert ledger.is_complete is False
        assert ledger.incomplete_reason
        # El snapshot incompleto es persistible (valido segun el modelo).
        store.save(ledger, overwrite=True)
        ingestor = MT5AccountIngestor(store, ledger)
        assert ingestor.confirm_durability() is PersistenceResult.DURABLE
        assert ingestor.durability is DurabilityState.CONFIRMED
        return store, ledger, ingestor

    def test_incomplete_ledger_blocks_apply_scan(self, tmp_path, monkeypatch):
        store, ledger, ingestor = self._incomplete(tmp_path, monkeypatch)
        reason = ledger.incomplete_reason
        snapshot_before = store.path.read_text(encoding="utf-8")
        with pytest.raises(IngestionBlockedError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
                account_fingerprint=FP,
            )
        # Ledger activo: sigue incompleto y conserva el motivo.
        assert ingestor.ledger.is_complete is False
        assert ingestor.ledger.incomplete_reason == reason
        # No se aplicaron deals ni avanzo el cursor.
        assert ingestor.ledger.processed_deal_count() == 0
        assert ingestor.ledger.ingestion.has_successful_scan is False
        # Snapshot previo intacto (byte a byte).
        assert store.path.read_text(encoding="utf-8") == snapshot_before

    def test_durability_confirmation_does_not_repair(self, tmp_path, monkeypatch):
        store, ledger, ingestor = self._incomplete(tmp_path, monkeypatch)
        assert ingestor.durability is DurabilityState.CONFIRMED
        assert ingestor.ledger.is_complete is False
        # Reconfirmar durabilidad NO repara la contabilidad.
        assert ingestor.confirm_durability() is PersistenceResult.DURABLE
        assert ingestor.ledger.is_complete is False
        with pytest.raises(IngestionBlockedError):
            ingestor.apply_scan(_res(HISTORY_EMPTY_CONFIRMED, []), account_fingerprint=FP)
        assert ingestor.ledger.is_complete is False
        assert ingestor.ledger.ingestion.has_successful_scan is False

    def test_from_store_restores_incomplete_and_blocks(self, tmp_path, monkeypatch):
        store, ledger, ingestor = self._incomplete(tmp_path, monkeypatch)
        reason = ledger.incomplete_reason
        resumed = MT5AccountIngestor.from_store(store)
        assert resumed.ledger.is_complete is False
        assert resumed.ledger.incomplete_reason == reason
        assert resumed.confirm_durability() is PersistenceResult.DURABLE
        with pytest.raises(IngestionBlockedError):
            resumed.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
                account_fingerprint=FP,
            )
        assert resumed.ledger.processed_deal_count() == 0
        assert resumed.ledger.ingestion.has_successful_scan is False


# --------------------------------------------------------------------------
# 05F. Identidad y resultados malformados
# --------------------------------------------------------------------------


class TestMalformedResultsAndIdentity:
    @pytest.mark.parametrize("bad", ["", "   ", 123, 1.5, True, None, b"xx"])
    def test_bad_fingerprints_rejected_without_state_change(
        self, tmp_path, monkeypatch, bad
    ):
        store, ingestor = _setup(tmp_path, monkeypatch)
        result = _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)])
        snapshot_before = store.path.read_text(encoding="utf-8")
        with pytest.raises(AccountIdentityError):
            ingestor.apply_scan(result, account_fingerprint=bad)  # type: ignore[arg-type]
        assert ingestor.ledger.processed_deal_count() == 0
        assert ingestor.ledger.ingestion.has_successful_scan is False
        assert store.path.read_text(encoding="utf-8") == snapshot_before

    def test_valid_fingerprint_admitted(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        out = ingestor.apply_scan(
            _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
            account_fingerprint=FP,
        )
        assert out.status is IngestionStatus.APPLIED

    def test_wrong_identity_does_not_advance_cursor(self, tmp_path, monkeypatch):
        store, ingestor = _setup(tmp_path, monkeypatch)
        other = account_fingerprint(server=SERVER, account_id="1000002")
        with pytest.raises(AccountIdentityError):
            ingestor.apply_scan(
                _res(HISTORY_COMPLETE, [_deposit(1, 5.0, time=AT)]),
                account_fingerprint=other,
            )
        assert ingestor.ledger.ingestion.has_successful_scan is False
        assert store.load().ingestion.has_successful_scan is False

    @pytest.mark.parametrize("status", [HISTORY_COMPLETE, HISTORY_EMPTY_CONFIRMED])
    def test_deals_none_is_not_a_false_empty_confirmed(
        self, tmp_path, monkeypatch, status
    ):
        store, ingestor = _setup(tmp_path, monkeypatch)
        malformed = DealHistoryResult(AT, TO, status, None)
        with pytest.raises(ScanRejectedError):
            ingestor.apply_scan(malformed, account_fingerprint=FP)
        assert ingestor.ledger.ingestion.has_successful_scan is False
        assert store.load().ingestion.has_successful_scan is False
