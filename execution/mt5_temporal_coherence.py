"""ATLAS-MT5-ACCOUNTING-06J: evaluador temporal puro (frescura y coherencia).

Modulo PURO y determinista. No importa `MetaTrader5` ni `brokers.mt5`, no
consulta el reloj real, no accede a red, variables de entorno ni estado global
mutable. Todos los instantes, umbrales y evidencias se reciben como datos
explicitos.

Semantica del offset: `Delta = C_tick - C_local`. Un timestamp de tick se
convierte a la linea temporal local como `tau_local = tau - Delta_hat`.

El evaluador puede validar estructura, vigencia y coherencia de una evidencia
de offset suministrada, pero NO autentica su procedencia. Atlas no dispone hoy
de un proveedor real de offset ni de captura de flags. Este modulo solo evalua
la evidencia que recibe: no integra la politica en el adaptador, el motor de
PnL, la reconciliacion de equity ni el runtime.

No modifica `MT5PricePnlResult.timestamp_verified`.

`MT5TemporalMarkInput.required_sides=()` significa que el llamador NO solicita
comprobar la frescura individual de bid/ask: `side_statuses` queda vacio y un
resultado global `TEMPORALLY_VERIFIED` no certifica la frescura de esos lados.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "MT5_TEMPORAL_REASON_ORDER",
    "MT5_TEMPORAL_REASON_MESSAGES",
    "ClockOffsetEstimate",
    "MT5TemporalPolicy",
    "MT5TemporalScope",
    "MT5TemporalMarkInput",
    "MT5TemporalInput",
    "MT5TemporalDimensionStatus",
    "MT5TemporalOverallStatus",
    "MT5MarkTemporalResult",
    "MT5MarkSetTemporalResult",
    "MT5SnapshotTemporalResult",
    "MT5AccountMarkTemporalResult",
    "MT5ClockOffsetValidation",
    "MT5TemporalCoherenceResult",
    "MT5TemporalCoherenceError",
    "evaluate_mt5_temporal_coherence",
]

_UTC = timezone.utc


# Precedencia canonica y determinista de motivos (06J design revision).
MT5_TEMPORAL_REASON_ORDER: tuple[str, ...] = (
    "TEMPORAL_CONFIG_MISSING",
    "TEMPORAL_CONFIG_INVALID",
    "VALUATION_AS_OF_MISSING",
    "VALUATION_AS_OF_INVALID",
    "CLOCK_OFFSET_MISSING",
    "CLOCK_OFFSET_INVALID",
    "CLOCK_DOMAIN_MISMATCH",
    "CLOCK_OFFSET_STALE",
    "CLOCK_OFFSET_MEASURED_AFTER_VALUATION",
    "CLOCK_OFFSET_UNCERTAINTY_EXCEEDED",
    "CLOCK_SKEW_UNVERIFIED",
    "TICK_TIME_MISSING",
    "TICK_TIME_INVALID",
    "TICK_TIME_FUTURE",
    "TICK_TOO_OLD",
    "MARK_AGE_UNVERIFIED",
    "RECEIVED_AT_MISSING",
    "RECEIVED_AT_INVALID",
    "RECEIVE_AGE_INVALID",
    "RECEIVE_AGE_EXCEEDED",
    "RECEIVE_DELAY_UNVERIFIED",
    "RECEIVE_DELAY_EXCEEDED",
    "MARK_FLAGS_MISSING",
    "MARK_SIDE_UNVERIFIED",
    "MARK_SET_EMPTY",
    "MARK_SET_INVALID",
    "MARK_SKEW_UNVERIFIED",
    "MARK_SKEW_EXCEEDED",
    "SNAPSHOT_TIME_MISSING",
    "SNAPSHOT_TIME_INVALID",
    "SNAPSHOT_TIME_FUTURE",
    "SNAPSHOT_AGE_EXCEEDED",
    "SNAPSHOT_SKEW_UNVERIFIED",
    "SNAPSHOT_SKEW_EXCEEDED",
    "READ_SPAN_MISSING",
    "READ_CLOCK_NON_MONOTONIC",
    "READ_SPAN_EXCEEDED",
    "SNAPSHOT_READ_SPAN_MISSING",
    "SNAPSHOT_READ_SPAN_EXCEEDED",
    "TEMPORAL_COHERENCE_UNVERIFIED",
)

MT5_TEMPORAL_REASON_MESSAGES: Mapping[str, str] = MappingProxyType({
    "TEMPORAL_CONFIG_MISSING": "falta un umbral obligatorio para el ambito",
    "TEMPORAL_CONFIG_INVALID": "umbral temporal invalido (tipo/valor)",
    "VALUATION_AS_OF_MISSING": "valuation_as_of ausente",
    "VALUATION_AS_OF_INVALID": "valuation_as_of invalido (tipo/naive/rango)",
    "CLOCK_OFFSET_MISSING": "no se aporto evidencia de offset",
    "CLOCK_OFFSET_INVALID": "evidencia de offset invalida",
    "CLOCK_DOMAIN_MISMATCH": "dominio temporal incompatible",
    "CLOCK_OFFSET_STALE": "offset fuera de vigencia o tick corregido fuera de ella",
    "CLOCK_OFFSET_MEASURED_AFTER_VALUATION": "offset medido despues de la valoracion",
    "CLOCK_OFFSET_UNCERTAINTY_EXCEEDED": "incertidumbre del offset excesiva",
    "CLOCK_SKEW_UNVERIFIED": "desfase tick/local no verificable",
    "TICK_TIME_MISSING": "tick_time ausente",
    "TICK_TIME_INVALID": "tick_time invalido",
    "TICK_TIME_FUTURE": "tick_time en el futuro",
    "TICK_TOO_OLD": "edad de mercado por encima del umbral",
    "MARK_AGE_UNVERIFIED": "edad de mercado no certificable",
    "RECEIVED_AT_MISSING": "received_at ausente",
    "RECEIVED_AT_INVALID": "received_at invalido",
    "RECEIVE_AGE_INVALID": "received_at posterior a valuation_as_of",
    "RECEIVE_AGE_EXCEEDED": "edad de recepcion local excesiva",
    "RECEIVE_DELAY_UNVERIFIED": "retraso tick->recepcion no certificable",
    "RECEIVE_DELAY_EXCEEDED": "retraso tick->recepcion excesivo",
    "MARK_FLAGS_MISSING": "flags de tick o mascaras de lado ausentes/invalidas",
    "MARK_SIDE_UNVERIFIED": "frescura del lado requerido no verificable",
    "MARK_SET_EMPTY": "no hay marcas que evaluar",
    "MARK_SET_INVALID": "conjunto de marcas invalido o contradictorio",
    "MARK_SKEW_UNVERIFIED": "dispersion entre marcas no verificable",
    "MARK_SKEW_EXCEEDED": "dispersion entre marcas excesiva",
    "SNAPSHOT_TIME_MISSING": "snapshot_observed_at ausente",
    "SNAPSHOT_TIME_INVALID": "snapshot_observed_at invalido",
    "SNAPSHOT_TIME_FUTURE": "snapshot_observed_at en el futuro",
    "SNAPSHOT_AGE_EXCEEDED": "edad del snapshot excesiva",
    "SNAPSHOT_SKEW_UNVERIFIED": "coherencia snapshot/marcas no verificable",
    "SNAPSHOT_SKEW_EXCEEDED": "coherencia snapshot/marcas excedida",
    "READ_SPAN_MISSING": "limites monotonicos de lectura ausentes/invalidos",
    "READ_CLOCK_NON_MONOTONIC": "reloj monotono retrocedido",
    "READ_SPAN_EXCEEDED": "ventana de lectura excesiva",
    "SNAPSHOT_READ_SPAN_MISSING": "limites monotonicos de snapshot ausentes/invalidos",
    "SNAPSHOT_READ_SPAN_EXCEEDED": "ventana de lectura del snapshot excesiva",
    "TEMPORAL_COHERENCE_UNVERIFIED": "coherencia temporal no verificable",
})


class MT5TemporalCoherenceError(ValueError):
    """Violacion del contrato de invocacion del evaluador."""


class MT5TemporalScope(str, Enum):
    """Ambito explicito de evaluacion temporal."""

    MARK_ONLY = "MARK_ONLY"
    MARK_SET = "MARK_SET"
    ACCOUNT_AND_MARKS = "ACCOUNT_AND_MARKS"


class MT5TemporalDimensionStatus(str, Enum):
    """Estado de una dimension temporal."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    EXCEEDED = "EXCEEDED"
    INVALID = "INVALID"
    MISSING = "MISSING"
    NOT_REQUIRED = "NOT_REQUIRED"


