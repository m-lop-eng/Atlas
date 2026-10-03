"""M4 — Strategy Research Record + selección explícita (lifecycle de hipótesis).

BT3-C: cierre mínimo de G-M4-1 (hypothesis pre-registration) y G-M4-2
(selection). NO ejecuta backtests ni elige ninguna hipótesis.

Reglas del Master Spec (02_QUANT_RESEARCH_METHODOLOGY.md §2, §4, §5, §6):

    No hypothesis record        -> NO BACKTEST (research)
    Hypothesis preregistered    -> Eligible for selection
    Explicit selection/opening  -> Implementation/backtest eligibility
    Gate failure                -> STOP (no promotion)

Autoridad de estado (D2): el registro de evidencia (`EvidenceRegistry`) y este
Strategy Research Record son la autoridad; `StrategyMetadata.status` es
descriptivo y NUNCA promueve una hipótesis.

Distinción (BT3-B §5): el `BacktestEngine` usado por tests técnicos está
permitido sin record; solo el backtest EXPERIMENTAL/RESEARCH exige hypothesis +
selection. Este módulo provee el guard para ese uso, no bloquea el motor.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Campos obligatorios del Strategy Research Record (02 §4/§6).
REQUIRED_FIELDS: tuple[str, ...] = (
    "strategy_id",
    "strategy_name",
    "version",
    "asset_class",
    "instrument",
    "market",
    "timeframe",
    "researcher",
    "creation_date",
    "status",
    "hypothesis",
    "market_phenomenon",
    "expected_mechanism",
    "expected_edge",
    "entry_rules",
    "exit_rules",
    "risk_model",
    "execution_assumptions",
    "known_limitations",
    "validation_status",
    "data_scope",
    "falsification_criteria",
)

VALID_STATUSES = (
    "HYPOTHESIS",
    "RESEARCH",
    "PROTOTYPE",
    "BACKTEST",
    "ROBUSTNESS",
    "OOS",
    "PAPER",
    "REJECTED",
)

VALID_DATA_SCOPES = ("DEVELOPMENT", "VALIDATION", "FINAL_OOS", "OBSERVED")


class HypothesisError(ValueError):
    """Violación del lifecycle de hipótesis (record/selección/estado)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


@dataclass(slots=True)
class FalsificationCriterion:
    """Criterio de falsación estructurado y testeable (D5)."""

    metric: str
    threshold: float
    condition: str  # p. ej. ">", ">=", "<", "<="

    def to_dict(self) -> dict:
        return {"metric": self.metric, "threshold": self.threshold, "condition": self.condition}


@dataclass(slots=True)
class StrategyResearchRecord:
    """Strategy Research Record (02 §4). Estado inicial HYPOTHESIS."""

    strategy_id: str
    strategy_name: str
    version: str
    asset_class: str
    instrument: str
    market: str
    timeframe: str
    researcher: str
    creation_date: str
    hypothesis: str
    market_phenomenon: str
    expected_mechanism: str
    expected_edge: str
    entry_rules: str
    exit_rules: str
    risk_model: str
    execution_assumptions: str
    known_limitations: str
    falsification_criteria: list[FalsificationCriterion]
    status: str = "HYPOTHESIS"
    validation_status: str = "NOT_VALIDATED"
    data_scope: str = "DEVELOPMENT"
    time_horizon: str | None = None
    parent_hypothesis: str | None = None
    notes: str | None = None
    retrospective: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["falsification_criteria"] = [c.to_dict() for c in self.falsification_criteria]
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "StrategyResearchRecord":
        crit = [
            FalsificationCriterion(**c)
            for c in raw.get("falsification_criteria", [])
        ]
        known = {f for f in REQUIRED_FIELDS} | {
            "validation_status",
            "time_horizon",
            "parent_hypothesis",
            "notes",
            "retrospective",
        }
        kwargs = {k: v for k, v in raw.items() if k in known}
        kwargs["falsification_criteria"] = crit
        return cls(**kwargs)


def validate_record(record: StrategyResearchRecord) -> list[str]:
    """Valida el esquema. Devuelve la lista de problemas (vacía = elegible).

    Mecánico y determinista; un record con problemas NO es elegible.
    """
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        value = getattr(record, name, None)
        if name == "falsification_criteria":
            continue  # se valida aparte
        if value is None or (isinstance(value, str) and not _is_nonempty_text(value)):
            problems.append(f"falta:{name}")
    if record.status not in VALID_STATUSES:
        problems.append(f"status_invalido:{record.status}")
    if record.data_scope not in VALID_DATA_SCOPES:
        problems.append(f"data_scope_invalido:{record.data_scope}")
    if not record.falsification_criteria:
        problems.append("falta:falsification_criteria")
    else:
        for i, c in enumerate(record.falsification_criteria):
            if not _is_nonempty_text(c.metric):
                problems.append(f"falsification_criteria[{i}].metric")
            if not _is_nonempty_text(c.condition):
                problems.append(f"falsification_criteria[{i}].condition")
            if not isinstance(c.threshold, (int, float)):
                problems.append(f"falsification_criteria[{i}].threshold")
    return problems


