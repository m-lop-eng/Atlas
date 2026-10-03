"""MarketSessionManager (B2): ¿está el instrumento en una ventana operativa?

El manager responde UNA pregunta:

    ¿El instrumento está actualmente en una ventana en la que Atlas puede operar?

NO decide qué estrategia ejecutar, qué señal es buena, cuánto arriesgar ni
qué orden enviar. Tampoco cierra posiciones: un estado CLOSING informa, y la
política de qué hacer al cierre pertenece a la capa estratégica/operacional.

Separaciones explícitas:
    - Estado de mercado  !=  permiso de trading. `can_open_positions` /
      `can_hold_positions` son permisos derivados del CALENDARIO; capas
      posteriores (Risk, Prop Rules, kill switch) pueden restringirlos a
      False con un estado OPEN intacto.
    - HALTED y UNKNOWN son fail-safe: nunca permiten operar.
    - El manager NO conoce mercados concretos: BTC 24/7, CME, FX o NYSE
      llegan como MarketCalendar configurado, nunca hard-coded aquí.

Contrato determinista: mismo instrumento + timestamp (UTC) + versión de
calendario => mismo resultado. La consulta no muta calendario ni manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping
from zoneinfo import ZoneInfo

from sessions.calendar import MarketCalendar, SessionDefinition


class SessionState(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """Resultado inmutable y determinista de una consulta de sesión.

    Attributes:
        instrument: Instrumento consultado.
        state: Estado de sesión del mercado en `timestamp`.
        timestamp: Instante UTC normalizado de la consulta (siempre aware).
        calendar_id: Calendario que produjo la decisión (traza).
        calendar_version: Versión del calendario (parte del contrato).
        timezone: Zona IANA de origen del calendario (solo interpretación).
        can_open_positions: ¿El calendario permite ABRIR nuevas posiciones?
        can_hold_positions: ¿El calendario permite MANTENER posiciones?
        next_transition: Próximo instante UTC de cambio de estado (o None).
        session_open_utc: Apertura (UTC) de la ventana relevante.
        session_close_utc: Cierre (UTC) de la ventana relevante.
        reason: Motivo legible (holiday, halt, sin calendario, weekend...).
    """

    instrument: str
    state: SessionState
    timestamp: datetime
    calendar_id: str
    calendar_version: str
    timezone: str
    can_open_positions: bool
    can_hold_positions: bool
    next_transition: datetime | None = None
    session_open_utc: datetime | None = None
    session_close_utc: datetime | None = None
    reason: str = ""


def normalize_utc(timestamp: int | float | datetime) -> datetime:
    """Normaliza cualquier entrada de tiempo a un datetime aware en UTC.

    Raises:
        ValueError: si `timestamp` es un datetime naive (nunca se decide
            una sesión con hora local ambigua ni sin timezone).
        TypeError: si el tipo no es int/float/datetime.
    """
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            raise ValueError(
                "timestamp naive sin timezone: se exige un datetime aware (UTC)"
            )
        return timestamp.astimezone(timezone.utc)
    raise TypeError(
        f"tipo de timestamp no soportado: {type(timestamp).__name__!r}"
    )


@dataclass(frozen=True, slots=True)
class _Window:
    open_utc: datetime
    close_utc: datetime
    pre_open_utc: datetime
    closing_end_utc: datetime


class MarketSessionManager:
    """Resuelve el estado de sesión a partir de un MarketCalendar por instrumento.

    No tiene conocimiento de ningún mercado concreto: recibe un mapeo
    instrumento -> calendario (configuración). Lo que no está mapeado,
    o un calendario sin sesiones definidas, responde UNKNOWN (fail-safe).
    """

    def __init__(self, calendars: Mapping[str, MarketCalendar] | None = None) -> None:
        self._calendars = dict(calendars or {})

    def register(self, instrument: str, calendar: MarketCalendar) -> None:
        """Registra/reemplaza el calendario de un instrumento (config)."""
        self._calendars[instrument] = calendar

    def get_session(
        self, instrument: str, timestamp: int | float | datetime
    ) -> SessionDecision:
        """Estado de sesión de `instrument` en `timestamp` (determinista)."""
        ts = normalize_utc(timestamp)

        calendar = self._calendars.get(instrument)
        if calendar is None:
            return SessionDecision(
                instrument=instrument,
                state=SessionState.UNKNOWN,
                timestamp=ts,
                calendar_id="-",
                calendar_version="-",
                timezone="UTC",
                can_open_positions=False,
                can_hold_positions=False,
                reason="sin calendario registrado",
            )
        if not calendar.definitions():
            return SessionDecision(
                instrument=instrument,
                state=SessionState.UNKNOWN,
                timestamp=ts,
                calendar_id=calendar.calendar_id,
                calendar_version=calendar.version,
                timezone=calendar.timezone,
                can_open_positions=False,
                can_hold_positions=False,
                reason="calendario sin sesiones definidas",
            )

        # HALTED prevalece sobre OPEN: fail-safe siempre.
        if calendar.is_halting(ts):
            return SessionDecision(
                instrument=instrument,
                state=SessionState.HALTED,
                timestamp=ts,
                calendar_id=calendar.calendar_id,
                calendar_version=calendar.version,
                timezone=calendar.timezone,
                can_open_positions=False,
                can_hold_positions=False,
                next_transition=calendar.halt_end_utc(ts),
                reason="halt activo",
            )

        zone = ZoneInfo(calendar.timezone)
        local_dt = ts.astimezone(zone)
        local_date = local_dt.date()

        # Feriado: día cerrado en el timezone del calendario.
        if calendar.is_holiday(local_date):
            nxt = self._next_window(calendar, local_date, ts)
            return self._closed(
                instrument, calendar, ts, nxt, reason="feriado"
            )
        for definition in calendar.definitions():
            if not definition.matches_weekday(local_dt.isoweekday()):
                continue
            window = self._window_for(calendar, definition, local_date)
            if window.pre_open_utc <= ts < window.open_utc:
                return self._decision(
                    instrument,
                    calendar,
                    ts,
                    SessionState.PRE_OPEN,
                    window,
                    can_hold=True,
                    next_transition=window.open_utc,
                    reason="pre-apertura",
                )
            if window.open_utc <= ts < window.close_utc:
                return self._decision(
                    instrument,
                    calendar,
                    ts,
                    SessionState.OPEN,
                    window,
                    can_hold=True,
                    next_transition=window.close_utc,
                    reason="ventana abierta",
                )
            if window.close_utc <= ts < window.closing_end_utc:
                return self._decision(
                    instrument,
                    calendar,
                    ts,
                    SessionState.CLOSING,
                    window,
                    can_hold=True,
                    next_transition=window.closing_end_utc,
                    reason="ventana de cierre",
                )

        # Sin sesión este día local (weekend sin calendario => cerrado).
        nxt = self._next_window(calendar, local_date, ts)
        return self._closed(instrument, calendar, ts, nxt, reason="fuera de sesión")

    # ------------------------------------------------------------------ helpers

    def _decision(
        self,
        instrument: str,
        calendar: MarketCalendar,
        ts: datetime,
        state: SessionState,
        window: _Window,
        *,
        can_hold: bool,
        next_transition: datetime | None,
        reason: str,
    ) -> SessionDecision:
        return SessionDecision(
            instrument=instrument,
            state=state,
            timestamp=ts,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            timezone=calendar.timezone,
            can_open_positions=(state == SessionState.OPEN),
            can_hold_positions=can_hold,
            next_transition=next_transition,
            session_open_utc=window.open_utc,
            session_close_utc=window.close_utc,
            reason=reason,
        )

    def _closed(
        self,
        instrument: str,
        calendar: MarketCalendar,
        ts: datetime,
        next_window: _Window | None,
        *,
        reason: str,
    ) -> SessionDecision:
        return SessionDecision(
            instrument=instrument,
            state=SessionState.CLOSED,
            timestamp=ts,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            timezone=calendar.timezone,
            can_open_positions=False,
            can_hold_positions=True,
            next_transition=(
                next_window.pre_open_utc if next_window is not None else None
            ),
            session_open_utc=(
                next_window.open_utc if next_window is not None else None
            ),
            session_close_utc=(
                next_window.close_utc if next_window is not None else None
            ),
            reason=reason,
        )

    def _window_for(
        self, calendar: MarketCalendar, definition: SessionDefinition, local_date
    ) -> _Window:
        """Convierte la ventana local del día a instantes UTC (DST-aware)."""
        open_utc = self._local_minute_to_utc(calendar.timezone, local_date,
                                             definition.open_minute)
        close_utc = self._local_minute_to_utc(calendar.timezone, local_date,
                                              definition.close_minute)
        pre_open_utc = open_utc - timedelta(minutes=definition.pre_open_minutes)
        if definition.is_24_7 and definition.closing_minutes == 0:
            closing_end_utc = close_utc
        else:
            closing_end_utc = close_utc + timedelta(
                minutes=definition.closing_minutes
            )
        return _Window(
            open_utc=open_utc,
            close_utc=close_utc,
            pre_open_utc=pre_open_utc,
            closing_end_utc=closing_end_utc,
        )

    def _local_minute_to_utc(self, timezone_name: str, local_date, minute: int) -> datetime:
        """Minuto del día local -> instante UTC (respeta DST del calendario)."""
        day = local_date
        if minute >= 1440:  # close_minute == 1440 => 00:00 del día siguiente
            day = day + timedelta(days=1)
            minute -= 1440
        hour, rem = divmod(minute, 60)
        local_aware = datetime(
            day.year, day.month, day.day, hour, rem,
            tzinfo=ZoneInfo(timezone_name),
        )
        return local_aware.astimezone(timezone.utc)

    def _next_window(
        self, calendar: MarketCalendar, from_local_date, ts: datetime
    ) -> _Window | None:
        """Primera ventana futura (buscando desde el día actual, sin holidays)."""
        for offset in range(0, 730):
            day = from_local_date + timedelta(days=offset)
            if calendar.is_holiday(day):
                continue
            for definition in calendar.definitions():
                if not definition.matches_weekday(day.isoweekday()):
                    continue
                window = self._window_for(calendar, definition, day)
                if window.pre_open_utc > ts:
                    return window
        return None