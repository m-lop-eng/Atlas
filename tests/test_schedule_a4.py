"""Tests del programador de A4 (deployment/schedule_a4.py, Change 6).

Deterministas y sin efectos: validan la DEFINICION de la tarea (spec) y el
script PowerShell generado. El test de integracion (Windows) lee la tarea real
si esta instalada; se salta si no lo esta.

Propiedades exigidas: StartWhenAvailable=True, WakeToRun=False, bateria
bloqueada, diaria intacta, no-interactiva (objetivo S4U), idempotencia, sin
duplicar tareas, sin catch-up propio, y aislamiento (no importa A4/perimetro).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deployment import schedule_a4 as sched


def _spec() -> dict:
    return sched.task_spec(
        python_exe="py.exe", script_path="e14.py", working_dir="wd", user_id="user1"
    )


def test_settings_hardened() -> None:
    s = _spec()["settings"]
    assert s["start_when_available"] is True
    assert s["wake_to_run"] is False
    assert s["disallow_start_if_on_batteries"] is True
    assert s["stop_if_going_on_batteries"] is True
    assert s["execution_time_limit"] == "PT72H"
    assert s["multiple_instances"] == "IgnoreNew"


def test_trigger_single_daily() -> None:
    t = _spec()["trigger"]
    assert t["schedule"] == "DAILY"
    assert t["at"] == "00:30"
    assert t["days_interval"] == 1
    assert t["enabled"] is True


def test_action_runs_one_cycle() -> None:
    a = _spec()["action"]
    assert a["arguments"].endswith("--once")
    assert a["working_directory"] == "wd"
    assert a["execute"] == "py.exe"


def test_principal_target_non_interactive() -> None:
    assert _spec()["principal"]["logon_type"] == "S4U"


def test_spec_is_idempotent() -> None:
    assert _spec() == _spec()


def test_ps_script_hardened() -> None:
    ps = sched.ps_register_script(_spec())
    assert "-StartWhenAvailable" in ps
    assert "-AllowStartIfOnBatteries" not in ps  # bateria bloqueada
    assert "-WakeToRun" not in ps
    assert "-LogonType S4U" in ps
    assert "-RunLevel Limited" in ps
    assert "-MultipleInstances IgnoreNew" in ps
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 72)" in ps


def test_ps_script_has_no_self_catchup() -> None:
    ps = sched.ps_register_script(_spec())
    assert "Repetition" not in ps  # sin repeticion/catch-up propio
    assert ps.count("-Daily") == 1
    assert ps.count("Register-ScheduledTask") == 1


def test_single_task_name_no_duplication() -> None:
    ps = sched.ps_register_script(_spec())
    assert sched.TASK_NAME == "Atlas-Forward-A4"
    assert ps.count("Atlas-Forward-A4") == 1
    assert "-Force" in ps  # reinstalar actualiza en sitio, no duplica


def test_missed_run_behavior_documented() -> None:
    doc = sched.MISSED_RUN_BEHAVIOR
    assert "StartWhenAvailable" in doc
    assert "un ciclo" in doc
    assert "bateria" in doc


def test_module_is_isolated_from_a4_and_perimeter() -> None:
    src = Path(sched.__file__).read_text(encoding="utf-8")
    assert "from experiments" not in src
    assert "import experiments" not in src
    assert "papertrading" not in src
    assert "forward_runner" not in src


def test_default_spec_points_to_e14_once(tmp_path: Path) -> None:
    spec = sched.default_spec(project_root=tmp_path)
    assert spec["action"]["working_directory"] == str(tmp_path)
    assert "e14_forward_continuous.py" in spec["action"]["arguments"]
    assert spec["action"]["arguments"].endswith("--once")


@pytest.mark.skipif(os.name != "nt", reason="solo Windows: Task Scheduler")
def test_real_task_is_hardened() -> None:
    st = sched.status()
    if not st.get("exists"):
        pytest.skip("tarea no instalada en este equipo")
    assert st["start_when_available"] is True
    assert st["wake_to_run"] is False
    assert st["disallow_start_if_on_batteries"] is True
    assert st["stop_if_going_on_batteries"] is True
    assert st["daily_at"] == "00:30"
    assert st["trigger_count"] == 1
    assert st["multiple_instances"] == "IgnoreNew"
    assert st["logon_type"] in {"S4U", "Interactive"}
