"""ATLAS-MT5-ACCOUNTING-06C: tests offline del motor de reconciliacion de posiciones.

Deterministas, sin MT5 real ni conexion. Cubren identidad, disponibilidad,
integridad del ledger, politica HEDGING/NETTING y determinismo.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers.models import Position
from brokers.mt5_symbols import MT5SymbolResolver
from execution.mt5_accounting import MT5Position
from execution.mt5_position_reconciliation import (
    MT5CanonicalPosition,
    MT5PositionDiff,
    MT5PositionReconciliationError,
    MT5PositionSource,
    MT5PositionStatus,
    canonicalize_mt5_broker_positions,
    canonicalize_mt5_ledger_positions,
    reconcile_mt5_positions,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NETTING = 0
HEDGING = 2
# Resolver explicito (inyectivo): EURUSD/GBPUSD.
_RESOLVER = MT5SymbolResolver.from_mapping({"EURUSD": "EURUSD", "GBPUSD": "GBPUSD"})


def _broker(position_id, *, ticket="901", instrument="EURUSD", quantity=1.0, price=1.10,
            mt5_symbol=None):
    return Position(
        instrument=instrument, quantity=quantity, avg_entry_price=price,
        updated_at=NOW, ticket=ticket, position_id=position_id, mt5_symbol=mt5_symbol,
    )


def _ledger(position_id, *, symbol="EURUSD", quantity=1.0, price=1.10):
    return MT5Position(
        position_id=position_id, symbol=symbol, quantity=quantity, avg_price=price
    )


def _canon(pos):
    """Canonicaliza un objeto Position (broker) o MT5Position (ledger)."""
    if hasattr(pos, "symbol") and hasattr(pos, "avg_price"):
        return canonicalize_mt5_ledger_positions([pos], _RESOLVER)[0]
    return canonicalize_mt5_broker_positions([pos], _RESOLVER)[0]


def _src(*, positions, available=True, detail="", canonicalized=True):
    if not available or positions is None:
        return MT5PositionSource(
            positions=positions, available=available, detail=detail,
            canonicalized=canonicalized,
        )
    canon = tuple(_canon(p) for p in positions)
    return MT5PositionSource(
        positions=canon, available=True, detail=detail, canonicalized=canonicalized
    )


def _reconcile(broker, internal, *, mode=HEDGING, complete=True, durable=True,
               qt=0.0, pt=0.0):
    return reconcile_mt5_positions(
        broker, internal, account_mode=mode, ledger_complete=complete,
        ledger_durable=durable, quantity_tolerance=qt, price_tolerance=pt,
    )


def _classes(report):
    return [d.classification for d in report.diffs]


# ---------------------------------------------------------------- identidad


class TestIdentity:
    def test_matching_identity_reconciled(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[_ledger(777)]))
        assert r.status is MT5PositionStatus.RECONCILED
        assert r.reconciled is True
        assert _classes(r) == [MT5PositionStatus.RECONCILED]

    def test_ticket_change_preserves_identity(self):
        r = _reconcile(
            _src(positions=[_broker("777", ticket="901")]),
            _src(positions=[_ledger(777)]),
        )
        assert r.status is MT5PositionStatus.RECONCILED
        # ticket distinto no altera la identidad: el ledger no lleva ticket.

    def test_missing_identifier_unknown(self):
        r = _reconcile(_src(positions=[_broker(None)]), _src(positions=[_ledger(777)]))
        assert r.status is MT5PositionStatus.BLOCKED
        assert MT5PositionStatus.UNKNOWN in _classes(r)

    def test_invalid_identifier_unknown(self):
        r = _reconcile(_src(positions=[_broker("abc")]), _src(positions=[_ledger(777)]))
        assert r.status is MT5PositionStatus.BLOCKED
        assert MT5PositionStatus.UNKNOWN in _classes(r)

    def test_duplicate_identifier_blocks(self):
        r = _reconcile(
            _src(positions=[_broker("777"), _broker("777", ticket="902")]),
            _src(positions=[_ledger(777)]),
        )
        assert r.status is MT5PositionStatus.BLOCKED
        assert MT5PositionStatus.UNKNOWN in _classes(r)

    def test_duplicate_identifier_in_ledger_blocks(self):
        r = _reconcile(
            _src(positions=[_broker("777")]),
            _src(positions=[_ledger(777), _ledger(777)]),
        )
        assert r.status is MT5PositionStatus.BLOCKED

    def test_expected_missing_in_broker(self):
        r = _reconcile(_src(positions=[]), _src(positions=[_ledger(777)]))
        assert r.status is MT5PositionStatus.BLOCKED
        assert _classes(r) == [MT5PositionStatus.MISSING_BROKER]

    def test_observed_without_internal(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[]))
        assert r.status is MT5PositionStatus.BLOCKED
        assert _classes(r) == [MT5PositionStatus.MISSING_INTERNAL]

    def test_distinct_ids_same_attributes(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[_ledger(888)]))
        assert r.status is MT5PositionStatus.BLOCKED
        assert set(_classes(r)) == {
            MT5PositionStatus.MISSING_BROKER,
            MT5PositionStatus.MISSING_INTERNAL,
        }


# ---------------------------------------------------------------- atributos


class TestAttributes:
    def test_symbol_mismatch(self):
        r = _reconcile(
            _src(positions=[_broker("777", instrument="EURUSD")]),
            _src(positions=[_ledger(777, symbol="GBPUSD")]),
        )
        assert r.status is MT5PositionStatus.BLOCKED
        assert _classes(r) == [MT5PositionStatus.ATTRIBUTE_MISMATCH]

    def test_direction_mismatch(self):
        r = _reconcile(
            _src(positions=[_broker("777", quantity=1.0)]),
            _src(positions=[_ledger(777, quantity=-1.0)]),
        )
        assert r.status is MT5PositionStatus.BLOCKED
        assert _classes(r) == [MT5PositionStatus.ATTRIBUTE_MISMATCH]

    def test_volume_mismatch(self):
        r = _reconcile(
            _src(positions=[_broker("777", quantity=1.0)]),
            _src(positions=[_ledger(777, quantity=0.5)]),
        )
        assert r.status is MT5PositionStatus.BLOCKED

    def test_price_mismatch(self):
        r = _reconcile(
            _src(positions=[_broker("777", price=1.10)]),
            _src(positions=[_ledger(777, price=1.20)]),
        )
        assert r.status is MT5PositionStatus.BLOCKED

    def test_tolerance_can_admit_small_difference(self):
        broker = _src(positions=[_broker("777", quantity=1.0, price=1.1000)])
        internal = _src(positions=[_ledger(777, quantity=1.0001, price=1.1001)])
        assert _reconcile(broker, internal).status is MT5PositionStatus.BLOCKED
        assert (
            _reconcile(broker, internal, qt=0.001, pt=0.001).status
            is MT5PositionStatus.RECONCILED
        )

    def test_invalid_tolerance_rejected(self):
        broker = _src(positions=[])
        internal = _src(positions=[])
        with pytest.raises(MT5PositionReconciliationError):
            _reconcile(broker, internal, qt=-1.0)
        with pytest.raises(MT5PositionReconciliationError):
            _reconcile(broker, internal, pt=float("inf"))


# ------------------------------------------------------------- disponibilidad


class TestAvailability:
    def test_both_empty_valid_reconciled(self):
        r = _reconcile(_src(positions=[]), _src(positions=[]))
        assert r.status is MT5PositionStatus.RECONCILED
        assert r.diffs == ()

    def test_one_empty_other_not(self):
        r = _reconcile(_src(positions=[]), _src(positions=[_ledger(777)]))
        assert r.status is MT5PositionStatus.BLOCKED

    def test_none_response_blocked(self):
        r = _reconcile(
            MT5PositionSource(positions=None, available=True),
            _src(positions=[]),
        )
        assert r.status is MT5PositionStatus.BLOCKED

    def test_exception_source_blocked(self):
        r = _reconcile(
            MT5PositionSource(positions=None, available=False, detail="BrokerConnectionError"),
            _src(positions=[]),
        )
        assert r.status is MT5PositionStatus.BLOCKED
        assert r.broker_available is False

    def test_ledger_incomplete_blocks_even_if_matching(self):
        r = _reconcile(
            _src(positions=[_broker("777")]),
            _src(positions=[_ledger(777)]),
            complete=False,
        )
        assert r.status is MT5PositionStatus.BLOCKED
        assert r.ledger_trusted is False

    def test_ledger_not_durable_blocks(self):
        r = _reconcile(
            _src(positions=[_broker("777")]),
            _src(positions=[_ledger(777)]),
            durable=False,
        )
        assert r.status is MT5PositionStatus.BLOCKED


# ----------------------------------------------------------------- politica


class TestAccountPolicy:
    def test_hedging_verified_reconciled(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[_ledger(777)]), mode=HEDGING)
        assert r.status is MT5PositionStatus.RECONCILED

    def test_hedging_external_position_blocked(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[]), mode=HEDGING)
        assert r.status is MT5PositionStatus.BLOCKED
        assert _classes(r) == [MT5PositionStatus.MISSING_INTERNAL]

    def test_netting_flat_reconciled(self):
        r = _reconcile(_src(positions=[]), _src(positions=[]), mode=NETTING)
        assert r.status is MT5PositionStatus.RECONCILED

    def test_netting_with_positions_blocked(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[_ledger(777)]), mode=NETTING)
        assert r.status is MT5PositionStatus.BLOCKED
        assert "NETTING" in (r.blocked_reason or "")

    def test_unsupported_mode_blocked(self):
        r = _reconcile(_src(positions=[]), _src(positions=[]), mode=7)
        assert r.status is MT5PositionStatus.BLOCKED


# -------------------------------------------------------------- determinismo


class TestDeterminism:
    def test_order_independent(self):
        b1, b2 = _broker("777"), _broker("888", ticket="902")
        i1, i2 = _ledger(777), _ledger(888)
        r1 = _reconcile(_src(positions=[b1, b2]), _src(positions=[i1, i2]))
        r2 = _reconcile(_src(positions=[b2, b1]), _src(positions=[i2, i1]))
        assert r1 == r2
        assert r1.status is MT5PositionStatus.RECONCILED

    def test_report_has_no_secrets(self):
        r = _reconcile(_src(positions=[_broker("777")]), _src(positions=[_ledger(888)]))
        text = repr(r)
        assert "password" not in text.lower()


# ------------------------------------------- 06C-FIX: validacion numerica


def _unverified(report):
    return MT5PositionStatus.UNVERIFIED in [d.classification for d in report.diffs]


class TestNonFiniteValues:
    def test_broker_quantity_nan_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=float("nan"))]),
                       _src(positions=[_ledger(777, quantity=1.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_internal_quantity_nan_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=1.0)]),
                       _src(positions=[_ledger(777, quantity=float("nan"))]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_both_quantity_infinite_not_reconciled(self, value):
        r = _reconcile(_src(positions=[_broker("777", quantity=value)]),
                       _src(positions=[_ledger(777, quantity=value)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_both_price_non_finite_not_reconciled(self, value):
        r = _reconcile(_src(positions=[_broker("777", price=value)]),
                       _src(positions=[_ledger(777, price=value)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_opposite_infinite_prices_block(self):
        r = _reconcile(_src(positions=[_broker("777", price=float("inf"))]),
                       _src(positions=[_ledger(777, price=float("-inf"))]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)


class TestNonNumericValues:
    def test_broker_quantity_string_no_exception(self):
        r = _reconcile(_src(positions=[_broker("777", quantity="x")]),
                       _src(positions=[_ledger(777, quantity=1.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_internal_quantity_string_no_exception(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=1.0)]),
                       _src(positions=[_ledger(777, quantity="x")]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_broker_price_string_no_exception(self):
        r = _reconcile(_src(positions=[_broker("777", price="x")]),
                       _src(positions=[_ledger(777, price=1.10)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_internal_price_string_no_exception(self):
        r = _reconcile(_src(positions=[_broker("777", price=1.10)]),
                       _src(positions=[_ledger(777, price="x")]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_none_quantity_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=None)]),
                       _src(positions=[_ledger(777, quantity=1.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_bool_quantity_not_accepted_as_number(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=True)]),
                       _src(positions=[_ledger(777, quantity=1.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)


class TestZeroQuantity:
    def test_both_zero_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=0.0)]),
                       _src(positions=[_ledger(777, quantity=0.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_one_zero_other_nonzero_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=0.0)]),
                       _src(positions=[_ledger(777, quantity=1.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_negative_zero_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=0.0)]),
                       _src(positions=[_ledger(777, quantity=-0.0)]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)


class TestValidNumericRegression:
    def test_finite_valid_still_reconciled(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=1.0, price=1.10)]),
                       _src(positions=[_ledger(777, quantity=1.0, price=1.10)]))
        assert r.status is MT5PositionStatus.RECONCILED

    def test_finite_within_tolerance_reconciled(self):
        r = _reconcile(_src(positions=[_broker("777", quantity=1.0001, price=1.1001)]),
                       _src(positions=[_ledger(777, quantity=1.0, price=1.10)]),
                       qt=0.001, pt=0.001)
        assert r.status is MT5PositionStatus.RECONCILED


# ---------------------------------- 06E: contrato canonico de instrumentos


class TestCanonicalInstrumentContract:
    def test_instrument_none_both_blocks(self):
        b = _broker("777", instrument=None, mt5_symbol=None)
        i = _ledger(777, symbol="")
        r = _reconcile(_src(positions=[b]), _src(positions=[i]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_empty_instrument_blocks(self):
        r = _reconcile(_src(positions=[_broker("777", instrument="")]),
                       _src(positions=[_ledger(777, symbol="")]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_same_text_unmapped_not_equivalent(self):
        # broker sin mapping ("EURUSDx"); ledger "EURUSD": no hay equivalencia.
        b = _broker("777", instrument="EURUSD", mt5_symbol="EURUSDx")
        i = _ledger(777, symbol="EURUSD")
        r = _reconcile(_src(positions=[b]), _src(positions=[i]))
        assert r.status is MT5PositionStatus.BLOCKED and _unverified(r)

    def test_provenance_prefers_mt5_symbol(self):
        # La canonicalizacion usa el simbolo MT5 ORIGINAL (`mt5_symbol`), no el
        # campo `instrument` (aqui deliberadamente incorrecto).
        b = _broker("777", instrument="WRONG", mt5_symbol="EURUSD")
        i = _ledger(777, symbol="EURUSD")
        assert _reconcile(_src(positions=[b]), _src(positions=[i])).status is (
            MT5PositionStatus.RECONCILED
        )

    def test_non_canonicalized_source_with_positions_blocks(self):
        canon_b = MT5CanonicalPosition("777", "EURUSD", 1.0, 1.10)
        canon_i = MT5CanonicalPosition("777", "EURUSD", 1.0, 1.10)
        broker = MT5PositionSource((canon_b,), True, canonicalized=False)
        internal = MT5PositionSource((canon_i,), True, canonicalized=False)
        r = _reconcile(broker, internal)
        assert r.status is MT5PositionStatus.BLOCKED
        assert "canonicalizar" in (r.blocked_reason or "")

    def test_canonicalized_sources_reconcile(self):
        canon_b = MT5CanonicalPosition("777", "EURUSD", 1.0, 1.10)
        canon_i = MT5CanonicalPosition("777", "EURUSD", 1.0, 1.10)
        broker = MT5PositionSource((canon_b,), True, canonicalized=True)
        internal = MT5PositionSource((canon_i,), True, canonicalized=True)
        assert _reconcile(broker, internal).status is MT5PositionStatus.RECONCILED

    def test_non_canonicalized_empty_still_reconciled(self):
        broker = MT5PositionSource((), True, canonicalized=False)
        internal = MT5PositionSource((), True, canonicalized=False)
        assert _reconcile(broker, internal).status is MT5PositionStatus.RECONCILED

    def test_default_source_is_fail_closed(self):
        # `canonicalized` por defecto False => fuente con posiciones bloquea.
        canon = MT5CanonicalPosition("777", "EURUSD", 1.0, 1.10)
        src = MT5PositionSource((canon,), True)  # sin canonicalized
        assert src.canonicalized is False
        r = _reconcile(src, MT5PositionSource((canon,), True, canonicalized=True))
        assert r.status is MT5PositionStatus.BLOCKED
