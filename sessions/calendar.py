"""Abstracción de calendario de mercado (B2).

`MarketCalendar` describe CUÁNDO un instrumento tiene sesión, en el tiempo
de origen del propio calendario (UTC, America/New_York, ...). No decide qué
estrategia ejecutar, ni cuánto arriesgar, ni qué orden enviar: solo sesiones.

Cadena de responsabilidad:

    MarketCalendar
        ->  SessionDefinition (ventana semanal en hora local del calendario)
            ->  MarketSessionManager (convierte a UTC y clasifica)

Mismo instrumento + timestamp + version de calendario => mismo resultado
(determinista e inmutable). Todas las operaciones internas usan instantes
UTC; el timezone del calendario solo se usa para interpretar su horario.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


class SessionDefinition:
    """Ventana de sesión semanal expresada en MINUTOS del timezone local.

    Attributes:
        name: Nombre legible de la sesión (p. ej. "core", "24-7").
        open_minute: Minuto de apertura del día local (0..1439).
        close_minute: Minuto de cierre del día local (open < close <= 1440).
            `1440` representa el final del día (00:00 del día siguiente) e
            implica 24/7 junto con open_minute == 0.
        weekdays: Días ISO (1=lunes .. 7=domingo) en que aplica la sesión.
            Vacío => todos los días.
        pre_open_minutes: Ventana PRE_OPEN antes de la apertura (>= 0).
        closing_minutes: Ventana CLOSING tras el cierre (>= 0).
            Restricción B2: open/pre y close/closing deben caber en el MISMO
            día local (sin cruzar medianoche); los mercados 24/7 no llevan
            ventanas de transición.
    """

    __slots__ = (
        "name",
        "open_minute",
        "close_minute",
        "weekdays",
        "pre_open_minutes",
        "closing_minutes",
    )

    def __init__(
        self,
        name: str,
        *,
        open_minute: int,
        close_minute: int,
        weekdays: frozenset[int] = frozenset(),
        pre_open_minutes: int = 0,
        closing_minutes: int = 0,
    ) -> None:
        if not (0 <= open_minute < close_minute <= 1440):
            raise ValueError(
                f"open/close inválidos: open={open_minute}, close={close_minute} "
                "(se exige 0 <= open < close <= 1440)"
            )
        if pre_open_minutes < 0 or closing_minutes < 0:
            raise ValueError("pre_open/closing deben ser >= 0")
        if open_minute - pre_open_minutes < 0:
            raise ValueError(
                f"pre_open ({pre_open_minutes}) antes del inicio del día local"
            )
        if close_minute == 1440 and closing_minutes > 0:
            raise ValueError("una sesión 24/7 (close=1440) no puede tener CLOSING")
        if close_minute < 1440 and close_minute + closing_minutes > 1440:
            raise ValueError(
                "closing cruza la medianoche local; fuera de alcance en B2 "
                f"(close={close_minute}, closing={closing_minutes})"
            )
        for wd in weekdays:
            if not 1 <= wd <= 7:
                raise ValueError(f"weekday ISO inválido: {wd} (1=lun .. 7=dom)")
        self.name = name
        self.open_minute = open_minute
        self.close_minute = close_minute
        self.weekdays = weekdays
        self.pre_open_minutes = pre_open_minutes
        self.closing_minutes = closing_minutes

    @property
    def is_24_7(self) -> bool:
        return self.open_minute == 0 and self.close_minute == 1440

    def matches_weekday(self, iso_weekday: int) -> bool:
        if not self.weekdays:
            return True
        return iso_weekday in self.weekdays

    def __repr__(self) -> str:
        return (
            f"SessionDefinition({self.name!r}, open={self.open_minute}, "
            f"close={self.close_minute}, weekdays={sorted(self.weekdays)}, "
            f"pre={self.pre_open_minutes}, closing={self.closing_minutes})"
        )


@dataclass(frozen=True, slots=True)
class HaltWindow:
    """Ventana explícita de halt del mercado (prevalecerá sobre OPEN)."""

    start_utc: datetime
    end_utc: datetime
    reason: str = ""


class MarketCalendar(ABC):
    """Contrato mínimo para cualquier calendario de mercado."""

    @property
    @abstractmethod
    def calendar_id(self) -> str:
        """Identificador estable del calendario (traza en decisiones)."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Versión inmutable; forma parte del contrato determinista."""

    @property
    @abstractmethod
    def timezone(self) -> str:
        """Nombre IANA del timezone de ORIGEN del calendario (ej. 'UTC')."""

    @abstractmethod
    def definitions(self) -> tuple[SessionDefinition, ...]:
        """Sesiones semanales. Vacío => sin datos suficientes (UNKNOWN)."""

    def is_holiday(self, local_date: date) -> bool:
        """Devuelve True si `local_date` (en el timezone del calendario)
        es un día festivo/cerrado sin sesión."""
        return False

    def is_halting(self, timestamp_utc: datetime) -> bool:
        """Halt explícito en un instante UTC (prevalece sobre OPEN)."""
        return False

    def halt_end_utc(self, timestamp_utc: datetime) -> datetime | None:
        """Fin del halt que cubre `timestamp_utc`; None si no hay halt."""
        return None


class StaticMarketCalendar(MarketCalendar):
    """Calendario configurable/empírico (fixtures, pruebas de DST, BTC 24/7).

    Pensado para B2: sesiones semanales fijas + holidays + halt windows,
    sin bases de datos externas. La lista de holidays se interpreta en el
    timezone del propio calendario.
    """

    __slots__ = ("_calendar_id", "_version", "_timezone", "_definitions",
                 "_holidays", "_halt_windows")

    def __init__(
        self,
        *,
        calendar_id: str,
        version: str,
        timezone: str,
        definitions: tuple[SessionDefinition, ...] = (),
        holidays: tuple[date, ...] = (),
        halt_windows: tuple[HaltWindow, ...] = (),
    ) -> None:
        if not calendar_id:
            raise ValueError("calendar_id es obligatorio")
        if not version:
            raise ValueError("version es obligatoria (contrato determinista)")
        # Validar que el timezone IANA existe (fail-fast en config, no en consulta).
        ZoneInfo(timezone)
        self._calendar_id = calendar_id
        self._version = version
        self._timezone = timezone
        self._definitions = definitions
        self._holidays = frozenset(holidays)
        self._halt_windows = halt_windows

    @property
    def calendar_id(self) -> str:
        return self._calendar_id

    @property
    def version(self) -> str:
        return self._version

    @property
    def timezone(self) -> str:
        return self._timezone

    def definitions(self) -> tuple[SessionDefinition, ...]:
        return self._definitions

    def is_holiday(self, local_date: date) -> bool:
        return local_date in self._holidays

    def is_halting(self, timestamp_utc: datetime) -> bool:
        return self._halt_for(timestamp_utc) is not None

    def halt_end_utc(self, timestamp_utc: datetime) -> datetime | None:
        window = self._halt_for(timestamp_utc)
        return window.end_utc if window is not None else None

    def _halt_for(self, timestamp_utc: datetime) -> HaltWindow | None:
        ts = timestamp_utc.astimezone(timezone.utc)
        for window in self._halt_windows:
            if window.start_utc <= ts < window.end_utc:
                return window
        return None