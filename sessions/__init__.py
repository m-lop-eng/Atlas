"""Capas de sesiones de mercado (B2): MarketSessionManager.

Arquitectura:

    MarketCalendar (configurable, timezone de origen explícito)
        ->  SessionDefinition (ventanas semanales en minuto del día local)
            ->  MarketSessionManager (convierte a UTC, clasifica, decide)

Estados: PRE_OPEN / OPEN / CLOSING / CLOSED / HALTED / UNKNOWN.
El manager NO conoce mercados concretos ni envía órdenes: solo responde
"¿está el instrumento en una ventana en la que se puede operar?"
"""

from __future__ import annotations

from sessions.calendar import HaltWindow, MarketCalendar, SessionDefinition, StaticMarketCalendar
from sessions.manager import MarketSessionManager, SessionDecision, SessionState, normalize_utc

__all__ = [
    "HaltWindow",
    "MarketCalendar",
    "MarketSessionManager",
    "SessionDecision",
    "SessionDefinition",
    "SessionState",
    "StaticMarketCalendar",
    "normalize_utc",
]