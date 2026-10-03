"""Programador Windows de la Fase A4 (Change 6): endurecimiento del scheduler.

Este modulo SOLO define/instala la tarea programada del sistema que ejecuta
`experiments/e14_forward_continuous.py --once`. NO contiene logica de A4, no
importa e14/ForwardRunner, no toca H002/FINAL_OOS ni el directorio de estado.

Configuracion objetivo (blindada):

  * StartWhenAvailable = True  -> si el equipo no estaba disponible a la hora
    programada, Windows ejecuta la tarea cuando vuelva a estarlo (recuperacion
    delegada al SO). Es el cambio importante frente a perder el ciclo.
  * WakeToRun = False          -> no despertar el equipo por una adquisicion batch.
  * bateria bloqueada          -> DisallowStartIfOnBatteries=True y
    StopIfGoingOnBatteries=True (no depender de bateria para escribir estado).
  * frecuencia diaria, hora fija (00:30), un unico trigger.
  * MultipleInstances=IgnoreNew -> una sola instancia; reinstalar es idempotente
    (mismo nombre de tarea + `-Force`, nunca duplica tareas).
  * Principal S4U (ejecutar sin sesion interactiva). Requiere privilegios de
    administrador; si no estan disponibles, `install()` cae a Interactive y lo
    reporta (el resto del endurecimiento si se aplica).

IMPORTANTE (no catch-up propio): A4 NO implementa recuperacion de ciclos
perdidos. Windows, con StartWhenAvailable, reejecuta la tarea UNA vez cuando
puede; A4 sigue procesando exactamente un ciclo con su semantica actual. Nunca
se ejecutan varios ciclos por una recuperacion ni se duplican barras (el runner
deduplica por open_time).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "Atlas-Forward-A4"
DEFAULT_AT = "00:30"
EXECUTION_TIME_LIMIT = "PT72H"
RUN_LEVEL = "Limited"
MULTIPLE_INSTANCES = "IgnoreNew"

MISSED_RUN_BEHAVIOR = (
    "Con StartWhenAvailable=True, si el equipo no estaba disponible a la hora "
    "programada, Windows ejecuta la tarea (una sola vez) cuando vuelva a estarlo; "
    "A4 no implementa catch-up propio y sigue procesando exactamente un ciclo "
    "(sin duplicar barras: el runner deduplica por open_time). Si el equipo esta "
    "a bateria la tarea no arranca/continua (bateria bloqueada). Sin privilegios "
    "de administrador la tarea corre solo con sesion iniciada (Interactive)."
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "experiments" / "e14_forward_continuous.py"


def task_spec(
    *,
    python_exe: str | os.PathLike[str],
    script_path: str | os.PathLike[str],
    working_dir: str | os.PathLike[str],
    user_id: str | None = None,
    at: str = DEFAULT_AT,
    logon_type: str = "S4U",
) -> dict:
    """Definicion normalizada e idempotente de la tarea (sin efectos).

    Es pura/determinista: mismos argumentos -> misma salida (idempotencia).
    """
    return {
        "task_name": TASK_NAME,
        "action": {
            "execute": str(python_exe),
            "arguments": f'"{script_path}" --once',
            "working_directory": str(working_dir),
        },
        "trigger": {
            "schedule": "DAILY",
            "at": at,
            "days_interval": 1,
            "enabled": True,
        },
        "principal": {
            "user_id": user_id if user_id is not None else os.environ.get("USERNAME", ""),
            "logon_type": logon_type,
            "run_level": RUN_LEVEL,
        },
        "settings": {
            "start_when_available": True,
            "wake_to_run": False,
            "disallow_start_if_on_batteries": True,
            "stop_if_going_on_batteries": True,
            "execution_time_limit": EXECUTION_TIME_LIMIT,
            "multiple_instances": MULTIPLE_INSTANCES,
        },
    }


def default_spec(project_root: str | os.PathLike[str] | None = None) -> dict:
    root = Path(project_root) if project_root is not None else ROOT
    return task_spec(
        python_exe=sys.executable,
        script_path=root / "experiments" / "e14_forward_continuous.py",
        working_dir=root,
    )


def _ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _iso_hours(limit: str) -> int:
    # "PT72H" -> 72 (suficiente para el formato que usamos)
    if limit.startswith("PT") and limit.endswith("H"):
        return int(limit[2:-1])
    raise ValueError(f"formato de ExecutionTimeLimit no soportado: {limit}")


def ps_register_script(spec: dict, *, logon_type: str | None = None) -> str:
    """Script PowerShell que registra/actualiza la tarea (idempotente, -Force)."""
    a = spec["action"]
    t = spec["trigger"]
    s = spec["settings"]
    p = spec["principal"]
    lt = logon_type or p["logon_type"]

    flags = []
    if s["start_when_available"]:
        flags.append("-StartWhenAvailable")
    # NO se pasa -AllowStartIfOnBatteries (bateria bloqueada) ni -WakeToRun.
    settings_args = " ".join(flags)
    hours = _iso_hours(s["execution_time_limit"])
    return "\n".join(
        [
            f"$action = New-ScheduledTaskAction -Execute {_ps_quote(a['execute'])} "
            f"-Argument {_ps_quote(a['arguments'])} "
            f"-WorkingDirectory {_ps_quote(a['working_directory'])}",
            f"$trigger = New-ScheduledTaskTrigger -Daily -At {_ps_quote(t['at'])}",
            f"$settings = New-ScheduledTaskSettingsSet {settings_args} "
            f"-MultipleInstances {s['multiple_instances']} "
            f"-ExecutionTimeLimit (New-TimeSpan -Hours {hours})",
            f"$principal = New-ScheduledTaskPrincipal -UserId {_ps_quote(p['user_id'])} "
            f"-LogonType {lt} -RunLevel {p['run_level']}",
            f"Register-ScheduledTask -TaskName {_ps_quote(spec['task_name'])} "
            f"-Action $action -Trigger $trigger -Settings $settings "
            f"-Principal $principal -Force | Out-Null",
        ]
    )


_STATUS_PS = """
$t = Get-ScheduledTask -TaskName __TASK__ -ErrorAction Stop
$i = Get-ScheduledTaskInfo -TaskName __TASK__
$trig = @($t.Triggers)
$at = $null
if ($trig.Count -gt 0) { $at = ([datetimeoffset]$trig[0].StartBoundary).ToString('HH:mm') }
[pscustomobject]@{
  exists=$true; task_name=$t.TaskName;
  start_when_available=[bool]$t.Settings.StartWhenAvailable;
  wake_to_run=[bool]$t.Settings.WakeToRun;
  disallow_start_if_on_batteries=[bool]$t.Settings.DisallowStartIfOnBatteries;
  stop_if_going_on_batteries=[bool]$t.Settings.StopIfGoingOnBatteries;
  logon_type=[string]$t.Principal.LogonType;
  run_level=[string]$t.Principal.RunLevel;
  daily_at=$at; trigger_count=$trig.Count;
  multiple_instances=[string]$t.Settings.MultipleInstances;
  execution_time_limit=[string]$t.Settings.ExecutionTimeLimit;
  next_run=([string]$i.NextRunTime); last_result=[int]$i.LastTaskResult
} | ConvertTo-Json -Compress
"""


def _run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )


def install(spec: dict | None = None, *, allow_interactive_fallback: bool = True) -> dict:
    """Registra/actualiza la tarea (idempotente). Devuelve el resultado."""
    spec = spec or default_spec()
    result = _run_ps(ps_register_script(spec))
    used = spec["principal"]["logon_type"]
    if (
        result.returncode != 0
        and used.upper() == "S4U"
        and allow_interactive_fallback
    ):
        result = _run_ps(ps_register_script(spec, logon_type="Interactive"))
        used = "Interactive"
    return {
        "ok": result.returncode == 0,
        "logon_type_used": used,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def status(task_name: str = TASK_NAME) -> dict:
    """Lee (solo lectura) la configuracion real de la tarea en el sistema."""
    script = _STATUS_PS.replace("__TASK__", _ps_quote(task_name))
    result = _run_ps(script)
    if result.returncode != 0:
        return {"exists": False, "task_name": task_name, "stderr": (result.stderr or "").strip()}
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    action = argv[0] if argv else "status"
    if action == "install":
        out = install()
        print(
            f"tarea {TASK_NAME}: {'OK' if out['ok'] else 'ERROR'} "
            f"(logon_type={out['logon_type_used']})"
        )
        if out["stderr"]:
            print(out["stderr"], file=sys.stderr)
        return 0 if out["ok"] else 1
    if action == "status":
        out = status()
        for k, v in out.items():
            print(f"{k} = {v}")
        return 0 if out.get("exists") else 1
    print("uso: python -m deployment.schedule_a4 [install|status]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
