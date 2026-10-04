"""Tests de F-06C: ACCOUNT esperado (expected) desde el AccountLedger.

Contrato:
  expected.account = { ledger.cash , ledger.valuation(marks).equity }
  actual.account   = broker.get_account_state()

ANTES de F-06C el expected se tomaba del propio broker -> ACCOUNT tautologico
(siempre MATCH). Ahora el expected procede del ledger interno (autoridad
contable) y el actual del broker observado; ambos son fuentes INDEPENDIENTES y
una divergencia real produce ACCOUNT MISMATCH -> RECONCILIATION_BLOCKED.

Estos tests comprueban VALORES y FUENTES, no solo `summary.ok`.
"""

from __future__ import annotations

import pytest

from reconciliation.models import DiscrepancyClass, Domain, ReconciliationStatus
from reconciliation.engine import ReconciliationEngine

from tests.test_paper_trading import (  # noqa: E402  (fixtures compartidas)
    HAPPY,
    INSTRUMENT,
    LEVEL,
    _LevelLong,
    _drive,
    _stack,
)


def _account_diffs(report):
    return tuple(d for d in report.diffs if d.domain is Domain.ACCOUNT)


def _acct_field(report, field):
    key = f"account/{field}"
    return next(d for d in report.diffs if d.key == key)


# --------------------------------------------------------------------------
# Fuente del expected: ledger (no broker)
# --------------------------------------------------------------------------


