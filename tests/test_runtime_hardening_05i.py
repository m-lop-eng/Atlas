"""ATLAS-MT5-ACCOUNTING-05I: privacidad de identidad + serializacion del runtime.

Offline (FakeMT5/FakeBroker y metricas hand-built). Sin MT5 real, sin credenciales,
sin ordenes. Cubre:
  * H-1: `AccountState`/`AccountSnapshot` no exponen `account_id` en `repr`.
  * H-2: `LiveExecutionRuntime` serializa por instancia (RLock) operaciones,
    post-operacion, reconciliacion e inicializacion.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_mt5_execution_foundation import NOW
from tests.test_mt5_runtime_admission import (
    _FakeBroker,
    _GenericBroker,
    _admission,
    _make_runtime,
    _store,
)

from brokers.models import AccountState
from reconciliation.models import (
    AccountSnapshot,
    ReconciliationReport,
    ReconciliationStatus,
)
from execution.live_execution import LiveExecutionCoordinator
from execution.live_runtime import LiveExecutionRuntime, RuntimeNotReadyError

UTC = timezone.utc
SYNTHETIC_ID = "99887766"


# --------------------------------------------------------------------------
# H-1. Privacidad de identidad de cuenta
# --------------------------------------------------------------------------


class TestAccountStatePrivacy:
    def _state(self, account_id=SYNTHETIC_ID):
        return AccountState(
            account_id=account_id, cash=1.0, equity=1.0, buying_power=0.0,
            currency="USD", as_of=NOW,
        )

    def test_repr_hides_full_account_id(self):
        state = self._state()
        assert SYNTHETIC_ID not in repr(state)
        assert state.account_id == SYNTHETIC_ID  # conservado internamente

    def test_identity_equality_preserved(self):
        assert self._state("1") == self._state("1")
        assert self._state("1") != self._state("2")

    def test_account_snapshot_repr_hides_account_id(self):
        snap = AccountSnapshot(SYNTHETIC_ID, 1.0, 1.0, "USD")
        assert SYNTHETIC_ID not in repr(snap)
        assert snap.account_id == SYNTHETIC_ID
        assert snap == AccountSnapshot(SYNTHETIC_ID, 1.0, 1.0, "USD")


# --------------------------------------------------------------------------
# H-2. Serializacion por instancia
# --------------------------------------------------------------------------


def _ok_report() -> ReconciliationReport:
    return ReconciliationReport(ReconciliationStatus.RECONCILIATION_OK, (), True)


def _ready_runtime(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    admission = _admission(store)
    rt, ledger, journal = _make_runtime(
        tmp_path, broker=_FakeBroker(), admission=admission
    )
    assert rt.initialize().ready is True
    return rt


def _one_shot_block(*, raise_on_release=None):
    """Devuelve (fake_reconcile, entered, release).

    `fake_reconcile` bloquea la PRIMERA llamada hasta `release`; despues no vuelve
    a bloquear. Se patchea como metodo de clase (funcion, no callable-object).
    """
    entered = threading.Event()
    release = threading.Event()
    state = {"first": True}

    def fake_reconcile(self):
        if state["first"]:
            state["first"] = False
            entered.set()
            assert release.wait(5.0), "release no llego a tiempo"
            if raise_on_release is not None:
                raise raise_on_release
        return _ok_report()

    return fake_reconcile, entered, release


def _record_coordinator(monkeypatch):
    calls: list = []

    def fake_submit(self, **kwargs):
        calls.append(kwargs.get("client_order_id"))
        return SimpleNamespace(submission=None, order=None)

    monkeypatch.setattr(LiveExecutionCoordinator, "submit", fake_submit)
    return calls


class TestRuntimeSerialization:
    def test_operation_holds_critical_section(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        calls = _record_coordinator(monkeypatch)
        fake, entered, release = _one_shot_block()
        monkeypatch.setattr(LiveExecutionRuntime, "_reconcile", fake)

        errors: list = []

        def run(cid):
            try:
                rt.submit(strategy_id="s", strategy_version="1", client_order_id=cid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        a = threading.Thread(target=run, args=("A",))
        a.start()
        assert entered.wait(5.0)
        b_started = threading.Event()

        def run_b():
            b_started.set()
            run("B")

        b = threading.Thread(target=run_b)
        b.start()
        assert b_started.wait(5.0)
        # B intento operar pero A mantiene la seccion critica: el coordinador NO
        # recibio una segunda operacion todavia.
        assert calls == ["A"]
        release.set()
        a.join(5.0)
        b.join(5.0)
        assert not a.is_alive() and not b.is_alive()
        assert errors == []
        assert calls == ["A", "B"]

    def test_post_operation_failure_blocks_waiting_call(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        calls = _record_coordinator(monkeypatch)
        fake, entered, release = _one_shot_block(
            raise_on_release=RuntimeError("post-op fail")
        )
        monkeypatch.setattr(LiveExecutionRuntime, "_reconcile", fake)

        errors: list = []

        def run(cid):
            try:
                rt.submit(strategy_id="s", strategy_version="1", client_order_id=cid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        a = threading.Thread(target=run, args=("A",))
        a.start()
        assert entered.wait(5.0)
        b = threading.Thread(target=run, args=("B",))
        b.start()
        release.set()
        a.join(5.0)
        b.join(5.0)
        assert any(isinstance(e, RuntimeError) for e in errors)  # A fallo
        assert any(isinstance(e, RuntimeNotReadyError) for e in errors)  # B bloqueado
        # B nunca alcanzo el coordinador.
        assert calls == ["A"]
        assert rt._post_operation_failed is True

    def test_reinit_waits_for_in_flight_operation(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        _record_coordinator(monkeypatch)
        fake, entered, release = _one_shot_block()
        monkeypatch.setattr(LiveExecutionRuntime, "_reconcile", fake)

        a = threading.Thread(
            target=lambda: rt.submit(
                strategy_id="s", strategy_version="1", client_order_id="A"
            )
        )
        a.start()
        assert entered.wait(5.0)

        b_done = threading.Event()

        def run_reinit():
            rt.initialize()
            b_done.set()

        b = threading.Thread(target=run_reinit)
        b.start()
        # La reinicializacion NO puede completarse mientras A esta en la seccion.
        assert not b_done.is_set()
        release.set()
        a.join(5.0)
        b.join(5.0)
        assert b_done.is_set()

    def test_reconcile_now_serializes_with_operation(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        calls = _record_coordinator(monkeypatch)
        fake, entered, release = _one_shot_block()
        monkeypatch.setattr(LiveExecutionRuntime, "_reconcile", fake)

        r = threading.Thread(target=rt.reconcile_now)
        r.start()
        assert entered.wait(5.0)
        b = threading.Thread(
            target=lambda: rt.submit(
                strategy_id="s", strategy_version="1", client_order_id="B"
            )
        )
        b.start()
        # Mientras `reconcile_now()` mantiene la seccion, B no llega al coordinador.
        assert calls == []
        release.set()
        r.join(5.0)
        b.join(5.0)
        assert calls == ["B"]

    def test_nested_calls_no_deadlock(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        _record_coordinator(monkeypatch)
        # `submit` -> `_post_operation` -> `reconcile_now` (mismo hilo): RLock.
        result: dict = {}

        def run():
            result["out"] = rt.submit(
                strategy_id="s", strategy_version="1", client_order_id="N"
            )

        t = threading.Thread(target=run)
        t.start()
        t.join(5.0)
        assert not t.is_alive(), "deadlock en llamadas anidadas"
        assert "out" in result

    def test_generic_broker_serialized_without_flag(self, tmp_path, monkeypatch):
        rt, _, _ = _make_runtime(
            tmp_path, broker=_GenericBroker(), admission=None
        )
        assert rt.initialize().ready is True
        _record_coordinator(monkeypatch)
        rt.submit(strategy_id="s", strategy_version="1", client_order_id="G")
        # Broker NO MT5: sin bloqueo H-4 (comportamiento previo), con serializacion.
        assert rt._post_operation_failed is False

    def test_all_verbs_gated_under_lock(self, tmp_path, monkeypatch):
        rt = _ready_runtime(tmp_path, monkeypatch)
        calls = _record_coordinator(monkeypatch)
        rt._post_operation_failed = True  # simula fallo post-operacion previo
        for call in (
            lambda: rt.submit(strategy_id="s", strategy_version="1", client_order_id="X"),
            lambda: rt.cancel_pending(client_order_id="X"),
            lambda: rt.close_position(),
            lambda: rt.reduce_position(quantity=0.1),
        ):
            with pytest.raises(RuntimeNotReadyError):
                call()
        assert calls == []
