"""Mercados configurables para MarketSessionManager (B2).

El manager no conoce mercados: aquí se definen los calendarios que se le
inyectan. BTC es 24/7 de forma TRIVIAL (una ventana que cubre todo el día en
UTC), pero esa decisión vive en la configuración, no en el manager.
"""

from __future__ import annotations

from sessions.calendar import SessionDefinition, StaticMarketCalendar

BTC_24_7_DEFINITION = SessionDefinition(
    name="24-7",
    open_minute=0,
    close_minute=1440,
    weekdays=frozenset(),  # todos los días
)


def btc_24_7_calendar(*, version: str = "1.0.0") -> StaticMarketCalendar:
    """Calendario BTC/USDT: 24/7, sin feriados ni halts (a configurar luego)."""
    return StaticMarketCalendar(
        calendar_id="btcusdt-24-7",
        version=version,
        timezone="UTC",
        definitions=(BTC_24_7_DEFINITION,),
    )


def default_calendars() -> dict[str, StaticMarketCalendar]:
    """Registro por defecto: BTC/USDT con calendario 24/7."""
    return {"BTCUSDT": btc_24_7_calendar()}