"""ATLAS-MT5-ACCOUNTING-05D: aplicacion idempotente de deals y avance atomico
del cursor de ingestion (OFFLINE, aislado).

Capa de COORDINACION entre el adaptador (un `DealHistoryResult` read-only), el
modelo contable (`MT5AccountLedger`) y el store durable (`MT5AccountingStore`).
NO se integra con `LiveExecutionRuntime`, NO conecta con MT5, NO ejecuta trading
y NO implementa reconciliacion de cuenta.

Contrato (fail-closed):
  * Una SOLA operacion publica aplica una consulta EXITOSA y actualiza el cursor
    conjuntamente (`MT5AccountIngestor.apply_scan`). NO existe una operacion
    publica independiente para adelantar el cursor.
  * Se reconstruye un ledger CANDIDATO desde la baseline y el conjunto de deals
    unicos en orden canonico `(time, time_msc, ticket)`; el ledger ACTIVO nunca
    se muta durante la preparacion (una validacion o aplicacion fallida no deja
    estado parcial).
  * `HISTORY_INCOMPLETE`/`None`/excepciones/estados desconocidos/incoherentes NO
    avanzan el cursor.
  * Una consulta exitosa NO demuestra cobertura historica global ni descarta
    huecos o eventos tardios.
  * Si la persistencia NO confirma durabilidad, se BLOQUEAN nuevas ingestiones
    hasta una confirmacion durable posterior explicita.

Un solo escritor por ruta: el store no coordina escritores concurrentes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from brokers.models import HistoricalDeal
from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_EMPTY_CONFIRMED,
    DealHistoryResult,
)
from execution.mt5_accounting import (
    AccountingEpoch,
    DealIdentityCollisionError,
    IngestionScanStatus,
    IngestionState,
    MT5AccountBaseline,
    MT5AccountingError,
    MT5AccountLedger,
    MT5Deal,
)
from execution.mt5_accounting_store import (
    MT5AccountingStore,
    PersistenceResult,
)

_UTC = timezone.utc

_INGESTION_STATUS = {
    HISTORY_COMPLETE: IngestionScanStatus.COMPLETE,
    HISTORY_EMPTY_CONFIRMED: IngestionScanStatus.EMPTY_CONFIRMED,
}

# Clasificacion de un deal respecto al corte de cobertura de la baseline.
_BEFORE_COVERAGE = "before"
_WITHIN_COVERAGE = "within"
_AMBIGUOUS_COVERAGE = "ambiguous"


class MT5IngestionError(MT5AccountingError):
    """Error base de la capa de ingestion contable MT5."""


class ScanRejectedError(MT5IngestionError):
    """Consulta invalida/incompleta/incoherente: no se aplica ni avanza el cursor."""


class AccountIdentityError(MT5IngestionError):
    """La identidad de cuenta verificada no coincide con la baseline."""


class IngestionBlockedError(MT5IngestionError):
    """Durabilidad no confirmada: nuevas ingestiones bloqueadas (fail-closed)."""


class DurabilityState(str, Enum):
    """Estado de durabilidad confirmada del snapshot activo."""

    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"


class IngestionStatus(str, Enum):
    """Resultado de una operacion de ingestion."""

    APPLIED = "APPLIED"  # se aplicaron deals nuevos y se avanzo el cursor
    NO_NEW_DEALS = "NO_NEW_DEALS"  # cursor avanzado sin deals nuevos
    DURABILITY_UNCONFIRMED = "DURABILITY_UNCONFIRMED"


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """Resultado explicito de aplicar una consulta exitosa."""

    status: IngestionStatus
    new_deals: int
    total_deals: int
    excluded_before_coverage: int
    ingestion: IngestionState
    persistence: PersistenceResult
    durability: DurabilityState

    @property
    def advanced(self) -> bool:
        return self.ingestion.has_successful_scan


def _norm_utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ScanRejectedError(f"{field} debe ser datetime con zona horaria (UTC)")
    return value.astimezone(_UTC)


class MT5AccountIngestor:
    """Aplica consultas de historial de forma idempotente y transaccional.

    La operacion reconstruye un candidato completo y lo publica con UNA sola
    llamada al store; el estado activo del componente pasa a ser el candidato
    solo si la persistencia tiene exito. La durabilidad del snapshot debe
    CONFIRMARSE explicitamente (`confirm_durability`) antes de aceptar nuevas
    ingestiones; una instancia nueva (p. ej. tras un reinicio) empieza sin
    durabilidad confirmada.
    """

    def __init__(
        self, store: MT5AccountingStore, ledger: MT5AccountLedger
    ) -> None:
        if not isinstance(store, MT5AccountingStore):
            raise MT5IngestionError("store debe ser MT5AccountingStore")
        if not isinstance(ledger, MT5AccountLedger):
            raise MT5IngestionError("ledger debe ser MT5AccountLedger")
        self._store = store
        self._ledger = ledger
        # Fail-closed: la durabilidad NO se asume; debe confirmarse con una
        # escritura durable real antes de admitir ingestiones.
        self._durability = DurabilityState.UNCONFIRMED

    @classmethod
    def from_store(cls, store: MT5AccountingStore) -> "MT5AccountIngestor":
        """Reanuda desde el snapshot persistido (p. ej. tras un reinicio).

        Carga y valida el snapshot; la durabilidad queda SIN confirmar hasta que
        el llamante invoque `confirm_durability()`.
        """
        if not isinstance(store, MT5AccountingStore):
            raise MT5IngestionError("store debe ser MT5AccountingStore")
        return cls(store, store.load())

    # --------------------------------------------------------------- estado

    @property
    def store(self) -> MT5AccountingStore:
        return self._store

    @property
    def ledger(self) -> MT5AccountLedger:
        return self._ledger

    @property
    def durability(self) -> DurabilityState:
        return self._durability

    @property
    def baseline(self) -> MT5AccountBaseline:
        return self._ledger.baseline

    @property
    def epoch(self) -> AccountingEpoch:
        return self._ledger.epoch

    # -------------------------------------------------------- durabilidad

    def confirm_durability(self) -> PersistenceResult:
        """Reestablece y CONFIRMA la durabilidad del snapshot activo.

        Re-persiste el ledger activo (`overwrite=True`) y solo libera el bloqueo
        si el resultado es `DURABLE`. Un `REPLACED_DURABILITY_UNCONFIRMED` mantiene
        el bloqueo (no se trata como plenamente durable).
        """
        result = self._store.save(self._ledger, overwrite=True)
        self._durability = (
            DurabilityState.CONFIRMED
            if result is PersistenceResult.DURABLE
            else DurabilityState.UNCONFIRMED
        )
        return result

    # ----------------------------------------------------------- ingestion

    def apply_scan(
        self, result: DealHistoryResult, *, account_fingerprint: str
    ) -> IngestionOutcome:
        """Aplica UNA consulta EXITOSA y actualiza el cursor conjuntamente.

        La identidad de cuenta verificada (`account_fingerprint`) es OBLIGATORIA
        y debe coincidir con la baseline; los deals historicos NO traen huella y
        NO se infiere su pertenencia.

        Fail-closed: valida y reconstruye el candidato ANTES de publicar; ante
        cualquier fallo el snapshot previo y el ledger activo quedan intactos y
        el cursor NO avanza.
        """
        if self._durability is not DurabilityState.CONFIRMED:
            raise IngestionBlockedError(
                "durabilidad no confirmada: reestablezca la durabilidad antes de "
                "aplicar nuevas ingestiones (fail-closed)"
            )
        fingerprint = self._require_identity(account_fingerprint)
        frm, to, status, new_deals = self._validate_result(result, fingerprint)

        previous = self._ledger.ingestion
        if previous.has_successful_scan and to < previous.last_successful_scan_to:
            raise ScanRejectedError(
                "el intervalo de la consulta retrocede el cursor de ingestion "
                f"({to.isoformat()} < {previous.last_successful_scan_to.isoformat()})"
            )

        merged, new_count, excluded = self._merge(new_deals)
        ingestion = IngestionState(frm, to, status)
        candidate = self._reconstruct(merged, ingestion)

        persistence = self._store.save(candidate, overwrite=True)
        # El estado activo corresponde al resultado REAL de la persistencia.
        self._ledger = candidate
        if persistence is PersistenceResult.DURABLE:
            self._durability = DurabilityState.CONFIRMED
            outcome_status = (
                IngestionStatus.APPLIED
                if new_count > 0
                else IngestionStatus.NO_NEW_DEALS
            )
        else:
            self._durability = DurabilityState.UNCONFIRMED
            outcome_status = IngestionStatus.DURABILITY_UNCONFIRMED
        return IngestionOutcome(
            status=outcome_status,
            new_deals=new_count,
            total_deals=len(merged),
            excluded_before_coverage=excluded,
            ingestion=ingestion,
            persistence=persistence,
            durability=self._durability,
        )

    # ---------------------------------------------------------- validacion

    def _require_identity(self, account_fingerprint: object) -> str:
        if isinstance(account_fingerprint, bool) or not isinstance(
            account_fingerprint, str
        ):
            raise AccountIdentityError("account_fingerprint debe ser str")
        if not account_fingerprint.strip():
            raise AccountIdentityError("account_fingerprint vacio")
        if account_fingerprint != self._ledger.account_fingerprint:
            raise AccountIdentityError(
                "la identidad de cuenta verificada no coincide con la baseline"
            )
        return account_fingerprint

    def _validate_result(
        self, result: DealHistoryResult, fingerprint: str
    ) -> tuple[datetime, datetime, IngestionScanStatus, list[MT5Deal]]:
        if not isinstance(result, DealHistoryResult):
            raise ScanRejectedError("resultado no es DealHistoryResult")
        status = result.status
        if status not in _INGESTION_STATUS:
            # INCOMPLETE/desconocido/None => NUNCA avanza el cursor.
            raise ScanRejectedError(
                f"consulta no exitosa ({status!r}): no se aplica ni avanza el cursor"
            )
        frm = _norm_utc(result.date_from, "date_from")
        to = _norm_utc(result.date_to, "date_to")
        if frm > to:
            raise ScanRejectedError("date_from no puede ser posterior a date_to")

        if not isinstance(result.deals, (list, tuple)):
            raise ScanRejectedError("los deals del resultado no son una coleccion")
        raw_deals = tuple(result.deals)
        if status == HISTORY_COMPLETE and not raw_deals:
            raise ScanRejectedError("HISTORY_COMPLETE exige una consulta con deals")
        if status == HISTORY_EMPTY_CONFIRMED and raw_deals:
            raise ScanRejectedError(
                "HISTORY_EMPTY_CONFIRMED exige una consulta sin deals"
            )

        converted: list[MT5Deal] = []
        for hist in raw_deals:
            deal = self._to_mt5_deal(hist, fingerprint)
            instant = deal.time.astimezone(_UTC)
            if instant < frm or instant > to:
                raise ScanRejectedError(
                    "deal fuera del intervalo solicitado (resultado incoherente)"
                )
            converted.append(deal)
        return frm, to, _INGESTION_STATUS[status], converted

    def _to_mt5_deal(self, hist: object, fingerprint: str) -> MT5Deal:
        if not isinstance(hist, HistoricalDeal):
            raise ScanRejectedError("deal historico no es HistoricalDeal")
        if (
            not isinstance(hist.time, datetime)
            or hist.time.tzinfo is None
            or hist.time.utcoffset() is None
        ):
            raise ScanRejectedError("deal historico sin `time` con zona horaria")
        if isinstance(hist.time_msc, bool) or not isinstance(hist.time_msc, int):
            raise ScanRejectedError("deal historico con `time_msc` no entero")
        return MT5Deal(
            account_fingerprint=fingerprint,
            ticket=hist.ticket,
            order=hist.order,
            type=hist.type,
            entry=hist.entry,
            position_id=hist.position_id,
            symbol=hist.symbol,
            volume=hist.volume,
            price=hist.price,
            profit=hist.profit,
            commission=hist.commission,
            swap=hist.swap,
            fee=hist.fee,
            time=hist.time,
            time_msc=hist.time_msc,
        )

    # -------------------------------------------------------------- merge

    def _merge(
        self, new_deals: list[MT5Deal]
    ) -> tuple[dict[int, MT5Deal], int, int]:
        """Une deals existentes y nuevos, deduplicando por ticket.

        Identico => idempotente (se omite). Contradictorio => se bloquea.
        Los eventos anteriores a `baseline.coverage_from` se EXCLUYEN; los que
        caen en una zona temporal ambigua respecto al corte se rechazan.
        """
        merged: dict[int, MT5Deal] = {
            deal.ticket: deal for deal, _kind in self._ledger.processed_deals_in_order()
        }
        new_count = 0
        excluded = 0
        for deal in new_deals:
            relation = self._classify_vs_coverage(deal)
            if relation == _BEFORE_COVERAGE:
                excluded += 1
                continue
            if relation == _AMBIGUOUS_COVERAGE:
                raise ScanRejectedError(
                    "relacion temporal ambigua con el corte de la baseline: no se "
                    "puede decidir de que lado cae el deal (fail-closed)"
                )
            existing = merged.get(deal.ticket)
            if existing is None:
                merged[deal.ticket] = deal
                new_count += 1
            elif existing != deal:
                raise DealIdentityCollisionError(
                    "ticket ya procesado con contenido distinto"
                )
            # identico => idempotente: no se duplica ni se re-cuenta.
        return merged, new_count, excluded

    def _classify_vs_coverage(self, deal: MT5Deal) -> str:
        """Decide si un deal es ANTERIOR, POSTERIOR/incluido o AMBIGUO respecto a
        `baseline.coverage_from`, con la resolucion temporal disponible."""
        coverage = self._ledger.baseline.coverage_from.astimezone(_UTC)
        instant = deal.time.astimezone(_UTC)
        cov_sec = coverage.replace(microsecond=0)
        deal_sec = instant.replace(microsecond=0)
        if deal_sec < cov_sec:
            return _BEFORE_COVERAGE
        if deal_sec > cov_sec:
            return _WITHIN_COVERAGE
        # Mismo segundo entero que el corte: decidir con milisegundos.
        if coverage.microsecond % 1000 == 0:
            cutoff_ms = int(cov_sec.timestamp()) * 1000 + coverage.microsecond // 1000
            return _WITHIN_COVERAGE if deal.time_msc >= cutoff_ms else _BEFORE_COVERAGE
        # El corte tiene resolucion sub-milisegundo: no se puede decidir.
        return _AMBIGUOUS_COVERAGE

    # -------------------------------------------------------- reconstruccion

    def _reconstruct(
        self, merged: dict[int, MT5Deal], ingestion: IngestionState
    ) -> MT5AccountLedger:
        """Reconstruye un candidato AISLADO aplicando TODOS los deals unicos en
        orden canonico `(time, time_msc, ticket)`.

        Incluye deals tardios dentro de la cobertura (reordenados por timestamp,
        no por ticket). Si la secuencia no es reconstruible con seguridad, el
        modelo falla y el candidato se descarta (el ledger activo no se toca).
        """
        ordered = sorted(
            merged.values(), key=lambda d: (d.time, d.time_msc, d.ticket)
        )
        candidate = MT5AccountLedger(
            self._ledger.baseline, epoch=self._ledger.epoch, ingestion=ingestion
        )
        try:
            for deal in ordered:
                candidate.apply_deal(deal)
        except MT5AccountingError as exc:
            # Reconstruccion no segura (secuencia no reproducible, deal no
            # soportado, etc.): se BLOQUEA; no se publica un lote parcial.
            raise ScanRejectedError(
                f"lote no reconstruible de forma segura: {exc}"
            ) from exc
        return candidate


__all__ = [
    "MT5AccountIngestor",
    "MT5IngestionError",
    "ScanRejectedError",
    "AccountIdentityError",
    "IngestionBlockedError",
    "DurabilityState",
    "IngestionStatus",
    "IngestionOutcome",
]
