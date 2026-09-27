"""Backfill + reconciliación de integridad (S4).

Pregunta de S4 (distinta de S3): tras una reconexión,
¿puedo DEMOSTRAR que no perdí ni dupliqué datos?

Flujo:

    reconnect -> DATA_INTEGRITY_UNKNOWN
        -> identificar rango potencialmente perdido  [última_vela+intervalo, resume)
        -> BackfillProvider.fetch_closed_bars(...)
        -> validar / ordenar / deduplicar / detectar conflictos
            -> conflicto / dato inválido  -> BLOCKED
            -> hueco no cubierto          -> UNRECOVERABLE
            -> secuencia contigua íntegra -> HEALTHY
        -> (solo si HEALTHY) confirm_data_integrity()
        -> HEALTHY

PROHIBIDO EL ATAJO: `reconnect() -> confirm_data_integrity()`. El único camino
a la confirmación es un `IntegrityReport` HEALTHY con evidencia de que la
secuencia fue comprobada. Si el backfill falla, la integridad sigue UNKNOWN y
NO hay autorización para seguir (el `IntegrityCoordinator` no confirma).

Fuera de S4: integración con `LiveDataEngine`/B1, PaperRunner, estrategia,
órdenes, broker y capital.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from live.adapter import BackfillProvider
from live.events import MarketDataEvent

from streaming.reconnect import ReconnectManager, ReconnectState


class IntegrityStatus(str, Enum):
    """Resultado de la reconciliación de integridad de la secuencia."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    UNRECOVERABLE = "UNRECOVERABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Evidencia auditable de la comprobación de la secuencia."""

    status: IntegrityStatus
    symbol: str
    interval_seconds: int
    expected_start: int | None
    resume_open_time: int | None
    recovered: tuple[MarketDataEvent, ...] = ()
    missing: tuple[int, ...] = ()
    duplicates: tuple[int, ...] = ()
    conflicts: tuple[int, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Solo una secuencia HEALTHY autoriza a confirmar integridad."""
        return self.status is IntegrityStatus.HEALTHY

    def evidence(self) -> dict:
        """Resumen serializable para audit trail."""
        return {
            "status": self.status.value,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "expected_start": self.expected_start,
            "resume_open_time": self.resume_open_time,
            "recovered": len(self.recovered),
            "recovered_first": self.recovered[0].open_time if self.recovered else None,
            "recovered_last": self.recovered[-1].open_time if self.recovered else None,
            "missing": list(self.missing),
            "duplicates": list(self.duplicates),
            "conflicts": list(self.conflicts),
            "reason": self.reason,
        }


