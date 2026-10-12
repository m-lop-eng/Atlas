"""IntentJournal (LIVE-08): persistencia write-ahead del intent de orden.

GAP-2 / GAP-2a de LIVE-07: el sistema no persistia de forma general el intent
de una orden (metadata + `client_order_id`) antes del submit, por lo que no
podia garantizar recovery crash-safe de una orden real cuyo submit fue aceptado
y cuyo proceso murio antes de registrar el resultado.

Este modulo implementa la pieza MINIMA segura:

    crear orden
      -> client_order_id estable
      -> persistir intent (write-ahead, durable success)
      -> BrokerAdapter.submit_order
      -> crash en cualquier punto
      -> restart -> load intents -> lookup -> reconciliacion
      -> NUNCA resubmit ciego

Garantia: **write-ahead intent + stable identity + reconciliation +
idempotency**. NO es una transaccion atomica broker+storage.

Storage: un unico fichero JSON versionado en el sistema de ficheros, escrito
de forma ATOMICA (`tmp` en el mismo directorio -> `flush` -> `fsync` ->
`os.replace`), el mismo patron ya usado por el proyecto para adquisicion
(`research/final_oos_acquisition.py::_atomic_write_bytes`) y por
`papertrading/forward_runner.py::_atomic_replace`. Cada actualizacion reescribe
el conjunto completo de intents de forma atomica: una escritura interrumpida
deja el fichero anterior INTACTO (nunca un registro a medias).

Limites de durability (honestos, no se finge mas de lo que hay):
  * `fsync` garantiza que el CONTENIDO del fichero temporal llega a disco antes
    del `os.replace`; el `os.replace` es atomico en el mismo volumen
    (NTFS/POSIX). No se hace `fsync` del directorio (no portable en Windows).
  * single-writer: el journal asume UN solo proceso escritor (el lock de
    instancia del driver: `ForwardInstanceLock`/`StreamingInstanceLock`). No
    implementa locking multi-escritor.
  * almacenamiento local (no red/OneDrive como fuente de verdad): el fichero
    puede vivir en una ruta sincronizada, pero la garantia de atomicidad es del
    sistema de ficheros local.

Ambiguedad/corrupcion => fail-closed: cualquier schema desconocido, JSON
invalido o registro malformado lanza y el llamante NO debe enviar ordenes.

Inicializacion y retencion:

  * FIRST INIT: solo `IntentJournal.initialize(path)` (o `create=True`) crea de
    forma durable el journal vacio + un marcador `<path>.init`. Sin esto, la
    ausencia de journal es un error (`IntentJournalNotInitializedError`).
  * RUNTIME YA INICIALIZADO: si el marcador existe y el journal falta (o
    viceversa) => `IntentJournalStorageAmbiguousError` (STORAGE_AMBIGUOUS): un
    journal live inicializado NUNCA desaparece en silencio -> no new submit.
  * RETENCION: `cleanup()` sin politica explicita es un NO-OP. Los intents
    terminales se conservan como **tombstones** (client_order_id,
    broker_order_id, estado terminal, `fills_applied`, schema), preservando la
    identidad de fill `(client_order_id, fill_sequence)` de LIVE-06. La
    eliminacion fisica queda **deferred** (requiere politica explicita).

Clasificacion de gap (LIVE-08):
  * GAP-2 / GAP-2a: **ADDRESSED — single-process/local durable intent journal**.
  * LIVE CRASH-SAFETY: garantizada dentro de las capacidades de este journal
    (single-process, almacenamiento local, un solo escritor).
  * PERSISTENCE INFRASTRUCTURE GAP: **deferred** para multi-writer / distribuido
    / durabilidad cross-filesystem (fsync+atomic replace NO es una garantia
    distribuida).

NO usa `pickle`. NO persiste secrets (el modelo `Order` no los contiene).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# MT5-DEMO-EXECUTION-CONTROLS-02A (revision de compatibilidad):
#   * v1 = formato LIVE-08 (solo intents de APERTURA; sin campos de operacion).
#   * v2 = formato con semantica de operacion (`operation` OPEN/CANCEL y
#     `target_client_order_id`/`target_broker_order_id`).
# El writer SIEMPRE persiste `SCHEMA_VERSION` (v2). Un lector antiguo (que exige
# v1) DEBE fallar cerrado ante el nuevo esquema, en vez de ignorar
# `operation=CANCEL` y confundir una cancelacion con una apertura.
# La version declarada DETERMINA el contrato: un journal v1 legitimo solo puede
# contener registros sin campos v2 (=> OPEN). Un journal declarado v1 que incluya
# cualquier campo exclusivo de v2 es INCONSISTENTE y se rechaza (fail-closed).
SCHEMA_VERSION = 2
SCHEMA_VERSION_V1 = 1
_READABLE_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION_V1, SCHEMA_VERSION})

# Campos exclusivos del esquema v2: NO pueden aparecer en un documento v1.
_V2_ONLY_FIELDS = (
    "operation",
    "target_client_order_id",
    "target_broker_order_id",
)

# MT5-DEMO-EXECUTION-CONTROLS-02A: tipo de operacion del intent.
OPERATION_OPEN = "OPEN"
OPERATION_CANCEL = "CANCEL"
_KNOWN_OPERATIONS = frozenset({OPERATION_OPEN, OPERATION_CANCEL})

# Estados del ciclo de vida persistidos. Es un superconjunto de
# `OrderStatus`/`BrokerOrderState` (SUBm...); `CREATED` es el estado
# write-ahead inicial (intent persistido, submit aun no ejecutado).
_TERMINAL = frozenset(
    {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"}
)
_KNOWN_STATES = frozenset(
    {
        "CREATED",
        "SUBMITTING",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "UNKNOWN",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "FAILED",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IntentJournalError(ValueError):
    """Error base del journal de intents (siempre fail-closed)."""


class IntentJournalCorruptionError(IntentJournalError):
    """Fichero ausente/invalido/truncado o registro malformado."""


class IntentJournalNotInitializedError(IntentJournalError):
    """Journal nunca inicializado explicitamente (no hay marcador ni fichero).

    Este es el UNICO estado en el que la ausencia de datos es valida: primera
    inicializacion. Nunca se crea el journal en silencio.
    """


class IntentJournalStorageAmbiguousError(IntentJournalCorruptionError):
    """STORAGE_AMBIGUOUS: journal inicializado ausente, o sin marcador.

    Un journal live ya inicializado NUNCA puede desaparecer silenciosamente y
    convertirse en un journal vacio. Su ausencia (o la de su marcador) es una
    ambiguedad de storage => recovery_required / no new submit.
    """


class IntentJournalSchemaError(IntentJournalError):
    """`schema_version` desconocida: no se interpreta (fail-closed)."""


class IntentJournalDuplicateError(IntentJournalError):
    """Conflicto de `client_order_id`: intent ya persistido con otra identidad."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Intent persistible de una orden (write-ahead)."""

    client_order_id: str
    order_id: str
    instrument: str
    side: str
    order_type: str
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    signal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    account_id: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    state: str = "CREATED"
    broker_order_id: str | None = None
    fills_applied: tuple[int, ...] = ()
    updated_at: str | None = None
    # MT5-DEMO-EXECUTION-CONTROLS-02A: tipo de operacion y objetivo canonico.
    # Default OPEN/None => los intents LIVE-08 existentes no cambian.
    operation: str = OPERATION_OPEN
    target_client_order_id: str | None = None
    target_broker_order_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def unresolved(self) -> bool:
        """Intent sin resolucion terminal conocida (indice de recovery)."""
        return not self.terminal

    @staticmethod
    def from_order(order, *, run_id: str | None = None) -> "OrderIntent":
        """Construye el intent MINIMO a partir de una `Order` (sin secrets)."""
        return OrderIntent(
            client_order_id=str(order.client_order_id),
            order_id=str(order.order_id),
            instrument=str(order.instrument),
            side=order.side.value if hasattr(order.side, "value") else str(order.side),
            order_type=(
                order.order_type.value
                if hasattr(order.order_type, "value")
                else str(order.order_type)
            ),
            quantity=float(order.quantity),
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            reduce_only=bool(order.reduce_only),
            signal_id=order.signal_id,
            strategy_id=order.strategy_id,
            strategy_version=order.strategy_version,
            account_id=order.account_id,
            run_id=run_id,
            created_at=(
                order.created_at.isoformat() if order.created_at is not None else None
            ),
        )

    @staticmethod
    def for_cancel(
        *,
        operation_id: str,
        target_client_order_id: str,
        instrument: str,
        side: str,
        order_type: str,
        quantity: float,
        target_broker_order_id: str | int | None = None,
        run_id: str | None = None,
    ) -> "OrderIntent":
        """Construye el intent DURABLE de una operacion de cancelacion (02A).

        Identidad de operacion = `operation_id` (p.ej. `CANCEL:<cid>`); referencia
        canonica al `client_order_id` original en `target_client_order_id` y, si
        se conoce, a su ticket en `target_broker_order_id`. `order_id` es
        determinista para que una repeticion sea idempotente (`begin`).
        `side`/`order_type`/`quantity` copian la orden original solo como
        metadatos del objetivo (no se crea una segunda orden de trading).
        """
        return OrderIntent(
            client_order_id=str(operation_id),
            order_id=f"CANCELORD:{operation_id}",
            instrument=str(instrument),
            side=str(side),
            order_type=str(order_type),
            quantity=float(quantity),
            reduce_only=False,
            run_id=run_id,
            operation=OPERATION_CANCEL,
            target_client_order_id=str(target_client_order_id),
            target_broker_order_id=(
                str(target_broker_order_id)
                if target_broker_order_id is not None
                else None
            ),
        )

    def to_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "instrument": self.instrument,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "reduce_only": self.reduce_only,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "account_id": self.account_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "state": self.state,
            "broker_order_id": self.broker_order_id,
            "fills_applied": list(self.fills_applied),
            "updated_at": self.updated_at,
            "operation": self.operation,
            "target_client_order_id": self.target_client_order_id,
            "target_broker_order_id": self.target_broker_order_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "OrderIntent":
        if not isinstance(d, dict):
            raise IntentJournalCorruptionError(f"intent no es dict: {d!r}")
        try:
            cid = str(d["client_order_id"])
            order_id = str(d["order_id"])
            instrument = str(d["instrument"])
            side = str(d["side"])
            order_type = str(d["order_type"])
            quantity = float(d["quantity"])
            state = str(d["state"])
            fills = tuple(int(x) for x in d.get("fills_applied", ()))
        except (KeyError, TypeError, ValueError) as exc:
            raise IntentJournalCorruptionError(
                f"intent malformado: {exc}"
            ) from exc
        if not cid or not order_id or not instrument:
            raise IntentJournalCorruptionError("intent con campos obligatorios vacios")
        if state not in _KNOWN_STATES:
            raise IntentJournalCorruptionError(f"estado desconocido: {state!r}")
        if quantity <= 0 or quantity != quantity:
            raise IntentJournalCorruptionError(f"quantity invalida: {quantity!r}")
        # 02A: campos ADITIVOS. Ausentes => OPEN (registros LIVE-08 intactos).
        operation = str(d.get("operation", OPERATION_OPEN))
        if operation not in _KNOWN_OPERATIONS:
            raise IntentJournalCorruptionError(
                f"operacion desconocida: {operation!r}"
            )
        target_cid = d.get("target_client_order_id")
        target_bid = d.get("target_broker_order_id")
        if operation == OPERATION_CANCEL and not target_cid:
            raise IntentJournalCorruptionError(
                "intent CANCEL sin target_client_order_id (objetivo no identificable)"
            )
        return OrderIntent(
            client_order_id=cid,
            order_id=order_id,
            instrument=instrument,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=d.get("limit_price"),
            stop_price=d.get("stop_price"),
            reduce_only=bool(d.get("reduce_only", False)),
            signal_id=d.get("signal_id"),
            strategy_id=d.get("strategy_id"),
            strategy_version=d.get("strategy_version"),
            account_id=d.get("account_id"),
            run_id=d.get("run_id"),
            created_at=d.get("created_at"),
            state=state,
            broker_order_id=d.get("broker_order_id"),
            fills_applied=fills,
            updated_at=d.get("updated_at"),
            operation=operation,
            target_client_order_id=target_cid,
            target_broker_order_id=target_bid,
        )