class MT5TemporalOverallStatus(str, Enum):
    """Estado agregado del evaluador temporal."""

    TEMPORALLY_VERIFIED = "TEMPORALLY_VERIFIED"
    TEMPORALLY_UNVERIFIED = "TEMPORALLY_UNVERIFIED"
    TEMPORAL_CHECK_BLOCKED = "TEMPORAL_CHECK_BLOCKED"


@dataclass(frozen=True, slots=True)
class ClockOffsetEstimate:
    """Evidencia externa de desfase entre el dominio de ticks y el local.

    `offset_seconds` es `Delta = C_tick - C_local` (con signo).
    `uncertainty_seconds` es una cota de `|Delta - Delta_hat|`.
    """

    offset_seconds: float
    uncertainty_seconds: float
    measured_at: datetime
    valid_from: datetime
    valid_to: datetime
    clock_domain_id: str
    local_clock_domain_id: str
    source_id: str
    provenance: str
    evidence_ref: str
    tick_time_domain_confirmed: bool


@dataclass(frozen=True, slots=True)
class MT5TemporalPolicy:
    """Umbrales explicitos. `None` solo si el ambito no requiere el parametro."""

    max_mark_age_s: float | None
    max_mark_skew_s: float | None
    max_snapshot_skew_s: float | None
    max_snapshot_age_s: float | None
    max_clock_offset_uncertainty_s: float | None
    max_read_span_s: float | None
    max_receive_delay_s: float | None
    max_receive_age_s: float | None


@dataclass(frozen=True, slots=True)
class MT5TemporalMarkInput:
    """Datos temporales explicitos de UNA marca.

    Los campos opcionales representan informacion que Atlas aun no captura.
    `None` NUNCA se interpreta como cero, fecha actual ni evidencia afirmativa.
    `bid_flag`/`ask_flag` son valores ya resueltos desde las constantes publicas
    del modulo MT5 por el llamador; este modulo no importa ni hardcodea flags.

    `required_sides=()` significa que el llamador NO solicita comprobar la
    frescura individual de bid/ask. En ese caso `side_statuses` queda vacio y un
    resultado global `TEMPORALLY_VERIFIED` NO certifica la frescura de esos
    lados; solo cubre las dimensiones temporales y las flags explicitamente
    requeridas.
    """

    position_id: Any = None
    atlas_instrument: str | None = None
    mt5_symbol: str | None = None
    tick_time: datetime | None = None
    received_at: datetime | None = None
    clock_domain_id: str | None = None
    tick_time_domain_confirmed: bool = False
    tick_flags: int | None = None
    bid_flag: int | None = None
    ask_flag: int | None = None
    flag_semantics_confirmed: bool = False
    required_sides: tuple[str, ...] = ()
    read_started_monotonic: float | None = None
    read_finished_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class MT5TemporalInput:
    """Entrada completa del evaluador temporal."""

    scope: MT5TemporalScope
    valuation_as_of: datetime | None
    local_clock_domain_id: str | None
    clock_offset_estimate: ClockOffsetEstimate | None
    marks: tuple[MT5TemporalMarkInput, ...]
    snapshot_observed_at: datetime | None = None
    snapshot_read_started_monotonic: float | None = None
    snapshot_read_finished_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class MT5MarkTemporalResult:
    """Resultado temporal de una marca."""

    position_id: Any
    atlas_instrument: str | None
    mt5_symbol: str | None
    tick_time: datetime | None
    corrected_tick_time: datetime | None
    age_estimate_s: float | None
    age_interval_s: tuple[float, float] | None
    received_at: datetime | None
    receive_delay_estimate_s: float | None
    receive_delay_interval_s: tuple[float, float] | None
    receive_age_s: float | None
    required_sides: tuple[str, ...]
    side_statuses: tuple[tuple[str, MT5TemporalDimensionStatus], ...]
    read_span_s: float | None
    status: MT5TemporalDimensionStatus
    reason_code: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MT5MarkSetTemporalResult:
    """Resultado de coherencia del conjunto de marcas."""

    status: MT5TemporalDimensionStatus
    clock_domain_id: str | None
    spread_s: float | None
    marks: tuple[MT5MarkTemporalResult, ...]
    reason_code: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MT5SnapshotTemporalResult:
    """Resultado temporal del snapshot de cuenta."""

    status: MT5TemporalDimensionStatus
    observed_at: datetime | None
    age_s: float | None
    read_span_s: float | None
    reason_code: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MT5AccountMarkTemporalResult:
    """Resultado de coherencia entre snapshot de cuenta y marcas."""

    status: MT5TemporalDimensionStatus
    max_abs_skew_s: float | None
    reason_code: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MT5ClockOffsetValidation:
    """Resultado de validacion de la evidencia de offset."""

    status: MT5TemporalDimensionStatus
    estimate: ClockOffsetEstimate | None
    reason_code: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MT5TemporalCoherenceResult:
    """Resultado compuesto del evaluador temporal."""

    overall: MT5TemporalOverallStatus
    scope: MT5TemporalScope
    config_status: MT5TemporalDimensionStatus
    valuation_as_of: datetime | None
    clock_domain_id: str | None
    offset: MT5ClockOffsetValidation
    markset: MT5MarkSetTemporalResult
    snapshot: MT5SnapshotTemporalResult
    account_mark_coherence: MT5AccountMarkTemporalResult
    reason_code: str | None
    reason_codes: tuple[str, ...]


