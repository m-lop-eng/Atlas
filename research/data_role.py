"""DATA_ROLE: roles temporales del dataset y guard metodológico de FINAL_OOS.

Concepto (introducido en H004): Atlas debe poder IMPEDIR errores metodológicos
automáticamente. Cada tramo de la serie temporal se etiqueta con un rol explícito:

    DEVELOPMENT — datos de desarrollo / walk-forward (train + validaciones).
    VALIDATION  — sub-tramo de validación dentro del walk-forward.
    FINAL_OOS   — tramo RESERVADO: nunca tocado; solo se evalúa UNA vez,
                  cuando TODAS las decisiones metodológicas estén congeladas.
    OBSERVED    — tramo que YA fue usado como OOS por experimentos anteriores:
                  Atlas no permite volver a presentarlo como desconocido.

El registro se persiste en `research/data_roles.json` (versionado en el repo).
Flujo de protección:
  * `RoleRegistry.guard_final_oos(...)`: lanza antes de correr un experimento
    si el tramo solicitado toca un tramo OBSERVED o un FINAL_OOS ya consumido.
  * `RoleRegistry.mark_final_oos_consumed(...)`: transición irreversible tras
    tomar una decisión con el FINAL_OOS; en adelante nadie puede tratarlo nuevo.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


class DataRole(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    FINAL_OOS = "FINAL_OOS"
    OBSERVED = "OBSERVED"


class DataRoleError(ValueError):
    pass


@dataclass(slots=True)
class DataRange:
    label: str
    start_epoch: int
    end_epoch: int
    role: DataRole
    consumed: bool = False
    note: str | None = None


DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "data_roles.json"


class RoleRegistry:
    """Registro persistente de tramos y sus roles (carga/guarda JSON)."""

    def __init__(
        self,
        chunks: dict[str, DataRange] | None = None,
        *,
        final_oos: DataRange | None = None,
        notes: dict | None = None,
    ) -> None:
        self.chunks: dict[str, DataRange] = chunks or {}
        self.final_oos: DataRange | None = final_oos
        self.notes: dict = notes or {}

    @classmethod
    def from_file(cls, path: Path | str) -> "RoleRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        chunks: dict[str, DataRange] = {}
        for item in raw.get("chunks", []):
            rng = DataRange(
                label=item["label"],
                start_epoch=int(item["start_epoch"]),
                end_epoch=int(item["end_epoch"]),
                role=DataRole(item["role"]),
                consumed=bool(item.get("consumed", False)),
                note=item.get("note"),
            )
            chunks[rng.label] = rng
        final_oos = None
        if raw.get("final_oos"):
            final_oos = DataRange(
                label=raw["final_oos"]["label"],
                start_epoch=int(raw["final_oos"]["start_epoch"]),
                end_epoch=int(raw["final_oos"]["end_epoch"]),
                role=DataRole.FINAL_OOS,
                consumed=bool(raw["final_oos"].get("consumed", False)),
                note=raw["final_oos"].get("note"),
            )
        return cls(chunks, final_oos=final_oos, notes=raw.get("notes", {}))

    def to_file(self, path: Path | str) -> None:
        raw = {
            "version": 1,
            "chunks": [
                {
                    "label": c.label,
                    "start_epoch": c.start_epoch,
                    "end_epoch": c.end_epoch,
                    "role": c.role.value,
                    "consumed": c.consumed,
                    "note": c.note,
                }
                for c in sorted(self.chunks.values(), key=lambda c: c.start_epoch)
            ],
            "final_oos": (
                {
                    "label": self.final_oos.label,
                    "start_epoch": self.final_oos.start_epoch,
                    "end_epoch": self.final_oos.end_epoch,
                    "consumed": self.final_oos.consumed,
                    "note": self.final_oos.note,
                }
                if self.final_oos
                else None
            ),
            "notes": self.notes,
        }
        Path(path).write_text(
            json.dumps(raw, indent=2, ensure_ascii=True), encoding="utf-8"
        )

    @classmethod
    def load_default(cls) -> "RoleRegistry":
        path = DEFAULT_REGISTRY_PATH
        if path.exists():
            return cls.from_file(path)
        return cls()

    @property
    def final_oos_consumed(self) -> bool:
        if self.final_oos is not None and self.final_oos.consumed:
            return True
        return any(c.consumed for c in self.chunks.values() if c.role is DataRole.FINAL_OOS)

    @property
    def previous_oos_consumed(self) -> bool:
        return any(c.role is DataRole.OBSERVED for c in self.chunks.values())

    def role_for_epoch(self, epoch: int) -> DataRole | None:
        for c in self.chunks.values():
            if c.start_epoch <= epoch < c.end_epoch:
                return c.role
        return None

    def chunks_in(self, start_epoch: int, end_epoch: int) -> list[DataRange]:
        return [
            c
            for c in self.chunks.values()
            if (c.start_epoch < end_epoch and c.end_epoch > start_epoch)
        ]

    def register(
        self,
        label: str,
        start_epoch: int,
        end_epoch: int,
        role: DataRole,
        *,
        note: str | None = None,
    ) -> DataRange:
        """Registra o actualiza (idempotente por label) un tramo.

        Protege contra disputas de función: un tramo no puede registrarse como
        FINAL_OOS/VALIDATION si solapa un tramo OBSERVED, y no puede registrarse
        un tramo DEVELOPMENT/VALIDATION que solape el FINAL_OOS activo.
        """
        if start_epoch >= end_epoch:
            raise DataRoleError(f"Rango inválido para '{label}': start >= end")
        for other in self.chunks.values():
            if other.label == label:
                continue
            overlaps = other.start_epoch < end_epoch and other.end_epoch > start_epoch
            if not overlaps:
                continue
            if other.role is DataRole.OBSERVED and role in (DataRole.FINAL_OOS, DataRole.VALIDATION, DataRole.DEVELOPMENT):
                raise DataRoleError(
                    f"'{label}' solapa '{other.label}' (OBSERVED): no puede "
                    f"presentarse como {role.value}. Los datos ya fueron usados."
                )
            if other.role is DataRole.FINAL_OOS and role is not DataRole.FINAL_OOS:
                raise DataRoleError(
                    f"'{label}' solapa el FINAL_OOS '{other.label}': reservado."
                )
            if role is DataRole.FINAL_OOS and other.role is DataRole.FINAL_OOS:
                raise DataRoleError(
                    f"'{label}' solapa el FINAL_OOS '{other.label}': solo existe "
                    "UN FINAL_OOS y ya está reservado."
                )
        rng = DataRange(
            label=label,
            start_epoch=int(start_epoch),
            end_epoch=int(end_epoch),
            role=role,
            consumed=False,
            note=note,
        )
        self.chunks[label] = rng
        return rng

    def declare_final_oos(self, label: str, start_epoch: int, end_epoch: int, *, note: str | None = None) -> DataRange:
        """Declara el FINAL_OOS (único). Imposible declarar un segundo."""
        if self.final_oos is not None:
            raise DataRoleError(
                f"Ya existe un FINAL_OOS ('{self.final_oos.label}'). Solo se "
                "define una vez: no se admite un segundo FINAL_OOS."
            )
        self.register(label, start_epoch, end_epoch, DataRole.FINAL_OOS, note=note)
        self.final_oos = self.chunks[label]
        return self.final_oos

    def mark_final_oos_consumed(self) -> None:
        """Marca el FINAL_OOS como CONSUMIDO (transición irreversible).

        Debe llamarse tras tomar una DECISIÓN con el FINAL_OOS. En adelante,
        `guard_final_oos` rechaza cualquier intento de re-evaluarlo como nuevo.
        """
        if self.final_oos is None:
            raise DataRoleError(
                "No hay FINAL_OOS declarado; nada que marcar como consumido."
            )
        self.final_oos.consumed = True
        self.chunks[self.final_oos.label].consumed = True

    def guard_final_oos(self, start_epoch: int, end_epoch: int, *, purpose: str = "") -> None:
        """Antes de correr: impide presentar como desconocido tramos observados.

        Lanza `DataRoleError` si el rango solicitado toca un tramo OBSERVED
        (ya usado en decisiones anteriores) o un FINAL_OOS ya consumido.
        Un FINAL_OOS limpio y NO consumido sí puede evaluarse.
        """
        for c in self.chunks_in(start_epoch, end_epoch):
            if c.role is DataRole.OBSERVED:
                raise DataRoleError(
                    f"[guard_final_oos] '{c.label}' es OBSERVED (ya consumido). "
                    f"Propósito: {purpose}. No se puede presentar como OOS desconocido."
                )
            if c.role is DataRole.FINAL_OOS and c.consumed:
                raise DataRoleError(
                    f"[guard_final_oos] FINAL_OOS '{c.label}' ya fue consumido. "
                    f"Propósito: {purpose}."
                )