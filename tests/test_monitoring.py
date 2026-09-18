"""Tests de monitoreo de estrategias (monitoring/)."""

from datetime import datetime, timezone

from monitoring.status import HealthCheck, StrategyMonitor


class TestHealthCheck:
    def test_ok_by_default(self) -> None:
        hc = HealthCheck(component="execution", status="OK")
        assert hc.is_ok

    def test_critical_not_ok(self) -> None:
        hc = HealthCheck(component="execution", status="CRITICAL", message="down")
        assert not hc.is_ok


class TestStrategyMonitor:
    def test_rejection_counter(self) -> None:
        mon = StrategyMonitor(
            strategy_id="strat-1",
            started_at=datetime.now(timezone.utc),
            signals_count=5,
            rejected_count=1,
        )
        updated = mon.record_rejection()
        assert updated.signals_count == 6
        assert updated.rejected_count == 2
        assert updated.strategy_id == "strat-1"