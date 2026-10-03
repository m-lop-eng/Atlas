"""MonitoringEngine (B6): produce estados de salud y los traduce a B5.

Arquitectura decidida en B6:
    Monitoring -> HealthStatus -> KillSwitchCoordinator.update(...)

El monitor DETECTA; B5 DECIDE el bloqueo. Este módulo solo mapea cada
resultado a una fuente de kill switch (set/clear); la precedencia y el
latch siguen viviendo exclusivamente en killswitches.coordinator.

NUNCA se envían ni cancelan órdenes aquí (sin adapter, sin API de órdenes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from killswitches.models import KillSource, KillSwitchState

from monitoring.checks import DEFAULT_CHECKS, utc_now
from monitoring.models import (
    HealthCheckResult,
    HealthStatus,
    MonitorComponent,
    MonitorConfig,
    MonitorSnapshot,
)
from monitoring.observations import Observations

SourceComponentMapping: dict[MonitorComponent, KillSource] = {
    MonitorComponent.DATA_FEED: KillSource.DATA_FEED,
    MonitorComponent.BROKER: KillSource.BROKER,
    MonitorComponent.RECONCILIATION: KillSource.RECONCILIATION,
    MonitorComponent.SESSION: KillSource.SESSION,
    MonitorComponent.SYSTEM: KillSource.SYSTEM,
    # EXECUTION no tiene fuente propia en B5: se reporta como SYSTEM (capa
    # de sistema), manteniendo el fuego cruzado fuera de Monitoring.
    MonitorComponent.EXECUTION: KillSource.SYSTEM,
}


@dataclass(frozen=True, slots=True)
class KillSwitchTransition:
    """Transición decidida por Monitoring hacia el KillSwitch (B5)."""

    component: MonitorComponent
    source: KillSource
    state: KillSwitchState | None  # None => clear_source
    reason: str


def _transition_for(
    result: HealthCheckResult,
) -> KillSwitchTransition:
    """Mapea UN resultado de salud a una transición de kill switch.

    Regla fail-safe B6: UNKNOWN y FAILED bloquean; DEGRADED despeja
    (la degradación se tolera, no es un fallo para el switch).
    """
    source = SourceComponentMapping[result.component]
    if result.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED):
        return KillSwitchTransition(
            result.component, source, None, result.reason
        )
    if result.status == HealthStatus.UNKNOWN:
        return KillSwitchTransition(
            result.component, source, KillSwitchState.HALT, result.reason
        )
    # FAILED (fail-safe):
    if result.component == MonitorComponent.RECONCILIATION:
        status = result.metrics.get("reconciliation_status")
        if status == "RECONCILIATION_BLOCKED":
            return KillSwitchTransition(
                result.component, source, KillSwitchState.BLOCK_NEW_ORDERS,
                result.reason,
            )
        return KillSwitchTransition(
            result.component, source, KillSwitchState.HALT, result.reason
        )
    if result.component == MonitorComponent.SYSTEM:
        return KillSwitchTransition(
            result.component, source, KillSwitchState.EMERGENCY, result.reason
        )
    return KillSwitchTransition(
        result.component, source, KillSwitchState.HALT, result.reason
    )


class MonitoringEngine:
    """Motor de monitoring con memoria diagnóstica y salida determinista.

    Memoria (no bloqueante): `last_success` y `consecutive_failures` por
    componente para auditoría. La FRESHNESS se deriva de las observaciones,
    no de esta memoria: un reinicio del motor no falsea HEALTHY si la
    observación no trae el último éxito.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        config: MonitorConfig | None = None,
        checks: Mapping[MonitorComponent, Callable] | None = None,
    ) -> None:
        self._clock = clock
        self.config = config or MonitorConfig()
        self._checks = dict(checks or DEFAULT_CHECKS)
        self._consecutive: dict[MonitorComponent, int] = {}
        self._last_success: dict[MonitorComponent, datetime] = {}
        self._last_snapshot: MonitorSnapshot | None = None

    def evaluate(self, observations: Observations | None = None) -> MonitorSnapshot:
        """Evalúa los checks sobre las observaciones del tick actual."""
        self._last_observations = observations
        now = self._clock()
        results: list[HealthCheckResult] = []
        for component in MonitorComponent.ordered():
            check = self._checks[component]
            obs = (
                getattr(observations, component.value.lower(), None)
                if observations is not None
                else None
            )
            status, reason, metrics = check(obs, now, self.config)
            if status in (HealthStatus.FAILED, HealthStatus.UNKNOWN):
                self._consecutive[component] = (
                    self._consecutive.get(component, 0) + 1
                )
            else:
                self._consecutive[component] = 0
            if status == HealthStatus.HEALTHY:
                self._last_success[component] = now
            results.append(
                HealthCheckResult(
                    component=component,
                    status=status,
                    at=now,
                    reason=reason,
                    last_success=self._last_success.get(component),
                    consecutive_failures=self._consecutive.get(component, 0),
                    metrics=dict(metrics),
                )
            )
        snapshot = MonitorSnapshot(at=now, results=tuple(results))
        self._last_snapshot = snapshot
        return snapshot

    def last_snapshot(self) -> MonitorSnapshot | None:
        return self._last_snapshot

    def transitions(self, snapshot: MonitorSnapshot | None = None) -> tuple[KillSwitchTransition, ...]:
        """Transiciones deducidas (monitor detecta; B5 decide)."""
        snapshot = snapshot or self._last_snapshot
        if snapshot is None:
            return ()
        return tuple(_transition_for(r) for r in snapshot.results)


def apply_to_kill_switch(coordinator, snapshot: MonitorSnapshot) -> None:
    """Representa las condiciones de Monitoring sobre un KillSwitchCoordinator (B5).

    Agregación por fuente: si varios componentes reportan sobre la MISMA fuente
    (p.ej. EXECUTION se proxya como SYSTEM), la fuente se representa con la
    **máxima severidad** entre ellos; un `set` de cualquier componente gana
    sobre un `clear` de otro componente de la misma fuente. Así un EMERGENCY de
    SYSTEM no puede ser degradado por un HALT de EXECUTION (colisión de
    último-escritor previa).

    Monitoring solo REPRESENTA las condiciones; la precedencia efectiva entre
    fuentes y el latch los decide B5 (`KillSwitchState.most_severe` + recovery
    explícito). No se replica aquí la política de decisión de B5.
    """
    transitions = tuple(_transition_for(r) for r in snapshot.results)
    sets: dict[KillSource, tuple[KillSwitchState, str]] = {}
    clears: set[KillSource] = set()
    for t in transitions:
        if t.state is None:
            clears.add(t.source)
            continue
        current = sets.get(t.source)
        if current is None or t.state.severity > current[0].severity:
            sets[t.source] = (t.state, t.reason)
    for source in clears - set(sets):
        coordinator.clear_source(source)
    for source, (state, reason) in sets.items():
        coordinator.set_source(source, state, detail=reason)