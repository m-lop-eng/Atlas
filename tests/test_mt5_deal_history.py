"""ATLAS-MT5-ACCOUNTING-04B: API de historial de deals por rango (offline).

Mocks locales del modulo MetaTrader5. Sin terminal real, sin credenciales, sin
operaciones de trading. El ledger contable NO se integra aqui.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from brokers.models import HistoricalDeal
from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    HISTORY_INCOMPLETE,
    DealHistoryResult,
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5DealHistoryError,
    MT5Environment,
)

UTC = timezone.utc
FROM = datetime(2026, 1, 1, tzinfo=UTC)
TO = datetime(2026, 1, 2, tzinfo=UTC)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeMT5:
    """Mock minimo del modulo MetaTrader5 (sin terminal real)."""

    def __init__(self) -> None:
        self.terminal = SimpleNamespace(connected=True)
        self.account = SimpleNamespace(
            login=42, balance=10000.0, equity=10000.0, margin_free=9000.0,
            currency="USD", margin_mode=0, trade_allowed=True, trade_expert=True,
            trade_mode=0,
        )
        self.last_error_value = (0, "ok")
        self.deals = ()
        self.history_calls = 0
        self.last_args = None
        self.trading_calls: list[str] = []

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        return True

    def last_error(self):
        return self.last_error_value

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    def symbol_info(self, name):
        return None

    def symbol_select(self, name, enable):
        return True

    def history_deals_get(self, *args, **kwargs):
        self.history_calls += 1
        self.last_args = args
        result = self.deals
        if isinstance(result, BaseException):
            raise result
        return result

    def order_check(self, request):  # pragma: no cover - nunca debe invocarse
        self.trading_calls.append("order_check")
        raise AssertionError("order_check NO debe invocarse en esta fase")

    def order_send(self, request):  # pragma: no cover - nunca debe invocarse
        self.trading_calls.append("order_send")
        raise AssertionError("order_send NO debe invocarse en esta fase")


def _adapter(fake: FakeMT5) -> MT5BrokerAdapter:
    cfg = MT5Config(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={"EURUSD": "EURUSD"},
    )
    creds = MT5Credentials(login=42, password="not-a-real-password", server="demo")
    adapter = MT5BrokerAdapter(cfg, creds, mt5=fake, clock=lambda: NOW)
    adapter.connect()
    return adapter


def _row(**over) -> SimpleNamespace:
    fields = dict(
        ticket=1, order=10, time=1_700_000_000, time_msc=1_700_000_000_000,
        type=0, entry=0, position_id=500, symbol="EURUSD", volume=1.0,
        price=1.10, profit=0.0, commission=0.0, swap=0.0, fee=0.0,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------
# 1-4. Camino feliz y errores de consulta
# --------------------------------------------------------------------------


class TestQueryOutcomes:
    def test_valid_interval_complete_ordered(self):
        fake = FakeMT5()
        fake.deals = (
            _row(ticket=2, time=1_700_000_100, time_msc=1_700_000_100_000),
            _row(ticket=1, time=1_700_000_000, time_msc=1_700_000_000_000),
        )
        result = _adapter(fake).deal_history(FROM, TO)
        assert result is not None and result.status == HISTORY_COMPLETE
        assert [d.ticket for d in result.deals] == [1, 2]
        assert fake.history_calls == 1
        assert fake.trading_calls == []
        # Sin filtro de simbolo ni de orden (posicional date_from, date_to).
        assert fake.last_args == (FROM, TO)

    def test_empty_confirmed(self):
        fake = FakeMT5()
        fake.deals = ()
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_EMPTY_CONFIRMED
        assert result.deals == ()

    def test_none_is_incomplete_with_last_error(self):
        fake = FakeMT5()
        fake.deals = None
        fake.last_error_value = (4401, "history request failed")
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()
        assert "history request failed" in result.detail

    def test_exception_is_incomplete(self):
        fake = FakeMT5()
        fake.deals = RuntimeError("transporte")
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert "RuntimeError" in result.detail


# --------------------------------------------------------------------------
# 5. Intervalo / zonas horarias
# --------------------------------------------------------------------------


class TestIntervalValidation:
    def test_naive_from_rejected(self):
        fake = FakeMT5()
        with pytest.raises(MT5DealHistoryError):
            _adapter(fake).deal_history(datetime(2026, 1, 1), TO)
        assert fake.history_calls == 0

    def test_inverted_limits_rejected(self):
        fake = FakeMT5()
        with pytest.raises(MT5DealHistoryError):
            _adapter(fake).deal_history(TO, FROM)
        assert fake.history_calls == 0

    def test_non_datetime_rejected(self):
        fake = FakeMT5()
        with pytest.raises(MT5DealHistoryError):
            _adapter(fake).deal_history("2026-01-01", TO)

    def test_non_utc_is_converted_explicitly(self):
        fake = FakeMT5()
        fake.deals = ()
        tz_plus2 = timezone(timedelta(hours=2))
        frm = datetime(2026, 1, 1, 2, 0, tzinfo=tz_plus2)  # == 2026-01-01T00:00Z
        adapter = _adapter(fake)
        adapter.deal_history(frm, TO)
        sent_from, sent_to = fake.last_args
        assert sent_from == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        assert sent_from.utcoffset() == timedelta(0)
        assert sent_to.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# 6. Registros malformados
# --------------------------------------------------------------------------


class TestMalformedRecords:
    def test_missing_ticket_is_incomplete(self):
        fake = FakeMT5()
        row = _row()
        del row.ticket
        fake.deals = (row,)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()

    def test_missing_time_is_incomplete(self):
        fake = FakeMT5()
        row = _row()
        del row.time
        fake.deals = (row,)
        assert _adapter(fake).deal_history(FROM, TO).status == HISTORY_INCOMPLETE

    def test_non_numeric_profit_is_incomplete(self):
        fake = FakeMT5()
        fake.deals = (_row(profit="x"),)
        assert _adapter(fake).deal_history(FROM, TO).status == HISTORY_INCOMPLETE

    def test_non_finite_price_is_incomplete(self):
        fake = FakeMT5()
        fake.deals = (_row(price=float("inf")),)
        assert _adapter(fake).deal_history(FROM, TO).status == HISTORY_INCOMPLETE


# --------------------------------------------------------------------------
# 7-10. Preservacion, orden, duplicados y tipos desconocidos
# --------------------------------------------------------------------------


class TestRecordsSemantics:
    def test_mixed_symbols_and_order_zero_preserved(self):
        fake = FakeMT5()
        fake.deals = (
            _row(ticket=1, symbol="EURUSD", order=10, type=0, volume=1.0),
            _row(ticket=2, symbol=None, order=0, type=2, volume=0.0,
                 price=0.0, profit=500.0),
            _row(ticket=3, symbol="XAUUSD", order=11, type=0, volume=0.5),
        )
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        symbols = {d.symbol for d in result.deals}
        assert symbols == {"EURUSD", "XAUUSD", None}
        assert any(d.order == 0 for d in result.deals)  # no se filtra order==0

    def test_same_second_ordered_by_msc_then_ticket(self):
        fake = FakeMT5()
        fake.deals = (
            _row(ticket=7, time=1_700_000_000, time_msc=1_700_000_000_100),
            _row(ticket=5, time=1_700_000_000, time_msc=1_700_000_000_200),
            _row(ticket=6, time=1_700_000_000, time_msc=1_700_000_000_100),
        )
        result = _adapter(fake).deal_history(FROM, TO)
        assert [d.ticket for d in result.deals] == [6, 7, 5]

    def test_identical_duplicate_ticket_collapses(self):
        fake = FakeMT5()
        fake.deals = (_row(ticket=1), _row(ticket=1))
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        assert len(result.deals) == 1

    def test_contradictory_duplicate_ticket_is_incomplete(self):
        fake = FakeMT5()
        fake.deals = (_row(ticket=1, profit=0.0), _row(ticket=1, profit=9.9))
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()

    def test_unknown_deal_type_preserved_without_classification(self):
        fake = FakeMT5()
        fake.deals = (_row(ticket=9, type=777, entry=2, order=0, symbol=None),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        deal = result.deals[0]
        assert isinstance(deal, HistoricalDeal)
        assert deal.type == 777 and deal.entry == 2

    def test_fee_swap_commission_preserved(self):
        fake = FakeMT5()
        fake.deals = (_row(ticket=4, commission=-0.02, swap=-0.01, fee=-0.005),)
        deal = _adapter(fake).deal_history(FROM, TO).deals[0]
        assert deal.commission == pytest.approx(-0.02)
        assert deal.swap == pytest.approx(-0.01)
        assert deal.fee == pytest.approx(-0.005)


# --------------------------------------------------------------------------
# 11. Sin inicializacion MT5 real ni operaciones de trading
# --------------------------------------------------------------------------


class TestNoTradingNoRealTerminal:
    def test_no_trading_calls_and_injected_module_only(self):
        fake = FakeMT5()
        fake.deals = ()
        adapter = _adapter(fake)
        adapter.deal_history(FROM, TO)
        assert fake.trading_calls == []  # ni order_check ni order_send
        assert fake.history_calls == 1
        # El adaptador usa el modulo inyectado (mock), no un terminal real.
        assert adapter._module() is fake


# --------------------------------------------------------------------------
# H-1: conversiones estrictas de campos enteros (04D)
# --------------------------------------------------------------------------

_INT_FIELDS = ["ticket", "order", "time", "time_msc", "type", "entry", "position_id"]
# Booleano (True/False), float fraccionario, float integral, string numerico.
_BAD_INT_TYPES = [True, False, 2.5, 9.0, "7", "0"]
_VALID_INT = {
    "ticket": 7, "order": 3, "time": 1_700_000_000,
    "time_msc": 1_700_000_000_123, "type": 1, "entry": 1, "position_id": 500,
}


class TestStrictIntegerFields:
    @pytest.mark.parametrize("field", _INT_FIELDS)
    @pytest.mark.parametrize("bad", [None, *_BAD_INT_TYPES])
    def test_none_or_invalid_is_incomplete(self, field, bad):
        fake = FakeMT5()
        fake.deals = (_row(**{field: bad}),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()

    @pytest.mark.parametrize("field", _INT_FIELDS)
    def test_absent_is_incomplete(self, field):
        # Ausencia REAL del atributo (no presente con valor None).
        fake = FakeMT5()
        row = _row()
        delattr(row, field)
        fake.deals = (row,)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()

    @pytest.mark.parametrize("field", _INT_FIELDS)
    def test_valid_int_accepted(self, field):
        fake = FakeMT5()
        fake.deals = (_row(**{field: _VALID_INT[field]}),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        if field == "time":
            # `time` se expone como datetime UTC, no como entero.
            assert result.deals[0].time == datetime.fromtimestamp(
                _VALID_INT["time"], tz=UTC
            )
        else:
            assert getattr(result.deals[0], field) == _VALID_INT[field]

    def test_zero_values_preserved_for_all_fields(self):
        # Cer0 legitimo en order/position_id/entry/type/time_msc no se confunde
        # con ausencia.
        fake = FakeMT5()
        fake.deals = (_row(order=0, position_id=0, entry=0, type=0, time_msc=0),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        d = result.deals[0]
        assert d.order == 0 and d.position_id == 0
        assert d.entry == 0 and d.type == 0 and d.time_msc == 0

    def test_all_integer_fields_valid_accepted(self):
        fake = FakeMT5()
        fake.deals = (_row(
            ticket=42, order=0, time=1_700_000_123, time_msc=1_700_000_123_456,
            type=0, entry=1, position_id=0,
        ),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        deal = result.deals[0]
        assert deal.ticket == 42 and deal.order == 0
        assert deal.time_msc == 1_700_000_123_456
        assert deal.entry == 1 and deal.position_id == 0


# --------------------------------------------------------------------------
# H-2: importes numericos obligatorios (04D)
# --------------------------------------------------------------------------

_NUMERIC_FIELDS = ["volume", "price", "profit", "commission", "swap", "fee"]
_BAD_NUMERICS = [None, True, "1.0", float("nan"), float("inf"), float("-inf")]


class TestStrictNumericFields:
    @pytest.mark.parametrize("field", _NUMERIC_FIELDS)
    def test_finite_value_accepted(self, field):
        fake = FakeMT5()
        fake.deals = (_row(**{field: 1.25}),)
        assert _adapter(fake).deal_history(FROM, TO).status == HISTORY_COMPLETE

    @pytest.mark.parametrize("field", _NUMERIC_FIELDS)
    def test_zero_is_admissible(self, field):
        fake = FakeMT5()
        fake.deals = (_row(**{field: 0.0}),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        assert getattr(result.deals[0], field) == pytest.approx(0.0)

    @pytest.mark.parametrize("field", _NUMERIC_FIELDS)
    @pytest.mark.parametrize("bad", _BAD_NUMERICS)
    def test_invalid_numeric_is_incomplete(self, field, bad):
        fake = FakeMT5()
        fake.deals = (_row(**{field: bad}),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_INCOMPLETE
        assert result.deals == ()

    @pytest.mark.parametrize("field", _NUMERIC_FIELDS)
    def test_missing_numeric_is_incomplete(self, field):
        fake = FakeMT5()
        row = _row()
        delattr(row, field)
        fake.deals = (row,)
        assert _adapter(fake).deal_history(FROM, TO).status == HISTORY_INCOMPLETE

    def test_negative_values_preserved_without_positivity_rule(self):
        # El conversor NO impone positividad: conserva signos/valores del broker.
        fake = FakeMT5()
        fake.deals = (_row(commission=-0.02, swap=-0.01, fee=-0.005, profit=-1.5),)
        result = _adapter(fake).deal_history(FROM, TO)
        assert result.status == HISTORY_COMPLETE
        d = result.deals[0]
        assert d.profit == pytest.approx(-1.5)
        assert d.commission == pytest.approx(-0.02)
