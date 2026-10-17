"""ATLAS-MT5-ACCOUNTING-06C: motor OFFLINE de reconciliacion de posiciones MT5.

Funcion PURA y determinista: recibe datos YA obtenidos (posiciones observadas del
broker y posiciones del ledger contable MT5) y produce un informe. NO llama al
broker, NO persiste, NO ejecuta y NO cambia readiness. NO sustituye al motor de
reconciliacion SPOT (`reconciliation.engine`).

Correspondencia por IDENTIDAD ESTABLE `POSITION_IDENTIFIER` (nunca por ticket).
La identidad encontrada NO implica ownership de Atlas: la pertenencia es una
garantia SEPARADA (journal/ownership), fuera de este motor.

No declara reconciliacion de produccion; solo evalua el contrato de datos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from brokers.mt5 import (
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
)

_NETTING = ACCOUNT_MARGIN_MODE_RETAIL_NETTING  # 0
_HEDGING = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING  # 2


class MT5PositionReconciliationError(ValueError):
    """Argumento de configuracion invalido para el motor de reconciliacion."""


class MT5PositionStatus(str, Enum):
    """Estado de una posicion o del resultado agregado.

    Por posicion: RECONCILED | ATTRIBUTE_MISMATCH | MISSING_BROKER |
    MISSING_INTERNAL | UNKNOWN | UNVERIFIED.
    Agregado: RECONCILED (todo conforme) | BLOCKED (no reconciliable).
    """

    RECONCILED = "RECONCILED"
    ATTRIBUTE_MISMATCH = "ATTRIBUTE_MISMATCH"
    MISSING_BROKER = "MISSING_BROKER"
    MISSING_INTERNAL = "MISSING_INTERNAL"
    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class MT5PositionSource:
    """Fuente de posiciones ya obtenida (sin acceder al broker aqui).

    * `available=False` => la consulta fallo o no hay respuesta (None/excepcion);
      el detalle libre (sin secretos) describe el motivo.
    * `available=True` con `positions=()` => lista VACIA valida (no un fallo).
    * `available=True` con `positions=None` se trata como no disponible.
    """

    positions: tuple[Any, ...] | None
    available: bool
    detail: str = ""

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(self.positions or ())


@dataclass(frozen=True, slots=True)
class MT5PositionDiff:
    """Diferencia estructurada de UNA posicion (sin secretos)."""

    classification: MT5PositionStatus
    position_id: str | None
    instrument: str | None
    detail: str
    internal_quantity: float | None = None
    broker_quantity: float | None = None


@dataclass(frozen=True, slots=True)
class MT5PositionReconciliationReport:
    """Informe agregado de reconciliacion de posiciones MT5."""

    status: MT5PositionStatus
    diffs: tuple[MT5PositionDiff, ...]
    blocked_reason: str | None
    account_mode: int
    broker_available: bool
    internal_available: bool
    ledger_trusted: bool

    @property
    def reconciled(self) -> bool:
        return self.status is MT5PositionStatus.RECONCILED


def _require_tolerance(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MT5PositionReconciliationError(f"{name} debe ser numerico")
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise MT5PositionReconciliationError(f"{name} debe ser finito y >= 0")
    return f


def _coerce_finite(value: object) -> float | None:
    """Convierte un campo numerico de posicion a float FINITO, o None.

    Fail-closed: rechaza `bool`, `None`, cadenas (NO se aceptan cadenas numericas
    como valores de posicion), NaN e +/-Inf, y cualquier conversion que lance
    `TypeError`/`ValueError`/`OverflowError`. NUNCA sustituye por cero ni
    descarta el dato; el llamante decide el estado (p. ej. `UNVERIFIED`).
    Centraliza la validacion para todas las comparaciones del motor.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _normalize_identifier(value: object) -> str | None:
    """Identidad estable canonica (POSITION_IDENTIFIER) como str, o None.

    Acepta `int` positivo o `str` de digitos (se recorta). Rechaza bool, None,
    vacio, no-digitos, cero/negativo. NO sustituye por ticket/simbolo/magic.
    NO convierte silenciosamente valores dudosos en identidad valida.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isdigit():
            return None
        number = int(text)
        return str(number) if number > 0 else None
    return None


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _index(
    positions: Sequence[Any], *, source: str
) -> tuple[dict[str, Any], list[MT5PositionDiff]]:
    mapping: dict[str, Any] = {}
    problems: list[MT5PositionDiff] = []
    for pos in positions:
        raw = getattr(pos, "position_id", None)
        pid = _normalize_identifier(raw)
        instrument = getattr(pos, "instrument", None) or getattr(pos, "symbol", None)
        if pid is None:
            problems.append(
                MT5PositionDiff(
                    classification=MT5PositionStatus.UNKNOWN,
                    position_id=None,
                    instrument=instrument,
                    detail=f"identidad POSITION_IDENTIFIER ausente/invalida ({source})",
                )
            )
            continue
        if pid in mapping:
            problems.append(
                MT5PositionDiff(
                    classification=MT5PositionStatus.UNKNOWN,
                    position_id=pid,
                    instrument=instrument,
                    detail=f"POSITION_IDENTIFIER duplicado ({source})",
                )
            )
            continue
        mapping[pid] = pos
    return mapping, problems


def _compare(
    pid: str,
    broker_pos: Any,
    internal_pos: Any,
    *,
    quantity_tolerance: float,
    price_tolerance: float,
) -> MT5PositionDiff:
    instrument = getattr(broker_pos, "instrument", None)
    symbol = getattr(internal_pos, "symbol", None)
    bq = _coerce_finite(getattr(broker_pos, "quantity", None))
    iq = _coerce_finite(getattr(internal_pos, "quantity", None))
    bp = _coerce_finite(getattr(broker_pos, "avg_entry_price", None))
    ip = _coerce_finite(getattr(internal_pos, "avg_price", None))

    # Validacion fail-closed ANTES de calcular diferencias: un dato no numerico o
    # no finito NUNCA puede convertirse en emparejamiento (no se usa NaN en `>`).
    invalid = [
        name
        for name, value in (
            ("broker.quantity", bq),
            ("internal.quantity", iq),
            ("broker.avg_entry_price", bp),
            ("internal.avg_price", ip),
        )
        if value is None
    ]
    if invalid:
        return MT5PositionDiff(
            MT5PositionStatus.UNVERIFIED,
            position_id=pid,
            instrument=instrument,
            detail="campo numerico invalido/no finito: " + ", ".join(invalid),
        )
    # Cantidad cero: NO es una posicion abierta valida ni prueba de coincidencia.
    if bq == 0.0 or iq == 0.0:
        return MT5PositionDiff(
            MT5PositionStatus.UNVERIFIED,
            position_id=pid,
            instrument=instrument,
            detail="cantidad cero: no es una posicion abierta valida",
            internal_quantity=iq,
            broker_quantity=bq,
        )

    base = dict(position_id=pid, instrument=instrument, internal_quantity=iq, broker_quantity=bq)

    if instrument != symbol:
        return MT5PositionDiff(
            MT5PositionStatus.ATTRIBUTE_MISMATCH,
            detail=f"simbolo/instrumento divergente ({symbol!r} != {instrument!r})",
            **base,
        )
    if _sign(bq) != _sign(iq):
        return MT5PositionDiff(
            MT5PositionStatus.ATTRIBUTE_MISMATCH,
            detail="direccion divergente (long/short)",
            **base,
        )
    if abs(abs(bq) - abs(iq)) > quantity_tolerance:
        return MT5PositionDiff(
            MT5PositionStatus.ATTRIBUTE_MISMATCH,
            detail="volumen divergente",
            **base,
        )
    if abs(bp - ip) > price_tolerance:
        return MT5PositionDiff(
            MT5PositionStatus.ATTRIBUTE_MISMATCH,
            detail="precio medio de apertura divergente",
            **base,
        )
    return MT5PositionDiff(
        MT5PositionStatus.RECONCILED,
        detail="posicion alineada",
        **base,
    )


def _report(
    status: MT5PositionStatus,
    diffs: list[MT5PositionDiff],
    blocked_reason: str | None,
    *,
    account_mode: int,
    broker_available: bool,
    internal_available: bool,
    ledger_trusted: bool,
) -> MT5PositionReconciliationReport:
    ordered = tuple(
        sorted(
            diffs,
            key=lambda d: (
                d.position_id is None,
                d.position_id or "",
                d.classification.value,
                d.detail,
            ),
        )
    )
    return MT5PositionReconciliationReport(
        status=status,
        diffs=ordered,
        blocked_reason=blocked_reason,
        account_mode=account_mode,
        broker_available=broker_available,
        internal_available=internal_available,
        ledger_trusted=ledger_trusted,
    )


def reconcile_mt5_positions(
    broker: MT5PositionSource,
    internal: MT5PositionSource,
    *,
    account_mode: int,
    ledger_complete: bool,
    ledger_durable: bool,
    quantity_tolerance: float = 0.0,
    price_tolerance: float = 0.0,
) -> MT5PositionReconciliationReport:
    """Reconcilia posiciones observadas (broker) vs ledger contable MT5.

    `quantity_tolerance`/`price_tolerance` son ABSOLUTAS en unidades del
    instrumento; por defecto 0.0 = igualdad EXACTA (sin tolerancia oculta). El
    llamante debe aportar una tolerancia justificada si necesita comparar con
    ruido; no se consultan metadatos de mercado en esta fase.
    """
    qt = _require_tolerance(quantity_tolerance, "quantity_tolerance")
    pt = _require_tolerance(price_tolerance, "price_tolerance")
    try:
        mode = int(account_mode)
    except (TypeError, ValueError):
        mode = -1
    ledger_trusted = bool(ledger_complete) and bool(ledger_durable)

    def _blocked(reason: str, diffs: list[MT5PositionDiff] | None = None):
        return _report(
            MT5PositionStatus.BLOCKED,
            diffs or [],
            reason,
            account_mode=mode,
            broker_available=broker.available,
            internal_available=internal.available,
            ledger_trusted=ledger_trusted,
        )

    if not broker.available or broker.positions is None:
        return _blocked(f"posiciones observadas no disponibles: {broker.detail or 'sin respuesta'}")
    if not internal.available or internal.positions is None:
        return _blocked(f"posiciones del ledger no disponibles: {internal.detail or 'sin respuesta'}")
    if not ledger_trusted:
        return _blocked("ledger MT5 incompleto o durabilidad no confirmada")

    broker_map, broker_problems = _index(broker.as_tuple(), source="broker")
    internal_map, internal_problems = _index(internal.as_tuple(), source="ledger")
    problems = broker_problems + internal_problems

    if mode not in (_NETTING, _HEDGING):
        return _blocked(f"account_mode no soportado ({account_mode!r})", problems)

    # NETTING: politica de atribucion pendiente; solo cuenta PLANA es admisible.
    if mode == _NETTING:
        if broker_map or internal_map or problems:
            return _blocked(
                "cuenta NETTING con posiciones: politica de atribucion/conciliacion "
                "neta pendiente (no se atribuye por simbolo/comentario/magic)",
                problems,
            )
        return _report(
            MT5PositionStatus.RECONCILED,
            [],
            None,
            account_mode=mode,
            broker_available=True,
            internal_available=True,
            ledger_trusted=True,
        )

    # HEDGING: correspondencia por POSITION_IDENTIFIER.
    diffs: list[MT5PositionDiff] = list(problems)
    for pid in sorted(set(broker_map) | set(internal_map)):
        b = broker_map.get(pid)
        i = internal_map.get(pid)
        if b is not None and i is None:
            diffs.append(
                MT5PositionDiff(
                    MT5PositionStatus.MISSING_INTERNAL,
                    position_id=pid,
                    instrument=getattr(b, "instrument", None),
                    detail="posicion observada sin correspondencia en el ledger MT5",
                    broker_quantity=_coerce_finite(getattr(b, "quantity", None)),
                )
            )
        elif b is None and i is not None:
            diffs.append(
                MT5PositionDiff(
                    MT5PositionStatus.MISSING_BROKER,
                    position_id=pid,
                    instrument=getattr(i, "symbol", None),
                    detail="posicion del ledger ausente en la observacion del broker",
                    internal_quantity=_coerce_finite(getattr(i, "quantity", None)),
                )
            )
        else:
            diffs.append(
                _compare(pid, b, i, quantity_tolerance=qt, price_tolerance=pt)
            )

    if any(d.classification is not MT5PositionStatus.RECONCILED for d in diffs):
        return _blocked("se detectaron discrepancias de posiciones", diffs)
    return _report(
        MT5PositionStatus.RECONCILED,
        diffs,
        None,
        account_mode=mode,
        broker_available=True,
        internal_available=True,
        ledger_trusted=True,
    )


__all__ = [
    "MT5PositionReconciliationError",
    "MT5PositionStatus",
    "MT5PositionSource",
    "MT5PositionDiff",
    "MT5PositionReconciliationReport",
    "reconcile_mt5_positions",
]
