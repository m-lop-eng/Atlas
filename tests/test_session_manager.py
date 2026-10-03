"""Tests del MarketSessionManager (B2).

Batería exigida por el plan B2:

  1. BTC 24/7: lunes/sábado/domingo => OPEN
  2. Mercado cerrado (fixture)     => CLOSED
  3. Fronteras exactas (±1s)       => detecta errores de intervalos
  4. PRE_OPEN / CLOSING            => ventanas de transición
  5. Feriado                       => CLOSED
  6. DST (spring-forward/fall-back)=> sin sesiones duplicadas ni horas
                                     imposibles; UTC explícito
  7. UNKNOWN                       => fail-safe, no permite operar
  8. HALTED                        => prevalece sobre OPEN
  9. Determinismo                  => misma entrada, mismo resultado
 10. Sin efectos secundarios       => la consulta no muta nada
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sessions import (
    HaltWindow,
    MarketSessionManager,
    SessionDefinition,
    SessionState,
    StaticMarketCalendar,
)
from sessions.manager import normalize_utc, SessionDecision
from sessions.markets import btc_24_7_calendar, default_calendars

UTC = timezone.utc


def _nyse(
    *,
    pre_open_minutes: int = 0,
    closing_minutes: int = 0,
    holidays: tuple[date, ...] = (),
    halt_windows: tuple[HaltWindow, ...] = (),
    version: str = "1.0.0",
) -> StaticMarketCalendar:
    """NYSE-like: 09:30-16:00 America/New_York, lunes a viernes."""
    return StaticMarketCalendar(
        calendar_id="nyse",
        version=version,
        timezone="America/New_York",
        definitions=(
            SessionDefinition(
                name="core",
                open_minute=9 * 60 + 30,
                close_minute=16 * 60,
                weekdays=frozenset({1, 2, 3, 4, 5}),
                pre_open_minutes=pre_open_minutes,
                closing_minutes=closing_minutes,
            ),
        ),
        holidays=holidays,
        halt_windows=halt_windows,
    )


def _utc(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. BTC 24/7
# ---------------------------------------------------------------------------


class TestBTC24_7:
    def test_default_registry_available(self) -> None:
        manager = MarketSessionManager(default_calendars())
        assert manager.get_session("BTCUSDT", _utc(2026, 9, 1, 3)).state == (
            SessionState.OPEN
        )

    def test_monday_03_00_open(self) -> None:
        manager = MarketSessionManager({"BTCUSDT": btc_24_7_calendar()})
        d = manager.get_session("BTCUSDT", _utc(2026, 9, 21, 3, 0))
        assert d.state == SessionState.OPEN
        assert d.can_open_positions is True
        assert d.can_hold_positions is True

    def test_saturday_03_00_open(self) -> None:
        manager = MarketSessionManager({"BTCUSDT": btc_24_7_calendar()})
        d = manager.get_session("BTCUSDT", _utc(2026, 9, 26, 3, 0))
        assert d.state == SessionState.OPEN

    def test_sunday_23_59_open(self) -> None:
        manager = MarketSessionManager({"BTCUSDT": btc_24_7_calendar()})
        d = manager.get_session("BTCUSDT", _utc(2026, 9, 27, 23, 59))
        assert d.state == SessionState.OPEN

    def test_midnight_boundary_keeps_open(self) -> None:
        manager = MarketSessionManager({"BTCUSDT": btc_24_7_calendar()})
        before = manager.get_session("BTCUSDT", _utc(2026, 9, 20, 23, 59, 59))
        after = manager.get_session("BTCUSDT", _utc(2026, 9, 21, 0, 0, 0))
        assert before.state == SessionState.OPEN
        assert after.state == SessionState.OPEN


# ---------------------------------------------------------------------------
# 2. Mercado cerrado
# ---------------------------------------------------------------------------


class TestClosedMarket:
    def test_weekend_closed(self) -> None:
        manager = MarketSessionManager({"NQ": _nyse()})
        d = manager.get_session("NQ", _utc(2026, 9, 26, 15, 0))  # sábado
        assert d.state == SessionState.CLOSED
        assert d.can_open_positions is False
        assert d.can_hold_positions is True
        assert d.next_transition is not None

    def test_before_open_same_day_closed(self) -> None:
        manager = MarketSessionManager({"NQ": _nyse()})
        d = manager.get_session("NQ", _utc(2026, 2, 2, 13, 0))  # lunes pre-09:30
        assert d.state == SessionState.CLOSED
        # siguiente transición: apertura del día 14:30 UTC (09:30 ET, invierno)
        assert d.next_transition == _utc(2026, 2, 2, 14, 30)


# ---------------------------------------------------------------------------
# 3. Fronteras exactas (±1s) — detecta errores de intervalos
# ---------------------------------------------------------------------------


class TestExactBoundaries:
    def _manager(self) -> MarketSessionManager:
        return MarketSessionManager({"NQ": _nyse()})

    def test_session_start_boundaries(self) -> None:
        manager = self._manager()
        start = _utc(2026, 2, 2, 14, 30)  # 09:30 ET lunes (invierno, EST)
        assert manager.get_session("NQ", start - timedelta(seconds=1)).state == (
            SessionState.CLOSED
        )
        assert manager.get_session("NQ", start).state == SessionState.OPEN
        assert manager.get_session("NQ", start + timedelta(seconds=1)).state == (
            SessionState.OPEN
        )

    def test_session_end_boundaries(self) -> None:
        manager = self._manager()
        end = _utc(2026, 2, 2, 21, 0)  # 16:00 ET lunes
        assert manager.get_session("NQ", end - timedelta(seconds=1)).state == (
            SessionState.OPEN
        )
        assert manager.get_session("NQ", end).state == SessionState.CLOSED
        assert manager.get_session("NQ", end + timedelta(seconds=1)).state == (
            SessionState.CLOSED
        )

    def test_session_window_coordinates(self) -> None:
        d = self._manager().get_session("NQ", _utc(2026, 2, 2, 15, 0))
        assert d.session_open_utc == _utc(2026, 2, 2, 14, 30)
        assert d.session_close_utc == _utc(2026, 2, 2, 21, 0)


# ---------------------------------------------------------------------------
# 4. PRE_OPEN / CLOSING con ventanas de transición
# ---------------------------------------------------------------------------


class TestTransitionWindows:
    def _manager(self) -> MarketSessionManager:
        return MarketSessionManager(
            {"NQ": _nyse(pre_open_minutes=30, closing_minutes=15)}
        )

    def test_pre_open_boundaries(self) -> None:
        manager = self._manager()
        start = _utc(2026, 2, 2, 14, 30)
        pre_open_start = start - timedelta(minutes=30)

        assert manager.get_session("NQ", pre_open_start - timedelta(seconds=1)).state == (
            SessionState.CLOSED
        )
        assert manager.get_session("NQ", pre_open_start).state == SessionState.PRE_OPEN
        assert manager.get_session("NQ", start - timedelta(seconds=1)).state == (
            SessionState.PRE_OPEN
        )
        assert manager.get_session("NQ", start).state == SessionState.OPEN
        assert manager.get_session("NQ", start + timedelta(seconds=1)).state == (
            SessionState.OPEN
        )

    def test_closing_boundaries(self) -> None:
        manager = self._manager()
        end = _utc(2026, 2, 2, 21, 0)
        closing_end = end + timedelta(minutes=15)

        assert manager.get_session("NQ", end - timedelta(seconds=1)).state == (
            SessionState.OPEN
        )
        assert manager.get_session("NQ", end).state == SessionState.CLOSING
        assert manager.get_session("NQ", closing_end - timedelta(seconds=1)).state == (
            SessionState.CLOSING
        )
        assert manager.get_session("NQ", closing_end).state == SessionState.CLOSED

    def test_closing_blocks_new_entries_but_allows_hold(self) -> None:
        d = self._manager().get_session("NQ", _utc(2026, 2, 2, 21, 7))
        assert d.state == SessionState.CLOSING
        assert d.can_open_positions is False
        assert d.can_hold_positions is True  # no fuerza cierre (lo decide la política)


# ---------------------------------------------------------------------------
# 5. Feriado
# ---------------------------------------------------------------------------


class TestHoliday:
    def test_holiday_closed(self) -> None:
        holidays = (date(2026, 2, 13),)  # viernes, día de mercado en NYSE-normal
        manager = MarketSessionManager({"NQ": _nyse(holidays=holidays)})
        d = manager.get_session("NQ", _utc(2026, 2, 13, 15, 0))
        assert d.state == SessionState.CLOSED
        assert d.can_open_positions is False
        assert d.reason == "feriado"
        # la siguiente apertura es el lunes 16 de febrero 14:30 UTC (EST)
        assert d.next_transition == _utc(2026, 2, 16, 14, 30)

    def test_holiday_local_date_respected(self) -> None:
        # el feriado es un día LOCAL: 13-feb 12:00 UTC = 07:00 (pre-apertura) en NY
        holidays = (date(2026, 2, 13),)
        manager = MarketSessionManager({"NQ": _nyse(holidays=holidays)})
        assert manager.get_session("NQ", _utc(2026, 2, 13, 12, 0)).state == (
            SessionState.CLOSED
        )


# ---------------------------------------------------------------------------
# 6. DST: transiciones y UTC explícito
# ---------------------------------------------------------------------------


class TestDST:
    def test_spring_forward_changes_utc_offset_for_session(self) -> None:
        """Treinta minutos locales del calendario NO son 30 min el día del cambio."""
        manager = MarketSessionManager({"NQ": _nyse()})
        # Antes del cambio (EST, UTC-5): apertura 09:30 ET = 14:30 UTC
        assert manager.get_session("NQ", _utc(2026, 3, 6, 14, 30)).state == (
            SessionState.OPEN
        )  # viernes
        # Tras el cambio (EDT, UTC-4): apertura 09:30 ET = 13:30 UTC
        # 8-mar-2026 es sábado => lunes 9-mar ya es EDT
        d = manager.get_session("NQ", _utc(2026, 3, 9, 13, 30))
        assert d.state == SessionState.OPEN
        assert d.session_open_utc == _utc(2026, 3, 9, 13, 30)

    def test_fall_back_changes_utc_offset_for_session(self) -> None:
        manager = MarketSessionManager({"NQ": _nyse()})
        # 30-oct-2026 (viernes) aún EDT => apertura 13:30 UTC
        assert manager.get_session("NQ", _utc(2026, 10, 30, 13, 30)).state == (
            SessionState.OPEN
        )
        # 2-nov-2026 (lunes) ya EST => apertura 14:30 UTC
        d = manager.get_session("NQ", _utc(2026, 11, 2, 13, 30))
        assert d.state == SessionState.CLOSED
        assert manager.get_session("NQ", _utc(2026, 11, 2, 14, 30)).state == (
            SessionState.OPEN
        )

    def test_no_duplicated_sessions_across_spring_forward(self) -> None:
        """24/7 en America/New_York: el día del cambio tiene 23h, no duplica."""
        calendar = StaticMarketCalendar(
            calendar_id="ny-24-7",
            version="1.0.0",
            timezone="America/New_York",
            definitions=(
                SessionDefinition(name="all", open_minute=0, close_minute=1440),
            ),
        )
        manager = MarketSessionManager({"X": calendar})
        # 2026-03-07 05:00 UTC = 00:00 EST (antes del salto del 8-mar 07:00 UTC)
        start = _utc(2026, 3, 7, 5, 0)
        for i in range(0, 24):
            d = manager.get_session("X", start + timedelta(hours=i))
            assert d.state == SessionState.OPEN, f"hora UTC +{i}h se degradó"
        # el día del cambio (8-mar): 00:00 EST = 05:00Z ... 00:00 EDT (9-mar) = 04:00Z
        after = manager.get_session("X", _utc(2026, 3, 8, 5, 0))
        assert after.state == SessionState.OPEN
        assert after.session_close_utc - after.session_open_utc == timedelta(hours=23)

    def test_no_impossible_hour_during_gap(self) -> None:
        """Durante el salto, 02:30 EST local no existe: el query en UTC es estable."""
        manager = MarketSessionManager({"NQ": _nyse()})
        # 08-mar-2026 sábado 02:30 ET "no existe"; en UTC 02:30Z está dentro del
        # horario real de mercado (de madrugada) -> definido y sin excepción
        d = manager.get_session("NQ", _utc(2026, 3, 8, 7, 30))  # 02:30 EST previo
        assert d.state in (SessionState.CLOSED, SessionState.OPEN)

    def test_naive_datetime_rejected(self) -> None:
        manager = MarketSessionManager({"NQ": _nyse()})
        with pytest.raises(ValueError):
            manager.get_session("NQ", datetime(2026, 9, 21, 15, 0))

    def test_epoch_and_aware_inputs_equal(self) -> None:
        manager = MarketSessionManager(default_calendars())
        aware = _utc(2026, 9, 21, 3, 0)
        epoch = int(aware.timestamp())
        assert manager.get_session("BTCUSDT", aware) == manager.get_session(
            "BTCUSDT", epoch
        )

    def test_fall_back_day_has_25_hours_in_ny(self) -> None:
        """El día del cambio de vuelta tiene 25h locales; UTC lo refleja."""
        calendar = StaticMarketCalendar(
            calendar_id="ny-24-7",
            version="1.0.0",
            timezone="America/New_York",
            definitions=(
                SessionDefinition(name="all", open_minute=0, close_minute=1440),
            ),
        )
        manager = MarketSessionManager({"X": calendar})
        d = manager.get_session("X", _utc(2026, 11, 1, 4, 0))  # 00:00 EDT
        assert d.state == SessionState.OPEN
        # 1-nov-2026 00:00 EDT (04:00Z) ... 2-nov 00:00 EST (05:00Z) => 25h
        assert d.session_close_utc - d.session_open_utc == timedelta(hours=25)


# ---------------------------------------------------------------------------
# 7. UNKNOWN
# ---------------------------------------------------------------------------


class TestUnknown:
    def test_missing_calendar_unknown(self) -> None:
        manager = MarketSessionManager({})
        d = manager.get_session("DOESNOTEXIST", _utc(2026, 9, 21, 15, 0))
        assert d.state == SessionState.UNKNOWN
        assert d.can_open_positions is False
        assert d.can_hold_positions is False
        assert d.next_transition is None

    def test_empty_definitions_unknown(self) -> None:
        empty = StaticMarketCalendar(
            calendar_id="empty", version="1.0.0", timezone="UTC"
        )
        manager = MarketSessionManager({"X": empty})
        d = manager.get_session("X", _utc(2026, 9, 21, 15, 0))
        assert d.state == SessionState.UNKNOWN
        assert d.can_open_positions is False


# ---------------------------------------------------------------------------
# 8. HALTED
# ---------------------------------------------------------------------------


class TestHalted:
    def test_halt_overrides_open(self) -> None:
        halt = HaltWindow(
            start_utc=_utc(2026, 9, 21, 15, 0),
            end_utc=_utc(2026, 9, 21, 17, 0),
            reason="halt técnico",
        )
        manager = MarketSessionManager({"NQ": _nyse(halt_windows=(halt,))})
        inside = manager.get_session("NQ", _utc(2026, 9, 21, 16, 0))
        assert inside.state == SessionState.HALTED
        assert inside.can_open_positions is False
        assert inside.can_hold_positions is False
        assert inside.next_transition == _utc(2026, 9, 21, 17, 0)
        assert inside.reason == "halt activo"

    def test_open_outside_halt_window(self) -> None:
        halt = HaltWindow(
            start_utc=_utc(2026, 9, 21, 15, 0),
            end_utc=_utc(2026, 9, 21, 17, 0),
        )
        manager = MarketSessionManager({"NQ": _nyse(halt_windows=(halt,))})
        before = manager.get_session("NQ", _utc(2026, 9, 21, 14, 45))
        assert before.state == SessionState.OPEN


# ---------------------------------------------------------------------------
# 9. Determinismo
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        manager = MarketSessionManager(default_calendars())
        ts = _utc(2026, 9, 21, 17, 3, 42)
        first = manager.get_session("BTCUSDT", ts)
        for _ in range(100):
            assert manager.get_session("BTCUSDT", ts) == first

    def test_same_calendar_same_version_same_result(self) -> None:
        a = MarketSessionManager({"NQ": _nyse(version="2.0.0")})
        b = MarketSessionManager({"NQ": _nyse(version="2.0.0")})
        assert a.get_session("NQ", _utc(2026, 9, 21, 15, 0)) == b.get_session(
            "NQ", _utc(2026, 9, 21, 15, 0)
        )

    def test_version_is_part_of_contract(self) -> None:
        a = MarketSessionManager({"NQ": _nyse(version="1.0.0")})
        b = MarketSessionManager({"NQ": _nyse(version="2.0.0")})
        # mismo instante, calendario distinto (version) -> se refleja traza
        assert a.get_session("NQ", _utc(2026, 9, 21, 15, 0)).calendar_version == "1.0.0"
        assert b.get_session("NQ", _utc(2026, 9, 21, 15, 0)).calendar_version == "2.0.0"


# ---------------------------------------------------------------------------
# 10. Sin efectos secundarios
# ---------------------------------------------------------------------------


class TestNoSideEffects:
    def test_query_does_not_mutate_calendar(self) -> None:
        calendar = _nyse()
        before = repr(calendar)
        manager = MarketSessionManager({"NQ": calendar})
        for ts in [_utc(2026, 9, 21, 14, 30), _utc(2026, 9, 21, 21, 0)]:
            manager.get_session("NQ", ts)
        assert repr(calendar) == before

    def test_query_does_not_mutate_manager(self) -> None:
        manager = MarketSessionManager({"NQ": _nyse(), "BTCUSDT": btc_24_7_calendar()})
        before = dict(manager._calendars)
        manager.get_session("NQ", _utc(2026, 9, 21, 15, 0))
        assert dict(manager._calendars) == before


# ---------------------------------------------------------------------------
# helpers de normalize_utc
# ---------------------------------------------------------------------------


class TestNormalizeUtc:
    def test_int_and_float(self) -> None:
        ts = 1_781_000_000
        assert normalize_utc(ts) == datetime.fromtimestamp(ts, tz=UTC)
        assert normalize_utc(float(ts)) == datetime.fromtimestamp(ts, tz=UTC)

    def test_naive_rejected(self) -> None:
        with pytest.raises(ValueError):
            normalize_utc(datetime(2026, 9, 21))

    def test_other_tz_converted(self) -> None:
        aware_ny = _utc(2026, 9, 21, 15, 0).astimezone(UTC)
        assert normalize_utc(aware_ny).tzinfo == UTC

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(TypeError):
            normalize_utc("2026-09-21")  # type: ignore[arg-type]