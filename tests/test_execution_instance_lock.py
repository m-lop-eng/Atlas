"""Tests ORDER-09E: lock inter-proceso de ejecucion MT5."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from execution.execution_authority import BrokerScope
from execution.execution_instance_lock import (
    ExecutionInstanceAlreadyRunning,
    ExecutionInstanceLock,
    ExecutionInstanceLockError,
    default_lock_dir,
    execution_resource_key,
)
from execution.intent_journal import IntentJournal, OrderIntent, SUBMIT_PROTOCOL_VERSION
from execution.order import OrderSide
from execution.order_manager import OrderManager
from execution.live_execution import ExecutionBlockedError, MarketDataReference
from execution.live_runtime import LiveExecutionRuntime, RuntimeNotReadyError
from execution.ledger_applier import CommissionPolicy
from execution.ledger_store import LedgerStore

from tests.test_live_runtime import (
    AccountingFakeMT5,
    ASK,
    BID,
    CLOSE_TIME,
    NOW,
    OPEN_TIME,
    PASSWORD_CANARY,
    _ledger,
    _open_order,
    _runtime,
    _spec,
)
from tests.test_mt5_execution_foundation import (
    INSTRUMENT,
    FakeMT5,
    FakeSessions,
    passing_admission,
)
from killswitches.coordinator import KillSwitchCoordinator
from risk.types import AccountRiskState
from brokers.models import BrokerError
from brokers.mt5 import (
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
)

REPO = Path(__file__).resolve().parents[1]


def _scope(
    *,
    environment: str = "demo",
    server: str = "S-DEMO",
    login: str = "1001",
) -> BrokerScope:
    return BrokerScope(environment=environment, server=server, login=login)


def _lock(tmp_path, scope=None, sub: str = "locks") -> ExecutionInstanceLock:
    return ExecutionInstanceLock.for_scope(
        scope or _scope(), lock_dir=tmp_path / sub
    )


def _unconnected_adapter(fake, *, login=42, server="demo"):
    return MT5BrokerAdapter(
        MT5Config(
            provider="mt5",
            environment=MT5Environment.DEMO,
            symbol_map={INSTRUMENT: INSTRUMENT},
        ),
        MT5Credentials(login=login, password=PASSWORD_CANARY, server=server),
        mt5=fake,
        clock=lambda: NOW,
    )


def _runtime_kwargs(tmp_path, fake, *, account_mode=0):
    return dict(
        order_manager=OrderManager(),
        ledger=_ledger(),
        ledger_store=LedgerStore(tmp_path / "ledger.json"),
        sessions=FakeSessions(),
        kill_switch=KillSwitchCoordinator(),
        instrument=INSTRUMENT,
        instrument_spec=_spec(),
        account_id="DEMO-1",
        account_currency="USD",
        max_open_positions=3,
        risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
        market_data_provider=lambda: MarketDataReference(
            INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME
        ),
        data_max_age_s=120.0,
        account_mode_provider=lambda: account_mode,
        marks_provider=lambda: {INSTRUMENT: fake._mark},
        commission_policy=CommissionPolicy("USD"),
        clock=lambda: NOW,
        run_id="LOCK-T",
        mt5_accounting=passing_admission(),
    )


class TestLockUnit:
    def test_acquire_release_reacquire(self, tmp_path):
        lock = _lock(tmp_path)
        lock.acquire()
        assert lock.held is True
        lock.release()
        assert lock.held is False
        lock.acquire()
        assert lock.held is True
        lock.release()

    def test_second_acquire_same_resource_rejected(self, tmp_path):
        first = _lock(tmp_path)
        second = _lock(tmp_path)
        first.acquire()
        try:
            with pytest.raises(ExecutionInstanceAlreadyRunning):
                second.acquire()
        finally:
            first.release()

    def test_double_acquire_same_instance_rejected(self, tmp_path):
        lock = _lock(tmp_path)
        lock.acquire()
        try:
            with pytest.raises(ExecutionInstanceLockError):
                lock.acquire()
        finally:
            lock.release()

    def test_claim_owner_and_release_other_owner_rejected(self, tmp_path):
        lock = _lock(tmp_path)
        lock.acquire()
        lock.claim("runtime-a")
        try:
            with pytest.raises(ExecutionInstanceLockError):
                lock.claim("runtime-b")
            with pytest.raises(ExecutionInstanceLockError):
                lock.release("runtime-b")
            assert lock.held is True
        finally:
            lock.release("runtime-a")
        assert lock.held is False

    def test_double_release_safe(self, tmp_path):
        lock = _lock(tmp_path)
        lock.acquire()
        lock.release()
        lock.release()  # no-op

    def test_existing_file_without_lock_is_acquirable(self, tmp_path):
        lock = _lock(tmp_path)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text("stale-owner-metadata\n", encoding="utf-8")
        lock.acquire()
        assert lock.held is True
        lock.release()
        assert lock.path.exists()

    def test_release_does_not_delete_lock_file(self, tmp_path):
        lock = _lock(tmp_path)
        lock.acquire()
        path = lock.path
        lock.release()
        assert path.exists()

    def test_invalid_lock_dir_inside_repo_rejected(self):
        with pytest.raises(ExecutionInstanceLockError):
            ExecutionInstanceLock.for_scope(
                _scope(), lock_dir=REPO / "locks"
            )

    def test_invalid_lock_dir_onedrive_rejected(self, tmp_path):
        with pytest.raises(ExecutionInstanceLockError):
            ExecutionInstanceLock.for_scope(
                _scope(), lock_dir=tmp_path / "OneDrive" / "locks"
            )

    def test_invalid_lock_dir_parent_is_file(self, tmp_path):
        not_a_dir = tmp_path / "not-a-dir"
        not_a_dir.write_text("x", encoding="utf-8")
        lock = ExecutionInstanceLock.for_scope(
            _scope(), lock_dir=not_a_dir / "locks"
        )
        with pytest.raises(ExecutionInstanceLockError):
            lock.acquire()

    @pytest.mark.skipif(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="POSIX permissions semantics (non-root POSIX only)",
    )
    def test_permission_error_fails_closed_posix(self, tmp_path):
        locked = tmp_path / "readonly"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            lock = ExecutionInstanceLock.for_scope(
                _scope(), lock_dir=locked / "locks"
            )
            with pytest.raises(ExecutionInstanceLockError):
                lock.acquire()
        finally:
            os.chmod(locked, 0o700)

    @pytest.mark.skipif(os.name != "nt", reason="Windows read-only semantics")
    def test_permission_error_fails_closed_windows(self, tmp_path):
        lock = _lock(tmp_path)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text("x", encoding="utf-8")
        os.chmod(lock.path, stat.S_IREAD)
        try:
            with pytest.raises(ExecutionInstanceLockError):
                lock.acquire()
        finally:
            os.chmod(lock.path, stat.S_IWRITE)

    def test_env_override_lock_dir(self, tmp_path, monkeypatch):
        override = tmp_path / "custom-locks"
        monkeypatch.setenv("ATLAS_EXECUTION_LOCK_DIR", str(override))
        assert default_lock_dir() == override
        lock = ExecutionInstanceLock.for_scope(_scope())
        assert lock.path.parent == override

    @pytest.mark.skipif(os.name != "nt", reason="UNC only on Windows")
    def test_unc_lock_dir_rejected(self):
        with pytest.raises(ExecutionInstanceLockError):
            ExecutionInstanceLock.for_scope(
                _scope(), lock_dir=r"\\server\share\locks"
            )

    @pytest.mark.skipif(os.name != "nt", reason="Windows PROGRAMDATA")
    def test_missing_programdata_fails_closed(self, monkeypatch):
        monkeypatch.delenv("ATLAS_EXECUTION_LOCK_DIR", raising=False)
        monkeypatch.delenv("PROGRAMDATA", raising=False)
        with pytest.raises(ExecutionInstanceLockError):
            default_lock_dir()

    def test_different_lock_dirs_are_different_resources(self, tmp_path):
        first = _lock(tmp_path, sub="locks-a")
        second = _lock(tmp_path, sub="locks-b")
        first.acquire()
        try:
            second.acquire()
            second.release()
        finally:
            first.release()

    def test_default_lock_dir_is_not_onedrive(self):
        path = default_lock_dir()
        assert "onedrive" not in " ".join(path.parts).lower()
        if os.name == "nt":
            assert path.name == "locks"
            assert path.parent.name == "Atlas"
        else:
            assert path == Path("/var/lock/atlas")

    def test_lock_path_and_content_have_no_plaintext_identity(self, tmp_path):
        scope = _scope(server="Secret-Server", login="987654321")
        lock = _lock(tmp_path, scope)
        text = str(lock.path).lower()
        assert "secret-server" not in text
        assert "987654321" not in text
        lock.acquire()
        lock.release()
        content = lock.path.read_text(encoding="utf-8")
        assert "secret-server" not in content
        assert "987654321" not in content
        assert execution_resource_key(scope) in content


class TestLockIdentity:
    def test_same_scope_same_path(self, tmp_path):
        assert _lock(tmp_path).path == _lock(tmp_path).path

    def test_login_change_changes_path(self, tmp_path):
        assert _lock(tmp_path, _scope(login="1001")).path != _lock(
            tmp_path, _scope(login="1002")
        ).path

    def test_server_change_changes_path(self, tmp_path):
        assert _lock(tmp_path, _scope(server="S-A")).path != _lock(
            tmp_path, _scope(server="S-B")
        ).path

    def test_environment_change_changes_path(self, tmp_path):
        assert _lock(tmp_path, _scope(environment="demo")).path != _lock(
            tmp_path, _scope(environment="live")
        ).path

    def test_delimited_values_do_not_collide(self):
        first = BrokerScope("a|b", "c", "d")
        second = BrokerScope("a", "b|c", "d")
        assert execution_resource_key(first) != execution_resource_key(second)

    def test_different_journal_or_cwd_does_not_change_identity(
        self, tmp_path, monkeypatch
    ):
        scope = _scope()
        expected = _lock(tmp_path / "a", scope).path
        journal_a = tmp_path / "journal-a.json"
        journal_b = tmp_path / "nested" / "journal-b.json"
        journal_a.write_text("{}", encoding="utf-8")
        journal_b.parent.mkdir(parents=True)
        journal_b.write_text("{}", encoding="utf-8")
        another_cwd = tmp_path / "cwd"
        another_cwd.mkdir()
        monkeypatch.chdir(another_cwd)
        assert _lock(tmp_path / "a", scope).path == expected
        assert journal_a != journal_b


_CHILD = r"""
import sys, time
from pathlib import Path
from execution.execution_authority import BrokerScope
from execution.execution_instance_lock import (
    ExecutionInstanceAlreadyRunning,
    ExecutionInstanceLock,
)

