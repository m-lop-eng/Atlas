"""Lock inter-proceso de ejecucion MT5 (ORDER-09E).

Garantiza que, como maximo, un proceso Atlas ejecuta contra una identidad MT5
concreta (`BrokerScope`: entorno + servidor + login).

El recurso se identifica por un hash SHA-256 canonico; ni el login ni el servidor
aparecen en el nombre del fichero. El lock efectivo es un lock de fichero del
sistema operativo (`msvcrt.locking` en Windows, `fcntl.flock` en POSIX) mantenido
por un descriptor abierto. El SO libera el lock al morir el proceso.

El lock es ADVISORY: protege a procesos Atlas que respetan el protocolo. No
impide que una aplicacion externa ignore deliberadamente el fichero ni que un
operador cambie la cuenta en el terminal MT5.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from execution.execution_authority import BrokerScope

_LOCK_PREFIX = "mt5-exec"
_RESOURCE_DOMAIN = "atlas-mt5-execution-v1"


class ExecutionInstanceLockError(RuntimeError):
    """Error generico de lock de ejecucion (ruta, permisos, I/O, uso)."""


class ExecutionInstanceAlreadyRunning(ExecutionInstanceLockError):
    """Otro proceso ya mantiene el lock para la misma identidad MT5."""


def execution_resource_key(scope: BrokerScope) -> str:
    """Hash SHA-256 canonico de environment/server/login (sin secretos en claro)."""
    if not isinstance(scope, BrokerScope):
        raise TypeError("scope debe ser BrokerScope")
    body = {
        "domain": _RESOURCE_DOMAIN,
        "version": 1,
        "environment": scope.environment,
        "server": scope.server,
        "login": scope.login,
    }
    material = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def default_lock_dir() -> Path:
    """Directorio local compartido por defecto (nunca repo/OneDrive/red)."""
    override = os.environ.get("ATLAS_EXECUTION_LOCK_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA")
        if not program_data:
            raise ExecutionInstanceLockError(
                "PROGRAMDATA no disponible: configure ATLAS_EXECUTION_LOCK_DIR"
            )
        return Path(program_data) / "Atlas" / "locks"
    return Path("/var/lock/atlas")


def _validate_lock_dir(lock_dir: Path) -> Path:
    try:
        resolved = lock_dir.expanduser().resolve()
    except OSError as exc:
        raise ExecutionInstanceLockError(
            f"lock_dir invalido: {type(exc).__name__}"
        ) from exc
    parts = [part.lower() for part in resolved.parts]
    if any("onedrive" in part for part in parts):
        raise ExecutionInstanceLockError(
            "lock_dir no puede estar en OneDrive (use un directorio local)"
        )
    if os.name == "nt" and str(resolved).startswith("\\\\"):
        raise ExecutionInstanceLockError(
            "lock_dir no puede ser una ruta UNC de red"
        )
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            raise ExecutionInstanceLockError(
                "lock_dir no puede estar dentro del repositorio"
            )
    return resolved


class ExecutionInstanceLock:
    """Lock exclusivo no bloqueante sobre una identidad MT5 canonica."""

    def __init__(self, *, resource_key: str, lock_path: Path) -> None:
        if not isinstance(resource_key, str) or not resource_key:
            raise ValueError("resource_key obligatorio")
        self._resource_key = resource_key
        self._path = Path(lock_path)
        self._fh: Any | None = None
        self._thread_lock = threading.Lock()
        self._owner_token = secrets.token_hex(16)
        self._owner_id: str | None = None

    @classmethod
    def for_scope(
        cls, scope: BrokerScope, *, lock_dir: str | Path | None = None
    ) -> "ExecutionInstanceLock":
        key = execution_resource_key(scope)
        directory = _validate_lock_dir(
            Path(lock_dir) if lock_dir is not None else default_lock_dir()
        )
        return cls(
            resource_key=key,
            lock_path=directory / f"{_LOCK_PREFIX}-{key}.lock",
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def resource_key(self) -> str:
        return self._resource_key

    @property
    def held(self) -> bool:
        with self._thread_lock:
            return self._fh is not None

    def __repr__(self) -> str:
        return (
            "ExecutionInstanceLock("
            f"resource={self._resource_key[:12]}..., "
            f"path={str(self._path)!r}, held={self.held})"
        )

    @staticmethod
    def _lock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass

    def _ensure_directory(self) -> None:
        try:
            created = not self._path.parent.exists()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if created and os.name != "nt":
                try:
                    os.chmod(self._path.parent, 0o700)
                except OSError:
                    pass
        except OSError as exc:
            raise ExecutionInstanceLockError(
                f"no se puede crear el directorio de lock {self._path.parent}: "
                f"{type(exc).__name__}"
            ) from exc

    def acquire(self) -> "ExecutionInstanceLock":
        with self._thread_lock:
            if self._fh is not None:
                raise ExecutionInstanceLockError(
                    "el lock ya esta adquirido por esta instancia"
                )
            self._ensure_directory()
            try:
                fh = self._path.open("a+", encoding="utf-8")
            except OSError as exc:
                raise ExecutionInstanceLockError(
                    f"no se puede abrir el fichero de lock {self._path}: "
                    f"{type(exc).__name__}"
                ) from exc
            try:
                fh.seek(0)
                self._lock(fh)
            except OSError as exc:
                fh.close()
                raise ExecutionInstanceAlreadyRunning(
                    f"otra instancia Atlas mantiene el lock {self._path.name}"
                ) from exc
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(
                    f"pid={os.getpid()} token={self._owner_token} "
                    f"resource={self._resource_key}\n"
                )
                fh.flush()
            except OSError as exc:
                try:
                    self._unlock(fh)
                finally:
                    fh.close()
                raise ExecutionInstanceLockError(
                    f"no se pudo escribir metadata del lock {self._path}: "
                    f"{type(exc).__name__}"
                ) from exc
            self._fh = fh
            return self

    def claim(self, owner_id: str) -> None:
        """Vincula el lock a un unico propietario logico (runtime)."""
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id obligatorio")
        with self._thread_lock:
            if self._fh is None:
                raise ExecutionInstanceLockError(
                    "no se puede reclamar un lock no adquirido"
                )
            if self._owner_id is None:
                self._owner_id = owner_id
                return
            if self._owner_id != owner_id:
                raise ExecutionInstanceLockError(
                    "el lock ya pertenece a otro runtime"
                )

    def release(self, owner_id: str | None = None) -> None:
        with self._thread_lock:
            if self._fh is None:
                return
            if (
                owner_id is not None
                and self._owner_id is not None
                and self._owner_id != owner_id
            ):
                raise ExecutionInstanceLockError(
                    "no se libera un lock propiedad de otro runtime"
                )
            fh = self._fh
            self._fh = None
            self._owner_id = None
            try:
                self._unlock(fh)
            finally:
                fh.close()

    def __enter__(self) -> "ExecutionInstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - defensa best-effort
        try:
            self.release()
        except Exception:  # noqa: BLE001 - nunca fallar en GC
            pass


__all__ = [
    "ExecutionInstanceAlreadyRunning",
    "ExecutionInstanceLock",
    "ExecutionInstanceLockError",
    "default_lock_dir",
    "execution_resource_key",
]
