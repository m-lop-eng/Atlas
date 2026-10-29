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

from execution.order import PURPOSE_CLOSE, PURPOSE_OPEN, PURPOSE_REDUCE

# MT5-DEMO-EXECUTION-CONTROLS-02A/02B (compatibilidad de esquema):
#   * v1 = formato LIVE-08 (solo intents de APERTURA; sin campos de operacion).
#   * v2 = `operation` OPEN/CANCEL + `target_client_order_id`/
#     `target_broker_order_id`.
#   * v3 = operaciones CLOSE/REDUCE + `target_position_id`/`available_quantity`.
# El writer SIEMPRE persiste `SCHEMA_VERSION` (v3). Un lector antiguo (que exige
# v1 o v2) DEBE fallar cerrado ante el nuevo esquema, en vez de ignorar/reinterpretar
# `operation` y confundir una operacion con otra.
# La version declarada DETERMINA el contrato: ningun documento puede contener
# campos exclusivos de una version posterior a la declarada (fail-closed).
SCHEMA_VERSION = 4
SCHEMA_VERSION_V1 = 1
SCHEMA_VERSION_V2 = 2
SCHEMA_VERSION_V3 = 3
_READABLE_SCHEMA_VERSIONS = frozenset(
    {SCHEMA_VERSION_V1, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION}
)

# Campos exclusivos del esquema v2: NO pueden aparecer en un documento v1.
_V2_ONLY_FIELDS = (
    "operation",
    "target_client_order_id",
    "target_broker_order_id",
)
# Campos exclusivos del esquema v3: NO pueden aparecer en v1 ni en v2.
_V3_ONLY_FIELDS = (
    "target_position_id",
    "available_quantity",
)
# Campos exclusivos del esquema v4 (ATLAS-MT5-HEDGING-01): NO pueden aparecer
# en v1/v2/v3. `position_id` = POSITION_IDENTIFIER resultante de una APERTURA
# (vinculacion durable cid -> posicion).
_V4_ONLY_FIELDS = ("position_id",)

# ORDER-07B: protocolo de frontera durable de envio. Los intents nuevos marcan
# este metadato por registro; los journals heredados (sin el) siguen el
# tratamiento conservador anterior (lookup; un CREATED no prueba "nunca enviado").
SUBMIT_PROTOCOL_VERSION = "ORDER-07B"

# MT5-DEMO-EXECUTION-CONTROLS-02A/02B: tipo de operacion del intent.
OPERATION_OPEN = "OPEN"
OPERATION_CANCEL = "CANCEL"
OPERATION_CLOSE = "CLOSE"
OPERATION_REDUCE = "REDUCE"
_KNOWN_OPERATIONS = frozenset(
    {OPERATION_OPEN, OPERATION_CANCEL, OPERATION_CLOSE, OPERATION_REDUCE}
)
# Operaciones validas en documentos v2 (v1 no admite `operation` en absoluto).
_V2_OPERATIONS = frozenset({OPERATION_OPEN, OPERATION_CANCEL})