mode, lock_dir, environment, server, login, ready, stop = sys.argv[1:8]
scope = BrokerScope(environment, server, login)
lock = ExecutionInstanceLock.for_scope(scope, lock_dir=lock_dir)
try:
    lock.acquire()
except ExecutionInstanceAlreadyRunning:
    print("already-running", flush=True)
    sys.exit(2)

if mode == "hold":
    Path(ready).write_text("ready", encoding="utf-8")
    deadline = time.time() + 30
    while time.time() < deadline and not Path(stop).exists():
        time.sleep(0.05)

lock.release()
sys.exit(0)
"""


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    return env


def _run_child(
    *,
    mode: str,
    lock_dir: Path,
    cwd: Path | None = None,
    environment: str = "demo",
    server: str = "S-DEMO",
    login: str = "1001",
    ready: str = "",
    stop: str = "",
    journal_path: str = "",
) -> subprocess.CompletedProcess:
    env = _child_env()
    if journal_path:
        env["ATLAS_JOURNAL_PATH"] = journal_path
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            mode,
            str(lock_dir),
            environment,
            server,
            login,
            ready,
            stop,
        ],
        cwd=str(cwd) if cwd is not None else str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


class TestLockSubprocess:
    def test_same_account_different_journal_and_cwd_is_excluded(self, tmp_path):
        lock_dir = tmp_path / "locks"
        ready = tmp_path / "ready"
        stop = tmp_path / "stop"
        scope = _scope()
        other_cwd = tmp_path / "other-cwd"
        other_cwd.mkdir()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                "hold",
                str(lock_dir),
                scope.environment,
                scope.server,
                scope.login,
                str(ready),
                str(stop),
            ],
            cwd=str(REPO),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            while not ready.exists() and time.time() < deadline:
                if child.poll() is not None:
                    break
                time.sleep(0.02)
            assert ready.exists(), (
                f"child did not acquire lock: {child.communicate()}"
            )

            same = _run_child(
                mode="try",
                lock_dir=lock_dir,
                cwd=other_cwd,
            )
            assert same.returncode == 2
            assert "already-running" in same.stdout

            different_journal = _run_child(
                mode="try",
                lock_dir=lock_dir,
                cwd=other_cwd,
                journal_path=str(tmp_path / "other-journal.json"),
            )
            assert different_journal.returncode == 2

            different_identity = _run_child(
                mode="try",
                lock_dir=lock_dir,
                cwd=other_cwd,
                login="1002",
            )
            assert different_identity.returncode == 0

            expected = _lock(tmp_path, scope).path
            assert expected.exists()

            child.terminate()
            child.wait(timeout=10)

            after = _run_child(
                mode="try",
                lock_dir=lock_dir,
                cwd=other_cwd,
                journal_path=str(tmp_path / "after-journal.json"),
            )
            assert after.returncode == 0
            assert expected.exists()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_runtime_open_is_rejected_while_child_holds_lock(self, tmp_path):
        lock_dir = tmp_path / "locks"
        ready = tmp_path / "ready"
        stop = tmp_path / "stop"
        scope = _scope()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                "hold",
                str(lock_dir),
                scope.environment,
                scope.server,
                scope.login,
                str(ready),
                str(stop),
            ],
            cwd=str(REPO),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            while not ready.exists() and time.time() < deadline:
                if child.poll() is not None:
                    break
                time.sleep(0.02)
            assert ready.exists(), (
                f"child did not acquire lock: {child.communicate()}"
            )
            adapter = MT5BrokerAdapter(
                MT5Config(
                    provider="mt5",
                    environment=MT5Environment.DEMO,
                    symbol_map={INSTRUMENT: INSTRUMENT},
                ),
                MT5Credentials(login=1001, password="x", server="S-DEMO"),
                mt5=FakeMT5(),
            )
            with pytest.raises(ExecutionInstanceAlreadyRunning):
                LiveExecutionRuntime.open(
                    broker=adapter,
                    journal_factory=lambda: (_ for _ in ()).throw(
                        AssertionError("journal must not be loaded")
                    ),
                    lock_dir=lock_dir,
                    connect=False,
                )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


class TestRuntimeLock:
    def test_runtime_without_lock_not_ready_and_no_recovery(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(
            OrderIntent.from_order(
                _open_order("O-PENDING"),
                protocol_version=SUBMIT_PROTOCOL_VERSION,
            )
        )
        rt, adapter, fake, ledger, journal, store = _runtime(
            tmp_path, journal=journal, with_lock=False
        )
        report = rt.initialize()
        assert report.ready is False
        assert report.stage == "instance_lock"
        assert journal.get("O-PENDING").state == "CREATED"
        assert fake.send_count == 0

    def test_runtime_without_lock_cannot_submit(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(
            tmp_path, with_lock=False
        )
        rt.initialize()
        with pytest.raises(RuntimeNotReadyError):
            rt.submit(
                side=OrderSide.BUY,
                quantity=0.1,
                strategy_id="s",
                strategy_version="1.0.0",
                client_order_id="O-NOLOCK",
            )
        # Incluso forzando readiness/estado, el lock sigue siendo obligatorio.
        rt._ready = True
        rt._lifecycle_state = "READY"
        with pytest.raises(RuntimeNotReadyError, match="lock inter-proceso"):
            rt.submit(
                side=OrderSide.BUY,
                quantity=0.1,
                strategy_id="s",
                strategy_version="1.0.0",
                client_order_id="O-NOLOCK",
            )
        assert fake.send_count == 0

    def test_runtime_with_lock_is_ready_and_bound(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        assert rt.initialize().ready is True
        assert rt.instance_lock is not None
        assert rt.instance_lock.held is True
        assert adapter._execution_lock is rt.instance_lock

    def test_shutdown_releases_lock(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        assert rt.initialize().ready is True
        lock = rt.instance_lock
        assert lock is not None
        lock_dir = lock.path.parent
        rt.shutdown()
        assert lock.held is False
        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        replacement.acquire()
        replacement.release()
        assert lock.path.exists()

    def test_start_releases_lock_on_connection_failure(self, tmp_path):
        fake = FakeMT5()
        fake.initialize_ok = False
        adapter = MT5BrokerAdapter(
            MT5Config(
                provider="mt5",
                environment=MT5Environment.DEMO,
                symbol_map={INSTRUMENT: INSTRUMENT},
            ),
            MT5Credentials(login=42, password="x", server="demo"),
            mt5=fake,
        )
        lock_dir = tmp_path / "locks"
        with pytest.raises(BrokerError):
            LiveExecutionRuntime.start(
                broker=adapter,
                journal_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("journal must not be loaded")
                ),
                lock_dir=lock_dir,
            )
        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        replacement.acquire()
        replacement.release()

    def test_start_acquires_lock_before_connect_and_journal(self, tmp_path):
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = MT5BrokerAdapter(
            MT5Config(
                provider="mt5",
                environment=MT5Environment.DEMO,
                symbol_map={INSTRUMENT: INSTRUMENT},
            ),
            MT5Credentials(login=42, password=PASSWORD_CANARY, server="demo"),
            mt5=fake,
            clock=lambda: NOW,
        )
        lock_dir = tmp_path / "locks"
        journal_path = tmp_path / "start-journal.json"
        observed: dict[str, object] = {}
        original_connect = adapter.connect

        def connect():
            probe = ExecutionInstanceLock.for_scope(
                adapter.expected_execution_scope(), lock_dir=lock_dir
            )
            with pytest.raises(ExecutionInstanceAlreadyRunning):
                probe.acquire()
            observed["lock_before_connect"] = True
            return original_connect()

        adapter.connect = connect

        def journal_factory():
            observed["adapter_health"] = adapter.health()
            probe = ExecutionInstanceLock.for_scope(
                adapter.expected_execution_scope(), lock_dir=lock_dir
            )
            with pytest.raises(ExecutionInstanceAlreadyRunning):
                probe.acquire()
            return IntentJournal.initialize(journal_path)

        runtime, report = LiveExecutionRuntime.start(
            broker=adapter,
            journal_factory=journal_factory,
            lock_dir=lock_dir,
            order_manager=OrderManager(),
            ledger=_ledger(),
            ledger_store=LedgerStore(tmp_path / "ledger.json"),
            sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(),
            instrument=INSTRUMENT,
            instrument_spec=_spec(),
            account_id="DEMO-1",
            account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME
            ),
            data_max_age_s=120.0,
            account_mode_provider=lambda: 0,
            marks_provider=lambda: {INSTRUMENT: fake._mark},
            commission_policy=CommissionPolicy("USD"),
            clock=lambda: NOW,
            run_id="START-LOCK",
            mt5_accounting=passing_admission(),
        )
        assert observed["lock_before_connect"] is True
        assert observed["adapter_health"] is True
        assert report.ready is True
        assert runtime.instance_lock is not None
        assert runtime.instance_lock.held is True
        runtime.shutdown()
        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        replacement.acquire()
        replacement.release()

    def test_lock_held_during_initialization(self, tmp_path, monkeypatch):
        runtime, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        observed: dict[str, bool] = {}
        original = runtime._coordinator.prime_identity_from_journal

        def prime():
            observed["held"] = runtime.instance_lock.held
            return original()

        monkeypatch.setattr(
            runtime._coordinator, "prime_identity_from_journal", prime
        )
        assert runtime.initialize().ready is True
        assert observed["held"] is True
        assert runtime.instance_lock.held is True

    def test_normal_order_rejection_does_not_release_lock(self, tmp_path):
        runtime, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        assert runtime.initialize().ready is True
        lock = runtime.instance_lock
        assert lock is not None and lock.held is True
        with pytest.raises(ExecutionBlockedError):
            runtime.submit(
                side=OrderSide.BUY,
                quantity=0.1,
                strategy_id="s",
                strategy_version="1.0.0",
                client_order_id="O-REJECT",
                is_entry=False,
            )
        assert lock.held is True
        assert runtime.lifecycle_state == "READY"
        assert fake.send_count == 0

    def test_open_then_initialize_not_ready_releases_lock(self, tmp_path):
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = _unconnected_adapter(fake)
        lock_dir = tmp_path / "locks"
        journal_path = tmp_path / "j.json"
        runtime = LiveExecutionRuntime.open(
            broker=adapter,
            journal_factory=lambda: IntentJournal.initialize(journal_path),
            lock_dir=lock_dir,
            **_runtime_kwargs(tmp_path, fake, account_mode=None),
        )
        lock = runtime.instance_lock
        assert lock is not None and lock.held is True
        report = runtime.initialize()
        assert report.ready is False and report.stage == "account_mode"
        assert lock.held is False
        assert runtime.ready is False
        assert runtime.lifecycle_state == "FAILED"
        closed = runtime.initialize()
        assert closed.ready is False and closed.stage == "closed"
        assert lock.held is False

        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        replacement.acquire()
        replacement.release()

    def test_second_runtime_can_acquire_after_failed_initialization(self, tmp_path):
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = _unconnected_adapter(fake)
        lock_dir = tmp_path / "locks"
        journal_path = tmp_path / "j.json"
        first = LiveExecutionRuntime.open(
            broker=adapter,
            journal_factory=lambda: IntentJournal.initialize(journal_path),
            lock_dir=lock_dir,
            **_runtime_kwargs(tmp_path, fake, account_mode=None),
        )
        assert first.initialize().ready is False
        assert first.instance_lock.held is False

        adapter2 = _unconnected_adapter(fake)
        second = LiveExecutionRuntime.open(
            broker=adapter2,
            journal_factory=lambda: IntentJournal(journal_path),
            lock_dir=lock_dir,
            **_runtime_kwargs(tmp_path, fake, account_mode=0),
        )
        try:
            assert second.initialize().ready is True
            assert second.instance_lock is not None
            assert second.instance_lock.held is True
        finally:
            second.shutdown()

    def test_open_initialize_exception_releases_lock(self, tmp_path, monkeypatch):
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = _unconnected_adapter(fake)
        lock_dir = tmp_path / "locks"
        runtime = LiveExecutionRuntime.open(
            broker=adapter,
            journal_factory=lambda: IntentJournal.initialize(tmp_path / "j.json"),
            lock_dir=lock_dir,
            **_runtime_kwargs(tmp_path, fake),
        )
        lock = runtime.instance_lock
        assert lock is not None and lock.held is True

        def boom(self):
            raise RuntimeError("initialize boom")

        monkeypatch.setattr(LiveExecutionRuntime, "_initialize_locked", boom)
        with pytest.raises(RuntimeError):
            runtime.initialize()
        assert lock.held is False
        assert runtime.lifecycle_state == "FAILED"

        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock_dir
        )
        replacement.acquire()
        replacement.release()

    def test_recovery_error_releases_lock(self, tmp_path, monkeypatch):
        fake = AccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=BID, ask=ASK)
        adapter = _unconnected_adapter(fake)
        lock_dir = tmp_path / "locks"
        runtime = LiveExecutionRuntime.open(
            broker=adapter,
            journal_factory=lambda: IntentJournal.initialize(tmp_path / "j.json"),
            lock_dir=lock_dir,
            **_runtime_kwargs(tmp_path, fake),
        )
        lock = runtime.instance_lock
        assert lock is not None and lock.held is True

        def boom():
            raise RuntimeError("recovery boom")

        monkeypatch.setattr(runtime._coordinator, "resolve_pending", boom)
        report = runtime.initialize()
        assert report.ready is False
        assert report.stage == "recovery"
        assert lock.held is False
        assert runtime.lifecycle_state == "FAILED"

    def test_shutdown_disconnect_error_still_releases_lock(self, tmp_path, monkeypatch):
        runtime, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        assert runtime.initialize().ready is True
        lock = runtime.instance_lock
        assert lock is not None and lock.held is True

        def boom():
            raise RuntimeError("disconnect boom")

        monkeypatch.setattr(adapter, "disconnect", boom)
        with pytest.raises(RuntimeError):
            runtime.shutdown()
        assert lock.held is False
        assert runtime.lifecycle_state == "CLOSED"
        assert runtime.ready is False

        replacement = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock.path.parent
        )
        replacement.acquire()
        replacement.release()

    def test_double_shutdown_is_safe_and_idempotent(self, tmp_path):
        runtime, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        assert runtime.initialize().ready is True
        lock = runtime.instance_lock
        assert lock is not None
        runtime.shutdown()
        runtime.shutdown()
        assert lock.held is False
        assert runtime.lifecycle_state == "CLOSED"
        assert runtime.ready is False

    def test_different_account_lock_does_not_block(self, tmp_path):
        first = _lock(tmp_path, _scope(login="1001"))
        second = _lock(tmp_path, _scope(login="1002"))
        first.acquire()
        try:
            second.acquire()
            second.release()
        finally:
            first.release()

    def test_two_runtimes_same_account_same_lock_dir_conflict(self, tmp_path):
        rt, adapter, fake, ledger, journal, store = _runtime(tmp_path)
        lock = rt.instance_lock
        assert lock is not None
        other = ExecutionInstanceLock.for_scope(
            adapter.expected_execution_scope(), lock_dir=lock.path.parent
        )
        with pytest.raises(ExecutionInstanceAlreadyRunning):
            other.acquire()
