"""ATLAS-MT5-ACCOUNTING-06F: tests offline de la API read-only de marcas MT5.

Sin MT5 real ni conexion: usa FakeMT5 y `symbol_info_tick` simulado. Cubre
resolucion, precios, timestamps, ausencia de sustituciones y aislamiento.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_mt5_execution_foundation import INSTRUMENT, NOW, FakeMT5, _fake

from brokers.mt5 import (
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
    MT5MarketMark,
    MT5MarketMarkDataError,
    MT5MarketMarkError,
    MT5MarketMarkSymbolError,
    MT5MarketMarkUnavailableError,
)
from brokers.models import BrokerConnectionError

UTC = timezone.utc
EPOCH = 1_700_000_000  # segundos


def _mk(
    fake: FakeMT5,
    symbol_map,
    *,
    clock=lambda: NOW,
    monotonic=lambda: 0.0,
    connect=True,
) -> MT5BrokerAdapter:
    cfg = MT5Config(
        provider="mt5", environment=MT5Environment.DEMO, symbol_map=symbol_map
    )
    creds = MT5Credentials(login=42, password="not-a-real-password", server="demo")
    adapter = MT5BrokerAdapter(
        cfg, creds, mt5=fake, clock=clock, monotonic=monotonic
    )
    if connect:
        adapter.connect()
    return adapter


def _tick(fake, symbol, **over):
    fields = dict(bid=1.0999, ask=1.1001)
    fields.update(over)
    fake.ticks[symbol] = SimpleNamespace(**fields)


class TestResolution:
    def test_mapped_instrument_returns_mark(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, time_msc=EPOCH * 1000)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        mark = adapter.read_market_mark(INSTRUMENT)
        assert isinstance(mark, MT5MarketMark)
        assert mark.atlas_instrument == INSTRUMENT
        assert mark.mt5_symbol == INSTRUMENT
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)

    def test_suffix_symbol_only_via_explicit_mapping(self):
        fake = _fake()
        fake.add_symbol("EURUSD.x")
        _tick(fake, "EURUSD.x", time=EPOCH)
        adapter = _mk(fake, {"EURUSD": "EURUSD.x"})
        mark = adapter.read_market_mark("EURUSD")
        assert mark.atlas_instrument == "EURUSD"
        assert mark.mt5_symbol == "EURUSD.x"  # simbolo MT5 exacto conservado

    @pytest.mark.parametrize("bad", ["NOPE", "", "   "])
    def test_unmapped_or_empty_instrument_typed_error(self, bad):
        adapter = _mk(_fake(), {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkSymbolError):
            adapter.read_market_mark(bad)
        assert issubclass(MT5MarketMarkSymbolError, MT5MarketMarkError)

    def test_no_fallback_to_raw_text(self):
        # "EURUSD" no mapeado NO debe resolverse por texto.
        adapter = _mk(_fake(), {"OTHER": "OTHER"})
        with pytest.raises(MT5MarketMarkSymbolError):
            adapter.read_market_mark("EURUSD")

    def test_requires_ready_session(self):
        adapter = _mk(_fake(), {INSTRUMENT: INSTRUMENT}, connect=False)
        with pytest.raises(BrokerConnectionError):
            adapter.read_market_mark(INSTRUMENT)


class TestPrices:
    def test_last_absent_not_substituted(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)  # sin last
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        mark = adapter.read_market_mark(INSTRUMENT)
        assert mark.last is None
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)

    def test_last_present_and_zero_is_not_absent(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, last=0.0, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.last == 0.0  # cero valido != None

    def test_all_prices_absent_is_data_error(self):
        fake = _fake()
        fake.ticks[INSTRUMENT] = SimpleNamespace(time=EPOCH)  # sin bid/ask/last
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                       True, "x", object()])
    def test_invalid_bid_rejected(self, value):
        fake = _fake()
        _tick(fake, INSTRUMENT, bid=value, time=EPOCH)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_none_response_is_unavailable(self):
        fake = _fake()
        fake.ticks.pop(INSTRUMENT, None)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkUnavailableError):
            adapter.read_market_mark(INSTRUMENT)

    def test_api_exception_is_unavailable(self):
        fake = _fake()

        def boom(_name):
            raise RuntimeError("transporte")

        fake.symbol_info_tick = boom
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkUnavailableError):
            adapter.read_market_mark(INSTRUMENT)


class TestTimestamps:
    def test_tick_time_seconds(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC)
        assert mark.tick_time.tzinfo is not None
        assert mark.received_at == NOW  # hora local inyectada

    def test_tick_time_msc_precision_authoritative(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, time_msc=EPOCH * 1000 + 250)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC).replace(
            microsecond=250_000
        )

    def test_incoherent_time_fields_rejected(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, time_msc=(EPOCH + 1) * 1000)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    @pytest.mark.parametrize("value", ["x", 0, -5, True, 1.5])
    def test_invalid_timestamp_rejected(self, value):
        fake = _fake()
        _tick(fake, INSTRUMENT, time_msc=value)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_absent_timestamp_is_none_not_substituted(self):
        fake = _fake()
        _tick(fake, INSTRUMENT)  # solo bid/ask, sin time
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time is None
        assert mark.received_at == NOW  # la hora local NO sustituye al tick
        assert mark.tick_time != mark.received_at

    def test_require_tick_time_fails_when_absent(self):
        fake = _fake()
        _tick(fake, INSTRUMENT)  # sin time
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT, require_tick_time=True)

    def test_received_at_uses_injected_clock(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}, clock=lambda: frozen).read_market_mark(
            INSTRUMENT
        )
        assert mark.received_at == frozen


class TestIsolation:
    def test_no_order_or_trading_calls(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)

        def boom(*_a, **_k):
            raise AssertionError("no debe llamarse a funciones de trading")

        fake.order_send = boom
        fake.order_check = boom
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        adapter.read_market_mark(INSTRUMENT)
        assert fake.send_count == 0
        assert fake.check_count == 0

    def test_repr_has_no_secret(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert "password" not in repr(mark).lower()
        assert "not-a-real-password" not in repr(mark)


# ------------------------------------- 06F-FIX: enteros gigantes y propiedades


def _property_attr_error_tick(name, **values):
    """Objeto tick con un `@property` (definido) cuyo getter lanza AttributeError."""

    def _getter(self):
        raise AttributeError(f"{name} getter failed")

    cls = type("_PropErrTick", (), {name: property(_getter)})
    obj = cls()
    for key, value in values.items():
        setattr(obj, key, value)
    return obj


class TestAttributeErrorGetter:
    @pytest.mark.parametrize("name", ["bid", "ask", "last", "time", "time_msc"])
    def test_defined_property_attributeerror_is_data_error(self, name):
        values = dict(bid=1.1, ask=1.2, time=EPOCH, time_msc=EPOCH * 1000)
        values.pop(name, None)  # el campo defectuoso NO se pone como atributo
        fake = _fake()
        fake.ticks[INSTRUMENT] = _property_attr_error_tick(name, **values)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_real_absence_is_not_a_defect(self):
        # Sin descriptor ni atributo: ausencia genuina => None (distinto del defecto).
        fake = _fake()
        fake.ticks[INSTRUMENT] = SimpleNamespace(
            bid=1.1, ask=1.2, time=EPOCH, time_msc=EPOCH * 1000
        )
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.last is None  # ausente real

    def test_dynamic_valid_attribute_still_read(self):
        class DynamicTick:
            def __getattr__(self, item):
                data = {"bid": 1.1, "ask": 1.2, "time": EPOCH}
                if item in data:
                    return data[item]
                raise AttributeError(item)

        fake = _fake()
        fake.ticks[INSTRUMENT] = DynamicTick()
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.bid == pytest.approx(1.1)
        assert mark.ask == pytest.approx(1.2)
        assert mark.last is None
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC)


class TestHugeIntegerPrices:
    @pytest.mark.parametrize("field", ["bid", "ask", "last"])
    def test_huge_integer_price_typed_error(self, field):
        fake = _fake()
        values = dict(bid=1.0999, ask=1.1001, time=EPOCH)
        values[field] = 10 ** 400
        fake.ticks[INSTRUMENT] = SimpleNamespace(**values)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_ordinary_finite_regression(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, last=0.0, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)
        assert mark.last == 0.0


class _RaisingAttrTick:
    """Objeto tick cuya lectura de `raising_name` lanza una excepcion arbitraria."""

    def __init__(self, raising_name, exc, **values):
        self._raising_name = raising_name
        self._exc = exc
        self.__dict__.update(values)

    def __getattr__(self, item):
        if item == self._raising_name:
            raise self._exc
        raise AttributeError(item)


class TestTickAttributeAccess:
    def test_bid_property_exception_typed(self):
        fake = _fake()
        fake.ticks[INSTRUMENT] = _RaisingAttrTick(
            "bid", RuntimeError("descriptor fail"), ask=1.1, time=EPOCH
        )
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    @pytest.mark.parametrize("name", ["bid", "ask", "last", "time", "time_msc"])
    def test_other_property_exceptions_typed(self, name):
        fake = _fake()
        values = dict(bid=1.1, ask=1.2, time=EPOCH, time_msc=EPOCH * 1000)
        values.pop(name, None)  # el atributo defectuoso NO debe existir en __dict__
        fake.ticks[INSTRUMENT] = _RaisingAttrTick(name, ValueError("bad"), **values)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_defective_property_not_treated_as_absent(self):
        # Un campo defectuoso NO se degrada silenciosamente a "ausente".
        fake = _fake()
        fake.ticks[INSTRUMENT] = _RaisingAttrTick(
            "last", RuntimeError("boom"), bid=1.1, ask=1.2, time=EPOCH
        )
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    def test_genuinely_absent_field_stays_optional(self):
        fake = _fake()
        fake.ticks[INSTRUMENT] = SimpleNamespace(bid=1.1, ask=1.2, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.last is None  # ausente real => None
        assert mark.tick_time is not None

    def test_query_exception_still_unavailable(self):
        fake = _fake()

        def boom(_name):
            raise RuntimeError("transporte")

        fake.symbol_info_tick = boom
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkUnavailableError):
            adapter.read_market_mark(INSTRUMENT)

    def test_defective_attr_error_message_safe(self):
        fake = _fake()
        fake.ticks[INSTRUMENT] = _RaisingAttrTick(
            "bid", RuntimeError("descriptor fail"), ask=1.1, time=EPOCH
        )
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError) as info:
            adapter.read_market_mark(INSTRUMENT)
        message = str(info.value)
        assert "bid" in message
        assert "password" not in message.lower()


class TestTickFlags:
    def test_flags_are_captured_raw(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, flags=3)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_flags == 3

    def test_absent_flags_is_none(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_flags is None

    def test_defective_flags_getter_is_typed_error(self):
        fake = _fake()
        fake.ticks[INSTRUMENT] = _property_attr_error_tick(
            "flags", bid=1.1, ask=1.2, time=EPOCH
        )
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)

    @pytest.mark.parametrize("value", [True, False, 1.5, "3", object()])
    def test_invalid_flags_type_is_typed_error(self, value):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, flags=value)
        adapter = _mk(fake, {INSTRUMENT: INSTRUMENT})
        with pytest.raises(MT5MarketMarkDataError):
            adapter.read_market_mark(INSTRUMENT)


class TestTemporalMetadata:
    def test_raw_time_fields_preserved(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH, time_msc=EPOCH * 1000 + 250)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time_seconds == EPOCH
        assert mark.tick_time_msc == EPOCH * 1000 + 250
        expected = datetime.fromtimestamp(EPOCH, tz=UTC).replace(
            microsecond=250_000
        )
        assert mark.tick_time == expected
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)
        assert mark.received_at == NOW

    def test_raw_time_fields_absent_are_none(self):
        fake = _fake()
        _tick(fake, INSTRUMENT)  # sin time/time_msc
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time is None
        assert mark.tick_time_seconds is None
        assert mark.tick_time_msc is None
        assert mark.received_at == NOW

    def test_raw_time_seconds_only_preserved(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        mark = _mk(fake, {INSTRUMENT: INSTRUMENT}).read_market_mark(INSTRUMENT)
        assert mark.tick_time_seconds == EPOCH
        assert mark.tick_time_msc is None
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC)

    def test_monotonic_span_around_symbol_info_tick(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        events = []
        values = iter([10.0, 10.25])

        def monotonic():
            value = next(values)
            events.append(("monotonic", value))
            return value

        original = fake.symbol_info_tick

        def wrapped(name):
            events.append(("tick", name))
            return original(name)

        fake.symbol_info_tick = wrapped
        adapter = _mk(
            fake, {INSTRUMENT: INSTRUMENT}, monotonic=monotonic
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert mark.read_started_monotonic == 10.0
        assert mark.read_finished_monotonic == 10.25
        assert events == [
            ("monotonic", 10.0),
            ("tick", INSTRUMENT),
            ("monotonic", 10.25),
        ]

    @pytest.mark.parametrize(
        "values",
        [
            (None, None),
            (float("nan"), float("inf")),
            ("x", object()),
        ],
    )
    def test_invalid_monotonic_readings_are_none(self, values):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        seq = iter(values)
        adapter = _mk(
            fake, {INSTRUMENT: INSTRUMENT}, monotonic=lambda: next(seq)
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert mark.read_started_monotonic is None
        assert mark.read_finished_monotonic is None
        assert mark.tick_time is not None

    def test_monotonic_exception_is_none(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)

        def monotonic():
            raise RuntimeError("monotonic failure")

        adapter = _mk(
            fake, {INSTRUMENT: INSTRUMENT}, monotonic=monotonic
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert mark.read_started_monotonic is None
        assert mark.read_finished_monotonic is None
        assert mark.tick_time is not None

    def test_monotonic_integer_overflow_is_none_and_api_still_called(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        called = []
        original = fake.symbol_info_tick

        def wrapped(name):
            called.append(name)
            return original(name)

        fake.symbol_info_tick = wrapped
        adapter = _mk(
            fake,
            {INSTRUMENT: INSTRUMENT},
            monotonic=lambda: 10**400,
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert called == [INSTRUMENT]
        assert mark.read_started_monotonic is None
        assert mark.read_finished_monotonic is None
        assert mark.read_started_monotonic != 0.0
        assert mark.read_finished_monotonic != 0.0
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC)
        assert mark.received_at == NOW
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)

    def test_monotonic_exception_at_start_still_calls_api(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        called = []
        original = fake.symbol_info_tick
        first = True

        def wrapped(name):
            called.append(name)
            return original(name)

        def monotonic():
            nonlocal first
            if first:
                first = False
                raise RuntimeError("start clock failure")
            return 20.0

        fake.symbol_info_tick = wrapped
        adapter = _mk(
            fake, {INSTRUMENT: INSTRUMENT}, monotonic=monotonic
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert called == [INSTRUMENT]
        assert mark.read_started_monotonic is None
        assert mark.read_finished_monotonic == 20.0
        assert mark.tick_time is not None

    def test_monotonic_exception_at_finish_keeps_mark_valid(self):
        fake = _fake()
        _tick(fake, INSTRUMENT, time=EPOCH)
        called = []
        original = fake.symbol_info_tick
        calls = 0

        def wrapped(name):
            called.append(name)
            return original(name)

        def monotonic():
            nonlocal calls
            calls += 1
            if calls == 1:
                return 20.0
            raise RuntimeError("finish clock failure")

        fake.symbol_info_tick = wrapped
        adapter = _mk(
            fake, {INSTRUMENT: INSTRUMENT}, monotonic=monotonic
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert called == [INSTRUMENT]
        assert mark.read_started_monotonic == 20.0
        assert mark.read_finished_monotonic is None
        assert mark.tick_time == datetime.fromtimestamp(EPOCH, tz=UTC)
        assert mark.received_at == NOW
        assert mark.bid == pytest.approx(1.0999)
        assert mark.ask == pytest.approx(1.1001)

    def test_invalid_monotonic_does_not_fill_missing_tick_time(self):
        fake = _fake()
        _tick(fake, INSTRUMENT)  # sin time/time_msc
        adapter = _mk(
            fake,
            {INSTRUMENT: INSTRUMENT},
            monotonic=lambda: 10**400,
        )
        mark = adapter.read_market_mark(INSTRUMENT)
        assert mark.tick_time is None
        assert mark.tick_time_seconds is None
        assert mark.tick_time_msc is None
        assert mark.read_started_monotonic is None
        assert mark.read_finished_monotonic is None
        assert mark.received_at == NOW