_REASON_STATUS: Mapping[str, MT5TemporalDimensionStatus] = MappingProxyType({
    "TEMPORAL_CONFIG_MISSING": MT5TemporalDimensionStatus.MISSING,
    "TEMPORAL_CONFIG_INVALID": MT5TemporalDimensionStatus.INVALID,
    "VALUATION_AS_OF_MISSING": MT5TemporalDimensionStatus.MISSING,
    "VALUATION_AS_OF_INVALID": MT5TemporalDimensionStatus.INVALID,
    "CLOCK_OFFSET_MISSING": MT5TemporalDimensionStatus.MISSING,
    "CLOCK_OFFSET_INVALID": MT5TemporalDimensionStatus.INVALID,
    "CLOCK_DOMAIN_MISMATCH": MT5TemporalDimensionStatus.INVALID,
    "CLOCK_OFFSET_STALE": MT5TemporalDimensionStatus.UNVERIFIED,
    "CLOCK_OFFSET_MEASURED_AFTER_VALUATION": MT5TemporalDimensionStatus.UNVERIFIED,
    "CLOCK_OFFSET_UNCERTAINTY_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "CLOCK_SKEW_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "TICK_TIME_MISSING": MT5TemporalDimensionStatus.MISSING,
    "TICK_TIME_INVALID": MT5TemporalDimensionStatus.INVALID,
    "TICK_TIME_FUTURE": MT5TemporalDimensionStatus.INVALID,
    "TICK_TOO_OLD": MT5TemporalDimensionStatus.EXCEEDED,
    "MARK_AGE_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "RECEIVED_AT_MISSING": MT5TemporalDimensionStatus.MISSING,
    "RECEIVED_AT_INVALID": MT5TemporalDimensionStatus.INVALID,
    "RECEIVE_AGE_INVALID": MT5TemporalDimensionStatus.INVALID,
    "RECEIVE_AGE_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "RECEIVE_DELAY_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "RECEIVE_DELAY_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "MARK_FLAGS_MISSING": MT5TemporalDimensionStatus.MISSING,
    "MARK_SIDE_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "MARK_SET_EMPTY": MT5TemporalDimensionStatus.MISSING,
    "MARK_SET_INVALID": MT5TemporalDimensionStatus.INVALID,
    "MARK_SKEW_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "MARK_SKEW_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "SNAPSHOT_TIME_MISSING": MT5TemporalDimensionStatus.MISSING,
    "SNAPSHOT_TIME_INVALID": MT5TemporalDimensionStatus.INVALID,
    "SNAPSHOT_TIME_FUTURE": MT5TemporalDimensionStatus.INVALID,
    "SNAPSHOT_AGE_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "SNAPSHOT_SKEW_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
    "SNAPSHOT_SKEW_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "READ_SPAN_MISSING": MT5TemporalDimensionStatus.MISSING,
    "READ_CLOCK_NON_MONOTONIC": MT5TemporalDimensionStatus.INVALID,
    "READ_SPAN_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "SNAPSHOT_READ_SPAN_MISSING": MT5TemporalDimensionStatus.MISSING,
    "SNAPSHOT_READ_SPAN_EXCEEDED": MT5TemporalDimensionStatus.EXCEEDED,
    "TEMPORAL_COHERENCE_UNVERIFIED": MT5TemporalDimensionStatus.UNVERIFIED,
})

_STATUS_SEVERITY: Mapping[MT5TemporalDimensionStatus, int] = MappingProxyType({
    MT5TemporalDimensionStatus.VERIFIED: 0,
    MT5TemporalDimensionStatus.UNVERIFIED: 1,
    MT5TemporalDimensionStatus.MISSING: 2,
    MT5TemporalDimensionStatus.EXCEEDED: 3,
    MT5TemporalDimensionStatus.INVALID: 4,
    MT5TemporalDimensionStatus.NOT_REQUIRED: -1,
})

_ALL_POLICY_FIELDS: tuple[str, ...] = (
    "max_mark_age_s",
    "max_mark_skew_s",
    "max_snapshot_skew_s",
    "max_snapshot_age_s",
    "max_clock_offset_uncertainty_s",
    "max_read_span_s",
    "max_receive_delay_s",
    "max_receive_age_s",
)

_MARK_ONLY_FIELDS: tuple[str, ...] = (
    "max_mark_age_s",
    "max_clock_offset_uncertainty_s",
    "max_read_span_s",
    "max_receive_delay_s",
    "max_receive_age_s",
)

_MARK_SET_FIELDS: tuple[str, ...] = _MARK_ONLY_FIELDS + ("max_mark_skew_s",)

