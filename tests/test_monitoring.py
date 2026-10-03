"""Tests de Monitoring (B6).

Cubre la batería obligatoria acordada: healthy inicial, stale por timeout,
recuperación de health, broker desconectado, reconciliation UNKNOWN, fallos
simultáneos, reloj UTC/DST, thresholds configurables, ausencia de falsos
HEALTHY sin éxito previo, integración Monitoring -> KillSwitch, el monitor
nunca envía/cancela, restart, determinismo, y el caso crítico: Monitoring se
recupera pero B5 permanece latched hasta recover() explícito.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from killswitches.coordinator import KillSwitchCoordinator
from killswitches.models import KillSwitchState

from monitoring import (
    HealthStatus,
    MonitorComponent,
    MonitorConfig,
    MonitoringEngine,
    Observations,
    apply_to_kill_switch,
    collect,
    observe_broker,
    observe_data_feed,
    observe_execution,
    observe_reconciliation,
    observe_session,
    observe_system,
)

T0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def __call__(self) -> datetime:
        return self._value


def _engine(clock_value: datetime = T0, config: MonitorConfig | None = None):
    return MonitoringEngine(clock=_FixedClock(clock_value), config=config)


def _healthy_observations(now: datetime = T0) -> Observations:
    return collect(
        data_feed=observe_data_feed(
            "CONNECTED", "HEALTHY", now - timedelta(seconds=1), 0
        ),
        broker=observe_broker(
            _FakeAdapter(True), now - timedelta(seconds=1), 0, 0.2
        ),
        reconciliation=observe_reconciliation(
            "RECONCILIATION_OK", now - timedelta(seconds=5)
        ),
        session=observe_session("OPEN", now),
        system=observe_system(now, heartbeat_at=now - timedelta(seconds=2)),
        execution=observe_execution(
            (_FakeOrder("APPROVED"), _FakeOrder("SUBMITTED")), 0
        ),
    )


class _FakeAdapter:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    def health(self) -> bool:
        return self._healthy


class _FakeOrder:
    def __init__(self, status: str) -> None:
        self.status = status


class TestHealthyInicial:
    def test_todos_los_componentes_healthy(self) -> None:
        snap = _engine().evaluate(_healthy_observations())
        for component in MonitorComponent.ordered():
            assert snap.get(component).status == HealthStatus.HEALTHY


class TestStalePorTimeout:
    def test_data_feed_stale_failed(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=31), 0
            ),
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        snap = _engine().evaluate(obs)
        result = snap.get(MonitorComponent.DATA_FEED)
        assert result.status == HealthStatus.FAILED
        assert "stale" in result.reason

    def test_mas_alla_del_umbral_sigue_failed(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(minutes=5), 0
            ),
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        assert (
            _engine().evaluate(obs).get(MonitorComponent.DATA_FEED).status
            == HealthStatus.FAILED
        )


class TestRecuperacionDeHealth:
    def test_failed_recupera_a_healthy(self) -> None:
        now = T0
        engine = _engine()
        stale = _healthy_observations(now)
        stale = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=45), 0
            ),
            broker=stale.broker,
            reconciliation=stale.reconciliation,
            session=stale.session,
            system=stale.system,
            execution=stale.execution,
        )
        failed = engine.evaluate(stale)
        assert failed.get(MonitorComponent.DATA_FEED).status == HealthStatus.FAILED
        fresh = _healthy_observations(now)
        recovered = engine.evaluate(fresh)
        result = recovered.get(MonitorComponent.DATA_FEED)
        assert result.status == HealthStatus.HEALTHY
        assert result.consecutive_failures == 0
        assert result.last_success == now


class TestBrokerDesconectado:
    def test_desconectado_failed(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=observe_broker(_FakeAdapter(False)),
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.BROKER)
        assert result.status == HealthStatus.FAILED
        assert "desconectado" in result.reason


class TestReconciliation:
    def test_unknown_failed(self) -> None:
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=observe_reconciliation("RECONCILIATION_UNKNOWN", T0),
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.RECONCILIATION)
        assert result.status == HealthStatus.FAILED

    def test_blocked_failed_con_marca(self) -> None:
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=observe_reconciliation("RECONCILIATION_BLOCKED", T0),
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.RECONCILIATION)
        assert result.status == HealthStatus.FAILED
        assert result.metrics["reconciliation_status"] == "RECONCILIATION_BLOCKED"


class TestFallosSimultaneos:
    def test_multiple_componentes_failed(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed("STALE", "STALE", None, 0),
            broker=observe_broker(_FakeAdapter(False)),
            reconciliation=observe_reconciliation("RECONCILIATION_UNKNOWN", now),
            session=observe_session("HALTED", now),
            system=obs.system,
            execution=obs.execution,
        )
        snap = _engine().evaluate(obs)
        failed = snap.by_status(HealthStatus.FAILED)
        assert {r.component for r in failed} == {
            MonitorComponent.DATA_FEED,
            MonitorComponent.BROKER,
            MonitorComponent.RECONCILIATION,
            MonitorComponent.SESSION,
        }


class TestReloj:
    def test_reloj_naive_failed(self) -> None:
        now_naive = datetime(2026, 5, 4, 12, 0, 0)
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now_naive, heartbeat_at=now_naive),
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.SYSTEM)
        assert result.status == HealthStatus.FAILED
        assert "UTC" in result.reason

    def test_reloj_utc_aware_ok(self) -> None:
        obs = _healthy_observations()
        result = _engine().evaluate(obs).get(MonitorComponent.SYSTEM)
        assert result.status == HealthStatus.HEALTHY

    def test_reloj_con_offset_no_utc_failed(self) -> None:
        now = datetime(2026, 3, 10, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now, heartbeat_at=now),
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.SYSTEM)
        assert result.status == HealthStatus.FAILED


class TestThresholdsConfigurables:
    def test_threshold_mas_estricto_falla_lo_que_por_defecto_es_ok(self) -> None:
        cfg = MonitorConfig(broker_max_age_s=2.0)
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=observe_broker(_FakeAdapter(True), now - timedelta(seconds=3), 0, 0.1),
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        assert (
            _engine(config=cfg).evaluate(obs).get(MonitorComponent.BROKER).status
            == HealthStatus.FAILED
        )
        assert (
            _engine().evaluate(obs).get(MonitorComponent.BROKER).status
            == HealthStatus.HEALTHY
        )

    def test_umbral_de_errores_broker(self) -> None:
        cfg = MonitorConfig(broker_max_consecutive_errors=2)
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=observe_broker(_FakeAdapter(True), now, 2, 0.1),
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        assert (
            _engine(config=cfg).evaluate(obs).get(MonitorComponent.BROKER).status
            == HealthStatus.FAILED
        )


class TestNoFalsosHealthy:
    @pytest.mark.parametrize(
        "override",
        [
            "broker",
            "data",
            "reconciliation",
            "execution",
        ],
    )
    def test_sin_exito_previo_nunca_healthy(self, override: str) -> None:
        now = T0
        obs = _healthy_observations(now)
        if override == "broker":
            obs = Observations(
                data_feed=obs.data_feed,
                broker=observe_broker(_FakeAdapter(True), None, 0, 0.2),
                reconciliation=obs.reconciliation,
                session=obs.session,
                system=obs.system,
                execution=obs.execution,
            )
        elif override == "data":
            obs = Observations(
                data_feed=observe_data_feed("CONNECTED", "HEALTHY", None, 0),
                broker=obs.broker,
                reconciliation=obs.reconciliation,
                session=obs.session,
                system=obs.system,
                execution=obs.execution,
            )
        elif override == "reconciliation":
            obs = Observations(
                data_feed=obs.data_feed,
                broker=obs.broker,
                reconciliation=observe_reconciliation(None, None),
                session=obs.session,
                system=obs.system,
                execution=obs.execution,
            )
        else:
            obs = Observations(
                data_feed=obs.data_feed,
                broker=obs.broker,
                reconciliation=obs.reconciliation,
                session=obs.session,
                system=obs.system,
                execution=None,  # sin observación de ejecución => UNKNOWN
            )
        snap = _engine().evaluate(obs)
        mapping = {
            "broker": MonitorComponent.BROKER,
            "data": MonitorComponent.DATA_FEED,
            "reconciliation": MonitorComponent.RECONCILIATION,
            "execution": MonitorComponent.EXECUTION,
        }
        assert snap.get(mapping[override]).status == HealthStatus.UNKNOWN


class TestMonitorNeverSubmitsOrCancels:
    def test_motor_sin_adapter_ni_api_de_ordenes(self) -> None:
        engine = _engine()
        for forbidden in ("submit_order", "cancel_order", "send_order", "adapter"):
            assert not hasattr(engine, forbidden)


class TestDeterminismo:
    def test_mismas_observaciones_mismo_snapshot(self) -> None:
        observations = _healthy_observations()
        a = _engine().evaluate(observations)
        b = _engine().evaluate(observations)
        assert a == b

    def test_restart_conserva_determinismo_y_no_falsea_healthy(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=observe_broker(_FakeAdapter(True), None, 0, 0.2),
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        first = _engine().evaluate(obs)
        restarted = _engine().evaluate(obs)
        assert first == restarted
        assert (
            restarted.get(MonitorComponent.BROKER).status == HealthStatus.UNKNOWN
        )
        assert restarted.get(MonitorComponent.BROKER).status != HealthStatus.HEALTHY


class TestAplicacionAlKillSwitch:
    def _coordinator(self):
        return KillSwitchCoordinator(clock=_FixedClock(T0))

    def test_healthy_no_bloquea(self) -> None:
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(_healthy_observations()))
        assert coordinator.status().can_trade is True

    def test_data_stale_bloquea_halt(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed("CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0),
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.HALT
        assert coordinator.status().can_trade is False

    def test_reconciliation_blocked_bloquea_nuevas_operaciones(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=observe_reconciliation("RECONCILIATION_BLOCKED", now),
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.BLOCK_NEW_ORDERS

    def test_reloj_invalido_eleva_a_emergency(self) -> None:
        now_naive = datetime(2026, 5, 4, 12, 0, 0)
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now_naive, heartbeat_at=now_naive),
            execution=obs.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.EMERGENCY

    def test_ejecucion_unknown_se_proxya_como_system_halt(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now, now - timedelta(seconds=2)),
            execution=observe_execution((_FakeOrder("UNKNOWN"),), 0),
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.HALT

    def test_ejecucion_sano_no_despeja_system_emergency(self) -> None:
        now_naive = datetime(2026, 5, 4, 12, 0, 0)
        obs = _healthy_observations()
        execution = observe_execution((_FakeOrder("FILLED"),), 0)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now_naive, heartbeat_at=now_naive),
            execution=execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.EMERGENCY

    def test_degradado_no_bloquea(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed("DEGRADED", "GAP_DETECTED", now - timedelta(seconds=1), 5),
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=obs.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().can_trade is True

    def test_fallo_desaparece_monitoring_recupera_pero_b5_latched(self) -> None:
        """Caso crítico B6: Monitoring se recupera, B5 no se auto-desbloquea."""
        now = T0
        coordinator = self._coordinator()

        stale = _healthy_observations(now)
        stale = Observations(
            data_feed=observe_data_feed("CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0),
            broker=stale.broker,
            reconciliation=stale.reconciliation,
            session=stale.session,
            system=stale.system,
            execution=stale.execution,
        )
        apply_to_kill_switch(coordinator, _engine().evaluate(stale))
        assert coordinator.status().can_trade is False
        assert coordinator.status().state == KillSwitchState.HALT

        apply_to_kill_switch(coordinator, _engine().evaluate(_healthy_observations(now)))
        assert coordinator.status().can_trade is False
        assert coordinator.status().state == KillSwitchState.BLOCK_NEW_ORDERS

        coordinator.recover(reason="data alive de nuevo", health_checks_ok=True)
        assert coordinator.status().can_trade is True
        assert coordinator.status().state == KillSwitchState.ALLOW

    def test_misma_fuente_emergency_prevalece_sobre_halt(self) -> None:
        """SYSTEM(EMERGENCY)+EXECUTION(HALT) -> misma fuente SYSTEM: EMERGENCY."""
        naive = datetime(2026, 5, 4, 12, 0, 0)
        base = _healthy_observations()
        obs = Observations(
            data_feed=base.data_feed,
            broker=base.broker,
            reconciliation=base.reconciliation,
            session=base.session,
            system=observe_system(naive, heartbeat_at=naive),
            execution=observe_execution((_FakeOrder("UNKNOWN"),), 0),
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.EMERGENCY

    def test_varias_fuentes_diferente_severidad_gana_la_maxima(self) -> None:
        now = T0
        naive = datetime(2026, 5, 4, 12, 0, 0)
        base = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0
            ),
            broker=base.broker,
            reconciliation=observe_reconciliation(
                "RECONCILIATION_BLOCKED", now - timedelta(seconds=1)
            ),
            session=base.session,
            system=observe_system(naive, heartbeat_at=naive),
            execution=base.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        assert coordinator.status().state == KillSwitchState.EMERGENCY

    def test_varias_fuentes_misma_severidad(self) -> None:
        now = T0
        base = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0
            ),
            broker=base.broker,
            reconciliation=base.reconciliation,
            session=observe_session("HALTED", now),
            system=base.system,
            execution=base.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        status = coordinator.status()
        assert status.state == KillSwitchState.HALT
        assert {"DATA_FEED", "SESSION"} <= {s.value for s, _ in status.active}

    def test_eliminar_una_fuente_no_elimina_otra_activa(self) -> None:
        now = T0
        base = _healthy_observations(now)
        both = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0
            ),
            broker=base.broker,
            reconciliation=base.reconciliation,
            session=observe_session("HALTED", now),
            system=base.system,
            execution=base.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(both))
        assert coordinator.status().state == KillSwitchState.HALT

        recovered_data = Observations(
            data_feed=base.data_feed,  # DATA_FEED sano -> se despeja
            broker=base.broker,
            reconciliation=base.reconciliation,
            session=observe_session("HALTED", now),  # SESSION sigue activa
            system=base.system,
            execution=base.execution,
        )
        apply_to_kill_switch(coordinator, _engine().evaluate(recovered_data))
        status = coordinator.status()
        assert status.state == KillSwitchState.HALT
        assert {s.value for s, _ in status.active} == {"SESSION"}

    def test_idempotencia_de_actualizaciones(self) -> None:
        naive = datetime(2026, 5, 4, 12, 0, 0)
        base = _healthy_observations()
        obs = Observations(
            data_feed=base.data_feed,
            broker=base.broker,
            reconciliation=base.reconciliation,
            session=base.session,
            system=observe_system(naive, heartbeat_at=naive),
            execution=observe_execution((_FakeOrder("UNKNOWN"),), 0),
        )
        coordinator = self._coordinator()
        snapshot = _engine().evaluate(obs)
        apply_to_kill_switch(coordinator, snapshot)
        state1 = coordinator.status().state
        audit1 = len(coordinator.audit_log)
        apply_to_kill_switch(coordinator, snapshot)
        assert coordinator.status().state == state1
        assert len(coordinator.audit_log) == audit1

    def test_b5_decide_la_precedencia_efectiva(self) -> None:
        """La agregación representa condiciones; B5 aplica la precedencia."""
        now = T0
        naive = datetime(2026, 5, 4, 12, 0, 0)
        base = _healthy_observations(now)
        obs = Observations(
            data_feed=observe_data_feed(
                "CONNECTED", "HEALTHY", now - timedelta(seconds=60), 0
            ),
            broker=base.broker,
            reconciliation=observe_reconciliation(
                "RECONCILIATION_BLOCKED", now - timedelta(seconds=1)
            ),
            session=base.session,
            system=observe_system(naive, heartbeat_at=naive),
            execution=base.execution,
        )
        coordinator = self._coordinator()
        apply_to_kill_switch(coordinator, _engine().evaluate(obs))
        status = coordinator.status()
        assert status.state == KillSwitchState.most_severe(
            tuple(cond.level for _, cond in status.active)
        )


class TestEjecucion:
    def test_orden_unknown_failed(self) -> None:
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=observe_execution((_FakeOrder("UNKNOWN"),), 0),
        )
        result = _engine().evaluate(obs).get(MonitorComponent.EXECUTION)
        assert result.status == HealthStatus.FAILED

    def test_congestion_degraded(self) -> None:
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=obs.system,
            execution=observe_execution((_FakeOrder("APPROVED"),) * 15, 0),
        )
        result = _engine().evaluate(obs).get(MonitorComponent.EXECUTION)
        assert result.status == HealthStatus.DEGRADED


class TestSesion:
    def test_halted_failed(self) -> None:
        obs = _healthy_observations()
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=observe_session("HALTED", T0),
            system=obs.system,
            execution=obs.execution,
        )
        assert (
            _engine().evaluate(obs).get(MonitorComponent.SESSION).status
            == HealthStatus.FAILED
        )

    def test_open_healthy(self) -> None:
        obs = _healthy_observations()
        result = _engine().evaluate(obs).get(MonitorComponent.SESSION)
        assert result.status == HealthStatus.HEALTHY


class TestSystem:
    def test_heartbeat_stale_failed(self) -> None:
        now = T0
        obs = _healthy_observations(now)
        obs = Observations(
            data_feed=obs.data_feed,
            broker=obs.broker,
            reconciliation=obs.reconciliation,
            session=obs.session,
            system=observe_system(now, heartbeat_at=now - timedelta(seconds=120)),
            execution=obs.execution,
        )
        result = _engine().evaluate(obs).get(MonitorComponent.SYSTEM)
        assert result.status == HealthStatus.FAILED
        assert "heartbeat" in result.reason