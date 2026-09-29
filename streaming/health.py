"""Salud y estado operacional del pipeline streaming (S6). SOLO OBSERVA.

S6 aporta un diagnóstico estructurado y auditable del pipeline S1-S5:
estado global, conexión, heartbeat, integridad, último evento, última vela
cerrada (aceptada por B1), freshness, contadores (reconexiones, gaps
recuperados/no recuperables, errores consecutivos), última causa y la evidencia
del último `IntegrityReport` (delegada en `evidence()`, sin duplicar S4).

REGLA: S6 observa; NO decide autorización de trading y NO es un kill switch.
La autorización seguirá perteneciendo a las capas de Atlas (B1/B5/monitoring/
risk). Por eso `StreamingHealthSnapshot.level` es puramente informativo: no
existe ningún campo de permiso (`can_trade`/`authorized`) y el monitor nunca
modifica el pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from streaming.bridge import StreamingPipeline
from streaming.clock import utc_now
from streaming.errors import StreamingError
from streaming.events import (
    StreamConnected,
    StreamDisconnected,
    StreamEvent,
    StreamHeartbeat,
)
from streaming.integrity import IntegrityReport, IntegrityStatus


class StreamingHealthLevel(str, Enum):
    """Nivel global OBSERVACIONAL (no es una autorización)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class HeartbeatStatus(str, Enum):
    OK = "OK"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class StreamingHealthSnapshot:
    """Diagnóstico inmutable y serializable del pipeline streaming."""

    level: StreamingHealthLevel
    checked_at: datetime
    connection_state: str
    heartbeat: HeartbeatStatus
    integrity: str
    pipeline_state: str
    last_event_at: datetime | None
    freshness_seconds: float | None
    last_closed_bar: tuple[str, int] | None
    reconnections: int
    gaps_recovered: int
    gaps_unrecoverable: int
    consecutive_errors: int
    last_error: str | None
    last_integrity_evidence: dict | None = None

    def as_dict(self) -> dict:
        """Representación audit trail (sin campos de autorización)."""
        return {
            "level": self.level.value,
            "checked_at": self.checked_at.isoformat(),
            "connection_state": self.connection_state,
            "heartbeat": self.heartbeat.value,
            "integrity": self.integrity,
            "pipeline_state": self.pipeline_state,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "freshness_seconds": self.freshness_seconds,
            "last_closed_bar": list(self.last_closed_bar) if self.last_closed_bar else None,
            "reconnections": self.reconnections,
            "gaps_recovered": self.gaps_recovered,
            "gaps_unrecoverable": self.gaps_unrecoverable,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "last_integrity_evidence": self.last_integrity_evidence,
        }


class StreamingHealthMonitor:
    """Acumula señales del pipeline y produce snapshots. Nunca actúa.

    Args:
        freshness_timeout_seconds: umbral de staleness para el heartbeat.
        clock: reloj de pared UTC-aware (para `checked_at`).
        monotonic: reloj monótono inyectable (para freshness determinista).
    """

    def __init__(
        self,
        *,
        freshness_timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if freshness_timeout_seconds <= 0:
            raise ValueError("freshness_timeout_seconds debe ser > 0")
        self._freshness_timeout = freshness_timeout_seconds
        self._clock = clock
        self._monotonic = monotonic

        self._last_event_at: datetime | None = None
        self._last_event_monotonic: float | None = None
        self._last_report_status: str | None = None
        self._last_report_evidence: dict | None = None
        self._saw_disconnect = False

        self._reconnections = 0
        self._gaps_recovered = 0
        self._gaps_unrecoverable = 0
        self._consecutive_errors = 0
        self._last_error: str | None = None

    # ------------------------------------------------------ señales entrantes

    def on_stream_event(self, event: StreamEvent) -> None:
        self._last_event_at = event.received_at
        self._last_event_monotonic = self._monotonic()
        self._consecutive_errors = 0
        if isinstance(event, StreamHeartbeat):
            return
        if isinstance(event, StreamDisconnected):
            self._saw_disconnect = True
            return
        if isinstance(event, StreamConnected):
            if self._saw_disconnect:
                self._reconnections += 1
                self._saw_disconnect = False

    def on_stream_error(self, error: StreamingError | str) -> None:
        self._consecutive_errors += 1
        self._last_error = str(error)

    def on_integrity_report(self, report: IntegrityReport) -> None:
        self._last_report_status = report.status.value
        self._last_report_evidence = report.evidence()
        if report.ok:
            self._gaps_recovered += 1
        else:
            self._gaps_unrecoverable += 1

    # ------------------------------------------------------------- snapshot

    def snapshot(
        self, pipeline: StreamingPipeline, *, now: datetime | None = None
    ) -> StreamingHealthSnapshot:
        """Construye un diagnóstico de solo lectura del pipeline."""
        connection = pipeline.manager.state.value
        pipeline_state = pipeline.engine.state.value
        integrity_unknown = pipeline.manager.data_integrity_unknown

        if integrity_unknown:
            integrity = IntegrityStatus.UNKNOWN.value
        elif self._last_report_status is not None:
            integrity = self._last_report_status
        else:
            integrity = IntegrityStatus.HEALTHY.value

        freshness: float | None = None
        if self._last_event_monotonic is not None:
            freshness = max(0.0, self._monotonic() - self._last_event_monotonic)

        if freshness is None:
            heartbeat = HeartbeatStatus.UNKNOWN
        elif freshness <= self._freshness_timeout:
            heartbeat = HeartbeatStatus.OK
        else:
            heartbeat = HeartbeatStatus.STALE

        level = self._level(
            connection=connection,
            pipeline_state=pipeline_state,
            integrity_unknown=integrity_unknown,
            heartbeat=heartbeat,
        )

        accepted = pipeline.closed_bars
        last_closed = (
            (accepted[-1].symbol, accepted[-1].open_time) if accepted else None
        )

        return StreamingHealthSnapshot(
            level=level,
            checked_at=now if now is not None else self._clock(),
            connection_state=connection,
            heartbeat=heartbeat,
            integrity=integrity,
            pipeline_state=pipeline_state,
            last_event_at=self._last_event_at,
            freshness_seconds=freshness,
            last_closed_bar=last_closed,
            reconnections=self._reconnections,
            gaps_recovered=self._gaps_recovered,
            gaps_unrecoverable=self._gaps_unrecoverable,
            consecutive_errors=self._consecutive_errors,
            last_error=self._last_error,
            last_integrity_evidence=self._last_report_evidence,
        )

    # ----------------------------------------------------------------- helpers

    def _level(
        self,
        *,
        connection: str,
        pipeline_state: str,
        integrity_unknown: bool,
        heartbeat: HeartbeatStatus,
    ) -> StreamingHealthLevel:
        if connection == "FAILED" or pipeline_state == "HALTED":
            return StreamingHealthLevel.FAILED
        if connection != "CONNECTED":
            return StreamingHealthLevel.UNKNOWN
        if integrity_unknown:
            return StreamingHealthLevel.UNKNOWN
        if heartbeat is HeartbeatStatus.UNKNOWN:
            return StreamingHealthLevel.UNKNOWN
        if (
            heartbeat is HeartbeatStatus.STALE
            or pipeline_state in ("DEGRADED", "RECONNECTING", "STALE")
            or self._consecutive_errors > 0
        ):
            return StreamingHealthLevel.DEGRADED
        return StreamingHealthLevel.HEALTHY


__all__ = [
    "HeartbeatStatus",
    "StreamingHealthLevel",
    "StreamingHealthMonitor",
    "StreamingHealthSnapshot",
]
