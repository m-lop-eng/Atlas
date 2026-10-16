"""ATLAS-MT5-ACCOUNTING-02/02B: modelo contable MT5 Forex/CFD (offline).

Solo dobles locales y valores explicitos. Sin conexion a MT5, sin credenciales,
sin `LiveExecutionRuntime`. El ledger spot (`AccountLedger`) NO se modifica.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution.instrument import InstrumentSpec
from execution.mt5_accounting import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_INOUT,
    DEAL_ENTRY_OUT,
    DEAL_ENTRY_OUT_BY,
    DEAL_TYPE_BALANCE,
    DEAL_TYPE_BONUS,
    DEAL_TYPE_BUY,
    DEAL_TYPE_BUY_CANCELED,
    DEAL_TYPE_CREDIT,
    DEAL_TYPE_SELL,
    MT5_ACCOUNTING_SCHEMA_VERSION,
    MT5_ACCOUNTING_STATE_VERSION,
    MT5AccountBaseline,
    MT5AccountLedger,
    MT5AccountingError,
    MT5Deal,
    AccountingIncompleteError,
    BaselineError,
    DealAccountMismatchError,
    DealEventKind,
    DealIdentityCollisionError,
    InvalidDealError,
    UnsupportedDealError,
    account_fingerprint,
    classify_deal,
    AccountingEpoch,
    AccountingEpochError,
    BaselineOrigin,
    IngestionScanStatus,
    IngestionState,
    IngestionStateError,
    MT5AccountStateVersionError,
)
from papertrading.ledger import AccountLedger

UTC = timezone.utc
BASELINE_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
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
        baseline_at=BASELINE_AT,
        coverage_from=BASELINE_AT,
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
        time=BASELINE_AT,
    )
    fields.update(over)
    if "time_msc" not in fields:
        t = fields.get("time")
        fields["time_msc"] = (
            int(t.timestamp()) * 1000 if isinstance(t, datetime) else 0
        )
    return MT5Deal(**fields)


def _ledger(**over):
    return MT5AccountLedger(_baseline(**over))


def _state(ledger):
    """Estado CONTABLE (excluye `is_complete`, que cambia legitimamente al
    bloquear un evento)."""
    return (
        ledger.balance,
        ledger.credit,
        ledger.realized_pnl,
        ledger.positions(),
        ledger.processed_deal_count(),
    )


# --------------------------------------------------------------------------
# 1. Baseline estricta
# --------------------------------------------------------------------------


class TestBaseline:
    def test_valid_baseline_constructs_ledger(self):
        ledger = _ledger()
        assert ledger.balance == pytest.approx(10000.0)
        assert ledger.credit == pytest.approx(0.0)
        assert ledger.account_currency == "USD"

    def test_ledger_requires_explicit_baseline(self):
        with pytest.raises(BaselineError):
            MT5AccountLedger(None)  # type: ignore[arg-type]

    def test_default_epoch_is_declared_unverified(self):
        ledger = _ledger()
        assert ledger.origin is BaselineOrigin.DECLARED
        assert ledger.continuity_verified is False
        assert ledger.epoch.continuity_verified is False

    def test_declared_cannot_claim_verified(self):
        with pytest.raises(AccountingEpochError):
            AccountingEpoch(BaselineOrigin.DECLARED, continuity_verified=True)

    def test_epoch_origin_coercion_and_validation(self):
        assert AccountingEpoch("DECLARED").origin is BaselineOrigin.DECLARED
        with pytest.raises(AccountingEpochError):
            AccountingEpoch("NOPE")
        with pytest.raises(AccountingEpochError):
            AccountingEpoch(BaselineOrigin.DECLARED, continuity_verified="yes")  # type: ignore[arg-type]

    def test_snapshot_roundtrip_preserves_epoch_and_baseline_state(self):
        ledger = _ledger()
        snap = ledger.to_snapshot()
        assert snap["state_version"] == MT5_ACCOUNTING_STATE_VERSION
        assert snap["epoch"] == {"origin": "DECLARED", "continuity_verified": False}
        restored = MT5AccountLedger.from_snapshot(snap)
        assert restored.origin is BaselineOrigin.DECLARED
        assert restored.continuity_verified is False
        assert restored.balance == pytest.approx(ledger.balance)
        assert restored.credit == pytest.approx(ledger.credit)
        assert restored.account_fingerprint == ledger.account_fingerprint
        assert restored.account_currency == ledger.account_currency
        assert restored.baseline.baseline_at == ledger.baseline.baseline_at

    def test_snapshot_missing_epoch_rejected(self):
        snap = _ledger().to_snapshot()
        del snap["epoch"]
        with pytest.raises(AccountingEpochError):
            MT5AccountLedger.from_snapshot(snap)

    def test_old_state_version_rejected(self):
        snap = _ledger().to_snapshot()
        snap["state_version"] = 1
        with pytest.raises(MT5AccountStateVersionError):
            MT5AccountLedger.from_snapshot(snap)

    def test_is_complete_semantics_independent_of_epoch(self):
        ledger = _ledger()
        assert ledger.is_complete is True
        with pytest.raises(UnsupportedDealError):
            ledger.apply_deal(_deal(ticket=1, type=99))
        assert ledger.is_complete is False
        assert ledger.origin is BaselineOrigin.DECLARED
        assert ledger.continuity_verified is False

    def test_reconstructed_rejected_regardless_of_continuity(self):
        with pytest.raises(AccountingEpochError):
            AccountingEpoch(BaselineOrigin.RECONSTRUCTED, continuity_verified=False)
        with pytest.raises(AccountingEpochError):
            AccountingEpoch(BaselineOrigin.RECONSTRUCTED, continuity_verified=True)

    def test_from_snapshot_reconstructed_rejected(self):
        snap = _ledger().to_snapshot()
        snap["epoch"] = {"origin": "RECONSTRUCTED", "continuity_verified": False}
        with pytest.raises(AccountingEpochError):
            MT5AccountLedger.from_snapshot(snap)
        snap["epoch"] = {"origin": "RECONSTRUCTED", "continuity_verified": True}
        with pytest.raises(AccountingEpochError):
            MT5AccountLedger.from_snapshot(snap)


_TO = datetime(2026, 1, 2, tzinfo=UTC)


class TestIngestionState:
    def _ledger_ing(self, state):
        return MT5AccountLedger(_baseline(), ingestion=state)

    def test_initial_has_no_successful_scan(self):
        ledger = _ledger()
        ing = ledger.ingestion
        assert ing.has_successful_scan is False
        assert ing.last_successful_scan_from is None
        assert ing.last_successful_scan_to is None
        assert ing.status is None
        assert ledger.to_snapshot()["ingestion"] == {
            "last_successful_scan_from": None,
            "last_successful_scan_to": None,
            "status": None,
        }

    def test_roundtrip_complete_interval(self):
        state = IngestionState(BASELINE_AT, _TO, IngestionScanStatus.COMPLETE)
        restored = MT5AccountLedger.from_snapshot(self._ledger_ing(state).to_snapshot())
        assert restored.ingestion.has_successful_scan is True
        assert restored.ingestion.status is IngestionScanStatus.COMPLETE
        assert restored.ingestion.last_successful_scan_from == BASELINE_AT
        assert restored.ingestion.last_successful_scan_to == _TO

    def test_roundtrip_empty_confirmed(self):
        state = IngestionState(BASELINE_AT, BASELINE_AT, IngestionScanStatus.EMPTY_CONFIRMED)
        restored = MT5AccountLedger.from_snapshot(self._ledger_ing(state).to_snapshot())
        assert restored.ingestion.status is IngestionScanStatus.EMPTY_CONFIRMED

    def test_utc_normalization(self):
        tz2 = timezone(timedelta(hours=2))
        state = IngestionState(
            datetime(2026, 1, 1, 2, 0, tzinfo=tz2),
            datetime(2026, 1, 1, 3, 0, tzinfo=tz2),
            IngestionScanStatus.COMPLETE,
        )
        assert state.last_successful_scan_from == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        assert state.last_successful_scan_to == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    def test_naive_rejected(self):
        with pytest.raises(IngestionStateError):
            IngestionState(datetime(2026, 1, 1), _TO, IngestionScanStatus.COMPLETE)

    def test_from_after_to_rejected(self):
        with pytest.raises(IngestionStateError):
            IngestionState(_TO, BASELINE_AT, IngestionScanStatus.COMPLETE)

    def test_partial_or_incoherent_fields_rejected(self):
        with pytest.raises(IngestionStateError):
            IngestionState(last_successful_scan_from=BASELINE_AT)
        with pytest.raises(IngestionStateError):
            IngestionState(last_successful_scan_to=BASELINE_AT)
        with pytest.raises(IngestionStateError):
            IngestionState(status=IngestionScanStatus.COMPLETE)
        with pytest.raises(IngestionStateError):  # intervalo sin status
            IngestionState(BASELINE_AT, BASELINE_AT, None)

    def test_invalid_status_rejected(self):
        with pytest.raises(IngestionStateError):
            IngestionState(BASELINE_AT, BASELINE_AT, "INCOMPLETE")
        with pytest.raises(IngestionStateError):
            IngestionState(BASELINE_AT, BASELINE_AT, 1)

    def test_status_coerced_from_string(self):
        assert (
            IngestionState(BASELINE_AT, BASELINE_AT, "COMPLETE").status
            is IngestionScanStatus.COMPLETE
        )

    def test_ingestion_does_not_change_is_complete_or_continuity(self):
        state = IngestionState(BASELINE_AT, _TO, IngestionScanStatus.COMPLETE)
        ledger = self._ledger_ing(state)
        assert ledger.is_complete is True
        assert ledger.continuity_verified is False

    def test_snapshot_missing_ingestion_rejected(self):
        snap = _ledger().to_snapshot()
        del snap["ingestion"]
        with pytest.raises(IngestionStateError):
            MT5AccountLedger.from_snapshot(snap)

    def test_snapshot_invalid_ingestion_rejected(self):
        snap = _ledger().to_snapshot()
        snap["ingestion"] = {
            "last_successful_scan_from": None,
            "last_successful_scan_to": None,
            "status": "INCOMPLETE",
        }
        with pytest.raises(IngestionStateError):
            MT5AccountLedger.from_snapshot(snap)

    @pytest.mark.parametrize("over", [
        {"schema_version": 99},
        {"account_fingerprint": ""},
        {"account_fingerprint": "zzz"},
        {"server": ""},
        {"server": 123},
        {"account_currency": ""},
        {"account_currency": "usd"},
        {"account_currency": 840},
        {"login_masked": ""},
        {"login_masked": "1000001"},
        {"login_masked": 123},
        {"baseline_at": datetime(2026, 1, 1)},
        {"baseline_at": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))},
        {"coverage_from": datetime(2025, 1, 1, tzinfo=UTC)},
        {"coverage_from": datetime(2025, 1, 1)},
        {"balance": float("nan")},
        {"balance": float("inf")},
        {"credit": float("nan")},
        {"credit": -1.0},
        {"leverage": "100"},
        {"leverage": True},
        {"trade_mode": "0"},
        {"margin_mode": True},
    ])
    def test_invalid_baseline_rejected(self, over):
        with pytest.raises(BaselineError):
            _baseline(**over)

    def test_negative_balance_allowed_but_credit_not(self):
        # Politica: balance negativo = estado observado legitimo (sin clamp);
        # credit negativo = rechazado (dominio propio).
        assert _baseline(balance=-500.0).balance == pytest.approx(-500.0)
        with pytest.raises(BaselineError):
            _baseline(credit=-0.01)


# --------------------------------------------------------------------------
# F-3/F-4. Identidad de cuenta y vinculacion deal-cuenta
# --------------------------------------------------------------------------


class TestAccountIdentity:
    def test_same_server_and_mask_distinct_ids_differ(self):
        assert account_fingerprint(server=SERVER, account_id="1000001") != account_fingerprint(
            server=SERVER, account_id="1000002"
        )

    def test_fingerprint_stable_across_restart(self):
        first = account_fingerprint(server=SERVER, account_id=ACCOUNT_ID)
        second = account_fingerprint(server=SERVER, account_id=ACCOUNT_ID)
        assert first == second == FP

    def test_deal_from_other_account_rejected_no_mutation(self):
        ledger = _ledger()
        before = _state(ledger)
        with pytest.raises(DealAccountMismatchError):
            ledger.apply_deal(_deal(account_fingerprint=FP_OTHER))
        assert _state(ledger) == before
        assert ledger.is_complete is False

    def test_deal_without_fingerprint_rejected(self):
        ledger = _ledger()
        with pytest.raises(DealAccountMismatchError):
            ledger.apply_deal(_deal(account_fingerprint=""))
        assert ledger.is_complete is False
        assert ledger.processed_deal_count() == 0

    def test_deal_with_invalid_fingerprint_type_rejected(self):
        ledger = _ledger()
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(_deal(account_fingerprint=123))  # type: ignore[arg-type]
        assert ledger.is_complete is False

    def test_reused_ticket_across_accounts_no_collision(self):
        a = MT5AccountLedger(_baseline(account_fingerprint=FP))
        b = MT5AccountLedger(_baseline(account_fingerprint=FP_OTHER))
        assert a.apply_deal(_deal(ticket=7, account_fingerprint=FP)).applied is True
        assert b.apply_deal(_deal(ticket=7, account_fingerprint=FP_OTHER)).applied is True

    def test_same_ticket_identical_payload_idempotent(self):
        ledger = _ledger()
        deal = _deal(ticket=21)
        assert ledger.apply_deal(deal).applied is True
        second = ledger.apply_deal(deal)
        assert second.applied is False
        assert ledger.processed_deal_count() == 1
        assert ledger.balance == pytest.approx(10000.0)

    def test_same_ticket_different_payload_conflicts_and_blocks(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=22, volume=1.0))
        before = _state(ledger)
        with pytest.raises(DealIdentityCollisionError):
            ledger.apply_deal(_deal(ticket=22, volume=2.0))
        assert ledger.balance == before[0]
        assert ledger.is_complete is False


# --------------------------------------------------------------------------
# F-1/F-2. Validacion de importes y atomicidad
# --------------------------------------------------------------------------


_NON_FINITE = [float("nan"), float("inf"), float("-inf")]


class TestAtomicityNonFinite:
    @pytest.mark.parametrize("field", ["profit", "commission", "swap", "fee"])
    @pytest.mark.parametrize("bad", _NON_FINITE)
    def test_open_with_non_finite_money_rejected_atomically(self, field, bad):
        ledger = _ledger()
        before = _state(ledger)
        deal = _deal(ticket=1, **{field: bad})
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(deal)
        # Sin mutacion, sin marcar como aplicado, modelo incompleto.
        assert _state(ledger) == before
        assert ledger.is_complete is False
        assert ledger.processed_deal_count() == 0

    @pytest.mark.parametrize("field", ["profit", "commission", "swap", "fee"])
    def test_failure_after_initial_classification_not_duplicated(self, field):
        # La validacion monetaria ocurre DESPUES de clasificar como TRADING_FILL:
        # demuestra que la atomicidad no depende solo del clasificador.
        ledger = _ledger()
        bad = _deal(ticket=2, **{field: float("nan")})
        assert classify_deal(bad).kind is DealEventKind.TRADING_FILL
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(bad)
        assert ledger.positions() == ()  # NO se creo posicion parcial
        assert ledger.balance == pytest.approx(10000.0)
        # Reintento: bloqueado por incompleto (no duplica).
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(bad)

    @pytest.mark.parametrize("field", ["commission", "swap", "fee"])
    def test_close_with_non_finite_cost_rejected_atomically(self, field):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        before = _state(ledger)
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(_deal(
                ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                volume=1.0, price=1.20, profit=0.10, **{field: float("inf")},
            ))
        assert _state(ledger) == before
        assert len(ledger.positions()) == 1
        assert ledger.is_complete is False

    @pytest.mark.parametrize("bad", _NON_FINITE)
    def test_balance_movement_non_finite_rejected(self, bad):
        ledger = _ledger()
        before = _state(ledger)
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=3, order=0, type=DEAL_TYPE_BALANCE, entry=None,
                position_id=None, symbol=None, volume=0.0, price=0.0, profit=bad,
            ))
        assert _state(ledger) == before
        assert ledger.is_complete is False

    @pytest.mark.parametrize("bad", ["1.0", True, object()])
    def test_non_numeric_money_rejected(self, bad):
        ledger = _ledger()
        with pytest.raises(MT5AccountingError):
            ledger.apply_deal(_deal(ticket=4, profit=bad))
        assert ledger.is_complete is False


# --------------------------------------------------------------------------
# F-1/F-2 (cont.) + contabilidad de trading
# --------------------------------------------------------------------------


class TestTradingAccounting:
    def test_open_does_not_debit_notional_from_balance(self):
        ledger = _ledger()
        out = ledger.apply_deal(_deal())
        assert out.applied is True
        assert ledger.balance == pytest.approx(10000.0)
        pos = ledger.positions()
        assert len(pos) == 1 and pos[0].quantity == pytest.approx(1.0)
        assert ledger.realized_pnl == pytest.approx(0.0)

    def test_increase_updates_volume_and_average(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deal(ticket=2, volume=0.5, price=1.20))
        pos = ledger.positions()[0]
        assert pos.quantity == pytest.approx(1.5)
        assert pos.avg_price == pytest.approx((1.0 * 1.10 + 0.5 * 1.20) / 1.5)

    def test_partial_reduce_keeps_avg_and_books_profit(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=2.0, price=1.10))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=0.5, price=1.20, profit=0.05,
        ))
        pos = ledger.positions()[0]
        assert pos.quantity == pytest.approx(1.5)
        assert pos.avg_price == pytest.approx(1.10)
        assert ledger.realized_pnl == pytest.approx(0.05)
        assert ledger.balance == pytest.approx(10000.05)

    def test_full_close_removes_position(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=1.0, price=1.20, profit=0.10,
        ))
        assert ledger.positions() == ()
        assert ledger.balance == pytest.approx(10000.10)

    def test_short_open_and_close(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, type=DEAL_TYPE_SELL, volume=2.0, price=1.20))
        pos = ledger.positions()[0]
        assert pos.quantity == pytest.approx(-2.0)
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_OUT,
            volume=0.5, price=1.10, profit=0.05,
        ))
        assert ledger.positions()[0].quantity == pytest.approx(-1.5)
        assert ledger.realized_pnl == pytest.approx(0.05)

    def test_commission_swap_fee_once_with_reported_signs(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0, price=1.10, commission=-0.02))
        ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=1.0, price=1.20, profit=0.10, commission=-0.02,
            swap=-0.01, fee=-0.005,
        ))
        assert ledger.balance == pytest.approx(10000.0 - 0.02 + 0.10 - 0.02 - 0.01 - 0.005)
        again = ledger.apply_deal(_deal(
            ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
            volume=1.0, price=1.20, profit=0.10, commission=-0.02,
            swap=-0.01, fee=-0.005,
        ))
        assert again.applied is False
        assert ledger.balance == pytest.approx(10000.0 - 0.02 + 0.10 - 0.02 - 0.01 - 0.005)

    def test_in_incoherent_sign_blocks(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, type=DEAL_TYPE_SELL, volume=1.0))
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(ticket=2, type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN, volume=1.0))
        assert ledger.is_complete is False

    def test_out_wrong_direction_blocks(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0))
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(ticket=2, entry=DEAL_ENTRY_OUT, volume=1.0))
        assert ledger.is_complete is False

    def test_out_exceeding_volume_blocks(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1, volume=1.0))
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=2, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT, volume=2.0,
            ))
        assert ledger.is_complete is False

    def test_out_without_position_blocks_without_mutation(self):
        ledger = _ledger()
        before = _state(ledger)
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=3, type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT, volume=1.0,
            ))
        assert _state(ledger) == before
        assert ledger.is_complete is False


# --------------------------------------------------------------------------
# Movimientos de balance / credito
# --------------------------------------------------------------------------


class TestMovements:
    def test_deposit_and_withdrawal_are_movements_not_fills(self):
        ledger = _ledger()
        out = ledger.apply_deal(_deal(
            ticket=10, order=0, type=DEAL_TYPE_BALANCE, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=500.0,
        ))
        assert out.kind is DealEventKind.BALANCE_MOVEMENT
        assert ledger.balance == pytest.approx(10500.0)
        assert ledger.positions() == ()
        ledger.apply_deal(_deal(
            ticket=11, order=0, type=DEAL_TYPE_BALANCE, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=-200.0,
        ))
        assert ledger.balance == pytest.approx(10300.0)
        assert len(ledger.movement_records()) == 2

    def test_credit_kept_separate_from_balance(self):
        ledger = _ledger()
        out = ledger.apply_deal(_deal(
            ticket=12, order=0, type=DEAL_TYPE_CREDIT, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=100.0,
        ))
        assert out.kind is DealEventKind.CREDIT_MOVEMENT
        assert ledger.credit == pytest.approx(100.0)
        assert ledger.balance == pytest.approx(10000.0)

    def test_balance_with_unexpected_fields_is_incomplete(self):
        ledger = _ledger()
        before = _state(ledger)
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=13, order=0, type=DEAL_TYPE_BALANCE, entry=None,
                position_id=None, symbol=None, volume=0.0, price=0.0,
                profit=10.0, swap=-0.01,
            ))
        assert ledger.balance == before[0]
        assert ledger.is_complete is False
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(ticket=14))

    def test_deal_order_zero_non_commercial_is_not_a_fill(self):
        event = classify_deal(_deal(
            ticket=17, order=0, type=DEAL_TYPE_BALANCE, entry=None,
            position_id=None, symbol=None, volume=0.0, price=0.0, profit=50.0,
        ))
        assert event.kind is DealEventKind.BALANCE_MOVEMENT


# --------------------------------------------------------------------------
# F-7. Eventos rechazados nunca marcados como aplicados
# --------------------------------------------------------------------------


class TestRejectedEvents:
    def test_non_commercial_not_marked_applied(self):
        ledger = _ledger()
        before = _state(ledger)
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=15, order=0, type=DEAL_TYPE_BONUS, entry=None,
                position_id=None, symbol=None, volume=0.0, price=0.0, profit=25.0,
            ))
        assert ledger.processed_deal_count() == 0  # NO aplicado
        assert ledger.balance == before[0]
        assert ledger.is_complete is False
        # Reintento: NO se trata como duplicado exitosamente aplicado.
        with pytest.raises(AccountingIncompleteError):
            ledger.apply_deal(_deal(
                ticket=15, order=0, type=DEAL_TYPE_BONUS, entry=None,
                position_id=None, symbol=None, volume=0.0, price=0.0, profit=25.0,
            ))

    def test_fill_then_balance_same_ticket_conflict(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=30, volume=1.0))
        before_balance = ledger.balance
        with pytest.raises(DealIdentityCollisionError):
            ledger.apply_deal(_deal(
                ticket=30, order=0, type=DEAL_TYPE_BALANCE, entry=None,
                position_id=None, symbol=None, volume=0.0, price=0.0, profit=99.0,
            ))
        assert ledger.balance == before_balance
        assert ledger.is_complete is False

    def test_unsupported_type_never_marked(self):
        ledger = _ledger()
        with pytest.raises(UnsupportedDealError):
            ledger.apply_deal(_deal(ticket=31, type=99))
        assert ledger.processed_deal_count() == 0
        assert ledger.is_complete is False


# --------------------------------------------------------------------------
# Clasificador: bloqueos
# --------------------------------------------------------------------------


class TestClassificationBlocks:
    def test_trading_fill_without_position_id_blocks(self):
        with pytest.raises(UnsupportedDealError):
            _ledger().apply_deal(_deal(position_id=None))

    @pytest.mark.parametrize("over", [
        {"volume": 0.0},
        {"volume": -1.0},
        {"volume": float("nan")},
        {"price": 0.0},
        {"price": -1.0},
        {"volume": "1.0"},
        {"symbol": 123},
    ])
    def test_invalid_volume_price_symbol_rejected(self, over):
        with pytest.raises(MT5AccountingError):
            _ledger().apply_deal(_deal(**over))

    @pytest.mark.parametrize("entry", [DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY])
    def test_inout_and_out_by_entries_blocked(self, entry):
        with pytest.raises(UnsupportedDealError):
            _ledger().apply_deal(_deal(entry=entry))

    def test_canceled_deal_blocked(self):
        with pytest.raises(UnsupportedDealError):
            _ledger().apply_deal(_deal(type=DEAL_TYPE_BUY_CANCELED))

    def test_malformed_ticket_or_time_rejected(self):
        with pytest.raises(MT5AccountingError):
            _ledger().apply_deal(_deal(ticket="1"))  # type: ignore[arg-type]
        with pytest.raises(MT5AccountingError):
            _ledger().apply_deal(_deal(time=datetime(2026, 1, 1)))


# --------------------------------------------------------------------------
# F-9. Tolerancia de reconciliacion
# --------------------------------------------------------------------------


class TestReconciliationTolerance:
    def test_balance_match_is_equity_unverified(self):
        ledger = _ledger()
        status = ledger.observe_broker_account(
            balance=10000.0, equity=10000.0, credit=0.0, margin=0.0,
            margin_free=10000.0, currency="USD",
        )
        assert status.value == "BALANCE_MATCH_EQUITY_UNVERIFIED"

    def test_mismatch_outside_tolerance(self):
        ledger = _ledger()
        status = ledger.observe_broker_account(
            balance=10000.01, equity=10000.0, credit=0.0, margin=0.0,
            margin_free=10000.0, currency="USD",
        )
        assert status.value == "BALANCE_MISMATCH"

    def test_within_default_tolerance_not_mismatch(self):
        ledger = _ledger()
        status = ledger.observe_broker_account(
            balance=10000.0 + 1e-8, equity=0.0, credit=0.0, margin=0.0,
            margin_free=0.0, currency="USD",
        )
        assert status.value == "BALANCE_MATCH_EQUITY_UNVERIFIED"

    def test_large_amounts_relative_tolerance(self):
        ledger = MT5AccountLedger(
            _baseline(balance=1_000_000_000.0),
            reconciliation_tolerance_abs=1e-9,
            reconciliation_tolerance_rel=1e-12,
        )
        status = ledger.observe_broker_account(
            balance=1_000_000_000.0 + 1e-4, equity=0.0, credit=0.0, margin=0.0,
            margin_free=0.0, currency="USD",
        )
        assert status.value == "BALANCE_MATCH_EQUITY_UNVERIFIED"

    def test_small_amounts_discrepancy_outside_tolerance(self):
        ledger = MT5AccountLedger(
            _baseline(balance=0.5),
            reconciliation_tolerance_abs=1e-9, reconciliation_tolerance_rel=1e-9,
        )
        status = ledger.observe_broker_account(
            balance=0.5 + 1e-6, equity=0.0, credit=0.0, margin=0.0,
            margin_free=0.0, currency="USD",
        )
        assert status.value == "BALANCE_MISMATCH"


# --------------------------------------------------------------------------
# Separacion del modelo spot
# --------------------------------------------------------------------------


class TestModelSeparation:
    def test_mt5_ledger_is_not_spot_ledger(self):
        ledger = _ledger()
        assert isinstance(ledger, AccountLedger) is False
        assert not hasattr(AccountLedger, "apply_deal")

    def test_spot_ledger_rejects_mt5_event(self):
        spot = AccountLedger(initial_cash=10000.0, account_currency="USD")
        with pytest.raises(TypeError):
            spot.apply_fill(deal=_deal())  # type: ignore[call-arg]

    def test_spot_snapshot_semantics_unchanged(self):
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
        spot.apply_fill(
            instrument="EURUSD", side="BUY", quantity=1.0, price=100.0,
            client_order_id="S-1", fill_sequence=0,
        )
        assert spot.cash == pytest.approx(9900.0)


class TestDealTimeMsc:
    """05D-SCAN-APPLY: `time_msc` en la identidad/estructura persistida del deal."""

    _BASE_MS = int(BASELINE_AT.timestamp()) * 1000

    def test_time_msc_roundtrip_and_equality(self):
        deal = _deal(ticket=1, time=BASELINE_AT, time_msc=self._BASE_MS + 250)
        ledger = _ledger()
        ledger.apply_deal(deal)
        snap = ledger.to_snapshot()
        assert snap["processed"][0]["deal"]["time_msc"] == self._BASE_MS + 250
        restored = MT5AccountLedger.from_snapshot(snap)
        stored, _kind = restored.processed_deals_in_order()[0]
        assert stored == deal
        assert stored.time_msc == self._BASE_MS + 250

    def test_incoherent_time_msc_rejected(self):
        with pytest.raises(InvalidDealError):
            _ledger().apply_deal(
                _deal(time=BASELINE_AT, time_msc=self._BASE_MS + 1000)
            )

    def test_non_integer_time_msc_rejected(self):
        with pytest.raises(InvalidDealError):
            _ledger().apply_deal(_deal(time_msc=1.5))  # type: ignore[arg-type]
        with pytest.raises(InvalidDealError):
            _ledger().apply_deal(_deal(time_msc=True))  # type: ignore[arg-type]

    def test_missing_time_msc_in_snapshot_rejected(self):
        ledger = _ledger()
        ledger.apply_deal(_deal(ticket=1))
        snap = ledger.to_snapshot()
        del snap["processed"][0]["deal"]["time_msc"]
        with pytest.raises(MT5AccountingError):
            MT5AccountLedger.from_snapshot(snap)

    def test_state_version_3_rejected(self):
        snap = _ledger().to_snapshot()
        snap["state_version"] = 3
        with pytest.raises(MT5AccountStateVersionError):
            MT5AccountLedger.from_snapshot(snap)

    def test_state_version_2_rejected(self):
        snap = _ledger().to_snapshot()
        snap["state_version"] = 2
        with pytest.raises(MT5AccountStateVersionError):
            MT5AccountLedger.from_snapshot(snap)