# Proposito tipado de `Order` -> operacion del intent (02B).
_PURPOSE_TO_OPERATION = {
    PURPOSE_OPEN: OPERATION_OPEN,
    PURPOSE_CLOSE: OPERATION_CLOSE,
    PURPOSE_REDUCE: OPERATION_REDUCE,
}

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
    # 02B (v3): identidad de la POSICION objetivo y exposicion disponible
    # observada en el broker cuando se decidio la operacion (evidencia).
    target_position_id: str | None = None
    available_quantity: float | None = None
    # v4 (HEDGING-01): POSITION_IDENTIFIER resultante de una APERTURA (identidad
    # estable de la posicion propiedad de Atlas). NO es `target_position_id`.
    position_id: str | None = None
    # ORDER-07B: protocolo por registro. `None` = journal heredado (sin frontera
    # SUBMITTING); `SUBMIT_PROTOCOL_VERSION` = intent con frontera durable.
    protocol_version: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def unresolved(self) -> bool:
        """Intent sin resolucion terminal conocida (indice de recovery)."""
        return not self.terminal

    @staticmethod
    def from_order(
        order,
        *,
        run_id: str | None = None,
        protocol_version: str | None = None,
    ) -> "OrderIntent":
        """Construye el intent MINIMO a partir de una `Order` (sin secrets).

        02B: el `purpose` tipado de la orden determina la `operation` del intent
        (OPEN/CLOSE/REDUCE). NO se infiere por `reduce_only`. Para CLOSE/REDUCE
        usar preferentemente `for_close`/`for_reduce` (añaden la evidencia de
        posicion). La compatibilidad LIVE-08 se mantiene: `purpose=OPEN` por
        defecto.
        """
        purpose = str(getattr(order, "purpose", PURPOSE_OPEN))
        operation = _PURPOSE_TO_OPERATION.get(purpose, OPERATION_OPEN)
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
            operation=operation,
            protocol_version=protocol_version,
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
        protocol_version: str | None = None,
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
            protocol_version=protocol_version,
        )

    @staticmethod
    def for_close(
        *,
        operation_id: str,
        instrument: str,
        side: str,
        available_quantity: float,
        target_position_id: str | int | None = None,
        order_type: str = "MARKET",
        run_id: str | None = None,
        protocol_version: str | None = None,
    ) -> "OrderIntent":
        """Intent DURABLE de un CIERRE total (02B). `operation=CLOSE`.

        `quantity` = exposicion neta observada en el broker (`available_quantity`);
        `reduce_only=True`; `side` = lado contrario a la posicion. Objetivo
        canonico NETTING = `instrument`; ticket de posicion en
        `target_position_id` cuando se conoce. `order_id` determinista para la
        idempotencia de `begin`.
        """
        return OrderIntent(
            client_order_id=str(operation_id),
            order_id=f"CLOSEORD:{operation_id}",
            instrument=str(instrument),
            side=str(side),
            order_type=str(order_type),
            quantity=float(available_quantity),
            reduce_only=True,
            run_id=run_id,
            operation=OPERATION_CLOSE,
            target_position_id=(
                str(target_position_id) if target_position_id is not None else None
            ),
            available_quantity=float(available_quantity),
            protocol_version=protocol_version,
        )

    @staticmethod
    def for_reduce(
        *,
        operation_id: str,
        instrument: str,
        side: str,
        quantity: float,
        available_quantity: float,
        target_position_id: str | int | None = None,
        order_type: str = "MARKET",
        run_id: str | None = None,
        protocol_version: str | None = None,
    ) -> "OrderIntent":
        """Intent DURABLE de una REDUCCION parcial (02B). `operation=REDUCE`.

        `quantity` = cantidad explicita solicitada (<= exposicion disponible);
        `available_quantity` = exposicion neta observada en el broker (evidencia);
        `reduce_only=True`; `side` contrario a la posicion.
        """
        return OrderIntent(
            client_order_id=str(operation_id),
            order_id=f"REDUCEORD:{operation_id}",
            instrument=str(instrument),
            side=str(side),
            order_type=str(order_type),
            quantity=float(quantity),
            reduce_only=True,
            run_id=run_id,
            operation=OPERATION_REDUCE,
            target_position_id=(
                str(target_position_id) if target_position_id is not None else None
            ),
            available_quantity=float(available_quantity),
            protocol_version=protocol_version,
        )

    def to_dict(self) -> dict:
        result = {
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
            "target_position_id": self.target_position_id,
            "available_quantity": self.available_quantity,
            "position_id": self.position_id,
        }
        if self.protocol_version is not None:
            result["protocol_version"] = self.protocol_version
        return result

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
        target_pos = d.get("target_position_id")
        available = d.get("available_quantity")
        if operation == OPERATION_CANCEL and not target_cid:
            raise IntentJournalCorruptionError(
                "intent CANCEL sin target_client_order_id (objetivo no identificable)"
            )
        if operation in (OPERATION_CLOSE, OPERATION_REDUCE):
            if not bool(d.get("reduce_only", False)):
                raise IntentJournalCorruptionError(
                    f"intent {operation} debe ser reduce_only=True (no inversion)"
                )
            if available is None:
                raise IntentJournalCorruptionError(
                    f"intent {operation} sin available_quantity (evidencia)"
                )
            try:
                available = float(available)
            except (TypeError, ValueError) as exc:
                raise IntentJournalCorruptionError(
                    f"available_quantity invalida: {available!r}"
                ) from exc
            if available <= 0 or available != available:
                raise IntentJournalCorruptionError(
                    f"available_quantity invalida: {available!r}"
                )
        # H-5 (ATLAS-MT5-HEDGING-02B): `position_id` es la identidad estable de
        # la posicion (v4). Ausente/None = sin vinculo (valido). Si esta
        # presente DEBE ser str no vacio; un numero, lista, objeto o cadena vacia
        # NO se normaliza ni se acepta como identidad valida (fail-closed).
        position_id = d.get("position_id")
        if position_id is not None:
            if not isinstance(position_id, str) or not position_id.strip():
                raise IntentJournalCorruptionError(
                    f"position_id invalido: {position_id!r} (requiere str no vacio)"
                )
        protocol_version = d.get("protocol_version")
        if protocol_version is not None:
            if (
                not isinstance(protocol_version, str)
                or not protocol_version.strip()
            ):
                raise IntentJournalCorruptionError(
                    "protocol_version invalido (requiere str no vacio)"
                )
            protocol_version = protocol_version.strip()
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
            target_position_id=target_pos,
            available_quantity=available,
            position_id=position_id,
            protocol_version=protocol_version,
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

    def link_position(self, client_order_id: str, position_id: str) -> OrderIntent:
        """Vincula DURADERAMENTE una APERTURA con su POSITION_IDENTIFIER (v4).

        Idempotente: repetir con el MISMO `position_id` es no-op; un
        `position_id` distinto para el mismo `client_order_id` es conflicto
        (fail-closed). No depende de magic/comment. Fuente de verdad única de la
        propiedad `client_order_id -> POSITION_IDENTIFIER`.
        """
        cid = str(client_order_id)
        pid = str(position_id)
        if not pid:
            raise IntentJournalError("position_id no puede estar vacio")
        current = self._intents.get(cid)
        if current is None:
            raise IntentJournalError(f"intent desconocido: {cid!r}")
        if current.position_id is not None:
            if current.position_id == pid:
                return current
            raise IntentJournalDuplicateError(
                f"intent {cid!r} ya vinculado a position_id "
                f"{current.position_id!r} != {pid!r}"
            )
        updated = self._stamp(current, position_id=pid)
        self._intents[cid] = updated
        self._persist()
        return updated

    def find_by_position_id(self, position_id: str) -> tuple[OrderIntent, ...]:
        """Intents vinculados a un POSITION_IDENTIFIER (para verificar propiedad)."""
        pid = str(position_id)
        return tuple(i for i in self.all() if i.position_id == pid)

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
            target_position_id=intent.target_position_id,
            available_quantity=intent.available_quantity,
            position_id=base.get("position_id", intent.position_id),
            protocol_version=base.get(
                "protocol_version", intent.protocol_version
            ),
        )

    def to_dict(self) -> dict:
        # Invariante 02A/02B: CANCEL exige >= v2; CLOSE/REDUCE exigen >= v3. El
        # writer declara SIEMPRE `SCHEMA_VERSION`; si alguna vez se rebajara por
        # debajo de la version minima del contenido, se falla cerrado en vez de
        # persistir un documento que ocultaria/reinterpretaria la operacion.
        has_non_open = any(
            i.operation != OPERATION_OPEN for i in self._intents.values()
        )
        has_reduce = any(
            i.operation in (OPERATION_CLOSE, OPERATION_REDUCE)
            for i in self._intents.values()
        )
        if has_non_open and SCHEMA_VERSION < SCHEMA_VERSION_V2:
            raise IntentJournalError(
                "operacion != OPEN requiere schema >= v2; el writer declara "
                f"v{SCHEMA_VERSION} (fail-closed)"
            )
        if has_reduce and SCHEMA_VERSION < 3:
            raise IntentJournalError(
                "operacion CLOSE/REDUCE requiere schema v3; el writer declara "
                f"v{SCHEMA_VERSION} (fail-closed)"
            )
        has_position_id = any(
            i.position_id is not None for i in self._intents.values()
        )
        if has_position_id and SCHEMA_VERSION < 4:
            raise IntentJournalError(
                "position_id requiere schema v4; el writer declara "
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
            # La version declarada DETERMINA el contrato: ningun registro puede
            # declarar campos exclusivos de una version POSTERIOR a la del
            # documento (seria una operacion oculta que un lector antiguo
            # interpretaria mal).
            forbidden = []
            if isinstance(payload, dict):
                if self._loaded_version == SCHEMA_VERSION_V1:
                    forbidden = [
                        k
                        for k in (*_V2_ONLY_FIELDS, *_V3_ONLY_FIELDS, *_V4_ONLY_FIELDS)
                        if k in payload
                    ]
                elif self._loaded_version == SCHEMA_VERSION_V2:
                    forbidden = [
                        k for k in (*_V3_ONLY_FIELDS, *_V4_ONLY_FIELDS) if k in payload
                    ]
                    op = payload.get("operation", OPERATION_OPEN)
                    if op not in _V2_OPERATIONS:
                        raise IntentJournalSchemaError(
                            f"journal v2 inconsistente: el intent {cid!r} declara "
                            f"operation {op!r} (solo OPEN/CANCEL en v2); fail-closed"
                        )
                elif self._loaded_version == SCHEMA_VERSION_V3:
                    forbidden = [k for k in _V4_ONLY_FIELDS if k in payload]
            if forbidden:
                raise IntentJournalSchemaError(
                    f"journal v{self._loaded_version} inconsistente: el intent "
                    f"{cid!r} declara campos exclusivos de una version posterior "
                    f"{forbidden}; fail-closed"
                )
            intent = OrderIntent.from_dict(payload)
            if intent.client_order_id != cid:
                raise IntentJournalCorruptionError(
                    f"clave {cid!r} != client_order_id {intent.client_order_id!r}"
                )
            if (
                self._loaded_version == SCHEMA_VERSION_V2
                and intent.operation not in _V2_OPERATIONS
            ):
                raise IntentJournalSchemaError(
                    f"journal v2 inconsistente: el intent {cid!r} declara "
                    f"operation {intent.operation!r} (solo OPEN/CANCEL en v2); "
                    "fail-closed"
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
        cid = intent.client_order_id
        # ORDER-07B: un CREATED nuevo acredita que NO se cruzo la frontera de
        # envio. Puede terminalizarse sin lookup. Un CREATED heredado (sin
        # protocol_version) NO prueba nada y sigue el camino conservador.
        if (
            intent.state == "CREATED"
            and intent.protocol_version == SUBMIT_PROTOCOL_VERSION
        ):
            journal.mark_state(cid, "FAILED")
            outcomes.append(
                IntentRecoveryOutcome(cid, "PRE_SEND_FAILED", "FAILED")
            )
            continue
        # 02A/02B: la resolucion de cancelaciones es un flujo SEPARADO
        # (`recover_pending_cancels`). Aqui se resuelven los intents que SON una
        # orden de broker: OPEN, CLOSE y REDUCE (todos con cid<->ticket propio).
        if intent.operation == OPERATION_CANCEL:
            continue
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

        def _mark_target(state: str, broker_order_id: str | None = None) -> None:
            if not target:
                return
            target_intent = journal.get(target)
            if target_intent is None or target_intent.terminal:
                return
            try:
                journal.mark_state(
                    target, state, broker_order_id=broker_order_id
                )
            except IntentJournalError:
                pass

        if (
            intent.state == "CREATED"
            and intent.protocol_version == SUBMIT_PROTOCOL_VERSION
        ):
            journal.mark_state(op_id, "FAILED")
            outcomes.append(
                IntentRecoveryOutcome(op_id, "PRE_SEND_FAILED", "FAILED")
            )
            continue
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
            _mark_target("UNKNOWN", bro.broker_order_id)
            outcomes.append(
                IntentRecoveryOutcome(op_id, "BROKER_UNKNOWN", intent.state)
            )
            continue
        if bro.state is BrokerOrderState.CANCELLED:
            journal.mark_state(
                op_id, "CANCELLED", broker_order_id=bro.broker_order_id
            )
            _mark_target("CANCELLED", bro.broker_order_id)
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
            _mark_target("FILLED", bro.broker_order_id)
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
            _mark_target(bro.state.value, bro.broker_order_id)
            outcomes.append(
                IntentRecoveryOutcome(
                    op_id, "TARGET_ACTIVE", intent.state,
                    broker_order_id=bro.broker_order_id,
                )
            )
            continue
        # REJECTED/EXPIRED: objetivo terminal sin exposicion pendiente.
        journal.mark_state(op_id, "REJECTED", broker_order_id=bro.broker_order_id)
        _mark_target(bro.state.value, bro.broker_order_id)
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
    "OPERATION_CLOSE",
    "OPERATION_OPEN",
    "OPERATION_REDUCE",
    "OrderIntent",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V1",
    "SCHEMA_VERSION_V2",
    "SUBMIT_PROTOCOL_VERSION",
    "recover_pending_cancels",
    "recover_pending_intents",
]
