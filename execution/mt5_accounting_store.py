"""Persistencia atomica y restauracion offline del ledger MT5. ATLAS-MT5-ACCOUNTING-03/03B.

Store INDEPENDIENTE del modelo contable MT5 (`execution.mt5_accounting`). NO se
integra con `LiveExecutionRuntime`, `LedgerApplier`, `LedgerStore`, el adaptador,
el coordinador ni la reconciliacion, y NO modifica la contabilidad spot.

Contrato de persistencia (H1/H2/H5):
  * `overwrite=False`: NO sobrescritura REAL. La publicacion se hace con un
    hard link (`os.link`): crea el destino de forma atomica y falla con
    `SnapshotExistsError` si ya existe (incluso si otro escritor lo creo entre la
    comprobacion previa y la publicacion). El snapshot preexistente se conserva
    byte a byte. Si el sistema de archivos no soporta `os.link`, se FALLA
    (`SnapshotWriteError`); NUNCA se recurre a `os.replace` en silencio.
  * `overwrite=True`: usa `os.replace` (reemplazo atomico).
  * CONCURRENCIA: el store asume UN UNICO ESCRITOR por ruta. `overwrite=True` no
    esta protegido frente a escritores simultaneos; no se anuncia seguridad
    multi-escritor.
  * Resultado de durabilidad: `save()` devuelve `PersistenceResult`. Tras
    sustituir el destino se intenta `fsync` del directorio; si no puede
    confirmarse (p. ej. Windows no permite `fsync` de directorio), el resultado es
    `REPLACED_DURABILITY_UNCONFIRMED` (el destino YA fue sustituido, pero la
    durabilidad no esta garantizada). No se comunica exito incondicional.
  * Destino ya sustituido => NO se afirma que el snapshot anterior siga vigente.

Bootstrap (H5): `create()` crea un ledger vacio desde una baseline EXPLICITA. No
sobrescribe silenciosamente. Sustituir un snapshot de la MISMA cuenta que contiene
ESTADO exige la confirmacion destructiva adicional `destructive=True`; un snapshot
de otra cuenta es siempre ineligible; un snapshot corrupto no se sobrescribe.
Nunca declara readiness operativa.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path

from execution.mt5_accounting import (
    AccountingEpoch,
    BaselineOrigin,
    MT5AccountBaseline,
    MT5AccountLedger,
    MT5AccountingError,
    MT5AccountStateVersionError,
)

STORE_SCHEMA_VERSION = 1

_EMPTY_EPS = 1e-9


class MT5AccountingStoreError(ValueError):
    """Error base del store contable MT5."""


class SnapshotNotFoundError(MT5AccountingStoreError):
    """No existe snapshot en la ruta."""


class SnapshotExistsError(MT5AccountingStoreError):
    """Ya existe un snapshot y no se autorizo sobrescribirlo."""


class SnapshotNotEmptyError(MT5AccountingStoreError):
    """El snapshot existente contiene estado: requiere `destructive=True`."""


class SnapshotCorruptError(MT5AccountingStoreError):
    """Snapshot ausente/vacio/truncado/invalido/incompatible."""


class SnapshotVersionError(MT5AccountingStoreError):
    """Version de esquema del store no soportada (futura/incompatible)."""


class SnapshotAccountMismatchError(MT5AccountingStoreError):
    """La identidad de cuenta del snapshot no coincide con la esperada."""


class SnapshotWriteError(MT5AccountingStoreError):
    """Fallo ANTES de sustituir el destino (el anterior, si existia, queda intacto)."""


class SnapshotDurabilityUnconfirmedError(MT5AccountingStoreError):
    """El destino YA fue sustituido pero la durabilidad del directorio no pudo
    confirmarse. No implica que el snapshot anterior siga vigente."""


class PersistenceResult(str, Enum):
    """Resultado de una operacion de persistencia."""

    DURABLE = "DURABLE"  # destino sustituido y durabilidad confirmada
    REPLACED_DURABILITY_UNCONFIRMED = "REPLACED_DURABILITY_UNCONFIRMED"


def _snapshot_is_empty(ledger: MT5AccountLedger) -> bool:
    """True solo si el ledger no contiene ESTADO mas alla de su baseline."""
    if len(ledger.positions()) != 0:
        return False
    if ledger.processed_deal_count() != 0:
        return False
    if len(ledger.movement_records()) != 0:
        return False
    if abs(ledger.realized_pnl) > _EMPTY_EPS:
        return False
    if not ledger.is_complete:
        return False
    baseline = ledger.baseline
    if abs(ledger.balance - baseline.balance) > _EMPTY_EPS:
        return False
    if abs(ledger.credit - baseline.credit) > _EMPTY_EPS:
        return False
    return True


class MT5AccountingStore:
    """Persistencia atomica y restauracion offline del ledger MT5 (un escritor)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.last_persistence_result: PersistenceResult | None = None

    @property
    def exists(self) -> bool:
        return self.path.exists()

    # ------------------------------------------------------------ bootstrap

    def create(
        self,
        baseline: MT5AccountBaseline,
        *,
        overwrite: bool = False,
        destructive: bool = False,
        origin: BaselineOrigin = BaselineOrigin.DECLARED,
        continuity_verified: bool = False,
    ) -> MT5AccountLedger:
        """Crea un ledger VACIO desde una baseline validada y lo persiste.

        Declara la EPOCA contable: `origin=DECLARED` (estado inicial declarado,
        NO reconstruccion historica) y `continuity_verified=False` (la creacion y
        persistencia NO demuestran continuidad). `BASELINE` se marca como
        declarada. `overwrite=True` sobre un snapshot de la MISMA cuenta exige
        `destructive=True` si contiene estado. No declara readiness operativa.
        """
        if not isinstance(baseline, MT5AccountBaseline):
            raise MT5AccountingStoreError(
                "create exige una MT5AccountBaseline ya validada (no se infiere)"
            )
        if origin is not BaselineOrigin.DECLARED:
            raise MT5AccountingStoreError(
                "create solo produce epocas DECLARED; la reconstruccion historica "
                "no esta soportada (no se declara un origen no demostrable)"
            )
        epoch = AccountingEpoch(
            origin=origin, continuity_verified=continuity_verified
        )
        if self.path.exists():
            if not overwrite:
                raise SnapshotExistsError(f"snapshot ya existe: {self.path}")
            existing = self.load()  # corrupto/otra cuenta => falla cerrado
            if existing.account_fingerprint != baseline.account_fingerprint:
                raise SnapshotAccountMismatchError(
                    "no se reemplaza un snapshot de otra cuenta"
                )
            if not _snapshot_is_empty(existing) and not destructive:
                raise SnapshotNotEmptyError(
                    "el snapshot existente contiene estado: requiere "
                    "destructive=True para reemplazarlo por una baseline vacia"
                )
        ledger = MT5AccountLedger(baseline, epoch=epoch)
        self._save(ledger, baseline_declared=True, overwrite=overwrite)
        return ledger

    # ------------------------------------------------------------------ save

    def save(
        self,
        ledger: MT5AccountLedger,
        *,
        baseline_declared: bool = False,
        overwrite: bool = False,
    ) -> PersistenceResult:
        """Persiste el estado actual del ledger de forma atomica.

        Devuelve el resultado de durabilidad (H2). `overwrite=False` (default)
        no sobrescribe (no-clobber atomico). Con `overwrite=True` exige que la
        identidad de cuenta coincida con el snapshot existente.
        """
        if not isinstance(ledger, MT5AccountLedger):
            raise MT5AccountingStoreError("save exige un MT5AccountLedger")
        return self._save(
            ledger, baseline_declared=baseline_declared, overwrite=overwrite
        )

    def _save(
        self, ledger: MT5AccountLedger, *, baseline_declared: bool, overwrite: bool
    ) -> PersistenceResult:
        if self.path.exists():
            if not overwrite:
                raise SnapshotExistsError(
                    f"snapshot ya existe (use overwrite=True): {self.path}"
                )
            existing = self.load()  # valida; falla cerrado si esta corrupto
            if existing.account_fingerprint != ledger.account_fingerprint:
                raise SnapshotAccountMismatchError(
                    "no se sobrescribe un snapshot de otra cuenta"
                )
        payload = self._envelope(ledger, baseline_declared)
        try:
            MT5AccountLedger.from_snapshot(payload["accounting"])
        except MT5AccountingError as exc:
            raise MT5AccountingStoreError(f"snapshot no restaurable: {exc}") from exc
        durable = self._write_atomic(payload, overwrite=overwrite)
        result = (
            PersistenceResult.DURABLE
            if durable
            else PersistenceResult.REPLACED_DURABILITY_UNCONFIRMED
        )
        self.last_persistence_result = result
        return result

    def _envelope(self, ledger: MT5AccountLedger, baseline_declared: bool) -> dict:
        return {
            "schema_version": STORE_SCHEMA_VERSION,
            "baseline_declared": bool(baseline_declared),
            "accounting": ledger.to_snapshot(),
        }

    # ------------------------------------------------------------------ load

    def load(self, *, expected_fingerprint: str | None = None) -> MT5AccountLedger:
        """Restaura el ledger. Falla cerrado ante cualquier anomalia."""
        if not self.path.exists():
            raise SnapshotNotFoundError(f"snapshot inexistente: {self.path}")
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotCorruptError(f"snapshot ilegible: {exc}") from exc
        if raw.strip() == "":
            raise SnapshotCorruptError(f"snapshot vacio/truncado: {self.path}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SnapshotCorruptError(f"snapshot JSON invalido: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotCorruptError("snapshot no es un objeto JSON")
        if data.get("schema_version") != STORE_SCHEMA_VERSION:
            raise SnapshotVersionError(
                f"schema_version no soportada: {data.get('schema_version')!r}"
            )
        accounting = data.get("accounting")
        if not isinstance(accounting, dict):
            raise SnapshotCorruptError("snapshot sin bloque `accounting`")
        try:
            ledger = MT5AccountLedger.from_snapshot(accounting)
        except MT5AccountStateVersionError as exc:
            # Version de estado incompatible/antigua: rechazo explicito (nunca se
            # interpreta con defaults).
            raise SnapshotVersionError(
                f"accounting state_version no soportada: {exc}"
            ) from exc
        except MT5AccountingError as exc:
            raise SnapshotCorruptError(f"snapshot invalido: {exc}") from exc
        if (
            expected_fingerprint is not None
            and ledger.account_fingerprint != expected_fingerprint
        ):
            raise SnapshotAccountMismatchError(
                "fingerprint de cuenta no coincide con el esperado"
            )
        return ledger

    def baseline_declared(self) -> bool:
        """Lee el marcador de baseline declarada sin restaurar el ledger."""
        if not self.path.exists():
            raise SnapshotNotFoundError(f"snapshot inexistente: {self.path}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptError(f"snapshot invalido: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotCorruptError("snapshot no es un objeto JSON")
        return bool(data.get("baseline_declared", False))

    # ------------------------------------------------------ escritura atomica

    def _write_atomic(self, payload: dict, *, overwrite: bool) -> bool:
        """Escribe y publica el snapshot. Devuelve True si la durabilidad del
        directorio quedo confirmada; False si el destino ya fue sustituido pero
        no pudo confirmarse. Lanza `SnapshotWriteError`/`SnapshotExistsError` si
        algo falla ANTES de sustituir (el anterior queda intacto)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self._write_temp(tmp, payload)
        except Exception as exc:  # noqa: BLE001
            self._safe_unlink(tmp)
            raise SnapshotWriteError(
                f"fallo al escribir el temporal: {exc}"
            ) from exc
        try:
            self._publish(tmp, self.path, overwrite=overwrite)
        except SnapshotExistsError:
            self._safe_unlink(tmp)
            raise
        except Exception as exc:  # noqa: BLE001
            self._safe_unlink(tmp)
            raise SnapshotWriteError(
                f"fallo antes de la sustitucion del destino: {exc}"
            ) from exc
        return self._sync_dir()

    def _write_temp(self, tmp: Path, payload: dict) -> None:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                payload, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            fh.flush()
            os.fsync(fh.fileno())

    def _publish(self, tmp: Path, path: Path, *, overwrite: bool) -> None:
        if overwrite:
            os.replace(tmp, path)  # atomico; reemplaza (un solo escritor)
            return
        # No-clobber atomico: crea el destino SOLO si no existe.
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise SnapshotExistsError(
                f"snapshot creado concurrentemente: {path}"
            ) from None
        except (OSError, NotImplementedError) as exc:
            raise SnapshotWriteError(
                f"publicacion sin reemplazo no soportada en este filesystem: {exc}"
            ) from exc
        self._safe_unlink(tmp)

    def _sync_dir(self) -> bool:
        """fsync del directorio. True si se confirma; False si no es posible."""
        try:
            fd = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(fd)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def _safe_unlink(self, tmp: Path) -> None:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


__all__ = [
    "MT5AccountingStore",
    "MT5AccountingStoreError",
    "PersistenceResult",
    "SnapshotAccountMismatchError",
    "SnapshotCorruptError",
    "SnapshotDurabilityUnconfirmedError",
    "SnapshotExistsError",
    "SnapshotNotEmptyError",
    "SnapshotNotFoundError",
    "SnapshotVersionError",
    "SnapshotWriteError",
    "STORE_SCHEMA_VERSION",
]