@dataclass(frozen=True, slots=True)
class IntentRecoveryOutcome:
    """Resultado de resolver (lookup-only) UN intent pendiente."""

    client_order_id: str
    result: str  # RESOLVED_FOUND | NOT_FOUND | LOOKUP_UNCERTAIN | BROKER_UNKNOWN | ALREADY_TERMINAL
    state: str
    broker_order_id: str | None = None
    fills: tuple[int, ...] = ()


def _atomic_write_json(
    path: Path, payload: dict, *, retries: int = 3, wait: float = 0.05
) -> None:
    """Escritura atomica: tmp en el mismo dir -> fsync -> os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                payload, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
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


class IntentJournal:
    """Journal write-ahead de intents de orden (fichero JSON versionado).

    El journal es la AUTORIDAD de "que intent de submit existio". NO es la
    autoridad contable (eso es `AccountLedger`) ni el estado observed del broker
    (eso es la reconciliacion). El ledger conserva los valores de accounting; el
    journal conserva la intencion + identidad + que fills ya se aplicaron.

    Inicializacion (fail-closed):

      * FIRST INIT: no existe ni journal ni marcador -> solo con `create=True`
        (o `IntentJournal.initialize`) se crea de forma DURABLE el journal vacio
        + su marcador. Sin esto => `IntentJournalNotInitializedError`.
      * RUNTIME YA INICIALIZADO: el marcador existe. Si el journal falta =>
        `IntentJournalStorageAmbiguousError` (STORAGE_AMBIGUOUS); NO se recrea un
        journal vacio en silencio. Si falta el marcador pero hay journal =>
        tambien ambiguedad.

    Garantia: "un journal live ya inicializado nunca puede desaparecer
    silenciosamente y convertirse en un journal vacio."

    Args:
        path: ruta del fichero JSON. El marcador durable es `<path>.init`.
        create: True solo en la PRIMERA inicializacion (default False).
        clock: reloj inyectable (determinismo en tests).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        create: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._marker_path = self.path.with_name(self.path.name + ".init")
        self._intents: dict[str, OrderIntent] = {}
        self._loaded_version: int = SCHEMA_VERSION
        has_journal = self.path.exists()
        has_marker = self._marker_path.exists()
        if has_marker and not has_journal:
            raise IntentJournalStorageAmbiguousError(
                f"journal inicializado pero ausente: {self.path} (STORAGE_AMBIGUOUS); "
                "no se recrea vacio en silencio"
            )
        if has_journal and not has_marker:
            raise IntentJournalStorageAmbiguousError(
                f"journal presente sin marcador de inicializacion: {self.path} "
                "(STORAGE_AMBIGUOUS); no se acepta como vacio"
            )
        if not has_journal and not has_marker:
            if not create:
                raise IntentJournalNotInitializedError(
                    f"journal no inicializado: {self.path}; use create=True "
                    "(o IntentJournal.initialize) en la primera inicializacion"
                )
            # FIRST INIT: crear de forma durable (journal vacio + marcador).
            self._persist()
            self._write_marker()
            return
        self._load()

    @classmethod
    def initialize(
        cls, path: str | Path, *, clock: Callable[[], datetime] = _utc_now
    ) -> "IntentJournal":
        """Primera inicializacion explicita y DURABLE del journal."""
        return cls(path, create=True, clock=clock)

    @property
    def initialized(self) -> bool:
        """True si el journal esta inicializado (siempre tras __init__ correcto)."""
        return self._marker_path.exists()

    @property
    def marker_path(self) -> Path:
        return self._marker_path

    @property
    def schema_version(self) -> int:
        """Version de esquema con la que se PERSISTE el journal (actual)."""
        return SCHEMA_VERSION

    @property
    def loaded_schema_version(self) -> int:
        """Version de esquema LEIDA del fichero (o la actual en primer init)."""
        return self._loaded_version

    def migrate_if_needed(self) -> bool:
        """Migra el journal a la version de esquema ACTUAL si procede.

        Determinista y NO destructivo: reescribe el MISMO conjunto de intents
        (identidades, fills aplicados, tombstones y estados terminales intactos)
        con `schema_version` actual. La conversion de registros v1 es implicita:
        un registro sin `operation` es OPEN. Devuelve True si reescribio.
        """
        if self._loaded_version >= SCHEMA_VERSION:
            return False
        self._persist()
        self._loaded_version = SCHEMA_VERSION
        return True

    # --------------------------------------------------------------- consulta

    def get(self, client_order_id: str) -> OrderIntent | None:
        return self._intents.get(client_order_id)

    def all(self) -> tuple[OrderIntent, ...]:
        return tuple(self._intents[c] for c in sorted(self._intents))

    def unresolved(self) -> tuple[OrderIntent, ...]:
        """Indice de recovery: intents sin resolucion terminal conocida."""
        return tuple(
            self._intents[c]
            for c in sorted(self._intents)
            if self._intents[c].unresolved
        )

    def __len__(self) -> int:
        return len(self._intents)

    # --------------------------------------------------------------- escritura

    def begin(self, intent: OrderIntent) -> OrderIntent:
        """Persiste el intent ANTES del submit (write-ahead).

        Idempotente si el `client_order_id` ya existe con la MISMA identidad
        (`order_id` + instrument + side + quantity): no reescribe. Si difiere,
        `IntentJournalDuplicateError` (nunca se crea una segunda orden).

        Raises:
            IntentJournalDuplicateError: conflicto de identidad.
            IntentJournalError: fallo de persistencia (=> NO submit).
        """
        existing = self._intents.get(intent.client_order_id)
        if existing is not None:
            if (
                existing.order_id == intent.order_id
                and existing.instrument == intent.instrument
                and existing.side == intent.side
                and float(existing.quantity) == float(intent.quantity)
            ):
                return existing
            raise IntentJournalDuplicateError(
                f"client_order_id {intent.client_order_id!r} ya persistido con "
                f"otra identidad (order_id {existing.order_id!r} != "
                f"{intent.order_id!r})"
            )
        self._intents[intent.client_order_id] = self._stamp(
            intent, state="CREATED"
        )
        self._persist()
        return self._intents[intent.client_order_id]

    def mark_state(
        self,
        client_order_id: str,
        state: str,
        *,
        broker_order_id: str | None = None,
    ) -> OrderIntent:
        """Actualiza el ciclo de vida del intent (idempotente; no regresa desde terminal)."""
        if state not in _KNOWN_STATES:
            raise IntentJournalError(f"estado desconocido: {state!r}")
        current = self._intents.get(client_order_id)
        if current is None:
            raise IntentJournalError(
                f"intent desconocido: {client_order_id!r}"
            )
        if current.terminal:
            return current  # no regresion terminal -> no resurreccion
        updated = self._stamp(
            current,
            state=state,
            broker_order_id=(
                broker_order_id
                if broker_order_id is not None
                else current.broker_order_id
            ),
        )
        self._intents[client_order_id] = updated
        self._persist()
        return updated

    def record_fill(self, client_order_id: str, fill_sequence: int) -> bool:
        """Registra una identidad de fill `(cid, seq)` (idempotente).

        Returns True si era nueva (se persiste); False si ya estaba.
        """
        current = self._intents.get(client_order_id)
        if current is None:
            raise IntentJournalError(
                f"intent desconocido: {client_order_id!r}"
            )
        seq = int(fill_sequence)
        if seq in current.fills_applied:
            return False
        updated = self._stamp(
            current,
            fills_applied=tuple(sorted(set(current.fills_applied) | {seq})),
        )
        self._intents[client_order_id] = updated
        self._persist()
        return True

    def cleanup(self, *, retention=None) -> int:
        """Compactacion SEGURA. Sin politica explicita NO elimina nada.

        Los intents terminales (FILLED/CANCELLED/REJECTED/EXPIRED/FAILED) se
        conservan como **tombstones**: preservan `client_order_id`,
        `broker_order_id`, estado terminal, `fills_applied` y el schema, de modo
        que la identidad de fill `(client_order_id, fill_sequence)` (LIVE-06)
        sobrevive y jamas se re-aplica un fill tras restart/redelivery.

        La eliminacion FISICA requiere una politica de retencion explicita
        (`retention`) y queda **deferred** en este bloque: sin ella no hay
        cleanup. NUNCA se usa `get_order() -> None`, timeout ni fin de replay
        como motivo para borrar.
        """
        if retention is None:
            return 0
        # Politica explicita: solo puede retirar intents TERMINALES y, aun asi,
        # nunca uno con fills aplicados (preserva la identidad de fill).
        removed = 0
        for cid in list(self._intents):
            intent = self._intents[cid]
            if not intent.terminal:
                continue
            if intent.fills_applied:
                continue  # conservar identidad de fill
            if retention(intent):
                del self._intents[cid]
                removed += 1
        if removed:
            self._persist()
        return removed

    def tombstones(self) -> tuple[OrderIntent, ...]:
        """Intents terminales conservados (identidad minima para dedup/recon)."""
        return tuple(
            self._intents[c] for c in sorted(self._intents) if self._intents[c].terminal
        )

    # --------------------------------------------------------------- internos

    def _stamp(self, intent: OrderIntent, **changes) -> OrderIntent:
        base = {
            "updated_at": self._clock().isoformat(),
        }
        base.update(changes)
        return OrderIntent(
            client_order_id=intent.client_order_id,
            order_id=intent.order_id,
            instrument=intent.instrument,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            stop_price=intent.stop_price,
            reduce_only=intent.reduce_only,
            signal_id=intent.signal_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            account_id=intent.account_id,
            run_id=intent.run_id,
            created_at=intent.created_at,
            state=base.get("state", intent.state),
            broker_order_id=base.get("broker_order_id", intent.broker_order_id),
            fills_applied=base.get("fills_applied", intent.fills_applied),
            updated_at=base["updated_at"],
            operation=intent.operation,
            target_client_order_id=intent.target_client_order_id,
            target_broker_order_id=intent.target_broker_order_id,
        )

    def to_dict(self) -> dict:
        # Invariante 02A: una operacion CANCEL exige esquema v2. El writer
        # declara SIEMPRE `SCHEMA_VERSION`; si alguna vez se rebajara por debajo
        # de v2 con un CANCEL presente, se falla cerrado en vez de persistir un
        # documento v1 que ocultaria la operacion.
        if (
            SCHEMA_VERSION < SCHEMA_VERSION_V1 + 1
            and any(i.operation != OPERATION_OPEN for i in self._intents.values())
        ):
            raise IntentJournalError(
                "operacion CANCEL requiere schema v2; el writer declara "
                f"v{SCHEMA_VERSION} (fail-closed)"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock().isoformat(),
            "intents": {c: i.to_dict() for c, i in sorted(self._intents.items())},
        }

    def _persist(self) -> None:
        try:
            _atomic_write_json(self.path, self.to_dict())
        except OSError as exc:
            raise IntentJournalError(
                f"no se pudo persistir el journal en {self.path}: {exc}"
            ) from exc

    def _write_marker(self) -> None:
        """Marcador durable de inicializacion (se escribe UNA sola vez)."""
        try:
            _atomic_write_json(
                self._marker_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "initialized_at": self._clock().isoformat(),
                },
            )
        except OSError as exc:
            raise IntentJournalError(
                f"no se pudo crear el marcador de inicializacion "
                f"{self._marker_path}: {exc}"
            ) from exc

    def _load(self) -> None:
        if not self.path.exists():
            self._intents = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IntentJournalCorruptionError(
                f"journal ilegible en {self.path}: {exc}"
            ) from exc
        if raw.strip() == "":
            # Fichero vacio/truncado: NO es un journal valido (el journal
            # inicializado vacio es JSON con schema_version). Fail-closed.
            raise IntentJournalCorruptionError(
                f"journal vacio/truncado en {self.path} (STORAGE_AMBIGUOUS)"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntentJournalCorruptionError(
                f"journal JSON invalido en {self.path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise IntentJournalCorruptionError("journal no es un objeto JSON")
        version = data.get("schema_version")
        if version not in _READABLE_SCHEMA_VERSIONS:
            # Fail-closed: una version que no entendemos (incluida una FUTURA)
            # NO se interpreta silenciosamente como v1 (eso ignoraria
            # `operation=CANCEL`). Igual criterio que exige el lector antiguo
            # frente al schema nuevo.
            raise IntentJournalSchemaError(
                f"schema_version {version!r} no soportada (soportadas "
                f"{sorted(_READABLE_SCHEMA_VERSIONS)}); fail-closed"
            )
        self._loaded_version = int(version)
        intents = data.get("intents")
        if not isinstance(intents, dict):
            raise IntentJournalCorruptionError("bloque `intents` invalido")
        parsed: dict[str, OrderIntent] = {}
        for cid, payload in intents.items():
            # La version declarada determina el contrato: un journal v1 NO puede
            # contener campos exclusivos de v2 (seria un CANCEL oculto que un
            # lector v1 interpretaria como OPEN).
            if (
                self._loaded_version == SCHEMA_VERSION_V1
                and isinstance(payload, dict)
                and any(key in payload for key in _V2_ONLY_FIELDS)
            ):
                present = [k for k in _V2_ONLY_FIELDS if k in payload]
                raise IntentJournalSchemaError(
                    f"journal v1 inconsistente: el intent {cid!r} declara campos "
                    f"exclusivos de v2 {present}; fail-closed (no se interpreta "
                    "silenciosamente como OPEN)"
                )
            intent = OrderIntent.from_dict(payload)
            if intent.client_order_id != cid:
                raise IntentJournalCorruptionError(
                    f"clave {cid!r} != client_order_id {intent.client_order_id!r}"
                )
            parsed[cid] = intent
        self._intents = parsed


def recover_pending_intents(
    journal: IntentJournal,
    broker,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[IntentRecoveryOutcome, ...]:
    """Resuelve (SOLO lookup) los intents pendientes contra el broker.

    Reglas (LIVE-02/LIVE-07/LIVE-08):
      * FOUND (estado distinto de UNKNOWN) -> registrar `broker_order_id`, las
        identidades de fill `(cid, seq)` y fijar el estado del broker.
      * `get_order` -> None / `OrderNotFoundError` (NOT_FOUND) -> **permanece
        unresolved** (NOT_FOUND NO prueba que el submit no ocurrio).
      * `BrokerConnectionError` (LOOKUP_UNCERTAIN) -> permanece unresolved.
      * `BrokerOrder.state == UNKNOWN` -> permanece unresolved.

    NUNCA envia/cancela/reenvia ordenes. NUNCA borra intents.
    """
    from brokers.models import BrokerOrderState, OrderNotFoundError
    from brokers.models import BrokerConnectionError

    outcomes: list[IntentRecoveryOutcome] = []
    for intent in journal.unresolved():
        # 02A: la resolucion de cancelaciones es un flujo SEPARADO
        # (`recover_pending_cancels`); aqui solo se resuelven intents de APERTURA.
        if intent.operation != OPERATION_OPEN:
            continue
        cid = intent.client_order_id
        try:
            bro = broker.get_order(cid)
        except OrderNotFoundError:
            bro = None
        except BrokerConnectionError:
            outcomes.append(
                IntentRecoveryOutcome(cid, "LOOKUP_UNCERTAIN", intent.state)
            )
            continue
        except Exception:  # noqa: BLE001 - transporte/estado incierto => unresolved
            outcomes.append(
                IntentRecoveryOutcome(cid, "LOOKUP_UNCERTAIN", intent.state)
            )
            continue
        if bro is None:
            outcomes.append(IntentRecoveryOutcome(cid, "NOT_FOUND", intent.state))
            continue
        if bro.state is BrokerOrderState.UNKNOWN:
            outcomes.append(
                IntentRecoveryOutcome(cid, "BROKER_UNKNOWN", intent.state)
            )
            continue
        # FOUND: adoptar la verdad del broker (sin reenviar).
        journal.mark_state(
            cid, bro.state.value, broker_order_id=bro.broker_order_id
        )
        fills: list[int] = []
        for i, f in enumerate(getattr(bro, "fills", ()) or ()):
            seq = int(getattr(f, "fill_sequence", i))
            journal.record_fill(cid, seq)
            fills.append(seq)
        outcomes.append(
            IntentRecoveryOutcome(
                cid,
                "RESOLVED_FOUND",
                bro.state.value,
                broker_order_id=bro.broker_order_id,
                fills=tuple(fills),
            )
        )
    return tuple(outcomes)


def recover_pending_cancels(
    journal: IntentJournal,
    broker,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[IntentRecoveryOutcome, ...]:
    """Resuelve (SOLO lookup) los intents de CANCELACION pendientes (02A).

    Mira el estado AUTORITATIVO del OBJETIVO (`target_client_order_id`) en el
    broker; NUNCA reenvia ni repite una cancelacion a ciegas:

      * target `CANCELLED` -> la cancelacion se confirma: intent resuelto.
      * target `SUBMITTED`/`PARTIALLY_FILLED` -> el objetivo sigue activo: NO se
        repite la cancelacion automaticamente (permanece sin resolver).
      * target `FILLED` -> NO se afirma que se cancelo; se preservan los fills y
        se marca la cancelacion como no aplicable (REJECTED) para reconciliacion.
      * target `REJECTED`/`EXPIRED` -> objetivo terminal sin exposicion pendiente:
        cancelacion no aplicable (REJECTED).
      * `NOT_FOUND`/`LOOKUP_UNCERTAIN`/`UNKNOWN`/identidad ambigua -> se mantiene
        sin resolver y bloquea operaciones incompatibles.

    NUNCA envia/cancela/reenvia ordenes. NUNCA borra intents.
    """
    from brokers.models import (
        BrokerConnectionError,
        BrokerOrderState,
        OrderNotFoundError,
    )

    outcomes: list[IntentRecoveryOutcome] = []
    for intent in journal.unresolved():
        if intent.operation != OPERATION_CANCEL:
            continue
        op_id = intent.client_order_id
        target = intent.target_client_order_id
        if not target:
            outcomes.append(
                IntentRecoveryOutcome(op_id, "NOT_FOUND", intent.state)
            )
            continue
        try:
            bro = broker.get_order(target)
        except OrderNotFoundError:
            outcomes.append(
                IntentRecoveryOutcome(op_id, "NOT_FOUND", intent.state)
            )
            continue
        except BrokerConnectionError:
            outcomes.append(
                IntentRecoveryOutcome(op_id, "LOOKUP_UNCERTAIN", intent.state)
            )
            continue
        except Exception:  # noqa: BLE001 - estado incierto => unresolved
            outcomes.append(
                IntentRecoveryOutcome(op_id, "LOOKUP_UNCERTAIN", intent.state)
            )
            continue
        if bro is None:
            outcomes.append(
                IntentRecoveryOutcome(op_id, "NOT_FOUND", intent.state)
            )
            continue
        if bro.state is BrokerOrderState.UNKNOWN:
            outcomes.append(
                IntentRecoveryOutcome(op_id, "BROKER_UNKNOWN", intent.state)
            )
            continue
        if bro.state is BrokerOrderState.CANCELLED:
            journal.mark_state(
                op_id, "CANCELLED", broker_order_id=bro.broker_order_id
            )
            outcomes.append(
                IntentRecoveryOutcome(
                    op_id, "RESOLVED_CANCELLED", "CANCELLED",
                    broker_order_id=bro.broker_order_id,
                )
            )
            continue
        if bro.state is BrokerOrderState.FILLED:
            # No se confirma cancelacion: preservar fills; cancelacion no aplicable.
            journal.mark_state(
                op_id, "REJECTED", broker_order_id=bro.broker_order_id
            )
            outcomes.append(
                IntentRecoveryOutcome(
                    op_id, "TARGET_FILLED", "REJECTED",
                    broker_order_id=bro.broker_order_id,
                )
            )
            continue
        if bro.state in (
            BrokerOrderState.SUBMITTED,
            BrokerOrderState.PARTIALLY_FILLED,
        ):
            outcomes.append(
                IntentRecoveryOutcome(
                    op_id, "TARGET_ACTIVE", intent.state,
                    broker_order_id=bro.broker_order_id,
                )
            )
            continue
        # REJECTED/EXPIRED: objetivo terminal sin exposicion pendiente.
        journal.mark_state(op_id, "REJECTED", broker_order_id=bro.broker_order_id)
        outcomes.append(
            IntentRecoveryOutcome(
                op_id, "TARGET_TERMINAL", "REJECTED",
                broker_order_id=bro.broker_order_id,
            )
        )
    return tuple(outcomes)


__all__ = [
    "IntentJournal",
    "IntentJournalCorruptionError",
    "IntentJournalDuplicateError",
    "IntentJournalError",
    "IntentJournalNotInitializedError",
    "IntentJournalSchemaError",
    "IntentJournalStorageAmbiguousError",
    "IntentRecoveryOutcome",
    "OPERATION_CANCEL",
    "OPERATION_OPEN",
    "OrderIntent",
    "SCHEMA_VERSION",
    "recover_pending_cancels",
    "recover_pending_intents",
]