class HypothesisRegistry:
    """Registro persistente de Strategy Research Records (research/hypotheses/).

    D1: un archivo por `strategy_id` en `research/hypotheses/<strategy_id>.json`.
    D2: este registro + `EvidenceRegistry` son la autoridad de estado; la
    metadata de la estrategia no promueve.
    """

    def __init__(self, records: dict[str, StrategyResearchRecord] | None = None,
                 *, path: Path | str | None = None) -> None:
        self.records: dict[str, StrategyResearchRecord] = records or {}
        self.path = Path(path) if path is not None else None

    @classmethod
    def dir_default(cls) -> Path:
        return Path(__file__).resolve().parent / "hypotheses"

    @classmethod
    def load_dir(cls, path: Path | str | None = None) -> "HypothesisRegistry":
        directory = Path(path) if path is not None else cls.dir_default()
        records: dict[str, StrategyResearchRecord] = {}
        if directory.exists():
            for f in sorted(directory.glob("*.json")):
                raw = json.loads(f.read_text(encoding="utf-8"))
                rec = StrategyResearchRecord.from_dict(raw)
                records[rec.strategy_id] = rec
        return cls(records, path=directory)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        for sid, rec in self.records.items():
            (self.path / f"{sid}.json").write_text(
                json.dumps(rec.to_dict(), indent=2, ensure_ascii=True),
                encoding="utf-8",
            )

    def register(self, record: StrategyResearchRecord) -> StrategyResearchRecord:
        """Registra un record. Idempotente en escritura, no valida elegibilidad."""
        self.records[record.strategy_id] = record
        return record

    def get(self, strategy_id: str) -> StrategyResearchRecord | None:
        return self.records.get(strategy_id)

    def validate(self, strategy_id: str) -> list[str]:
        rec = self.get(strategy_id)
        if rec is None:
            return ["no_record"]
        return validate_record(rec)


@dataclass(slots=True)
class SelectionRecord:
    """Selección explícita de UNA hipótesis para apertura/experimento (D4)."""

    strategy_id: str
    selected_at: str
    selected_by: str
    reason: str
    record_ref: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "SelectionRecord":
        return cls(**raw)


class SelectionRegistry:
    """Selecciones explícitas persistidas (research/selections/)."""

    def __init__(self, selections: dict[str, SelectionRecord] | None = None,
                 *, path: Path | str | None = None) -> None:
        self.selections: dict[str, SelectionRecord] = selections or {}
        self.path = Path(path) if path is not None else None

    @classmethod
    def dir_default(cls) -> Path:
        return Path(__file__).resolve().parent / "selections"

    @classmethod
    def load_dir(cls, path: Path | str | None = None) -> "SelectionRegistry":
        directory = Path(path) if path is not None else cls.dir_default()
        items: dict[str, SelectionRecord] = {}
        if directory.exists():
            for f in sorted(directory.glob("*.json")):
                raw = json.loads(f.read_text(encoding="utf-8"))
                rec = SelectionRecord.from_dict(raw)
                items[rec.strategy_id] = rec
        return cls(items, path=directory)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        for sid, rec in self.selections.items():
            (self.path / f"{sid}.json").write_text(
                json.dumps(rec.to_dict(), indent=2, ensure_ascii=True),
                encoding="utf-8",
            )

    def select(self, strategy_id: str, *, selected_by: str, reason: str) -> SelectionRecord:
        rec = SelectionRecord(
            strategy_id=strategy_id,
            selected_at=_now_iso(),
            selected_by=selected_by,
            reason=reason,
            record_ref=str(strategy_id),
        )
        self.selections[strategy_id] = rec
        return rec

    def is_selected(self, strategy_id: str) -> bool:
        return strategy_id in self.selections

    def get(self, strategy_id: str) -> SelectionRecord | None:
        return self.selections.get(strategy_id)


# ---------------------------------------------------------------------------
# Guards del lifecycle (G-M4-1 / G-M4-2)
# ---------------------------------------------------------------------------


def require_backtest_eligibility(
    strategy_id: str,
    hypotheses: HypothesisRegistry,
    selections: SelectionRegistry,
) -> StrategyResearchRecord:
    """Guard para un backtest EXPERIMENTAL/RESEARCH (no para tests técnicos).

    Orden: record existe -> record válido -> selección explícita.
    Lanza `HypothesisError` si falla (no bypass).
    """
    rec = hypotheses.get(strategy_id)
    if rec is None:
        raise HypothesisError(
            f"[M4] No existe StrategyResearchRecord para '{strategy_id}': "
            "una estrategia no es una hipótesis preregistrada. NO BACKTEST."
        )
    problems = validate_record(rec)
    if problems:
        raise HypothesisError(
            f"[M4] Record '{strategy_id}' inválido ({', '.join(problems)}): "
            "no elegible para backtest de investigación."
        )
    if not selections.is_selected(strategy_id):
        raise HypothesisError(
            f"[M4] Hipótesis '{strategy_id}' preregistrada pero NO seleccionada "
            "explícitamente. NO BACKTEST."
        )
    return rec


__all__ = [
    "FalsificationCriterion",
    "HypothesisError",
    "HypothesisRegistry",
    "REQUIRED_FIELDS",
    "SelectionRecord",
    "SelectionRegistry",
    "StrategyResearchRecord",
    "VALID_DATA_SCOPES",
    "VALID_STATUSES",
    "require_backtest_eligibility",
    "validate_record",
]