class IntegrityReconciler:
    """Comprueba/recupera la secuencia entre la última vela y la reanudación.

    Args:
        provider: `BackfillProvider` (mismo contrato B1) con las velas históricas.
        symbol: instrumento.
        interval_seconds: intervalo de las velas.
    """

    def __init__(
        self,
        provider: BackfillProvider,
        symbol: str,
        interval_seconds: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds debe ser > 0")
        self._provider = provider
        self._symbol = symbol
        self._interval = interval_seconds

    def reconcile(
        self,
        *,
        last_open_time: int | None,
        resume_open_time: int | None,
    ) -> IntegrityReport:
        """Reconcilia `[last_open_time + interval, resume_open_time)`."""

        def report(status: IntegrityStatus, **kwargs) -> IntegrityReport:
            base = dict(
                status=status,
                symbol=self._symbol,
                interval_seconds=self._interval,
                expected_start=last_open_time + self._interval if last_open_time else None,
                resume_open_time=resume_open_time,
            )
            base.update(kwargs)
            return IntegrityReport(**base)

        if last_open_time is None:
            return report(
                IntegrityStatus.UNKNOWN,
                reason="sin vela previa; rango potencialmente perdido no identificable",
            )
        if resume_open_time is None:
            return report(
                IntegrityStatus.UNKNOWN,
                reason="extremo superior no acotado; no se puede demostrar continuidad",
            )

        expected_start = last_open_time + self._interval
        if resume_open_time == expected_start:
            return report(IntegrityStatus.HEALTHY, reason="sin hueco")
        if resume_open_time < expected_start:
            return report(
                IntegrityStatus.DEGRADED,
                reason="reanudación anterior a la esperada (solape/duplicado)",
            )

        try:
            bars = self._provider.fetch_closed_bars(
                self._symbol, self._interval, expected_start
            )
        except ConnectionError as exc:
            return report(
                IntegrityStatus.BLOCKED,
                reason=f"backfill no disponible: {exc}",
            )

        by_time: dict[int, MarketDataEvent] = {}
        duplicates: list[int] = []
        conflicts: list[int] = []

        for event in bars:
            if event.open_time < expected_start or event.open_time >= resume_open_time:
                continue  # fuera del rango a reconciliar
            if not self._valid(event):
                return report(
                    IntegrityStatus.BLOCKED,
                    reason=f"dato de backfill inválido en open_time {event.open_time}",
                )
            existing = by_time.get(event.open_time)
            if existing is None:
                by_time[event.open_time] = event
                continue
            if self._same(existing, event):
                duplicates.append(event.open_time)
            else:
                conflicts.append(event.open_time)

        if conflicts:
            return report(
                IntegrityStatus.BLOCKED,
                duplicates=tuple(sorted(set(duplicates))),
                conflicts=tuple(sorted(set(conflicts))),
                reason="conflicto: mismo open_time con OHLC distinto",
            )

        recovered: list[MarketDataEvent] = []
        missing: list[int] = []
        stamp = expected_start
        while stamp < resume_open_time:
            event = by_time.get(stamp)
            if event is None:
                missing.append(stamp)
            else:
                recovered.append(event)
            stamp += self._interval

        if missing:
            return report(
                IntegrityStatus.UNRECOVERABLE,
                recovered=tuple(recovered),
                missing=tuple(missing),
                duplicates=tuple(sorted(set(duplicates))),
                reason=f"hueco no recuperable: faltan {len(missing)} velas",
            )

        return report(
            IntegrityStatus.HEALTHY,
            recovered=tuple(recovered),
            duplicates=tuple(sorted(set(duplicates))),
            reason="secuencia contigua verificada",
        )

    def reconcile_open_ended(self, *, start_open_time: int) -> IntegrityReport:
        """Reconcilia desde `start_open_time` sin cota superior (modo B1).

        Usado por `LiveDataEngine.reconcile()` (S5): recupera el prefijo
        contiguo desde `start_open_time`. No exige un extremo superior porque
        B1 valida la frontera viva con la siguiente vela. Si el proveedor no
        aporta ninguna vela o faltan las iniciales, no hay recuperación.
        """

        def report(status: IntegrityStatus, **kwargs) -> IntegrityReport:
            base = dict(
                status=status,
                symbol=self._symbol,
                interval_seconds=self._interval,
                expected_start=start_open_time,
                resume_open_time=None,
            )
            base.update(kwargs)
            return IntegrityReport(**base)

        try:
            bars = self._provider.fetch_closed_bars(
                self._symbol, self._interval, start_open_time
            )
        except ConnectionError as exc:
            return report(
                IntegrityStatus.BLOCKED,
                reason=f"backfill no disponible: {exc}",
            )

        by_time: dict[int, MarketDataEvent] = {}
        duplicates: list[int] = []
        conflicts: list[int] = []
        for event in bars:
            if event.open_time < start_open_time:
                continue
            if not self._valid(event):
                return report(
                    IntegrityStatus.BLOCKED,
                    reason=f"dato de backfill inválido en open_time {event.open_time}",
                )
            existing = by_time.get(event.open_time)
            if existing is None:
                by_time[event.open_time] = event
            elif self._same(existing, event):
                duplicates.append(event.open_time)
            else:
                conflicts.append(event.open_time)

        if conflicts:
            return report(
                IntegrityStatus.BLOCKED,
                duplicates=tuple(sorted(set(duplicates))),
                conflicts=tuple(sorted(set(conflicts))),
                reason="conflicto: mismo open_time con OHLC distinto",
            )

        recovered: list[MarketDataEvent] = []
        stamp = start_open_time
        while stamp in by_time:
            recovered.append(by_time[stamp])
            stamp += self._interval

        if not recovered:
            return report(
                IntegrityStatus.UNRECOVERABLE,
                duplicates=tuple(sorted(set(duplicates))),
                reason="sin velas recuperables desde el inicio esperado",
            )

        return report(
            IntegrityStatus.HEALTHY,
            recovered=tuple(recovered),
            duplicates=tuple(sorted(set(duplicates))),
            reason="secuencia contigua verificada (extremo abierto)",
        )

    # ----------------------------------------------------------------- helpers

    def _valid(self, event: MarketDataEvent) -> bool:
        if event.symbol.upper() != self._symbol.upper():
            return False
        if not event.is_closed:
            return False
        if event.open_time <= 0 or event.open_time % self._interval != 0:
            return False
        values = (event.open, event.high, event.low, event.close, event.volume)
        if any(v is None or v != v or abs(v) == float("inf") for v in values):
            return False
        if event.open <= 0 or event.close <= 0 or event.volume < 0:
            return False
        return (
            event.high >= max(event.open, event.close)
            and event.low <= min(event.open, event.close)
            and event.low <= event.high
        )

    @staticmethod
    def _same(a: MarketDataEvent, b: MarketDataEvent) -> bool:
        return (
            a.open,
            a.high,
            a.low,
            a.close,
            a.volume,
        ) == (
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
        )


class IntegrityCoordinator:
    """Une `ReconnectManager` + `IntegrityReconciler` sin atajos.

    Confirma la integridad SOLO cuando el reconciliador devuelve un reporte
    HEALTHY. Nunca confirma tras una reconexión por sí mismo.
    """

    def __init__(
        self,
        manager: ReconnectManager,
        reconciler: IntegrityReconciler,
    ) -> None:
        self._manager = manager
        self._reconciler = reconciler

    @property
    def pending(self) -> bool:
        """True si hay una integridad pendiente de demostrar."""
        return self._manager.data_integrity_unknown

    @property
    def can_operate(self) -> bool:
        """Autorización: conectado Y con integridad demostrada."""
        return (
            self._manager.state is ReconnectState.CONNECTED
            and not self._manager.data_integrity_unknown
        )

    def reconcile_data_integrity(self, resume_open_time: int) -> IntegrityReport:
        """Reconcilia y, solo si la secuencia es HEALTHY, confirma integridad.

        `resume_open_time` es el `open_time` de la primera vela cerrada
        observada tras la reconexión (límite superior exclusivo del rango
        potencialmente perdido).
        """
        last = self._manager.last_closed_bar
        last_open_time = last[1] if last is not None else None
        report = self._reconciler.reconcile(
            last_open_time=last_open_time,
            resume_open_time=resume_open_time,
        )
        if report.ok:
            self._manager.confirm_data_integrity()
        return report


__all__ = [
    "IntegrityCoordinator",
    "IntegrityReconciler",
    "IntegrityReport",
    "IntegrityStatus",
]
