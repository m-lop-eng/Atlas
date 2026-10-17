"""ATLAS-MT5-ACCOUNTING-06B: preservacion de POSITION_IDENTIFIER en snapshots.

Offline. Verifica que la identidad ESTABLE de posicion MT5 se conserva desde la
observacion (`Position`) hasta el `PositionSnapshot` usado por la reconciliacion,
SIN cambiar el motor de reconciliacion ni los caminos SPOT/genericos.
"""

from __future__ import annotations

from datetime import datetime, timezone

from brokers.models import AccountState, OrderNotFoundError, Position
from reconciliation.models import (
    BrokerSnapshot,
    InternalSnapshot,
    PositionSnapshot,
    ReconciliationStatus,
)
from reconciliation.orchestrator import broker_state_from_adapter
from reconciliation.engine import ReconciliationEngine

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeAdapter:
    """Adapter minimo para `broker_state_from_adapter` (solo lectura)."""

    def __init__(self, positions=()):
        self._positions = list(positions)

    def health(self) -> bool:
        return True

    def get_open_orders(self):
        return ()

    def get_order(self, client_order_id):
        raise OrderNotFoundError(client_order_id)

    def get_positions(self):
        return list(self._positions)

    def get_account_state(self) -> AccountState:
        return AccountState("DEMO-1", 10_000.0, 10_000.0, 0.0, "USD", NOW)


def _position(*, position_id, ticket, instrument="EURUSD", quantity=1.0, price=1.10):
    return Position(
        instrument=instrument, quantity=quantity, avg_entry_price=price,
        updated_at=NOW, ticket=ticket, position_id=position_id,
    )


def _broker_snapshot(adapter) -> BrokerSnapshot:
    return broker_state_from_adapter(adapter)


class TestIdentityPreserved:
    def test_position_id_preserved_into_snapshot(self):
        adapter = _FakeAdapter([_position(position_id="777", ticket="901")])
        snap = _broker_snapshot(adapter).positions[0]
        assert snap.position_id == "777"

    def test_ticket_distinct_from_identifier(self):
        pos = _position(position_id="777", ticket="901")
        assert pos.ticket == "901"
        assert pos.position_id == "777"
        snap = _broker_snapshot(_FakeAdapter([pos])).positions[0]
        # El snapshot conserva la identidad estable, no el ticket operativo.
        assert snap.position_id == "777"

    def test_ticket_change_preserves_identity(self):
        before = _broker_snapshot(
            _FakeAdapter([_position(position_id="777", ticket="901")])
        ).positions[0]
        after = _broker_snapshot(
            _FakeAdapter([_position(position_id="777", ticket="902")])
        ).positions[0]
        assert before.position_id == after.position_id == "777"

    def test_missing_identifier_not_invented(self):
        adapter = _FakeAdapter([_position(position_id=None, ticket="901")])
        snap = _broker_snapshot(adapter).positions[0]
        assert snap.position_id is None


class TestBackCompatAndGeneric:
    def test_positional_construction_back_compat(self):
        snap = PositionSnapshot("EURUSD", 1.0, 1.10)  # 3 posicionales
        assert snap.position_id is None

    def test_spot_default_none(self):
        snap = PositionSnapshot("EURUSD", 1.0, 1.10)
        assert snap.position_id is None

    def test_equality_default_unchanged(self):
        assert PositionSnapshot("EURUSD", 1.0, 1.10) == PositionSnapshot(
            "EURUSD", 1.0, 1.10
        )
        assert PositionSnapshot("EURUSD", 1.0, 1.10) != PositionSnapshot(
            "EURUSD", 1.0, 1.10, "777"
        )

    def test_position_id_in_repr_when_set(self):
        assert "777" in repr(PositionSnapshot("EURUSD", 1.0, 1.10, "777"))


class TestEngineUnchanged:
    def test_engine_ignores_position_id_for_matching(self):
        # Lado interno (SPOT, sin identidad) vs broker (MT5, con identidad):
        # el motor empareja por instrumento y NO compara position_id.
        internal = InternalSnapshot(
            positions=(PositionSnapshot("EURUSD", 1.0, 1.10),)
        )
        broker = BrokerSnapshot(
            positions=(PositionSnapshot("EURUSD", 1.0, 1.10, "777"),)
        )
        report = ReconciliationEngine().reconcile(internal, broker)
        assert report.status is ReconciliationStatus.RECONCILIATION_OK
        assert report.trading_allowed is True