_ACCOUNT_FIELDS: tuple[str, ...] = _MARK_SET_FIELDS + (
    "max_snapshot_skew_s",
    "max_snapshot_age_s",
)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _finite_number(value: Any) -> float | None:
    """Float finito, o `None`. Rechaza `bool` y tipos no numericos."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _finite_nonnegative(value: Any) -> float | None:
    result = _finite_number(value)
    if result is None or result < 0.0:
        return None
    return result


def _normalize_utc(value: Any) -> tuple[datetime | None, bool]:
    """Normaliza un timestamp tz-aware a UTC. Devuelve `(valor, valido)`."""
    if not isinstance(value, datetime):
        return None, False
    if value.tzinfo is None or value.utcoffset() is None:
        return None, False
    try:
        return value.astimezone(_UTC), True
    except (OverflowError, OSError, ValueError):
        return None, False


def _order_reasons(reasons: Any) -> tuple[str, ...]:
    index = {code: pos for pos, code in enumerate(MT5_TEMPORAL_REASON_ORDER)}
    return tuple(
        sorted(
            set(reasons),
            key=lambda code: (index.get(code, len(index)), code),
        )
    )


def _status_from_reasons(reasons: Any) -> MT5TemporalDimensionStatus:
    if not reasons:
        return MT5TemporalDimensionStatus.VERIFIED
    status = MT5TemporalDimensionStatus.VERIFIED
    for code in reasons:
        candidate = _REASON_STATUS.get(
            code, MT5TemporalDimensionStatus.UNVERIFIED
        )
        if _STATUS_SEVERITY[candidate] > _STATUS_SEVERITY[status]:
            status = candidate
    return status


def _reason_code(reason_codes: tuple[str, ...]) -> str | None:
    return reason_codes[0] if reason_codes else None


def _required_policy_fields(scope: MT5TemporalScope) -> tuple[str, ...]:
    if scope is MT5TemporalScope.MARK_ONLY:
        return _MARK_ONLY_FIELDS
    if scope is MT5TemporalScope.MARK_SET:
        return _MARK_SET_FIELDS
    return _ACCOUNT_FIELDS


def _validate_policy(
    policy: MT5TemporalPolicy, scope: MT5TemporalScope
) -> tuple[MT5TemporalDimensionStatus, set[str]]:
    required = set(_required_policy_fields(scope))
    missing = False
    invalid = False
    for name in _ALL_POLICY_FIELDS:
        value = getattr(policy, name)
        if value is None:
            if name in required:
                missing = True
            continue
        if _finite_nonnegative(value) is None:
            invalid = True

    reasons: set[str] = set()
    if missing:
        reasons.add("TEMPORAL_CONFIG_MISSING")
    if invalid:
        reasons.add("TEMPORAL_CONFIG_INVALID")

    if missing:
        return MT5TemporalDimensionStatus.MISSING, reasons
    if invalid:
        return MT5TemporalDimensionStatus.INVALID, reasons
    return MT5TemporalDimensionStatus.VERIFIED, reasons


def _validate_offset_estimate(
    estimate: ClockOffsetEstimate,
) -> ClockOffsetEstimate | None:
    offset = _finite_number(estimate.offset_seconds)
    uncertainty = _finite_nonnegative(estimate.uncertainty_seconds)
    if offset is None or uncertainty is None:
        return None
    try:
        timedelta(seconds=offset)
        timedelta(seconds=uncertainty)
    except (OverflowError, ValueError):
        return None

    measured_at, measured_ok = _normalize_utc(estimate.measured_at)
    valid_from, from_ok = _normalize_utc(estimate.valid_from)
    valid_to, to_ok = _normalize_utc(estimate.valid_to)
    if not (measured_ok and from_ok and to_ok):
        return None
    assert measured_at is not None
    assert valid_from is not None
    assert valid_to is not None
    if valid_from >= valid_to:
        return None
    if not (valid_from <= measured_at <= valid_to):
        return None
    if not isinstance(estimate.tick_time_domain_confirmed, bool):
        return None

    for name in (
        "clock_domain_id",
        "local_clock_domain_id",
        "source_id",
        "provenance",
        "evidence_ref",
    ):
        if not _is_nonempty_str(getattr(estimate, name)):
            return None

    return ClockOffsetEstimate(
        offset_seconds=offset,
        uncertainty_seconds=uncertainty,
        measured_at=measured_at,
        valid_from=valid_from,
        valid_to=valid_to,
        clock_domain_id=estimate.clock_domain_id.strip(),
        local_clock_domain_id=estimate.local_clock_domain_id.strip(),
        source_id=estimate.source_id.strip(),
        provenance=estimate.provenance.strip(),
        evidence_ref=estimate.evidence_ref.strip(),
        tick_time_domain_confirmed=estimate.tick_time_domain_confirmed,
    )


def _validate_offset(
    data: MT5TemporalInput,
    policy: MT5TemporalPolicy,
    valuation_as_of: datetime,
    local_domain_ok: bool,
) -> MT5ClockOffsetValidation:
    estimate = data.clock_offset_estimate
    if estimate is None:
        reasons = {"CLOCK_OFFSET_MISSING"}
        codes = _order_reasons(reasons)
        return MT5ClockOffsetValidation(
            status=MT5TemporalDimensionStatus.MISSING,
            estimate=None,
            reason_code=_reason_code(codes),
            reason_codes=codes,
        )

    normalized = _validate_offset_estimate(estimate)
    reasons: set[str] = set()
    if normalized is None:
        reasons.add("CLOCK_OFFSET_INVALID")
        codes = _order_reasons(reasons)
        return MT5ClockOffsetValidation(
            status=MT5TemporalDimensionStatus.INVALID,
            estimate=estimate,
            reason_code=_reason_code(codes),
            reason_codes=codes,
        )

    if not local_domain_ok:
        reasons.add("CLOCK_SKEW_UNVERIFIED")
    elif normalized.local_clock_domain_id != data.local_clock_domain_id:
        reasons.add("CLOCK_DOMAIN_MISMATCH")

    if normalized.tick_time_domain_confirmed is not True:
        reasons.add("CLOCK_SKEW_UNVERIFIED")

    for mark in data.marks:
        if (
            not _is_nonempty_str(mark.clock_domain_id)
            or mark.tick_time_domain_confirmed is not True
        ):
            reasons.add("CLOCK_SKEW_UNVERIFIED")
        elif mark.clock_domain_id.strip() != normalized.clock_domain_id:
            reasons.add("CLOCK_DOMAIN_MISMATCH")

    if normalized.measured_at > valuation_as_of:
        reasons.add("CLOCK_OFFSET_MEASURED_AFTER_VALUATION")

    if not (normalized.valid_from <= valuation_as_of <= normalized.valid_to):
        reasons.add("CLOCK_OFFSET_STALE")

    max_uncertainty = policy.max_clock_offset_uncertainty_s
    assert max_uncertainty is not None
    if normalized.uncertainty_seconds > max_uncertainty:
        reasons.add("CLOCK_OFFSET_UNCERTAINTY_EXCEEDED")

    for mark in data.marks:
        tick_time, tick_ok = _normalize_utc(mark.tick_time)
        if not tick_ok:
            continue
        assert tick_time is not None
        try:
            corrected = tick_time - timedelta(
                seconds=normalized.offset_seconds
            )
        except (OverflowError, ValueError):
            reasons.add("CLOCK_OFFSET_INVALID")
            continue
        if not (normalized.valid_from <= corrected <= normalized.valid_to):
            reasons.add("CLOCK_OFFSET_STALE")

    status = _status_from_reasons(reasons)
    codes = _order_reasons(reasons)
    return MT5ClockOffsetValidation(
        status=status,
        estimate=normalized,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )


def _evaluate_read_span(
    started: Any,
    finished: Any,
    max_span: float,
    missing_code: str,
    exceeded_code: str,
) -> tuple[float | None, set[str]]:
    start = _finite_number(started)
    end = _finite_number(finished)
    if start is None or end is None:
        return None, {missing_code}
    span = end - start
    if not math.isfinite(span):
        return None, {missing_code}
    if span < 0.0:
        return span, {"READ_CLOCK_NON_MONOTONIC"}
    if span > max_span:
        return span, {exceeded_code}
    return span, set()


def _normalize_sides(required_sides: Any) -> tuple[tuple[str, ...], bool]:
    """Normaliza lados requeridos; `()` no exige frescura individual bid/ask."""
    if required_sides is None:
        return (), True
    if not isinstance(required_sides, (tuple, list)):
        return (), False
    normalized: list[str] = []
    for side in required_sides:
        if not isinstance(side, str):
            return (), False
        upper = side.strip().upper()
        if upper not in ("BID", "ASK"):
            return (), False
        if upper not in normalized:
            normalized.append(upper)
    return tuple(normalized), True


def _evaluate_mark(
    mark: MT5TemporalMarkInput,
    policy: MT5TemporalPolicy,
    valuation_as_of: datetime,
    local_domain_ok: bool,
    offset: MT5ClockOffsetValidation,
) -> MT5MarkTemporalResult:
    reasons: set[str] = set()
    age_reasons: set[str] = set()
    offset_valid = offset.status is MT5TemporalDimensionStatus.VERIFIED

    tick_time, tick_ok = _normalize_utc(mark.tick_time)
    if mark.tick_time is None:
        reasons.add("TICK_TIME_MISSING")
        age_reasons.add("TICK_TIME_MISSING")
    elif not tick_ok:
        reasons.add("TICK_TIME_INVALID")
        age_reasons.add("TICK_TIME_INVALID")

    received_at, received_ok = _normalize_utc(mark.received_at)
    if mark.received_at is None:
        reasons.add("RECEIVED_AT_MISSING")
    elif not received_ok:
        reasons.add("RECEIVED_AT_INVALID")

    if not local_domain_ok:
        reasons.add("CLOCK_SKEW_UNVERIFIED")

    age_estimate: float | None = None
    age_interval: tuple[float, float] | None = None
    corrected_tick_time: datetime | None = None
    if tick_time is not None:
        if not offset_valid:
            age_reasons.add("CLOCK_SKEW_UNVERIFIED")
        else:
            assert offset.estimate is not None
            delta = offset.estimate.offset_seconds
            uncertainty = offset.estimate.uncertainty_seconds
            try:
                corrected_tick_time = tick_time - timedelta(seconds=delta)
            except (OverflowError, ValueError):
                corrected_tick_time = None
                age_reasons.add("MARK_AGE_UNVERIFIED")
            if corrected_tick_time is not None:
                estimate = (
                    (valuation_as_of - tick_time).total_seconds() + delta
                )
                lower = estimate - uncertainty
                upper = estimate + uncertainty
                if not (math.isfinite(estimate) and math.isfinite(lower)
                        and math.isfinite(upper)):
                    age_reasons.add("MARK_AGE_UNVERIFIED")
                else:
                    age_estimate = estimate
                    age_interval = (lower, upper)
                    max_age = policy.max_mark_age_s
                    assert max_age is not None
                    if upper < 0.0:
                        age_reasons.add("TICK_TIME_FUTURE")
                    elif lower < 0.0:
                        age_reasons.add("MARK_AGE_UNVERIFIED")
                    elif lower > max_age:
                        age_reasons.add("TICK_TOO_OLD")
                    elif upper > max_age:
                        age_reasons.add("MARK_AGE_UNVERIFIED")
    reasons |= age_reasons

    receive_age: float | None = None
    if received_at is not None and local_domain_ok:
        receive_age = (valuation_as_of - received_at).total_seconds()
        if receive_age < 0.0:
            reasons.add("RECEIVE_AGE_INVALID")
        else:
            max_receive_age = policy.max_receive_age_s
            assert max_receive_age is not None
            if receive_age > max_receive_age:
                reasons.add("RECEIVE_AGE_EXCEEDED")

    delay_estimate: float | None = None
    delay_interval: tuple[float, float] | None = None
    if tick_time is not None and received_at is not None:
        if not offset_valid:
            reasons.add("RECEIVE_DELAY_UNVERIFIED")
        else:
            assert offset.estimate is not None
            delta = offset.estimate.offset_seconds
            uncertainty = offset.estimate.uncertainty_seconds
            estimate = (
                (received_at - tick_time).total_seconds() + delta
            )
            lower = estimate - uncertainty
            upper = estimate + uncertainty
            if not (math.isfinite(estimate) and math.isfinite(lower)
                    and math.isfinite(upper)):
                reasons.add("RECEIVE_DELAY_UNVERIFIED")
            else:
                delay_estimate = estimate
                delay_interval = (lower, upper)
                max_delay = policy.max_receive_delay_s
                assert max_delay is not None
                if upper < 0.0:
                    reasons.add("RECEIVE_DELAY_UNVERIFIED")
                elif lower < 0.0:
                    reasons.add("RECEIVE_DELAY_UNVERIFIED")
                elif lower > max_delay:
                    reasons.add("RECEIVE_DELAY_EXCEEDED")
                elif upper > max_delay:
                    reasons.add("RECEIVE_DELAY_UNVERIFIED")

    read_span, span_reasons = _evaluate_read_span(
        mark.read_started_monotonic,
        mark.read_finished_monotonic,
        policy.max_read_span_s or 0.0,
        "READ_SPAN_MISSING",
        "READ_SPAN_EXCEEDED",
    )
    reasons |= span_reasons

    sides, sides_ok = _normalize_sides(mark.required_sides)
    if not sides_ok:
        reasons.add("MARK_SIDE_UNVERIFIED")
        sides = ()

    age_status = _status_from_reasons(age_reasons)
    side_statuses: list[tuple[str, MT5TemporalDimensionStatus]] = []
    for side in sides:
        flags_ok = (
            isinstance(mark.tick_flags, int)
            and not isinstance(mark.tick_flags, bool)
        )
        mask = mark.bid_flag if side == "BID" else mark.ask_flag
        mask_ok = isinstance(mask, int) and not isinstance(mask, bool)
        if not (flags_ok and mask_ok):
            reasons.add("MARK_FLAGS_MISSING")
            side_statuses.append(
                (side, MT5TemporalDimensionStatus.MISSING)
            )
            continue
        assert isinstance(mark.tick_flags, int)
        assert isinstance(mask, int)
        if (mark.tick_flags & mask) == 0:
            reasons.add("MARK_SIDE_UNVERIFIED")
            side_statuses.append(
                (side, MT5TemporalDimensionStatus.UNVERIFIED)
            )
        elif mark.flag_semantics_confirmed is not True:
            reasons.add("MARK_SIDE_UNVERIFIED")
            side_statuses.append(
                (side, MT5TemporalDimensionStatus.UNVERIFIED)
            )
        elif age_status is not MT5TemporalDimensionStatus.VERIFIED:
            reasons.add("MARK_SIDE_UNVERIFIED")
            side_statuses.append(
                (side, MT5TemporalDimensionStatus.UNVERIFIED)
            )
        else:
            side_statuses.append(
                (side, MT5TemporalDimensionStatus.VERIFIED)
            )

    status = _status_from_reasons(reasons)
    codes = _order_reasons(reasons)
    return MT5MarkTemporalResult(
        position_id=mark.position_id,
        atlas_instrument=mark.atlas_instrument,
        mt5_symbol=mark.mt5_symbol,
        tick_time=tick_time,
        corrected_tick_time=corrected_tick_time,
        age_estimate_s=age_estimate,
        age_interval_s=age_interval,
        received_at=received_at,
        receive_delay_estimate_s=delay_estimate,
        receive_delay_interval_s=delay_interval,
        receive_age_s=receive_age,
        required_sides=sides,
        side_statuses=tuple(side_statuses),
        read_span_s=read_span,
        status=status,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )


def _evaluate_mark_set(
    data: MT5TemporalInput,
    policy: MT5TemporalPolicy,
    marks: tuple[MT5MarkTemporalResult, ...],
) -> MT5MarkSetTemporalResult:
    scope = data.scope
    if scope is MT5TemporalScope.MARK_ONLY:
        return MT5MarkSetTemporalResult(
            status=MT5TemporalDimensionStatus.NOT_REQUIRED,
            clock_domain_id=None,
            spread_s=None,
            marks=marks,
            reason_code=None,
            reason_codes=(),
        )

    reasons: set[str] = set()
    spread: float | None = None
    common_domain: str | None = None
    if not data.marks:
        reasons.add("MARK_SET_EMPTY")
    else:
        groups: dict[tuple[Any, Any, Any], list[MT5TemporalMarkInput]] = {}
        for mark in data.marks:
            key = (
                mark.atlas_instrument,
                mark.mt5_symbol,
                mark.position_id,
            )
            groups.setdefault(key, []).append(mark)
        for group in groups.values():
            if len(group) <= 1:
                continue
            ticks = {
                _normalize_utc(item.tick_time)[0] for item in group
            }
            domains = {item.clock_domain_id for item in group}
            if len(ticks) > 1 or len(domains) > 1:
                reasons.add("MARK_SET_INVALID")

        valid_ticks: list[datetime] = []
        domains: set[str] = set()
        all_domains_confirmed = True
        for mark in data.marks:
            tick_time, tick_ok = _normalize_utc(mark.tick_time)
            if (
                not tick_ok
                or not _is_nonempty_str(mark.clock_domain_id)
                or mark.tick_time_domain_confirmed is not True
            ):
                all_domains_confirmed = False
            else:
                assert tick_time is not None
                valid_ticks.append(tick_time)
                assert isinstance(mark.clock_domain_id, str)
                domains.add(mark.clock_domain_id.strip())

        if not all_domains_confirmed or len(valid_ticks) != len(data.marks):
            reasons.add("MARK_SKEW_UNVERIFIED")
        elif len(domains) != 1:
            reasons.add("MARK_SKEW_UNVERIFIED")
        else:
            common_domain = next(iter(domains))
            if "MARK_SET_INVALID" not in reasons:
                spread = (
                    max(valid_ticks) - min(valid_ticks)
                ).total_seconds()
                max_skew = policy.max_mark_skew_s
                assert max_skew is not None
                if spread > max_skew:
                    reasons.add("MARK_SKEW_EXCEEDED")

    status = _status_from_reasons(reasons)
    codes = _order_reasons(reasons)
    return MT5MarkSetTemporalResult(
        status=status,
        clock_domain_id=common_domain,
        spread_s=spread,
        marks=marks,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )


def _evaluate_snapshot(
    data: MT5TemporalInput,
    policy: MT5TemporalPolicy,
    valuation_as_of: datetime,
    local_domain_ok: bool,
) -> MT5SnapshotTemporalResult:
    if data.scope is not MT5TemporalScope.ACCOUNT_AND_MARKS:
        return MT5SnapshotTemporalResult(
            status=MT5TemporalDimensionStatus.NOT_REQUIRED,
            observed_at=None,
            age_s=None,
            read_span_s=None,
            reason_code=None,
            reason_codes=(),
        )

    reasons: set[str] = set()
    observed_at, observed_ok = _normalize_utc(data.snapshot_observed_at)
    age: float | None = None
    if data.snapshot_observed_at is None:
        reasons.add("SNAPSHOT_TIME_MISSING")
    elif not observed_ok:
        reasons.add("SNAPSHOT_TIME_INVALID")
    elif not local_domain_ok:
        reasons.add("CLOCK_SKEW_UNVERIFIED")
    else:
        assert observed_at is not None
        age = (valuation_as_of - observed_at).total_seconds()
        if age < 0.0:
            reasons.add("SNAPSHOT_TIME_FUTURE")
        else:
            max_age = policy.max_snapshot_age_s
            assert max_age is not None
            if age > max_age:
                reasons.add("SNAPSHOT_AGE_EXCEEDED")

    read_span, span_reasons = _evaluate_read_span(
        data.snapshot_read_started_monotonic,
        data.snapshot_read_finished_monotonic,
        policy.max_read_span_s or 0.0,
        "SNAPSHOT_READ_SPAN_MISSING",
        "SNAPSHOT_READ_SPAN_EXCEEDED",
    )
    reasons |= span_reasons

    status = _status_from_reasons(reasons)
    codes = _order_reasons(reasons)
    return MT5SnapshotTemporalResult(
        status=status,
        observed_at=observed_at,
        age_s=age,
        read_span_s=read_span,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )


def _evaluate_account_mark_coherence(
    data: MT5TemporalInput,
    policy: MT5TemporalPolicy,
    valuation_as_of: datetime,
    local_domain_ok: bool,
    offset: MT5ClockOffsetValidation,
) -> MT5AccountMarkTemporalResult:
    if data.scope is not MT5TemporalScope.ACCOUNT_AND_MARKS:
        return MT5AccountMarkTemporalResult(
            status=MT5TemporalDimensionStatus.NOT_REQUIRED,
            max_abs_skew_s=None,
            reason_code=None,
            reason_codes=(),
        )

    reasons: set[str] = set()
    max_abs_skew: float | None = None
    observed_at, observed_ok = _normalize_utc(data.snapshot_observed_at)
    if data.snapshot_observed_at is None:
        reasons.add("SNAPSHOT_TIME_MISSING")
    elif not observed_ok:
        reasons.add("SNAPSHOT_TIME_INVALID")
    if not data.marks:
        reasons.add("MARK_SET_EMPTY")
    if not local_domain_ok:
        reasons.add("CLOCK_SKEW_UNVERIFIED")
    if offset.status is not MT5TemporalDimensionStatus.VERIFIED:
        reasons.add("CLOCK_SKEW_UNVERIFIED")

    can_compute = (
        observed_at is not None
        and bool(data.marks)
        and local_domain_ok
        and offset.status is MT5TemporalDimensionStatus.VERIFIED
    )
    if can_compute:
        assert offset.estimate is not None
        assert observed_at is not None
        delta = offset.estimate.offset_seconds
        uncertainty = offset.estimate.uncertainty_seconds
        max_skew = policy.max_snapshot_skew_s
        assert max_skew is not None
        all_computed = True
        for mark in data.marks:
            tick_time, tick_ok = _normalize_utc(mark.tick_time)
            if not tick_ok:
                all_computed = False
                reasons.add("SNAPSHOT_SKEW_UNVERIFIED")
                continue
            assert tick_time is not None
            estimate = (
                (observed_at - tick_time).total_seconds() + delta
            )
            lower = estimate - uncertainty
            upper = estimate + uncertainty
            if not (math.isfinite(estimate) and math.isfinite(lower)
                    and math.isfinite(upper)):
                all_computed = False
                reasons.add("SNAPSHOT_SKEW_UNVERIFIED")
                continue
            max_abs_skew = max(
                abs(estimate),
                max_abs_skew if max_abs_skew is not None else 0.0,
            )
            if upper < -max_skew or lower > max_skew:
                reasons.add("SNAPSHOT_SKEW_EXCEEDED")
            elif lower < -max_skew or upper > max_skew:
                reasons.add("SNAPSHOT_SKEW_UNVERIFIED")
        if not all_computed:
            max_abs_skew = None
    else:
        if observed_at is None or not data.marks:
            max_abs_skew = None

    status = _status_from_reasons(reasons)
    codes = _order_reasons(reasons)
    return MT5AccountMarkTemporalResult(
        status=status,
        max_abs_skew_s=max_abs_skew,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )


def _blocked_result(
    data: MT5TemporalInput,
    reasons: set[str],
    config_status: MT5TemporalDimensionStatus,
) -> MT5TemporalCoherenceResult:
    codes = _order_reasons(reasons)
    reason_code = _reason_code(codes)
    mark_results = tuple(
        MT5MarkTemporalResult(
            position_id=mark.position_id,
            atlas_instrument=mark.atlas_instrument,
            mt5_symbol=mark.mt5_symbol,
            tick_time=None,
            corrected_tick_time=None,
            age_estimate_s=None,
            age_interval_s=None,
            received_at=None,
            receive_delay_estimate_s=None,
            receive_delay_interval_s=None,
            receive_age_s=None,
            required_sides=(),
            side_statuses=(),
            read_span_s=None,
            status=MT5TemporalDimensionStatus.UNVERIFIED,
            reason_code=reason_code,
            reason_codes=codes,
        )
        for mark in data.marks
    )
    markset_status = (
        MT5TemporalDimensionStatus.NOT_REQUIRED
        if data.scope is MT5TemporalScope.MARK_ONLY
        else MT5TemporalDimensionStatus.UNVERIFIED
    )
    snapshot_status = (
        MT5TemporalDimensionStatus.NOT_REQUIRED
        if data.scope is not MT5TemporalScope.ACCOUNT_AND_MARKS
        else MT5TemporalDimensionStatus.UNVERIFIED
    )
    return MT5TemporalCoherenceResult(
        overall=MT5TemporalOverallStatus.TEMPORAL_CHECK_BLOCKED,
        scope=data.scope,
        config_status=config_status,
        valuation_as_of=None,
        clock_domain_id=None,
        offset=MT5ClockOffsetValidation(
            status=MT5TemporalDimensionStatus.UNVERIFIED,
            estimate=data.clock_offset_estimate,
            reason_code=reason_code,
            reason_codes=codes,
        ),
        markset=MT5MarkSetTemporalResult(
            status=markset_status,
            clock_domain_id=None,
            spread_s=None,
            marks=mark_results,
            reason_code=reason_code if markset_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else None,
            reason_codes=codes if markset_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else (),
        ),
        snapshot=MT5SnapshotTemporalResult(
            status=snapshot_status,
            observed_at=None,
            age_s=None,
            read_span_s=None,
            reason_code=reason_code if snapshot_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else None,
            reason_codes=codes if snapshot_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else (),
        ),
        account_mark_coherence=MT5AccountMarkTemporalResult(
            status=snapshot_status,
            max_abs_skew_s=None,
            reason_code=reason_code if snapshot_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else None,
            reason_codes=codes if snapshot_status is not MT5TemporalDimensionStatus.NOT_REQUIRED else (),
        ),
        reason_code=reason_code,
        reason_codes=codes,
    )


def evaluate_mt5_temporal_coherence(
    data: MT5TemporalInput,
    policy: MT5TemporalPolicy,
) -> MT5TemporalCoherenceResult:
    """Evalua la coherencia temporal de marcas y snapshot (puro).

    Lanza `MT5TemporalCoherenceError` solo si `data` no es `MT5TemporalInput`,
    `policy` no es `MT5TemporalPolicy` o el `scope` no es un enum valido.
    """
    if not isinstance(data, MT5TemporalInput):
        raise MT5TemporalCoherenceError(
            "evaluate_mt5_temporal_coherence exige un MT5TemporalInput"
        )
    if not isinstance(policy, MT5TemporalPolicy):
        raise MT5TemporalCoherenceError(
            "evaluate_mt5_temporal_coherence exige un MT5TemporalPolicy"
        )
    if not isinstance(data.scope, MT5TemporalScope):
        raise MT5TemporalCoherenceError("scope temporal invalido")

    config_status, config_reasons = _validate_policy(policy, data.scope)
    if config_status is not MT5TemporalDimensionStatus.VERIFIED:
        return _blocked_result(data, config_reasons, config_status)

    valuation_as_of, valuation_ok = _normalize_utc(data.valuation_as_of)
    if data.valuation_as_of is None:
        return _blocked_result(
            data,
            {"VALUATION_AS_OF_MISSING"},
            config_status,
        )
    if not valuation_ok:
        return _blocked_result(
            data,
            {"VALUATION_AS_OF_INVALID"},
            config_status,
        )
    assert valuation_as_of is not None

    local_domain_ok = _is_nonempty_str(data.local_clock_domain_id)
    offset = _validate_offset(
        data, policy, valuation_as_of, local_domain_ok
    )
    mark_results = tuple(
        _evaluate_mark(
            mark,
            policy,
            valuation_as_of,
            local_domain_ok,
            offset,
        )
        for mark in data.marks
    )
    markset = _evaluate_mark_set(data, policy, mark_results)
    snapshot = _evaluate_snapshot(
        data, policy, valuation_as_of, local_domain_ok
    )
    account_mark = _evaluate_account_mark_coherence(
        data, policy, valuation_as_of, local_domain_ok, offset
    )

    global_reasons: set[str] = set()
    global_reasons |= set(offset.reason_codes)
    for mark_result in mark_results:
        global_reasons |= set(mark_result.reason_codes)
    global_reasons |= set(markset.reason_codes)
    global_reasons |= set(snapshot.reason_codes)
    global_reasons |= set(account_mark.reason_codes)
    if not data.marks:
        global_reasons.add("MARK_SET_EMPTY")

    required_ok = bool(data.marks)
    required_ok = required_ok and (
        offset.status is MT5TemporalDimensionStatus.VERIFIED
    )
    required_ok = required_ok and all(
        result.status is MT5TemporalDimensionStatus.VERIFIED
        for result in mark_results
    )
    if data.scope in (
        MT5TemporalScope.MARK_SET,
        MT5TemporalScope.ACCOUNT_AND_MARKS,
    ):
        required_ok = required_ok and (
            markset.status is MT5TemporalDimensionStatus.VERIFIED
        )
    if data.scope is MT5TemporalScope.ACCOUNT_AND_MARKS:
        required_ok = required_ok and (
            snapshot.status is MT5TemporalDimensionStatus.VERIFIED
        )
        required_ok = required_ok and (
            account_mark.status is MT5TemporalDimensionStatus.VERIFIED
        )

    if required_ok and not global_reasons:
        overall = MT5TemporalOverallStatus.TEMPORALLY_VERIFIED
    else:
        overall = MT5TemporalOverallStatus.TEMPORALLY_UNVERIFIED
        if not global_reasons:
            global_reasons.add("TEMPORAL_COHERENCE_UNVERIFIED")

    clock_domain_id = markset.clock_domain_id
    if clock_domain_id is None:
        domains = {
            mark.clock_domain_id.strip()
            for mark in data.marks
            if _is_nonempty_str(mark.clock_domain_id)
        }
        if len(domains) == 1:
            clock_domain_id = next(iter(domains))
    if clock_domain_id is None and offset.estimate is not None:
        if offset.status is MT5TemporalDimensionStatus.VERIFIED:
            clock_domain_id = offset.estimate.clock_domain_id

    codes = _order_reasons(global_reasons)
    return MT5TemporalCoherenceResult(
        overall=overall,
        scope=data.scope,
        config_status=config_status,
        valuation_as_of=valuation_as_of,
        clock_domain_id=clock_domain_id,
        offset=offset,
        markset=markset,
        snapshot=snapshot,
        account_mark_coherence=account_mark,
        reason_code=_reason_code(codes),
        reason_codes=codes,
    )
