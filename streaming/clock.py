"""Reloj UTC-aware para el subsistema de streaming (S1).

Regla temporal del contrato: TODO timestamp de reloj (`received_at`,
`server_time`) es un `datetime` UTC-aware. Un `datetime` naive o con offset
distinto de cero es un error de programación, no un dato válido (DST).
Los timestamps de DATOS (`open_time`) siguen siendo epoch UTC en segundos,
idénticos al contrato B1.

El reloj es inyectable (`clock: Callable[[], datetime]`) para que S2/S3 y los
tests sean deterministas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    """Instante actual UTC-aware (default del reloj inyectable)."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Valida que `value` sea un `datetime` UTC-aware y lo devuelve.

    Raises:
        ValueError: si no es `datetime` o no es UTC-aware.
    """
    if not isinstance(value, datetime):
        raise ValueError(f"se esperaba datetime, recibido {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp debe ser UTC-aware, recibido {value!r}")
    return value


__all__ = ["ensure_utc", "utc_now"]
