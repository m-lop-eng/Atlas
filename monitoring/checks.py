"""Checks de salud puros por componente (B6).

Cada check es una función pura `(obs, now, config) -> (status, reason, metrics)`.
NO contienen lógica específica de broker, sesión ni precedencia de bloqueo:
Monitoring detecta; `apply_to_kill_switch` (engine.py) traduce a B5.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from monitoring.models import HealthStatus, MonitorComponent, MonitorConfig

CheckOutcome = tuple[HealthStatus, str, dict]


def _fresh(
    last: datetime | None,
    now: datetime,
    max_age_s: float,
    label: str,
) -> CheckOutcome:
    """Regla de freshness: sin éxito previo => UNKNOWN; antiguo => STALE."""
    if last is None:
        return (
            HealthStatus.UNKNOWN,
            f"{label}: nunca hubo un éxito registrado",
            {},
        )
    age = (now - last).total_seconds()
    if age > max_age_s:
        return (
            HealthStatus.FAILED,
            f"{label}: último éxito hace {age:.1f}s (max {max_age_s:.0f}s)",
            {"age_s": age},
        )
    return (
        HealthStatus.HEALTHY,
        f"{label}: respuesta reciente ({age:.1f}s)",
        {"age_s": age},
    )


def _age(last: datetime | None, now: datetime) -> float | None:
    if last is None:
        return None
    return (now - last).total_seconds()


def data_feed_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None:
        return (HealthStatus.UNKNOWN, "sin observación de data", {})
    state = obs.engine_state
    quality = obs.quality
    if state in ("HALTED", "DISCONNECTED", "RECONNECTING"):
        return (
            HealthStatus.FAILED,
            f"data engine {state}",
            {"engine_state": state},
        )
    if state == "STALE":
        return (HealthStatus.FAILED, "data stale detectado por el engine", {})
    if state == "CONNECTING":
        return (
            HealthStatus.UNKNOWN,
            "data engine conectando",
            {"engine_state": state},
        )
    if quality in ("INVALID_TIMESTAMP", "INVALID_OHLC", "OUT_OF_ORDER"):
        return (
            HealthStatus.FAILED,
            f"data inválida ({quality})",
            {"quality": quality},
        )
    if state == "DEGRADED":
        return (
            HealthStatus.DEGRADED,
            "data engine degradado",
            {"engine_state": state},
        )
    if quality in ("GAP_DETECTED", "DUPLICATE"):
        return (
            HealthStatus.DEGRADED,
            f"data con incidencias ({quality})",
            {"quality": quality},
        )
    if obs.consecutive_gaps > cfg.data_max_consecutive_gaps:
        return (
            HealthStatus.DEGRADED,
            f"gaps consecutivos {obs.consecutive_gaps}",
            {"consecutive_gaps": obs.consecutive_gaps},
        )
    # CONNECTED + quality HEALTHY: freshness del último evento.
    if obs.last_event_at is None:
        return (
            HealthStatus.UNKNOWN,
            "data: nunca llegó un evento",
            {},
        )
    age = _age(obs.last_event_at, now)
    if age is not None and age > cfg.data_max_age_s:
        return (
            HealthStatus.FAILED,
            f"data stale: último evento hace {age:.1f}s",
            {"age_s": age},
        )
    return (
        HealthStatus.HEALTHY,
        "data feed fresco",
        {"age_s": age, "engine_state": state},
    )


def broker_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None:
        return (HealthStatus.UNKNOWN, "sin observación de broker", {})
    if not obs.connected:
        return (
            HealthStatus.FAILED,
            "broker desconectado",
            {"connected": False},
        )
    if obs.consecutive_errors >= cfg.broker_max_consecutive_errors:
        return (
            HealthStatus.FAILED,
            f"broker: {obs.consecutive_errors} errores consecutivos",
            {"consecutive_errors": obs.consecutive_errors},
        )
    status, reason, metrics = _fresh(
        obs.last_success, now, cfg.broker_max_age_s, "broker"
    )
    if obs.last_success is not None:
        metrics["latency_s"] = obs.last_latency_s
        if (
            obs.last_latency_s is not None
            and obs.last_latency_s > cfg.broker_max_latency_s
            and status == HealthStatus.HEALTHY
        ):
            return (
                HealthStatus.DEGRADED,
                f"broker: latencia alta ({obs.last_latency_s:.1f}s)",
                metrics,
            )
    return status, reason, metrics


def reconciliation_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None or obs.status is None:
        return (
            HealthStatus.UNKNOWN,
            "reconciliación: sin resultado todavía",
            {},
        )
    metrics = {"reconciliation_status": obs.status}
    if obs.status == "RECONCILIATION_OK":
        if obs.checked_at is None:
            return (
                HealthStatus.UNKNOWN,
                "reconciliación OK pero sin marca de tiempo",
                metrics,
            )
        age = _age(obs.checked_at, now)
        if age is not None and age > cfg.reconciliation_max_ok_age_s:
            return (
                HealthStatus.DEGRADED,
                f"reconciliación OK desactualizada ({age:.0f}s)",
                {**metrics, "age_s": age},
            )
        return (
            HealthStatus.HEALTHY,
            "reconciliación OK y fresca",
            {**metrics, "age_s": age},
        )
    if obs.status == "RECONCILIATION_UNKNOWN":
        return (
            HealthStatus.FAILED,
            "reconciliación UNKNOWN (fail-safe: nunca es OK)",
            metrics,
        )
    if obs.status == "RECONCILIATION_BLOCKED":
        return (
            HealthStatus.FAILED,
            "reconciliación BLOCKED",
            metrics,
        )
    return (HealthStatus.UNKNOWN, f"reconciliación estado {obs.status}", metrics)


def session_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None or obs.state is None:
        return (HealthStatus.UNKNOWN, "sin observación de sesión", {})
    metrics = {"session_state": obs.state}
    if obs.state in ("UNKNOWN", "HALTED"):
        return (
            HealthStatus.FAILED,
            f"sesión {obs.state}",
            metrics,
        )
    if obs.as_of is None:
        return (
            HealthStatus.UNKNOWN,
            "sesión sin timestamp válido",
            metrics,
        )
    return (
        HealthStatus.HEALTHY,
        f"sesión {obs.state} con timestamp válido",
        metrics,
    )


def system_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None:
        return (HealthStatus.UNKNOWN, "sin observación de sistema", {})
    clock = obs.clock if obs.clock is not None else now
    # Reloj: debe ser UTC-aware (naive/local => riesgo de DST y comparaciones).
    if clock.tzinfo is None or clock.utcoffset() != timedelta(0):
        return (
            HealthStatus.FAILED,
            "reloj no UTC-aware (posible DST/naive)",
            {"clock": clock.isoformat()},
        )
    if obs.heartbeat_at is None:
        return (
            HealthStatus.UNKNOWN,
            "sistema: nunca hubo heartbeat",
            {},
        )
    age = _age(obs.heartbeat_at, now)
    if age is not None and age > cfg.system_max_heartbeat_age_s:
        return (
            HealthStatus.FAILED,
            f"heartbeat stale ({age:.1f}s)",
            {"age_s": age},
        )
    if obs.unhandled_errors > 0:
        return (
            HealthStatus.FAILED,
            f"{obs.unhandled_errors} excepciones no controladas",
            {"unhandled_errors": obs.unhandled_errors},
        )
    if obs.persistence_ok is False:
        return (
            HealthStatus.FAILED,
            "capacidad de persistencia fallando",
            {"persistence_ok": False},
        )
    return (HealthStatus.HEALTHY, "sistema sano", {"age_s": age})


def execution_check(
    obs, now: datetime, cfg: MonitorConfig
) -> CheckOutcome:
    if obs is None:
        return (HealthStatus.UNKNOWN, "sin observación de ejecución", {})
    metrics = {
        "open_orders": obs.open_orders,
        "unknown_orders": obs.unknown_orders,
        "stalled_orders": obs.stalled_orders,
    }
    if obs.unknown_orders > cfg.execution_max_unknown_orders:
        return (
            HealthStatus.FAILED,
            f"{obs.unknown_orders} órdenes UNKNOWN",
            metrics,
        )
    if obs.stalled_orders > cfg.execution_max_stalled_orders:
        return (
            HealthStatus.DEGRADED,
            f"{obs.stalled_orders} operaciones atascadas",
            metrics,
        )
    if obs.open_orders > cfg.execution_max_pending_orders:
        return (
            HealthStatus.DEGRADED,
            f"congestión: {obs.open_orders} órdenes abiertas",
            metrics,
        )
    return (HealthStatus.HEALTHY, "ejecución sana", metrics)


DEFAULT_CHECKS: dict[MonitorComponent, Callable] = {
    MonitorComponent.DATA_FEED: data_feed_check,
    MonitorComponent.BROKER: broker_check,
    MonitorComponent.RECONCILIATION: reconciliation_check,
    MonitorComponent.SESSION: session_check,
    MonitorComponent.SYSTEM: system_check,
    MonitorComponent.EXECUTION: execution_check,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)