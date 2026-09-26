"""Coordinador de Kill Switches (B5).

Controla la AUTORIZACIÓN de nuevas operaciones: expone `status().can_trade`
como señal hacia OrderManager / Risk boundary. NO liquida posiciones, NO
envía órdenes y NO contacta con ningún broker (no tiene adapter): solo
agrega condiciones reportadas por fuentes externas (B1/B2/B4/manual).

Precedencia determinista: la condición más severa (EMERGENCY > HALT >
BLOCK_NEW_ORDERS) domina; eliminar una fuente NUNCA anula a otra.

Recovery EXPLÍCITO y SIN auto-desbloqueo:

    BLOCK
      → CONDITION CLEARED
      → RECONCILIATION OK
      → HEALTH CHECKS OK
      → EXPLICIT RECOVERY (recover con motivo)
      → ALLOW

Aunque todas las condiciones desaparezcan, el gate `recovery_required`
permanece abierto/cerrado según corresponda: el sistema vuelve a ALLOW solo
tras `recover()` explícito con motivo (auditable). (06 §31, §67, §68)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from reconciliation.models import ReconciliationStatus
from sessions.manager import SessionState
from killswitches.models import (
    AuditEntry,
    KillSource,
    KillSwitchState,
    KillSwitchStatus,
    RecoveryResult,
    SourceStatus,
    now_utc,
)


@dataclass(slots=True)
class KillSwitchCoordinator:
    """Agrega condiciones de bloqueo y produce una postura efectiva.

    Attributes:
        clock: Callable() -> datetime (inyectable para determinismo).
    """

    clock: Callable[[], datetime] = now_utc
    _sources: dict[KillSource, SourceStatus] = field(
        init=False, default_factory=dict
    )
    _recovery_required: bool = field(init=False, default=False)
    _audit: list[AuditEntry] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:  # pragma: no cover - no-op de claridad
        pass

    # ---------------------------------------------------------------- estado

    def status(self) -> KillSwitchStatus:
        """Estado efectivo (puro y determinista a partir de las fuentes)."""
        active = tuple(
            (source, cond)
            for source, cond in sorted(self._sources.items(), key=lambda kv: kv[0].value)
            if cond.level != KillSwitchState.ALLOW
        )
        if active:
            state = KillSwitchState.most_severe(
                tuple(cond.level for _, cond in active)
            )
        elif self._recovery_required:
            # Sin condiciones activas pero sin recovery explícito:
            # permanecer bloqueado (BLOCK_NEW_ORDERS es el mínimo).
            state = KillSwitchState.BLOCK_NEW_ORDERS
        else:
            state = KillSwitchState.ALLOW
        return KillSwitchStatus(
            state=state,
            recovery_required=self._recovery_required,
            active=active,
        )

    @property
    def audit_log(self) -> tuple[AuditEntry, ...]:
        return tuple(self._audit)

    # ------------------------------------------------------------- fuentes

    def set_source(
        self,
        source: KillSource,
        level: KillSwitchState,
        detail: str = "",
    ) -> KillSwitchState:
        """Reporta una condición activa de una fuente (idempotente).

        `level` debe ser una condición real (nunca ALLOW; para despejar se
        usa `clear_source`).
        """
        if level == KillSwitchState.ALLOW:
            raise ValueError(
                "set_source no acepta ALLOW (usar clear_source para despejar)"
            )
        previous = self._sources.get(source)
        if previous is not None and previous.level == level:
            # Idempotente: no cambia estado ni duplica auditoría.
            return self.status().state

        self._sources[source] = SourceStatus(
            level=level,
            detail=detail,
            at=self.clock(),
        )
        # LATCH del gate: toda activación de bloqueo exige un recovery
        # posterior EXPLÍCITO (sin auto-desbloqueo cuando se despeje).
        self._recovery_required = True
        self._audit.append(
            AuditEntry(
                at=self.clock(),
                action="ACTIVATION",
                source=source.value,
                previous_level=previous.level.value if previous else "ALLOW",
                new_level=level.value,
                detail=detail,
            )
        )
        return self.status().state

    def clear_source(self, source: KillSource, detail: str = "") -> KillSwitchState:
        """Despeja la condición de una fuente (NO desbloquea por sí solo)."""
        previous = self._sources.get(source)
        if previous is None:
            return self.status().state

        self._sources[source] = SourceStatus(
            level=KillSwitchState.ALLOW,
            detail=detail,
            at=self.clock(),
        )
        self._audit.append(
            AuditEntry(
                at=self.clock(),
                action="CLEAR",
                source=source.value,
                previous_level=previous.level.value,
                new_level="ALLOW",
                detail=detail,
            )
        )
        # No se vuelve a ALLOW automáticamente: el gate manda.
        return self.status().state

    # ------------------------------------------------------------- recovery

    def recover(
        self,
        reason: str,
        health_checks_ok: bool = True,
    ) -> RecoveryResult:
        """Recovery explícito Y auditable; rechaza si cualquier bloqueo
        sigue activo (o si apareció otro durante la operación)."""
        current = self.status()
        if reason == "":
            return RecoveryResult(
                accepted=False,
                state=current.state,
                detail="se requiere un motivo explícito para el recovery",
            )
        if current.active:
            active_names = ", ".join(s.value for s, _ in current.active)
            self._audit.append(
                AuditEntry(
                    at=self.clock(),
                    action="RECOVERY_REJECTED",
                    source=None,
                    previous_level=current.state.value,
                    new_level=current.state.value,
                    detail=f"bloqueos activos: {active_names}",
                )
            )
            return RecoveryResult(
                accepted=False,
                state=current.state,
                detail=f"bloqueos activos: {active_names}",
            )
        if not health_checks_ok:
            self._audit.append(
                AuditEntry(
                    at=self.clock(),
                    action="RECOVERY_REJECTED",
                    source=None,
                    previous_level=current.state.value,
                    new_level=current.state.value,
                    detail="health checks no verificados",
                )
            )
            return RecoveryResult(
                accepted=False,
                state=current.state,
                detail="health checks no verificados",
            )

        self._recovery_required = False
        self._audit.append(
            AuditEntry(
                at=self.clock(),
                action="RECOVERY",
                source=None,
                previous_level=current.state.value,
                new_level=KillSwitchState.ALLOW.value,
                detail=reason,
            )
        )
        return RecoveryResult(
            accepted=True,
            state=KillSwitchState.ALLOW,
            detail=reason,
        )

    # ---------------------------------------------------- persistencia/restart

    def snapshot(self) -> dict:
        """Estado serializable para conservar tras un restart."""
        return {
            "recovery_required": self._recovery_required,
            "sources": [
                {
                    "source": source.value,
                    "level": cond.level.value,
                    "detail": cond.detail,
                    "at": cond.at.isoformat(),
                }
                for source, cond in sorted(
                    self._sources.items(), key=lambda kv: kv[0].value
                )
            ],
        }

    def load_snapshot(self, snap: dict) -> None:
        """Restaura estado previo tras restart (nunca auto-ALLOW).

        Si antes del restart había un gate o condición, se conserva: el
        sistema NO reautoriza por defecto.
        """
        self._sources = {}
        self._recovery_required = bool(snap["recovery_required"])
        for entry in snap.get("sources", []):
            source = KillSource(entry["source"])
            level = KillSwitchState(entry["level"])
            if level != KillSwitchState.ALLOW:
                self._sources[source] = SourceStatus(
                    level=level,
                    detail=entry.get("detail", ""),
                    at=self.clock(),
                )
        self._audit.append(
            AuditEntry(
                at=self.clock(),
                action="RESTORE",
                source=None,
                previous_level="(restart)",
                new_level=self.status().state.value,
                detail=(
                    "estado restaurado tras restart; "
                    "requiere recovery explícito"
                ),
            )
        )

    # ---------------------------------------------------------- integración

    def update_from_reconciliation(
        self, status: ReconciliationStatus, detail: str = ""
    ) -> KillSwitchState:
        """Conecta B4 al kill switch (B5) sin mezclar los componentes.

        RECONCILIATION_BLOCKED -> BLOCK_NEW_ORDERS (no cerrar posiciones).
        RECONCILIATION_UNKNOWN -> HALT (no se interpreta jamás como OK).
        RECONCILIATION_OK      -> se despeja la condición (el gate manda).
        """
        if status == ReconciliationStatus.RECONCILIATION_BLOCKED:
            return self.set_source(
                KillSource.RECONCILIATION,
                KillSwitchState.BLOCK_NEW_ORDERS,
                detail=detail or "reconciliation BLOCKED",
            )
        if status == ReconciliationStatus.RECONCILIATION_UNKNOWN:
            return self.set_source(
                KillSource.RECONCILIATION,
                KillSwitchState.HALT,
                detail=detail or "reconciliation UNKNOWN (fail-safe)",
            )
        return self.clear_source(
            KillSource.RECONCILIATION,
            detail="reconciliation OK",
        )

    def update_from_session(
        self, session: SessionState, detail: str = ""
    ) -> KillSwitchState:
        """Conecta B2 al kill switch (B5).

        OPEN/PRE_OPEN/CLOSING/CLOSED son estados normales (la prohibición
        de operar fuera de sesión es política del RiskEngine, no un fallo).
        UNKNOWN/HALTED son fallos reales: HALT.
        """
        if session in (SessionState.UNKNOWN, SessionState.HALTED):
            return self.set_source(
                KillSource.SESSION,
                KillSwitchState.HALT,
                detail=detail or f"sesión {session.value}",
            )
        return self.clear_source(
            KillSource.SESSION,
            detail="sesión normal",
        )


__all__ = ["KillSwitchCoordinator"]