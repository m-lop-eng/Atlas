"""LedgerStore (MT5-DEMO-LIVE-READINESS-03A): persistencia durable del ledger.

Problema de durabilidad y estrategia elegida (documentada, NO un sistema
paralelo):

  * El `AccountLedger` es la autoridad contable, pero vive en memoria. Su
    `snapshot()` ya contiene TODO el estado contable derivado (cash,
    realized_pnl, positions, specs y el conjunto `applied_fills` con la
    identidad `(client_order_id, fill_sequence)`).
  * `LedgerStore` persiste EXACTAMENTE ese snapshot de forma ATOMICA
    (tmp + fsync + os.replace), reutilizando el mismo patron que
    `execution.intent_journal` y `papertrading.forward_runner`. No se crea
    una base de datos ni un segundo modelo contable.
  * **El broker es la fuente de verdad de los fills.** El snapshot es una
    cache reconstruible: tras un reinicio se recarga el snapshot y, para cada
    orden conocida (journal + broker), se vuelven a leer los fills del broker
    y se aplican SOLO los que falten (idempotencia por `(cid, fill_sequence)`).
    Asi, un fallo entre "aplicar el fill" y "persistir el snapshot" converge
    igualmente a exactamente-una-vez al reiniciar y reconciliar.
  * `applied_fills` persistido evita re-aplicar dentro/fuera de proceso; el
    journal sigue siendo la autoridad de "que intent existio" (LIVE-08).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from papertrading.ledger import AccountLedger, LedgerError

SCHEMA_VERSION = 1


class LedgerStoreError(ValueError):
    """Snapshot de ledger ausente/corrupto o fallo de persistencia."""


def _atomic_write_json(path: Path, payload: dict, *, retries: int = 3, wait: float = 0.05) -> None:
    """Escritura atomica (mismo patron que el journal): tmp -> fsync -> replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt == retries - 1:
                    raise
                time.sleep(wait)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class LedgerStore:
    """Persistencia atomica y validada del snapshot contable del ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def load(self, ledger: AccountLedger) -> bool:
        """Carga el snapshot en el ledger. False si no existe (primer arranque).

        Fail-closed: un fichero presente pero corrupto/incompatible lanza
        `LedgerStoreError` y NO muta el ledger (se valida con `probe_snapshot`
        antes de aplicar).
        """
        if not self.path.exists():
            return False
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerStoreError(f"ledger ilegible en {self.path}: {exc}") from exc
        if raw.strip() == "":
            raise LedgerStoreError(f"ledger vacio/truncado en {self.path}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LedgerStoreError(f"ledger JSON invalido en {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise LedgerStoreError(
                f"ledger schema no soportada: {data!r}" if isinstance(data, dict) else "ledger no es objeto JSON"
            )
        block = data.get("ledger")
        if not isinstance(block, dict):
            raise LedgerStoreError("ledger sin bloque `ledger`")
        # Prevalida SIN mutar; solo aplica si es valido.
        try:
            ledger.probe_snapshot(block)
        except LedgerError as exc:
            raise LedgerStoreError(f"ledger corrupto: {exc}") from exc
        ledger.load_snapshot(block)
        return True

    def persist(self, ledger: AccountLedger) -> None:
        try:
            _atomic_write_json(
                self.path,
                {"schema_version": SCHEMA_VERSION, "ledger": ledger.snapshot()},
            )
        except OSError as exc:
            raise LedgerStoreError(
                f"no se pudo persistir el ledger en {self.path}: {exc}"
            ) from exc


__all__ = ["LedgerStore", "LedgerStoreError", "SCHEMA_VERSION"]
