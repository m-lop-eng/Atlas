"""Tests de Kill Switches (B5).

Batería obligatoria:

  1. ningún bloqueo -> ALLOW
  2. un bloqueo -> BLOCKED
  3. múltiples bloqueos simultáneos
  4. precedencia determinista
  5. UNKNOWN no se interpreta como OK
  6. reconciliation blocked bloquea nuevas órdenes
  7. broker desconectado bloquea
  8. data stale bloquea
  9. manual kill switch bloquea
 10. activar/desactivar manualmente
 11. condición desaparece pero no hay auto-recovery
 12. recovery explícito devuelve ALLOW
 13. un bloqueo vuelve durante recovery -> recovery rechazado
 14. restart conserva el estado requerido
 15. idempotencia
 16. concurrencia/actualizaciones repetidas
 17. ninguna llamada a submit_order()/cancel_order() desde coordinator
 18. auditoría de activación, motivo, timestamp y recovery
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brokers import PaperBrokerAdapter
from execution.order import Order, OrderSide, OrderType
from killswitches import (
    AuditEntry,
    KillSource,
    KillSwitchCoordinator,
    KillSwitchState,
)
from reconciliation.models import ReconciliationStatus
from sessions.manager import SessionState


def _clock() -> datetime:
    return datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc)


def _coord() -> KillSwitchCoordinator:
    return KillSwitchCoordinator(clock=_clock)


class TestEstado:
    def test_no_bloqueo_allow(self) -> None:
        status = _coord().status()
        assert status.state == KillSwitchState.ALLOW
        assert status.can_trade is True
        assert status.recovery_required is False
        assert status.active == ()

    def test_un_bloqueo_blocked(self) -> None:
        c = _coord()
        assert c.set_source(
            KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS
        ) == KillSwitchState.BLOCK_NEW_ORDERS
        status = c.status()
        assert status.can_trade is False
        assert status.blocked is True
        assert [(s.value, cond.level) for s, cond in status.active] == [
            ("RECONCILIATION", KillSwitchState.BLOCK_NEW_ORDERS)
        ]

    def test_multiplos_bloqueos_simultaneos(self) -> None:
        c = _coord()
        c.set_source(KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS)
        c.set_source(KillSource.BROKER, KillSwitchState.HALT)
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT)
        status = c.status()
        assert status.state == KillSwitchState.HALT
        assert len(status.active) == 3

    def test_precedencia_determinista(self) -> None:
        c = _coord()
        c.set_source(KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS)
        assert c.status().state == KillSwitchState.BLOCK_NEW_ORDERS
        c.set_source(KillSource.SESSION, KillSwitchState.HALT)
        assert c.status().state == KillSwitchState.HALT
        c.set_source(KillSource.SYSTEM, KillSwitchState.EMERGENCY)
        assert c.status().state == KillSwitchState.EMERGENCY
        # La condición crítica NO queda anulada por otras fuentes
        c.clear_source(KillSource.SYSTEM)
        assert c.status().state == KillSwitchState.HALT
        c.clear_source(KillSource.SESSION)
        assert c.status().state == KillSwitchState.BLOCK_NEW_ORDERS

    def test_emergencia_no_anulada_por_fuente_ok(self) -> None:
        c = _coord()
        c.set_source(KillSource.SYSTEM, KillSwitchState.EMERGENCY, "kernel panic")
        # otra fuente dice OPEN: no debe anular la emergencia
        c.clear_source(KillSource.RECONCILIATION)  # ya estaba limpia (no-op)
        assert c.status().state == KillSwitchState.EMERGENCY

    def test_unknown_no_es_ok(self) -> None:
        c = _coord()
        c.update_from_reconciliation(ReconciliationStatus.RECONCILIATION_UNKNOWN)
        assert c.status().state == KillSwitchState.HALT
        assert c.status().can_trade is False


class TestFuentes:
    def test_reconciliation_blocked_bloquea(self) -> None:
        c = _coord()
        c.update_from_reconciliation(
            ReconciliationStatus.RECONCILIATION_BLOCKED, "qty divergente"
        )
        status = c.status()
        assert status.state == KillSwitchState.BLOCK_NEW_ORDERS
        assert status.can_trade is False

    def test_broker_desconectado_bloquea(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT, "sin conexión")
        assert c.status().can_trade is False

    def test_data_stale_bloquea(self) -> None:
        c = _coord()
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT, "stale 90s")
        assert c.status().can_trade is False

    def test_manual_kill_switch_bloquea(self) -> None:
        c = _coord()
        st = c.set_source(KillSource.MANUAL, KillSwitchState.EMERGENCY, "operator")
        assert st == KillSwitchState.EMERGENCY
        assert c.status().can_trade is False

    def test_activar_desactivar_manual(self) -> None:
        c = _coord()
        c.set_source(KillSource.MANUAL, KillSwitchState.EMERGENCY, "operator off")
        assert c.status().can_trade is False
        c.clear_source(KillSource.MANUAL, "operator clear")
        # NO auto-recovery: tras activar, el gate exige recovery explícito
        assert c.status().can_trade is False
        assert c.status().recovery_required is True

    def test_update_from_session_halted_y_unknown(self) -> None:
        c = _coord()
        assert c.update_from_session(SessionState.HALTED) == KillSwitchState.HALT
        c.clear_source(KillSource.SESSION, "en pausa")
        assert c.update_from_session(SessionState.UNKNOWN) == KillSwitchState.HALT

        # estados normales NO son fallos (coordinador fresco)
        c_fresh = _coord()
        for normal in (
            SessionState.OPEN,
            SessionState.PRE_OPEN,
            SessionState.CLOSING,
            SessionState.CLOSED,
        ):
            assert c_fresh.update_from_session(normal) == KillSwitchState.ALLOW

    def test_reconciliation_ok_no_anula_otra_fuente(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT, "sin broker")
        c.update_from_reconciliation(ReconciliationStatus.RECONCILIATION_OK)
        assert c.status().state == KillSwitchState.HALT


class TestRecovery:
    def test_condicion_desaparece_sin_auto_recovery(self) -> None:
        c = _coord()
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT, "stale")
        c.clear_source(KillSource.DATA_FEED, "feed OK de nuevo")
        status = c.status()
        assert status.state != KillSwitchState.ALLOW
        assert status.can_trade is False
        assert status.recovery_required is True

    def test_recovery_explicito_devuelve_allow(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT, "caída")
        c.clear_source(KillSource.BROKER, "broker OK")
        result = c.recover(
            "broker reconectado y verificada la cuenta",
            health_checks_ok=True,
        )
        assert result.accepted is True
        assert result.state == KillSwitchState.ALLOW
        assert c.status().can_trade is True
        assert c.status().recovery_required is False

    def test_recovery_mientras_sigue_bloqueado_rechazado(self) -> None:
        c = _coord()
        c.set_source(KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS)
        result = c.recover("intento con bloqueo activo")
        assert result.accepted is False
        assert result.state == KillSwitchState.BLOCK_NEW_ORDERS

    def test_bloqueo_vuelve_durante_recovery_rechazado(self) -> None:
        c = _coord()
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT)
        c.clear_source(KillSource.DATA_FEED, "feed OK")
        # antes del recover, una nueva condición aparece
        c.set_source(KillSource.BROKER, KillSwitchState.HALT, "broker down")
        result = c.recover("nada debería desbloquear")
        assert result.accepted is False
        assert result.state == KillSwitchState.HALT
        assert c.status().can_trade is False

    def test_recovery_sin_motivo_rechazado(self) -> None:
        c = _coord()
        c.set_source(KillSource.MANUAL, KillSwitchState.EMERGENCY)
        c.clear_source(KillSource.MANUAL)
        result = c.recover("")
        assert result.accepted is False

    def test_recovery_con_health_checks_no_ok_rechazado(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT)
        c.clear_source(KillSource.BROKER)
        result = c.recover("motivo válido", health_checks_ok=False)
        assert result.accepted is False
        assert c.status().can_trade is False


class TestRestartEIdempotencia:
    def test_restart_conserva_estado_requerido(self) -> None:
        c = _coord()
        c.set_source(KillSource.MANUAL, KillSwitchState.EMERGENCY, "operator halt")
        c.clear_source(KillSource.MANUAL, "operator clear")
        snap = c.snapshot()
        assert snap["recovery_required"] is True

        # Restart: coordinador nuevo, se restaura el snapshot
        c2 = _coord()
        c2.load_snapshot(snap)
        assert c2.status().can_trade is False
        assert c2.status().state == KillSwitchState.BLOCK_NEW_ORDERS
        assert c2.status().recovery_required is True

    def test_restart_con_condicion_activa(self) -> None:
        c = _coord()
        c.set_source(KillSource.SYSTEM, KillSwitchState.EMERGENCY, "critical")
        snap = c.snapshot()
        c2 = _coord()
        c2.load_snapshot(snap)
        assert c2.status().state == KillSwitchState.EMERGENCY

    def test_restart_tras_recovery_correcto_devuelve_allow(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT)
        c.clear_source(KillSource.BROKER)
        c.recover("vuelta verificada")
        snap = c.snapshot()
        c2 = _coord()
        c2.load_snapshot(snap)
        assert c2.status().can_trade is True

    def test_idempotencia_set_source(self) -> None:
        c = _coord()
        c.set_source(KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS)
        first_audits = len(c.audit_log)
        st = c.set_source(
            KillSource.RECONCILIATION, KillSwitchState.BLOCK_NEW_ORDERS
        )
        assert st == KillSwitchState.BLOCK_NEW_ORDERS
        assert len(c.audit_log) == first_audits  # sin duplicar auditoría
        assert c.status().active[0][1].level == KillSwitchState.BLOCK_NEW_ORDERS

    def test_clear_sin_condicion_no_op(self) -> None:
        c = _coord()
        c.clear_source(KillSource.BROKER)
        assert c.status().state == KillSwitchState.ALLOW
        assert c.audit_log == ()

    def test_set_source_allow_raise(self) -> None:
        c = _coord()
        with pytest.raises(ValueError):
            c.set_source(KillSource.BROKER, KillSwitchState.ALLOW)

    def test_actualizaciones_repetidas_convergen(self) -> None:
        c1, c2 = _coord(), _coord()
        # operaciones sobre fuentes INDEPENDIENTES con distinto intercalado
        # => mismo estado final (los sources no interactúan entre sí)
        sets = [
            KillSource.RECONCILIATION,
            KillSource.SYSTEM,
            KillSource.BROKER,
        ]
        clears = [KillSource.RECONCILIATION, KillSource.BROKER]
        for obj, set_order, clear_order in (
            (c1, sets, clears),
            (c2, list(reversed(sets)), list(reversed(clears))),
        ):
            for src in set_order:
                obj.set_source(src, KillSwitchState.HALT)
            for src in clear_order:
                obj.clear_source(src)
        assert c1.status() == c2.status()
        assert c1.status().state == c2.status().state == KillSwitchState.HALT
        assert [s.value for s, _ in c1.status().active] == ["SYSTEM"]

    def test_status_repetido_determinista(self) -> None:
        c = _coord()
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT)
        first = c.status()
        for _ in range(10):
            assert c.status() == first


class TestNoOrdenesDesdeCoordinator:
    def test_coordinator_sin_api_de_ordenes(self) -> None:
        c = _coord()
        assert not hasattr(c, "submit_order")
        assert not hasattr(c, "cancel_order")
        assert not hasattr(c, "close_position")
        assert not hasattr(c, "close_all")

    def test_boundary_no_envia_orden_bloqueada(self) -> None:
        """El coordinator alimenta un gate: con can_trade False, ninguna
        orden llega al adapter (nunca submit)."""
        submissions: list[str] = []

        class RecordingPaper(PaperBrokerAdapter):
            def submit_order(self, order):
                submissions.append(order.client_order_id)
                return super().submit_order(order)

        adapter = RecordingPaper(clock=_clock)
        adapter.connect()

        def broker_bloqueado(coord, order):
            if not coord.status().can_trade:
                return False
            adapter.submit_order(order)
            return True

        order = Order(
            strategy_id="s1", strategy_version="1.0.0", account_id="acc",
            instrument="BTCUSDT", side=OrderSide.BUY, quantity=1.0,
            order_type=OrderType.MARKET, client_order_id="GATED-1",
        )

        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT, "sin broker")
        assert broker_bloqueado(c, order) is False
        assert submissions == []

        c.clear_source(KillSource.BROKER, "broker OK")
        assert broker_bloqueado(c, order) is False  # gate: falta recovery
        assert submissions == []

        c.recover("account verificada tras reconexión", health_checks_ok=True)
        assert broker_bloqueado(c, order) is True
        assert submissions == ["GATED-1"]


class TestAuditoria:
    def test_audit_activacion_motivo_timestamp(self) -> None:
        c = _coord()
        c.set_source(
            KillSource.SYSTEM, KillSwitchState.EMERGENCY, "kernel panic"
        )
        entry = c.audit_log[-1]
        assert entry.action == "ACTIVATION"
        assert entry.source == KillSource.SYSTEM.value
        assert entry.detail == "kernel panic"
        assert entry.new_level == KillSwitchState.EMERGENCY.value
        assert entry.previous_level == "ALLOW"
        assert isinstance(entry, AuditEntry)
        assert entry.at == _clock()
        assert entry.as_dict()["at"] == "2026-09-10T08:00:00+00:00"

    def test_audit_recovery(self) -> None:
        c = _coord()
        c.set_source(KillSource.DATA_FEED, KillSwitchState.HALT, "stale")
        c.clear_source(KillSource.DATA_FEED, "feed OK")
        c.recover("feed verificado y watchdog operativo")
        actions = [e.action for e in c.audit_log]
        assert "ACTIVATION" in actions
        assert "CLEAR" in actions
        assert "RECOVERY" in actions
        last = c.audit_log[-1]
        assert last.detail == "feed verificado y watchdog operativo"
        assert last.new_level == KillSwitchState.ALLOW.value

    def test_audit_recovery_rechazado_queda_trazado(self) -> None:
        c = _coord()
        c.set_source(KillSource.BROKER, KillSwitchState.HALT)
        c.recover("intento sin permiso")
        actions = [e.action for e in c.audit_log]
        assert "RECOVERY_REJECTED" in actions


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))