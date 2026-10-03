"""EVIDENCE: estado de evidencia por hipótesis y ciclo de vida del FINAL_OOS.

Añade a DATA_ROLE (research/data_role.py) el concepto de **estado de decisión**
formal. Mientras DATA_ROLE responde "qué tramo temporal se puede usar",
EVIDENCE responde "en qué punto del ciclo está cada hipótesis y si existe un
FINAL_OOS limpio disponible para evaluarla".

Estados de evidencia (cadena):
    HYPOTHESIS → RESEARCH_REQUIRED → ROBUSTNESS_SUPPORTED
              ↘ REJECTED
    ROBUSTNESS_SUPPORTED → FINAL_OOS_PENDING → FINAL_OOS_PASSED → PAPER
                                              ↘ FINAL_OOS_FAILED → REJECTED

Vida del FINAL_OOS:
    NOT_DECLARED → BLOCKED_BY_DATA_AVAILABILITY → DECLARED → CONSUMED

El estado `BLOCKED_BY_DATA_AVAILABILITY` es la respuesta CORRECTA de Atlas
cuando el dataset disponible llega hasta `observed_through` y todavía no existen
barras posteriores a `minimum_start`: lo que no se puede evaluar no debe poder
presentarse como desconocido. `guard()` lo combina con el `guard_final_oos` de
DATA_ROLE para impedir mecánicamente que un experimento posterior reutilice un
tramo ya consumido o cree un FINAL_OOS con datos que no existen.

El registro se persiste en `research/evidence.json` (versionado en el repo).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from research.data_role import DataRange, DataRole, DataRoleError, RoleRegistry


class EvidenceState(str, Enum):
    HYPOTHESIS = "HYPOTHESIS"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
    ROBUSTNESS_SUPPORTED = "ROBUSTNESS_SUPPORTED"
    FINAL_OOS_PENDING = "FINAL_OOS_PENDING"
    FINAL_OOS_PASSED = "FINAL_OOS_PASSED"
    FINAL_OOS_FAILED = "FINAL_OOS_FAILED"
    REJECTED = "REJECTED"
    PAPER = "PAPER"


class FinalOosStatus(str, Enum):
    NOT_DECLARED = "NOT_DECLARED"
    BLOCKED_BY_DATA_AVAILABILITY = "BLOCKED_BY_DATA_AVAILABILITY"
    DECLARED = "DECLARED"
    CONSUMED = "CONSUMED"


class EvidenceError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[EvidenceState, frozenset[EvidenceState]] = {
    EvidenceState.HYPOTHESIS: frozenset(
        {EvidenceState.RESEARCH_REQUIRED, EvidenceState.REJECTED}
    ),
    EvidenceState.RESEARCH_REQUIRED: frozenset(
        {EvidenceState.ROBUSTNESS_SUPPORTED, EvidenceState.REJECTED}
    ),
    EvidenceState.ROBUSTNESS_SUPPORTED: frozenset(
        {EvidenceState.FINAL_OOS_PENDING, EvidenceState.RESEARCH_REQUIRED}
    ),
    EvidenceState.FINAL_OOS_PENDING: frozenset(
        {EvidenceState.FINAL_OOS_PASSED, EvidenceState.FINAL_OOS_FAILED}
    ),
    EvidenceState.FINAL_OOS_PASSED: frozenset({EvidenceState.PAPER}),
    EvidenceState.FINAL_OOS_FAILED: frozenset(
        {EvidenceState.REJECTED, EvidenceState.RESEARCH_REQUIRED}
    ),
    EvidenceState.REJECTED: frozenset(),
    EvidenceState.PAPER: frozenset(),
}


DEFAULT_EVIDENCE_PATH = Path(__file__).resolve().parent / "evidence.json"


def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class EvidenceRecord:
    state: EvidenceState
    decided_at: str
    note: str | None = None


@dataclass(slots=True)
class FinalOosBlock:
    status: FinalOosStatus = FinalOosStatus.NOT_DECLARED
    minimum_start: int | None = None
    note: str | None = None


@dataclass(slots=True)
class DatasetEvidence:
    observed_through: int | None = None
    final_oos: FinalOosBlock = field(default_factory=FinalOosBlock)


class EvidenceRegistry:
    """Registro persistente de estados de evidencia + vida del FINAL_OOS."""

    def __init__(
        self,
        dataset: DatasetEvidence | None = None,
        hypotheses: dict[str, EvidenceRecord] | None = None,
    ) -> None:
        self.dataset = dataset or DatasetEvidence()
        self.hypotheses: dict[str, EvidenceRecord] = hypotheses or {}

    @classmethod
    def from_file(cls, path: Path | str) -> "EvidenceRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        d = raw.get("dataset", {})
        f = d.get("final_oos", {})
        dataset = DatasetEvidence(
            observed_through=int(d["observed_through"]) if d.get("observed_through") else None,
            final_oos=FinalOosBlock(
                status=FinalOosStatus(f.get("status", FinalOosStatus.NOT_DECLARED.value)),
                minimum_start=int(f["minimum_start"]) if f.get("minimum_start") else None,
                note=f.get("note"),
            ),
        )
        hyps: dict[str, EvidenceRecord] = {}
        for hid, item in raw.get("hypotheses", {}).items():
            hyps[hid] = EvidenceRecord(
                state=EvidenceState(item["state"]),
                decided_at=item["decided_at"],
                note=item.get("note"),
            )
        return cls(dataset=dataset, hypotheses=hyps)

    def to_file(self, path: Path | str) -> None:
        raw = {
            "version": 1,
            "dataset": {
                "observed_through": self.dataset.observed_through,
                "observed_through_iso": _iso(self.dataset.observed_through),
                "final_oos": {
                    "status": self.dataset.final_oos.status.value,
                    "minimum_start": self.dataset.final_oos.minimum_start,
                    "minimum_start_iso": _iso(self.dataset.final_oos.minimum_start),
                    "note": self.dataset.final_oos.note,
                },
            },
            "hypotheses": {
                hid: asdict(rec) for hid, rec in sorted(self.hypotheses.items())
            },
        }
        Path(path).write_text(
            json.dumps(raw, indent=2, ensure_ascii=True), encoding="utf-8"
        )

    @classmethod
    def from_roles(cls, roles: RoleRegistry, *, path: Path | str | None = None) -> "EvidenceRegistry":
        """Carga el registro o lo construye desde los roles (observed_through).

        `observed_through` siempre se deriva del tramo OBSERVED de DATA_ROLE
        (fuente de verdad de rangos); este archivo no puede contradecirlo.
        """
        observed = max(
            (c.end_epoch for c in roles.chunks.values() if c.role is DataRole.OBSERVED),
            default=None,
        )
        reg = cls()
        if path is not None and Path(path).exists():
            reg = cls.from_file(path)
        if observed is not None:
            reg.dataset.observed_through = observed
        return reg

    @classmethod
    def load_default(cls) -> "EvidenceRegistry":
        roles = RoleRegistry.load_default()
        return cls.from_roles(roles, path=DEFAULT_EVIDENCE_PATH)

    def state(self, hypothesis: str) -> EvidenceState:
        rec = self.hypotheses.get(hypothesis)
        return rec.state if rec else EvidenceState.HYPOTHESIS

    def register(self, hypothesis: str, *, state: EvidenceState | None = None, note: str | None = None) -> EvidenceRecord:
        if hypothesis in self.hypotheses:
            return self.hypotheses[hypothesis]
        rec = EvidenceRecord(
            state=state or EvidenceState.HYPOTHESIS,
            decided_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            note=note,
        )
        self.hypotheses[hypothesis] = rec
        return rec

    def transition(self, hypothesis: str, target: EvidenceState, *, note: str | None = None) -> EvidenceRecord:
        """Transición validada de estado (lanza si el salto no está permitido)."""
        cur = self.state(hypothesis)
        if target is cur:
            return self.hypotheses[hypothesis]
        if target not in ALLOWED_TRANSITIONS.get(cur, frozenset()):
            raise EvidenceError(
                f"Transición inválida para {hypothesis}: {cur.value} → {target.value}"
            )
        rec = EvidenceRecord(
            state=target,
            decided_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            note=note,
        )
        self.hypotheses[hypothesis] = rec
        return rec

    @property
    def final_oos_status(self) -> FinalOosStatus:
        return self.dataset.final_oos.status

    @property
    def final_oos_available(self) -> bool:
        """Un FINAL_OOS limpio existe y NO está bloqueado por falta de datos."""
        return self.final_oos_status in (FinalOosStatus.DECLARED, FinalOosStatus.CONSUMED)

    def block_final_oos(self, minimum_start: int, *, note: str | None = None) -> None:
        """Marca el FINAL_OOS como BLOCKED por disponibilidad de datos.

        `minimum_start` es el primer epoch de datos LIMPIOS tras `observed_through`
        (datos nuevos > tramo OBSERVED). Mientras no existan barras a partir de
        ese epoch, ningún experimento puede declarar un FINAL_OOS.
        """
        self.dataset.final_oos = FinalOosBlock(
            status=FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY,
            minimum_start=int(minimum_start),
            note=note,
        )

    def unblock_final_oos(
        self,
        roles: RoleRegistry,
        *,
        label: str,
        start_epoch: int,
        end_epoch: int,
        note: str | None = None,
    ) -> DataRange:
        """Desbloquea y DECLARA un FINAL_OOS limpio (transición mecánica).

        Requiere que el rango sea de datos nuevos (start >= minimum_start) y que
        no solape tramos OBSERVED/DECLARED. Una vez declarado, la única salida es
        `mark_consumed` tras la evaluación.
        """
        if self.final_oos_status is not FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY:
            raise EvidenceError(
                f"FINAL_OOS no está bloqueado (estado {self.final_oos_status.value}): "
                "nada que desbloquear."
            )
        if end_epoch <= start_epoch:
            raise EvidenceError(
                f"Rango FINAL_OOS inválido: end {end_epoch} <= start {start_epoch}."
            )
        minimum_start = self.dataset.final_oos.minimum_start
        if minimum_start is not None and start_epoch < minimum_start:
            raise EvidenceError(
                f"Inicio {start_epoch} < minimum_start {minimum_start}: solo datos "
                "nuevos (> tramo OBSERVED) pueden formar un FINAL_OOS."
            )
        roles.guard_final_oos(start_epoch, end_epoch, purpose="declarar FINAL_OOS")
        rng = roles.declare_final_oos(label, start_epoch, end_epoch, note=note)
        self.dataset.final_oos.status = FinalOosStatus.DECLARED
        self.dataset.final_oos.minimum_start = start_epoch
        self.dataset.final_oos.note = note
        return rng

    def mark_consumed(self, roles: RoleRegistry) -> None:
        """Evalúa (consume) el FINAL_OOS. Irreversible: guard lo bloquea luego."""
        if not roles.final_oos_consumed:
            roles.mark_final_oos_consumed()
        self.dataset.final_oos.status = FinalOosStatus.CONSUMED

    def guard(self, roles: RoleRegistry, start_epoch: int, end_epoch: int, *, purpose: str = "") -> None:
        """Guard combinado: DATA_ROLE + disponibilidad de FINAL_OOS.

        Rechaza:
          * tramos OBSERVED o FINAL_OOS consumido (delegado en `RoleRegistry`).
          * cualquier evaluación que toque la región de datos nuevos mientras el
            FINAL_OOS siga BLOCKED (esos datos aún no existen en el repo).
        """
        roles.guard_final_oos(start_epoch, end_epoch, purpose=purpose)
        if self.final_oos_status is FinalOosStatus.BLOCKED_BY_DATA_AVAILABILITY:
            minimum_start = self.dataset.final_oos.minimum_start
            if minimum_start is not None and end_epoch > minimum_start:
                raise EvidenceError(
                    f"[evidence.guard] FINAL_OOS BLOCKED_BY_DATA_AVAILABILITY: los "
                    f"datos a partir de {minimum_start} ({_iso(minimum_start)}) aún "
                    f"no existen. Propósito: {purpose}. No se puede evaluar un "
                    f"FINAL_OOS que no se puede formar."
                )
        if self.final_oos_status is FinalOosStatus.NOT_DECLARED:
            observed = self.dataset.observed_through
            if observed is not None and end_epoch > observed:
                raise EvidenceError(
                    f"[evidence.guard] No hay FINAL_OOS declarado y el rango pide "
                    f"datos posteriores a {observed} ({_iso(observed)}). Propósito: {purpose}."
                )