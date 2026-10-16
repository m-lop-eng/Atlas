"""ATLAS-MT5-ACCOUNTING-03/03B: persistencia y restauracion offline del ledger MT5.

Solo directorios temporales aislados; sin MT5, sin credenciales, sin runtime.
El ledger spot (`AccountLedger`) NO se modifica.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from execution.instrument import InstrumentSpec
from execution.mt5_accounting import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    DEAL_TYPE_BALANCE,
    DEAL_TYPE_BONUS,
    DEAL_TYPE_BUY,
    DEAL_TYPE_CREDIT,
    DEAL_TYPE_SELL,
    MT5_ACCOUNTING_SCHEMA_VERSION,
    MT5AccountBaseline,
    MT5AccountLedger,
    MT5Deal,
    AccountingEpochError,
    BaselineOrigin,
    DealIdentityCollisionError,
    IngestionScanStatus,
    IngestionState,
    UnsupportedDealError,
    account_fingerprint,
)
from execution.mt5_accounting_store import (
    MT5AccountingStore,
    MT5AccountingStoreError,
    PersistenceResult,
    STORE_SCHEMA_VERSION,
    SnapshotAccountMismatchError,
    SnapshotCorruptError,
    SnapshotExistsError,
    SnapshotNotEmptyError,
    SnapshotNotFoundError,
    SnapshotVersionError,
    SnapshotWriteError,
)
from papertrading.ledger import AccountLedger

UTC = timezone.utc
AT = datetime(2026, 1, 1, tzinfo=UTC)
SERVER = "Demo-Server"
ACCOUNT_ID = "1000001"
FP = account_fingerprint(server=SERVER, account_id=ACCOUNT_ID)
FP_OTHER = account_fingerprint(server=SERVER, account_id="1000002")


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


def _deal(**over):
    fields = dict(
        account_fingerprint=FP,
        ticket=1,
        order=1,
        type=DEAL_TYPE_BUY,
        entry=DEAL_ENTRY_IN,
        position_id=100,
        symbol="EURUSD",
        volume=1.0,
        price=1.10,
        profit=0.0,
        commission=0.0,
        swap=0.0,
        fee=0.0,
        time=AT,
    )
    fields.update(over)
    return MT5Deal(**fields)


def _deposit(ticket, amount):
    return _deal(
        ticket=ticket, order=0, type=DEAL_TYPE_BALANCE, entry=None,
        position_id=None, symbol=None, volume=0.0, price=0.0, profit=amount,
    )


def _deal_dict(**over):
    fields = dict(
        account_fingerprint=FP, ticket=1, order=1, type=DEAL_TYPE_BUY,
        entry=DEAL_ENTRY_IN, position_id=100, symbol="EURUSD", volume=1.0,
        price=1.10, profit=0.0, commission=0.0, swap=0.0, fee=0.0,
        time=AT.isoformat(),
    )
    fields.update(over)
    return fields


def _store(tmp_path):
    return MT5AccountingStore(tmp_path / "ledger.json")


def _read(tmp_path_store):
    return json.loads(tmp_path_store.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1-3. Crear, guardar y restaurar
# --------------------------------------------------------------------------


class TestCreateAndRestore:
    def test_create_empty_and_persist(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        assert store.exists
        loaded = store.load()
        assert loaded.balance == pytest.approx(10000.0)
        assert loaded.credit == pytest.approx(0.0)
        assert loaded.positions() == ()
        assert loaded.processed_deal_count() == 0
        assert store.baseline_declared() is True

    def test_save_restore_balances_positions_pnl(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=0.5, price=1.20, profit=0.05,
        ))
        ledger.apply_deal(_deal(
            ticket=3, order=0, type=DEAL_TYPE_CREDIT, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=100.0,
        ))
        store.save(ledger, overwrite=True)

        restored = store.load()
        assert restored.balance == pytest.approx(10000.05)
        assert restored.credit == pytest.approx(100.0)
        assert restored.realized_pnl == pytest.approx(0.05)
        pos = restored.positions()
        assert len(pos) == 1
        assert pos[0].position_id == 100
        assert pos[0].quantity == pytest.approx(0.5)
        assert pos[0].avg_price == pytest.approx(1.10)

    def test_save_restore_processed_and_movements(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deposit(4, 500.0))
        store.save(ledger, overwrite=True)

        restored = store.load()
        assert restored.processed_deal_count() == 2
        movements = restored.movement_records()
        assert len(movements) == 1
        assert movements[0].amount == pytest.approx(500.0)
        raw = store.path.read_text(encoding="utf-8")
        assert ACCOUNT_ID not in raw
        assert FP in raw


# --------------------------------------------------------------------------
# 4-7. Idempotencia y recuperacion
# --------------------------------------------------------------------------


class TestIdempotencyRestore:
    def test_reapply_processed_deal_after_restore_is_noop(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        deal = _deal(ticket=1, volume=1.0, price=1.10)
        ledger.apply_deal(deal)
        store.save(ledger, overwrite=True)
        restored = store.load()
        out = restored.apply_deal(deal)
        assert out.applied is False
        assert restored.processed_deal_count() == 1
        assert restored.balance == pytest.approx(10000.0)

    def test_reused_ticket_different_payload_after_restore_conflicts(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=9, volume=1.0))
        store.save(ledger, overwrite=True)
        restored = store.load()
        with pytest.raises(DealIdentityCollisionError):
            restored.apply_deal(_deal(ticket=9, volume=2.0))
        assert restored.is_complete is False

    def test_restore_incomplete_stays_incomplete(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        with pytest.raises(UnsupportedDealError):
            ledger.apply_deal(_deal(ticket=5, type=99))
        reason = ledger.incomplete_reason
        store.save(ledger, overwrite=True)
        restored = store.load()
        assert restored.is_complete is False
        assert restored.incomplete_reason == reason

    def test_restore_wrong_fingerprint_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        with pytest.raises(SnapshotAccountMismatchError):
            store.load(expected_fingerprint=FP_OTHER)


# --------------------------------------------------------------------------
# 8-14. Snapshots invalidos (estructura)
# --------------------------------------------------------------------------


class TestInvalidSnapshots:
    def test_missing_file(self, tmp_path):
        with pytest.raises(SnapshotNotFoundError):
            _store(tmp_path).load()

    def test_empty_or_truncated_file(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        store.path.write_text("", encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_invalid_json(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        store.path.write_text("{ no-json", encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_unsupported_schema_version(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["schema_version"] = STORE_SCHEMA_VERSION + 99
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotVersionError):
            store.load()

    def test_missing_fields_or_wrong_types(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        del raw["accounting"]["balance"]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_non_finite_values(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["accounting"]["balance"] = float("nan")
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_incoherent_positions(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0))
        store.save(ledger, overwrite=True)
        raw = _read(store)
        raw["accounting"]["positions"] = [
            {"position_id": 1, "symbol": "EURUSD", "quantity": 1.0, "avg_price": 1.1},
            {"position_id": 1, "symbol": "EURUSD", "quantity": 2.0, "avg_price": 1.2},
        ]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()


# --------------------------------------------------------------------------
# H3. Clases de evento imposibles / consistencia movimiento<->evento
# --------------------------------------------------------------------------


class TestH3EventKinds:
    def _base(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deposit(2, 500.0))
        store.save(ledger, overwrite=True)
        return store

    def test_processed_non_commercial_rejected(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["processed"].append({
            "kind": "NON_COMMERCIAL",
            "deal": _deal_dict(ticket=77, order=0, type=DEAL_TYPE_BONUS,
                               entry=None, position_id=None, symbol=None,
                               volume=0.0, price=0.0, profit=25.0),
        })
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_movement_trading_fill_rejected(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["movements"].append({
            "deal_key": [FP, 55], "kind": "TRADING_FILL",
            "amount": 0.0, "time": AT.isoformat(),
        })
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_movement_without_processed_rejected(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["movements"] = [{
            "deal_key": [FP, 999], "kind": "BALANCE_MOVEMENT",
            "amount": 1.0, "time": AT.isoformat(),
        }]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_processed_balance_without_movement_rejected(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["movements"] = []  # falta el movimiento del deposito
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_movement_amount_mismatch_rejected(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["movements"][0]["amount"] = 999.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()


# --------------------------------------------------------------------------
# H4. Reconstruccion del estado derivado
# --------------------------------------------------------------------------


class TestH4DerivedState:
    def _base(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=2.0, price=1.10))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=0.5, price=1.20, profit=0.05,
        ))
        ledger.apply_deal(_deposit(3, 500.0))
        store.save(ledger, overwrite=True)
        return store

    def _expect_corrupt(self, store):
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_position_quantity_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["positions"][0]["quantity"] = 99.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_position_id_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["positions"][0]["position_id"] = 999
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_position_avg_price_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["positions"][0]["avg_price"] = 9.99
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_realized_pnl_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["realized_pnl"] = 1234.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_balance_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["balance"] = 1.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_credit_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["credit"] = 5.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_movements_tampered(self, tmp_path):
        store = self._base(tmp_path)
        raw = _read(store)
        raw["accounting"]["movements"][0]["amount"] = 1.0
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)

    def test_processed_order_tampered(self, tmp_path):
        # Cierra y reabre: el orden altera el estado derivado.
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=1.0, price=1.20, profit=0.10,
        ))
        ledger.apply_deal(_deal(ticket=3, volume=0.5, price=1.30))
        store.save(ledger, overwrite=True)
        raw = _read(store)
        # Reordena [1, 2, 3] -> [1, 3, 2].
        raw["accounting"]["processed"] = [
            raw["accounting"]["processed"][0],
            raw["accounting"]["processed"][2],
            raw["accounting"]["processed"][1],
        ]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self._expect_corrupt(store)


# --------------------------------------------------------------------------
# H1. No-clobber / condicion de carrera
# --------------------------------------------------------------------------


class TestH1NoClobber:
    def test_concurrent_destination_not_clobbered(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = MT5AccountLedger(_baseline())
        real_write = store._write_temp

        def racing(tmp, payload):
            real_write(tmp, payload)
            store.path.write_text("CONCURRENT", encoding="utf-8")

        monkeypatch.setattr(store, "_write_temp", racing)
        with pytest.raises(SnapshotExistsError):
            store.save(ledger, overwrite=False)
        # El destino concurrente se conserva byte a byte.
        assert store.path.read_text(encoding="utf-8") == "CONCURRENT"
        assert not store.path.with_name(store.path.name + ".tmp").exists()


# --------------------------------------------------------------------------
# H2. Resultado de durabilidad
# --------------------------------------------------------------------------


class TestH2Durability:
    def test_temp_write_failure(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        monkeypatch.setattr(
            store, "_write_temp",
            lambda tmp, payload: (_ for _ in ()).throw(OSError("x")),
        )
        with pytest.raises(SnapshotWriteError):
            store.save(ledger, overwrite=True)

    def test_temp_fsync_failure(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        monkeypatch.setattr(
            os, "fsync",
            lambda fd: (_ for _ in ()).throw(OSError("fsync")),
        )
        with pytest.raises(SnapshotWriteError):
            store.save(ledger, overwrite=True)

    def test_failure_before_publish(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        before = store.path.read_text(encoding="utf-8")
        monkeypatch.setattr(
            store, "_publish",
            lambda tmp, path, *, overwrite: (_ for _ in ()).throw(OSError("pre")),
        )
        ledger.apply_deal(_deal(ticket=1, volume=1.0))
        with pytest.raises(SnapshotWriteError):
            store.save(ledger, overwrite=True)
        assert store.path.read_text(encoding="utf-8") == before

    def test_dir_fsync_unconfirmed_after_replace(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        monkeypatch.setattr(store, "_sync_dir", lambda: False)
        result = store.save(ledger, overwrite=True)
        assert result is PersistenceResult.REPLACED_DURABILITY_UNCONFIRMED
        # El destino YA fue sustituido.
        assert store.load().processed_deal_count() == 1

    def test_normal_confirmed(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        monkeypatch.setattr(store, "_sync_dir", lambda: True)
        result = store.save(ledger, overwrite=True)
        assert result is PersistenceResult.DURABLE


# --------------------------------------------------------------------------
# 15-18 (previos). Politica de fallos de escritura
# --------------------------------------------------------------------------


class TestWriteFailures:
    def test_write_temp_failure_leaves_previous_intact(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        before = store.path.read_text(encoding="utf-8")

        def boom(tmp, payload):
            raise OSError("disco lleno")

        monkeypatch.setattr(store, "_write_temp", boom)
        ledger.apply_deal(_deal(ticket=1, volume=1.0))
        with pytest.raises(SnapshotWriteError):
            store.save(ledger, overwrite=True)
        assert store.path.read_text(encoding="utf-8") == before
        assert store.load().processed_deal_count() == 0
        assert not store.path.with_name(store.path.name + ".tmp").exists()

    def test_deals_not_marked_applied_if_persist_fails(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        deal = _deal(ticket=7, volume=1.0)
        ledger.apply_deal(deal)
        assert ledger.processed_deal_count() == 1
        monkeypatch.setattr(
            store, "_publish",
            lambda tmp, path, *, overwrite: (_ for _ in ()).throw(OSError("nope")),
        )
        with pytest.raises(SnapshotWriteError):
            store.save(ledger, overwrite=True)
        reloaded = store.load()
        assert reloaded.processed_deal_count() == 0


# --------------------------------------------------------------------------
# 19-20 (previos) + H5. Bootstrap, sobrescritura y guarda destructiva
# --------------------------------------------------------------------------


class TestBootstrapAndOverwrite:
    def test_absent_or_invalid_baseline_cannot_create(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(MT5AccountingStoreError):
            store.create(None)  # type: ignore[arg-type]
        with pytest.raises(MT5AccountingStoreError):
            store.create("not-a-baseline")  # type: ignore[arg-type]
        assert not store.exists

    def test_cannot_silently_overwrite(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        with pytest.raises(SnapshotExistsError):
            store.create(_baseline())
        with pytest.raises(SnapshotExistsError):
            store.save(MT5AccountLedger(_baseline()))

    def test_overwrite_other_account_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        other = MT5AccountLedger(_baseline(account_fingerprint=FP_OTHER))
        with pytest.raises(SnapshotAccountMismatchError):
            store.save(other, overwrite=True)

    def test_snapshot_excludes_full_account_identifier(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = store.path.read_text(encoding="utf-8")
        assert ACCOUNT_ID not in raw
        assert "password" not in raw.lower()

    def test_empty_snapshot_overwrite_without_destructive_ok(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        ledger = store.create(_baseline(), overwrite=True)
        assert ledger.positions() == ()

    @pytest.mark.parametrize("seed", ["positions", "deals", "movements", "incomplete"])
    def test_stateful_snapshot_requires_destructive(self, tmp_path, seed):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        if seed == "positions":
            ledger.apply_deal(_deal(ticket=1, volume=1.0))
        elif seed == "deals":
            ledger.apply_deal(_deposit(2, 5.0))
        elif seed == "movements":
            ledger.apply_deal(_deposit(3, 7.0))
        else:
            with pytest.raises(UnsupportedDealError):
                ledger.apply_deal(_deal(ticket=4, type=99))
        store.save(ledger, overwrite=True)

        with pytest.raises(SnapshotNotEmptyError):
            store.create(_baseline(), overwrite=True)
        # Con confirmacion destructiva explicita SI se reemplaza por baseline vacia.
        fresh = store.create(_baseline(), overwrite=True, destructive=True)
        assert fresh.positions() == () and fresh.processed_deal_count() == 0

    def test_corrupt_snapshot_not_overwritten(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        store.path.write_text("{ corrupt", encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.create(_baseline(), overwrite=True)
        assert store.path.read_text(encoding="utf-8") == "{ corrupt"

    def test_other_account_not_replaceable_even_with_destructive(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        with pytest.raises(SnapshotAccountMismatchError):
            store.create(
                _baseline(account_fingerprint=FP_OTHER),
                overwrite=True, destructive=True,
            )


# --------------------------------------------------------------------------
# 21-22 (previos). Separacion spot y ausencia de readiness
# --------------------------------------------------------------------------


class TestSeparationAndNoReadiness:
    def test_spot_ledger_and_snapshot_unchanged(self):
        spot = AccountLedger(initial_cash=10000.0, account_currency="USD")
        spot.register_instrument(
            InstrumentSpec("EURUSD", contract_size=1.0, quote_currency="USD")
        )
        spot.apply_fill(
            instrument="EURUSD", side="BUY", quantity=1.0, price=100.0,
            client_order_id="S-1", fill_sequence=0,
        )
        assert spot.cash == pytest.approx(9900.0)
        snap = spot.snapshot()
        assert snap["cash"] == pytest.approx(9900.0)
        assert {"cash", "positions", "applied_fills", "specs"} <= set(snap)

    def test_restore_does_not_declare_readiness(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        restored = store.load()
        assert not hasattr(restored, "ready")
        assert restored.valuation_available() is False
        assert restored.floating_pnl is None


# --------------------------------------------------------------------------
# 05B: epoca contable declarada (origen + continuidad no verificada)
# --------------------------------------------------------------------------


class TestAccountingEpochPersistence:
    def test_create_declares_epoch_and_persists(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        assert ledger.origin is BaselineOrigin.DECLARED
        assert ledger.continuity_verified is False
        assert store.baseline_declared() is True
        loaded = store.load()
        assert loaded.origin is BaselineOrigin.DECLARED
        assert loaded.continuity_verified is False
        assert loaded.balance == pytest.approx(10000.0)
        assert loaded.account_currency == "USD"
        assert loaded.baseline.baseline_at == AT

    def test_declared_not_auto_verified_after_save_load(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        store.save(ledger, overwrite=True)
        loaded = store.load()
        assert loaded.origin is BaselineOrigin.DECLARED
        assert loaded.continuity_verified is False

    def test_create_declared_verified_rejected(self, tmp_path):
        with pytest.raises(AccountingEpochError):
            _store(tmp_path).create(_baseline(), continuity_verified=True)

    def test_create_reconstructed_rejected(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(MT5AccountingStoreError):
            store.create(_baseline(), origin=BaselineOrigin.RECONSTRUCTED)
        assert not store.exists  # no se crea snapshot para un origen no soportado

    def test_load_reconstructed_snapshot_rejected_unverified(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["accounting"]["epoch"] = {
            "origin": "RECONSTRUCTED", "continuity_verified": False,
        }
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):  # estado de epoca no permitido
            store.load()
        assert store.path.read_text(encoding="utf-8") != ""  # sin ledger vacio

    def test_load_reconstructed_snapshot_rejected_verified(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["accounting"]["epoch"] = {
            "origin": "RECONSTRUCTED", "continuity_verified": True,
        }
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_save_revalidates_and_rejects_reconstructed_epoch(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        # Mutacion via API normal: save() debe REVALIDAR (round-trip) el estado.
        ledger._epoch = SimpleNamespace(
            origin=BaselineOrigin.RECONSTRUCTED, continuity_verified=False
        )
        with pytest.raises(MT5AccountingStoreError):
            store.save(ledger, overwrite=True)
        # El snapshot en disco conserva el estado DECLARED valido.
        assert store.load().origin is BaselineOrigin.DECLARED

    def test_epoch_preserved_through_roundtrip(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        assert raw["accounting"]["epoch"] == {
            "origin": "DECLARED",
            "continuity_verified": False,
        }

    def test_old_accounting_state_version_rejected_as_version(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["accounting"]["state_version"] = 1
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotVersionError):
            store.load()

    def test_snapshot_missing_epoch_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        del raw["accounting"]["epoch"]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()


# --------------------------------------------------------------------------
# 05C-INGESTION: estado durable de ingestion y cursor (mismo snapshot)
# --------------------------------------------------------------------------

_TO = datetime(2026, 1, 2, tzinfo=UTC)


class TestIngestionStatePersistence:
    def test_create_has_no_successful_scan(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        assert ledger.ingestion.has_successful_scan is False
        loaded = store.load()
        assert loaded.ingestion.has_successful_scan is False
        assert loaded.ingestion.last_successful_scan_to is None
        assert loaded.ingestion.status is None

    def test_save_load_preserves_ingestion_interval(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger._ingestion = IngestionState(
            AT, _TO, IngestionScanStatus.COMPLETE
        )
        store.save(ledger, overwrite=True)
        loaded = store.load()
        assert loaded.ingestion.status is IngestionScanStatus.COMPLETE
        assert loaded.ingestion.last_successful_scan_from == AT
        assert loaded.ingestion.last_successful_scan_to == _TO
        # baseline/epoca/ledger preservados conjuntamente.
        assert loaded.balance == pytest.approx(10000.0)
        assert loaded.origin is BaselineOrigin.DECLARED
        assert loaded.continuity_verified is False

    def test_empty_confirmed_scan_preserved(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger._ingestion = IngestionState(
            AT, AT, IngestionScanStatus.EMPTY_CONFIRMED
        )
        store.save(ledger, overwrite=True)
        assert store.load().ingestion.status is IngestionScanStatus.EMPTY_CONFIRMED

    def test_snapshot_missing_ingestion_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        del raw["accounting"]["ingestion"]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            store.load()

    def test_old_state_version_2_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.create(_baseline())
        raw = _read(store)
        raw["accounting"]["state_version"] = 2
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SnapshotVersionError):
            store.load()

    def test_failed_save_preserves_previous_snapshot(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        # Ingesta parcial (invalida) via mutacion normal => save() revalida y falla.
        ledger._ingestion = SimpleNamespace(
            last_successful_scan_from=AT, last_successful_scan_to=None, status=None
        )
        with pytest.raises(MT5AccountingStoreError):
            store.save(ledger, overwrite=True)
        assert store.load().ingestion.has_successful_scan is False

    def test_ingestion_does_not_mark_continuity_verified(self, tmp_path):
        store = _store(tmp_path)
        ledger = store.create(_baseline())
        ledger._ingestion = IngestionState(
            AT, _TO, IngestionScanStatus.COMPLETE
        )
        store.save(ledger, overwrite=True)
        loaded = store.load()
        assert loaded.continuity_verified is False
        assert loaded.origin is BaselineOrigin.DECLARED
