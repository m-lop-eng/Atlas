"""ATLAS-MT5-ACCOUNTING-05G: autoridad de ADMISION contable MT5 (OFFLINE).

Coordina, ANTES de habilitar cualquier operacion en el runtime, una secuencia
fail-closed:

    observacion FIABLE de identidad/economicos (adapter, solo lectura)
      -> carga del snapshot MT5 (ausente/corrupto/otra cuenta/version => BLOCK)
      -> identidad observada vs baseline (huella + divisa)
      -> integridad contable (`is_complete`) y epoca DECLARED
      -> CONFIRMACION de durabilidad (nunca se asume)
      -> ingesta idempotente de historial en intervalos acotados
      -> reconciliacion BASICA de balance/credito observados

NO conecta con MT5, NO envia ordenes, NO crea baseline, NO sustituye al ledger
SPOT y NO calcula equity de mercado completa (`BALANCE_MATCH_EQUITY_UNVERIFIED`).
La huella de cuenta se DERIVA de la observacion del adaptador; NO se acepta una
huella arbitraria del llamante como prueba de identidad. La comparacion de
huellas NO demuestra la procedencia global del historial.

El cursor registra SOLO el ultimo intervalo consultado con exito; no demuestra
ausencia de huecos historicos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    DealHistoryResult,
    MT5AccountSnapshot,
)
from execution.mt5_accounting import (
    BaselineOrigin,
    MT5AccountingError,
    account_fingerprint,
)
from execution.mt5_accounting_store import (
    MT5AccountingStore,
    MT5AccountingStoreError,
    PersistenceResult,
)
from execution.mt5_ingestion import (
    MT5AccountIngestor,
    MT5IngestionError,
)

_SUCCESS_STATUSES = frozenset({HISTORY_COMPLETE, HISTORY_EMPTY_CONFIRMED})
_UTC = timezone.utc


def _now_utc() -> datetime:
    return datetime.now(_UTC)


def _norm_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MT5AdmissionError(
            f"{field_name} debe ser datetime con zona horaria (UTC), no naive"
        )
    return value.astimezone(_UTC)


class MT5AdmissionError(MT5AccountingError):
    """Error base de la autoridad de admision contable MT5."""


class MT5AdmissionBlockedError(MT5AdmissionError):
    """La admision contable MT5 no esta satisfecha (fail-closed)."""


class MT5AdmissionState(str, Enum):
    ADMITTED = "ADMITTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class MT5AdmissionReport:
    """Resultado tipado de una evaluacion de admision."""

    state: MT5AdmissionState
    stage: str
    reason: str
    ingestion_successful: bool = False
    balance_match: bool = False
    credit_match: bool = False

    @property
    def admitted(self) -> bool:
        return self.state is MT5AdmissionState.ADMITTED


@dataclass
class MT5AccountingAdmission:
    """Autoridad de admision contable MT5 (un solo ledger, sin paralelos).

    Recibe el store durable y un proveedor de historial acotado. Construye la
    identidad a partir de la OBSERVACION del adaptador (`read_account_snapshot`)
    y la compara con la baseline persistida. No crea baseline.
    """

    store: MT5AccountingStore
    # El provider recibe EXPLICITAMENTE el intervalo acotado (frm, to) y debe
    # consultar EXACTAMENTE ese intervalo (p. ej. `adapter.deal_history(frm, to)`).
    history_provider: Callable[[datetime, datetime], DealHistoryResult]
    # Fuente temporal explicita del limite final (normalizada a UTC).
    clock: Callable[[], datetime] = _now_utc
    # Solapamiento declarado para consultas posteriores (segundos, >= 0). 0 = sin
    # solapamiento. Nunca deja que el inicio retroceda por debajo de
    # `baseline.coverage_from`.
    overlap_seconds: int = 0
    tolerance: float = 1e-9

    _ingestor: MT5AccountIngestor | None = field(init=False, default=None)
    _admitted: bool = field(init=False, default=False)
    _report: MT5AdmissionReport | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if isinstance(self.overlap_seconds, bool) or not isinstance(
            self.overlap_seconds, int
        ):
            raise MT5AdmissionError("overlap_seconds debe ser int")
        if self.overlap_seconds < 0:
            raise MT5AdmissionError("overlap_seconds no puede ser negativo")

    # ------------------------------------------------------------- estado

    @property
    def admitted(self) -> bool:
        return self._admitted

    @property
    def report(self) -> MT5AdmissionReport | None:
        return self._report

    @property
    def ingestor(self) -> MT5AccountIngestor | None:
        return self._ingestor

    def ensure_admitted(self) -> None:
        """Lanza si la admision no esta satisfecha (para `_ensure_operable`)."""
        if not self._admitted:
            reason = self._report.reason if self._report is not None else "sin admision"
            raise MT5AdmissionBlockedError(
                f"admision contable MT5 no satisfecha: {reason}"
            )

    # ------------------------------------------------------------ ciclo

    def initialize(self, broker: Any) -> MT5AdmissionReport:
        """Admision de arranque (carga + durabilidad + ingesta + reconciliacion)."""
        return self._evaluate(broker)

    def refresh(self, broker: Any) -> MT5AdmissionReport:
        """Reevaluacion tras actividad del broker (misma secuencia fail-closed)."""
        return self._evaluate(broker)

    # ---------------------------------------------------------- interno

    def _block(self, stage: str, reason: str) -> MT5AdmissionReport:
        self._admitted = False
        report = MT5AdmissionReport(MT5AdmissionState.BLOCKED, stage, reason)
        self._report = report
        return report

    def _clock_utc(self) -> datetime:
        value = self.clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise MT5AdmissionError("el reloj no devuelve datetime con zona horaria")
        return value.astimezone(_UTC)

    def _window(self, ledger: Any) -> tuple[datetime, datetime]:
        """Intervalo [frm, to] en UTC impuesto por la autoridad.

        Primera consulta (sin cursor exitoso): `frm == baseline.coverage_from`.
        Consultas posteriores: `frm = cursor - overlap_seconds`, nunca por debajo
        de `baseline.coverage_from`. `to` = reloj explicitamente normalizado a UTC.
        """
        coverage = ledger.baseline.coverage_from.astimezone(_UTC)
        ingestion = ledger.ingestion
        if ingestion.has_successful_scan:
            frm = ingestion.last_successful_scan_to - timedelta(
                seconds=self.overlap_seconds
            )
        else:
            frm = coverage
        if frm < coverage:
            frm = coverage
        to = self._clock_utc()
        if frm > to:
            raise MT5AdmissionError("intervalo de historial invertido (frm > to)")
        return frm, to

    def _evaluate(self, broker: Any) -> MT5AdmissionReport:
        # 1) Observacion FIABLE de la cuenta (solo lectura). El adaptador debe
        #    exponerla; una huella arbitraria del llamante NO se acepta.
        read = getattr(broker, "read_account_snapshot", None)
        if not callable(read):
            return self._block("identity", "el adaptador no expone identidad observable")
        try:
            observation: MT5AccountSnapshot = read()
        except Exception as exc:  # noqa: BLE001 - observacion incierta => fail-closed
            return self._block("identity", f"identidad de cuenta no observable: {exc}")

        # 2) Carga del snapshot (ausente/corrupto/otra cuenta/version => BLOCK).
        if self._ingestor is None:
            try:
                self._ingestor = MT5AccountIngestor.from_store(self.store)
            except MT5AccountingStoreError as exc:
                return self._block("snapshot", f"snapshot MT5 no cargable: {exc}")
            except MT5AccountingError as exc:
                return self._block("snapshot", f"snapshot MT5 invalido: {exc}")
        ledger = self._ingestor.ledger

        # 3) Identidad/divisa observadas vs baseline persistida.
        observed_fp = account_fingerprint(
            server=observation.server, account_id=observation.login
        )
        if observed_fp != ledger.account_fingerprint:
            return self._block("identity", "huella de cuenta observada != baseline")
        if observation.currency != ledger.account_currency:
            return self._block(
                "currency",
                f"divisa observada {observation.currency!r} != baseline "
                f"{ledger.account_currency!r}",
            )

        # 4) Integridad contable (no se repara ni se descarta el motivo).
        if not ledger.is_complete:
            return self._block(
                "incomplete",
                f"ledger contable incompleto: {ledger.incomplete_reason!r}",
            )

        # 5) Epoca DECLARED y continuidad NO verificada.
        if (
            ledger.epoch.origin is not BaselineOrigin.DECLARED
            or ledger.epoch.continuity_verified
        ):
            return self._block(
                "epoch",
                "epoca contable debe ser DECLARED con continuidad NO verificada",
            )

        # 6) Confirmacion EXPLICITA de durabilidad (no se asume DURABLE).
        try:
            durability = self._ingestor.confirm_durability()
        except Exception as exc:  # noqa: BLE001
            return self._block("durability", f"confirm_durability fallo: {exc}")
        if durability is not PersistenceResult.DURABLE:
            return self._block(
                "durability", f"durabilidad no confirmada ({durability.value})"
            )

        # 7) Ingesta idempotente del historial acotado. La AUTORIDAD determina los
        #    limites (inicio desde `baseline.coverage_from` en la primera consulta;
        #    solapamiento declarado despues) y el provider DEBE consultar ese
        #    intervalo exacto.
        try:
            frm, to = self._window(ledger)
        except MT5AdmissionError as exc:
            return self._block("history", f"intervalo de historial invalido: {exc}")
        try:
            history = self.history_provider(frm, to)
        except Exception as exc:  # noqa: BLE001
            return self._block("history", f"historial no consultable: {exc}")
        if not isinstance(history, DealHistoryResult):
            return self._block("history", "historial no es DealHistoryResult")
        try:
            declared_frm = _norm_utc(history.date_from, "history.date_from")
            declared_to = _norm_utc(history.date_to, "history.date_to")
        except MT5AdmissionError as exc:
            return self._block("history", str(exc))
        if declared_frm != frm or declared_to != to:
            return self._block(
                "history",
                "el intervalo declarado por el resultado NO coincide con el solicitado",
            )
        status = getattr(history, "status", None)
        if status not in _SUCCESS_STATUSES:
            return self._block("history", f"historial no exitoso ({status!r})")
        try:
            outcome = self._ingestor.apply_scan(
                history, account_fingerprint=observed_fp
            )
        except (MT5IngestionError, MT5AccountingError) as exc:
            return self._block("ingestion", f"aplicacion de deals fallo: {exc}")
        if outcome.persistence is not PersistenceResult.DURABLE:
            return self._block(
                "durability",
                f"persistencia tras ingesta no durable ({outcome.persistence.value})",
            )

        # 8) Reconciliacion BASICA balance/credito (equity NO verificada).
        ledger = self._ingestor.ledger
        balance_match = abs(ledger.balance - observation.balance) <= self.tolerance
        credit_match = abs(ledger.credit - observation.credit) <= self.tolerance
        if not (balance_match and credit_match):
            return self._block(
                "reconciliation",
                "balance/credito contable != observado "
                f"(balance {ledger.balance} vs {observation.balance}; "
                f"credit {ledger.credit} vs {observation.credit}); "
                "equity NO verificada (BALANCE_MATCH_EQUITY_UNVERIFIED)",
            )

        # 9) Admision satisfecha.
        self._admitted = True
        report = MT5AdmissionReport(
            MT5AdmissionState.ADMITTED,
            "admitted",
            "admision contable MT5 satisfecha (balance/credito; equity NO verificada)",
            ingestion_successful=True,
            balance_match=True,
            credit_match=True,
        )
        self._report = report
        return report


__all__ = [
    "MT5AccountingAdmission",
    "MT5AdmissionError",
    "MT5AdmissionBlockedError",
    "MT5AdmissionState",
    "MT5AdmissionReport",
]
