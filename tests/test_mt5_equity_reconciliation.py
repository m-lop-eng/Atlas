"""ATLAS-MT5-ACCOUNTING-06H: tests offline del esqueleto de reconciliacion de equity.

Deterministas, sin MT5 real. Validan los contratos y el comportamiento conservador
(NO que el calculo economico este completo).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from execution.mt5_position_reconciliation import MT5PositionStatus
from execution.mt5_equity_reconciliation import (
    MT5EquityReconciliationError,
    MT5EquityReconciliationReport,
    MT5EquityReconciliationRequest,
    MT5EquityReconciliationStatus,
    reconcile_mt5_equity,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)
RECONCILED = MT5PositionStatus.RECONCILED


def _request(**over):
    fields = dict(
        account_identity_verified=True,
        ledger_complete=True,
        ledger_durable=True,
        positions_status=RECONCILED,
        observed_balance=10_000.0,
        observed_credit=0.0,
        observed_equity=10_000.0,
        account_currency="USD",
        clock=lambda: NOW,
    )
    fields.update(over)
    return MT5EquityReconciliationRequest(**fields)


# ------------------------------------------------------------------ contratos


class TestContracts:
    def test_valid_request_and_report_construction(self):
        report = reconcile_mt5_equity(_request())
        assert isinstance(report, MT5EquityReconciliationReport)
        assert report.status is MT5EquityReconciliationStatus.EQUITY_UNVERIFIED

    def test_status_enum_values(self):
        assert {s.value for s in MT5EquityReconciliationStatus} == {
            "EQUITY_RECONCILED",
            "EQUITY_MISMATCH",
            "EQUITY_UNVERIFIED",
            "BALANCE_MISMATCH",
            "BLOCKED",
        }

    def test_optional_fields_are_none_not_zero(self):
        report = reconcile_mt5_equity(_request(observed_equity=None))
        assert report.expected_equity is None
        assert report.difference is None
        assert report.tolerance is None
        assert report.observed_equity is None

    def test_report_equality_and_repr(self):
        a = reconcile_mt5_equity(_request())
        b = reconcile_mt5_equity(_request())
        assert a == b
        assert "password" not in repr(a).lower()

    def test_reconciled_property_false(self):
        assert reconcile_mt5_equity(_request()).reconciled is False

    def test_invalid_request_object_raises(self):
        with pytest.raises(MT5EquityReconciliationError):
            reconcile_mt5_equity(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------- motor


class TestSkeletonFailClosed:
    def test_valuation_pending_returns_unverified(self):
        report = reconcile_mt5_equity(_request())
        assert report.status is MT5EquityReconciliationStatus.EQUITY_UNVERIFIED
        assert report.expected_equity is None
        assert report.difference is None
        assert report.reason_code == "VALUATION_NOT_IMPLEMENTED"
        assert "equity_valuation_policy_not_implemented" in report.missing_inputs

    def test_never_reconciled_or_mismatch_in_06h(self):
        for over in ({}, {"observed_equity": 0.0}, {"observed_equity": 999_999.0}):
            status = reconcile_mt5_equity(_request(**over)).status
            assert status not in (
                MT5EquityReconciliationStatus.EQUITY_RECONCILED,
                MT5EquityReconciliationStatus.EQUITY_MISMATCH,
            )

    def test_coincident_balance_credit_is_not_equity_reconciliation(self):
        # Balance/credito coincidentes NO producen reconciliacion de equity.
        report = reconcile_mt5_equity(
            _request(observed_balance=10_000.0, observed_credit=0.0)
        )
        assert report.status is MT5EquityReconciliationStatus.EQUITY_UNVERIFIED

    def test_observed_equity_reported_when_valid(self):
        report = reconcile_mt5_equity(_request(observed_equity=12_345.5))
        assert report.observed_equity == pytest.approx(12_345.5)

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), True, "x", object()]
    )
    def test_invalid_observed_equity_blocks(self, value):
        report = reconcile_mt5_equity(_request(observed_equity=value))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert "observed_equity_invalid" in report.missing_inputs
        assert report.observed_equity is None

    def test_invalid_observed_balance_blocks(self):
        report = reconcile_mt5_equity(_request(observed_balance=float("nan")))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert "observed_balance_invalid" in report.missing_inputs


class TestPreconditions:
    def test_account_identity_not_verified_blocks(self):
        report = reconcile_mt5_equity(_request(account_identity_verified=False))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert "account_identity_not_verified" in report.missing_inputs

    def test_ledger_incomplete_blocks(self):
        report = reconcile_mt5_equity(_request(ledger_complete=False))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert report.ledger_trusted is False

    def test_ledger_not_durable_blocks(self):
        report = reconcile_mt5_equity(_request(ledger_durable=False))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert report.ledger_trusted is False

    def test_complete_and_durable_is_trusted(self):
        report = reconcile_mt5_equity(_request())
        assert report.ledger_trusted is True

    @pytest.mark.parametrize(
        "status",
        [
            None,
            MT5PositionStatus.ATTRIBUTE_MISMATCH,
            MT5PositionStatus.MISSING_BROKER,
            MT5PositionStatus.MISSING_INTERNAL,
            MT5PositionStatus.UNKNOWN,
            MT5PositionStatus.UNVERIFIED,
            MT5PositionStatus.BLOCKED,
        ],
    )
    def test_positions_not_reconciled_blocks(self, status):
        report = reconcile_mt5_equity(_request(positions_status=status))
        assert report.status is MT5EquityReconciliationStatus.BLOCKED
        assert "positions_not_reconciled" in report.missing_inputs

    def test_non_bool_precondition_raises(self):
        with pytest.raises(MT5EquityReconciliationError):
            reconcile_mt5_equity(_request(account_identity_verified=1))  # type: ignore[arg-type]


class TestDeterminismAndPurity:
    def test_same_inputs_same_report(self):
        req = _request()
        assert reconcile_mt5_equity(req) == reconcile_mt5_equity(req)

    def test_explicit_clock_sets_as_of_utc(self):
        naive = lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        report = reconcile_mt5_equity(_request(clock=naive))
        assert report.as_of == naive()

    def test_no_clock_as_of_none(self):
        report = reconcile_mt5_equity(_request(clock=None))
        assert report.as_of is None

    def test_naive_clock_is_contract_error(self):
        with pytest.raises(MT5EquityReconciliationError):
            reconcile_mt5_equity(_request(clock=lambda: datetime(2026, 1, 1)))

    def test_marks_and_fx_not_used_in_skeleton(self):
        report = reconcile_mt5_equity(
            _request(marks=({"symbol": "X"},), fx_rates=({"pair": "A/B"},))
        )
        assert report.marks_used == ()
        assert report.fx_used == ()