class TestExpectedSourceIsLedger:
    def test_expected_cash_comes_from_ledger(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        internal = env.engine._internal_snapshot()
        assert internal.account.cash == env.engine.ledger.cash
        # El ledger es la fuente, no el broker.
        assert internal.account.cash == 999_750.0

    def test_expected_equity_comes_from_ledger_valuation(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        marks = env.engine._marks()
        val = env.engine.ledger.valuation(marks)
        internal = env.engine._internal_snapshot()
        assert internal.account.equity == val.equity
        # Sin posicion abierta: equity == cash del ledger.
        assert internal.account.equity == env.engine.ledger.cash

    def test_expected_equity_with_open_position_uses_ledger_and_mark(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])  # deja posicion LONG abierta
        assert env.engine.position is not None
        marks = env.engine._marks()
        val = env.engine.ledger.valuation(marks)
        internal = env.engine._internal_snapshot()
        # equity = cash + qty*mark, calculado por el ledger (no por el broker).
        assert internal.account.equity == pytest.approx(val.equity)
        pos = env.engine.ledger.position(INSTRUMENT)
        expected_eq = env.engine.ledger.cash + pos.quantity * marks[INSTRUMENT]
        assert internal.account.equity == pytest.approx(expected_eq)

    def test_expected_account_id_is_internal_config(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        internal = env.engine._internal_snapshot()
        assert internal.account.account_id == env.engine.config.account_id


# --------------------------------------------------------------------------
# Independencia: cambiar el broker observado NO cambia el expected
# --------------------------------------------------------------------------


class TestIndependence:
    def test_mutating_broker_observed_does_not_change_expected(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        before = env.engine._internal_snapshot().account

        # Modificar SOLO el estado observed del broker.
        env.broker._cash += 12_345.0

        after = env.engine._internal_snapshot().account
        # El expected (ledger) permanece identico.
        assert after.cash == before.cash
        assert after.equity == before.equity
        # El actual (broker) SI cambio.
        observed = env.broker.get_account_state()
        assert observed.cash == before.cash + 12_345.0

    def test_expected_does_not_read_broker_account_state(self, monkeypatch):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)

        def _boom():  # pragma: no cover - solo debe invocarse el lado observed
            raise AssertionError(
                "expected NO debe consultar broker.get_account_state()"
            )

        # `_internal_snapshot` (expected) no debe tocar el broker.
        monkeypatch.setattr(env.broker, "get_account_state", _boom)
        internal = env.engine._internal_snapshot()
        assert internal.account is not None
        assert internal.account.cash == env.engine.ledger.cash


# --------------------------------------------------------------------------
# ACCOUNT match / mismatch
# --------------------------------------------------------------------------


class TestAccountMatchMismatch:
    def test_account_match_when_ledger_and_broker_consistent(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        report = env.engine.reconcile()
        acct = _account_diffs(report)
        assert all(d.classification is DiscrepancyClass.MATCH for d in acct)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK

    def test_account_mismatch_by_cash(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        expected_cash = env.engine.ledger.cash

        env.broker._cash += 1_000.0  # solo observed
        report = env.engine.reconcile()

        cash = _acct_field(report, "cash")
        assert cash.classification is DiscrepancyClass.MISMATCH
        assert cash.internal_value == expected_cash  # expected = ledger
        assert cash.broker_value == expected_cash + 1_000.0  # actual = broker
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
        assert report.trading_allowed is False

    def test_account_mismatch_by_equity_only(self, monkeypatch):
        from datetime import datetime, timezone

        from brokers.models import AccountState

        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        expected = env.engine._internal_snapshot().account
        observed = env.broker.get_account_state()

        # Sólo el equity observed diverge; el cash observed coincide.
        diverged = AccountState(
            account_id=observed.account_id,
            cash=observed.cash,
            equity=observed.equity + 777.0,
            buying_power=observed.buying_power,
            currency=observed.currency,
            as_of=datetime.now(timezone.utc),
        )
        monkeypatch.setattr(env.broker, "get_account_state", lambda: diverged)

        report = env.engine.reconcile()
        equity = _acct_field(report, "equity")
        assert equity.classification is DiscrepancyClass.MISMATCH
        assert equity.internal_value == pytest.approx(expected.equity)  # ledger
        assert equity.broker_value == pytest.approx(expected.equity + 777.0)
        # El cash NO divergió -> sólo hay diff de equity.
        assert not any(d.key == "account/cash" for d in report.diffs)
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED

    def test_matching_legitimate_still_passes_with_open_position(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:4])  # posicion abierta, sin salida
        report = env.engine.reconcile()
        acct = _account_diffs(report)
        assert all(d.classification is DiscrepancyClass.MATCH for d in acct)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK


# --------------------------------------------------------------------------
# Tolerancia (se conserva la del ReconciliationEngine)
# --------------------------------------------------------------------------


class TestTolerance:
    def test_within_tolerance_matches(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        tol = env.reconciliation.tolerance
        env.broker._cash += tol / 2.0  # dentro de tolerancia
        report = env.engine.reconcile()
        acct = _account_diffs(report)
        assert all(d.classification is DiscrepancyClass.MATCH for d in acct)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK

    def test_beyond_tolerance_mismatches(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY)
        # `_approx` usa rel_tol=abs_tol=tolerance; con cash ~1e6 la banda
        # relativa es ~1e-3. Un delta de 1.0 excede claramente la tolerancia.
        env.broker._cash += 1.0
        report = env.engine.reconcile()
        cash = _acct_field(report, "cash")
        assert cash.classification is DiscrepancyClass.MISMATCH
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED

    def test_tolerance_unchanged_default(self):
        assert ReconciliationEngine().tolerance == 1e-9


# --------------------------------------------------------------------------
# Valuation unavailable -> account=None (UNKNOWN), sin fabricar equity
# --------------------------------------------------------------------------


class TestValuationUnavailable:
    def test_open_position_without_valid_mark_yields_no_expected(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        assert env.engine.position is not None
        # Invalidar el mark del runtime (precio <= 0 -> valuation None).
        env.engine._set_price(0.0)
        internal = env.engine._internal_snapshot()
        assert internal.account is None  # no se fabrica equity ni se usa broker

    def test_no_expected_makes_account_unknown(self):
        env = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env, HAPPY[:3])
        env.engine._set_price(-1.0)  # mark invalido
        # Reconciliar directamente con el snapshot interno (account=None) frente
        # a un broker disponible -> ACCOUNT UNKNOWN (no MATCH falso).
        from reconciliation.orchestrator import reconcile_with_broker

        internal = env.engine._internal_snapshot()
        assert internal.account is None
        report = reconcile_with_broker(env.reconciliation, internal, env.broker)
        acct = _account_diffs(report)
        assert any(d.classification is DiscrepancyClass.UNKNOWN for d in acct)
        assert report.status is not ReconciliationStatus.RECONCILIATION_OK


# --------------------------------------------------------------------------
# Restart: consistente -> MATCH ; divergente -> MISMATCH
# --------------------------------------------------------------------------


class TestRestartAccountReconciliation:
    def test_restart_consistent_yields_account_match(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        # Mismo mark del runtime tras restaurar (reinyectado por _mark/bar).
        env2.engine._set_price(float(HAPPY[-1][3]))
        report = env2.engine.reconcile()
        acct = _account_diffs(report)
        assert all(d.classification is DiscrepancyClass.MATCH for d in acct)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK

    def test_restart_divergent_broker_yields_account_mismatch(self):
        env1 = _stack(strategy=_LevelLong(level=LEVEL))
        _drive(env1, HAPPY)
        snap = env1.engine.snapshot()
        # Broker restaurado con estado observed distinto al ledger restaurado.
        snap["broker"]["cash"] = snap["ledger"]["cash"] + 9_999.0

        env2 = _stack(strategy=_LevelLong(level=LEVEL))
        env2.engine.load_snapshot(snap)
        env2.engine._set_price(float(HAPPY[-1][3]))
        report = env2.engine.reconcile()

        cash = _acct_field(report, "cash")
        assert cash.classification is DiscrepancyClass.MISMATCH
        assert cash.internal_value == env2.engine.ledger.cash  # expected ledger
        assert cash.broker_value == env2.engine.ledger.cash + 9_999.0
        assert report.status is ReconciliationStatus.RECONCILIATION_BLOCKED
