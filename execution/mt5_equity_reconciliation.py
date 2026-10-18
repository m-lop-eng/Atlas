"""ATLAS-MT5-ACCOUNTING-06H: contratos y esqueleto del reconciliador de equity MT5.

Funcion PURA y determinista: recibe entradas YA obtenidas (estado de cuenta
observado, integridad del ledger, reconciliacion de posiciones) y produce un
informe. NO llama al broker, NO persiste, NO ejecuta y NO cambia readiness.

IMPORTANTE (06H): el CALCULO ECONOMICO del equity aun NO esta implementado. En esta
fase el motor NO puede emitir `EQUITY_RECONCILED` ni `EQUITY_MISMATCH`; devuelve
`EQUITY_UNVERIFIED` (politica de valoracion pendiente) o `BLOCKED` (precondicion
estructural no satisfecha). `expected_equity`/`difference` permanecen `None`
mientras no exista una evaluacion valida.

Dimensiones INDEPENDIENTES (ninguna prueba otra): identidad de cuenta, integridad
del ledger, posiciones reconciliadas, ownership, equity reconciliado y readiness.
La valoracion (bid/ask/last, formula, FX) se define en 06I; frescura/tolerancias en
06J; la integracion en admision/runtime en 06K.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from execution.mt5_position_reconciliation import MT5PositionStatus

_UTC = timezone.utc


class MT5EquityReconciliationError(ValueError):
    """Error de configuracion/contrato del reconciliador de equity (no silenciar)."""


class MT5EquityReconciliationStatus(str, Enum):
    """Estado de la evaluacion de equity.

    * `EQUITY_RECONCILED`: equity esperado calculado y diferencia dentro de la
      tolerancia aprobada. (NO emitible en 06H.)
    * `EQUITY_MISMATCH`: equity esperado calculado, diferencia material. (NO
      emitible en 06H.)
    * `EQUITY_UNVERIFIED`: no puede calcularse/compararse el equity (valoracion,
      marks, FX o semantica no disponibles). NO es un exito.
    * `BALANCE_MISMATCH`: discrepancia contable de BALANCE (nivel cuenta),
      conservando la distincion frente al equity.
    * `BLOCKED`: agregado no admisible por una PRECONDICION ESTRUCTURAL no
      satisfecha (identidad, ledger no confiable, posiciones no reconciliadas,
      datos invalidos). Distinto de `EQUITY_UNVERIFIED` (que es falta de
      valoracion economica, no de estructura).
    """

    EQUITY_RECONCILED = "EQUITY_RECONCILED"
    EQUITY_MISMATCH = "EQUITY_MISMATCH"
    EQUITY_UNVERIFIED = "EQUITY_UNVERIFIED"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class MT5EquityReconciliationRequest:
    """Entradas YA obtenidas para la evaluacion de equity (sin acceder al broker).

    `clock` es explicito y opcional: si se aporta, `as_of` se deriva de el
    (normalizado a UTC); un reloj invalido es un error de contrato tipado.
    """

    account_identity_verified: bool
    ledger_complete: bool
    ledger_durable: bool
    positions_status: MT5PositionStatus | None
    observed_balance: float | None = None
    observed_credit: float | None = None
    observed_equity: float | None = None
    account_currency: str | None = None
    marks: tuple[Any, ...] = ()
    fx_rates: tuple[Any, ...] = ()
    clock: Callable[[], datetime] | None = None


@dataclass(frozen=True, slots=True)
class MT5EquityReconciliationReport:
    """Informe inmutable de la evaluacion de equity.

    Reglas de datos: `expected_equity=None` => no hay equity esperado verificable;
    `difference=None` => no existe comparacion valida; NUNCA se usa cero como
    sustituto de ausencia. Sin secretos.
    """

    status: MT5EquityReconciliationStatus
    observed_equity: float | None
    expected_equity: float | None
    difference: float | None
    tolerance: float | None
    account_currency: str | None
    positions_status: MT5PositionStatus | None
    ledger_trusted: bool
    marks_used: tuple[Any, ...] = ()
    fx_used: tuple[Any, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    reason_code: str = ""
    reason: str = ""
    as_of: datetime | None = None

    @property
    def reconciled(self) -> bool:
        return self.status is MT5EquityReconciliationStatus.EQUITY_RECONCILED


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MT5EquityReconciliationError(f"{name} debe ser bool")
    return value


def _coerce_finite(value: object) -> float | None:
    """Float finito, o `None` (rechaza bool, no numericos, NaN, +/-Inf)."""
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


def _clock_utc(clock: Callable[[], datetime] | None) -> datetime | None:
    if clock is None:
        return None
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MT5EquityReconciliationError(
            "clock debe devolver datetime con zona horaria"
        )
    return value.astimezone(_UTC)


def _report(
    status: MT5EquityReconciliationStatus,
    *,
    request: MT5EquityReconciliationRequest,
    observed_equity: float | None,
    ledger_trusted: bool,
    missing_inputs: tuple[str, ...],
    reason_code: str,
    reason: str,
    as_of: datetime | None,
) -> MT5EquityReconciliationReport:
    return MT5EquityReconciliationReport(
        status=status,
        observed_equity=observed_equity,
        expected_equity=None,
        difference=None,
        tolerance=None,
        account_currency=request.account_currency,
        positions_status=request.positions_status,
        ledger_trusted=ledger_trusted,
        marks_used=(),
        fx_used=(),
        missing_inputs=missing_inputs,
        reason_code=reason_code,
        reason=reason,
        as_of=as_of,
    )


def reconcile_mt5_equity(
    request: MT5EquityReconciliationRequest,
) -> MT5EquityReconciliationReport:
    """Evaluacion FAIL-CLOSED de equity (esqueleto 06H; sin calculo economico).

    - Valida precondiciones estructurales y datos numericos observados.
    - Nunca emite `EQUITY_RECONCILED`/`EQUITY_MISMATCH` (valoracion pendiente).
    - Con precondiciones OK y sin valoracion implementada => `EQUITY_UNVERIFIED`.
    """
    if not isinstance(request, MT5EquityReconciliationRequest):
        raise MT5EquityReconciliationError(
            "reconcile_mt5_equity exige un MT5EquityReconciliationRequest"
        )
    _require_bool(request.account_identity_verified, "account_identity_verified")
    _require_bool(request.ledger_complete, "ledger_complete")
    _require_bool(request.ledger_durable, "ledger_durable")

    as_of = _clock_utc(request.clock)
    ledger_trusted = request.ledger_complete and request.ledger_durable

    problems: list[str] = []
    if not request.account_identity_verified:
        problems.append("account_identity_not_verified")
    if not request.ledger_complete:
        problems.append("ledger_incomplete")
    if not request.ledger_durable:
        problems.append("ledger_not_durable")
    if request.positions_status is not MT5PositionStatus.RECONCILED:
        problems.append("positions_not_reconciled")

    observed_equity = _coerce_finite(request.observed_equity)
    if request.observed_equity is not None and observed_equity is None:
        problems.append("observed_equity_invalid")
    if request.observed_balance is not None and _coerce_finite(
        request.observed_balance
    ) is None:
        problems.append("observed_balance_invalid")
    if request.observed_credit is not None and _coerce_finite(
        request.observed_credit
    ) is None:
        problems.append("observed_credit_invalid")

    if problems:
        return _report(
            MT5EquityReconciliationStatus.BLOCKED,
            request=request,
            observed_equity=observed_equity,
            ledger_trusted=ledger_trusted,
            missing_inputs=tuple(problems),
            reason_code="BLOCKED_PRECONDITION",
            reason="precondiciones estructurales no satisfechas: "
            + ", ".join(problems),
            as_of=as_of,
        )

    # 06H: precondiciones OK, pero la valoracion economica NO esta implementada.
    return _report(
        MT5EquityReconciliationStatus.EQUITY_UNVERIFIED,
        request=request,
        observed_equity=observed_equity,
        ledger_trusted=ledger_trusted,
        missing_inputs=("equity_valuation_policy_not_implemented",),
        reason_code="VALUATION_NOT_IMPLEMENTED",
        reason=(
            "politica de valoracion de equity no implementada (06I): no se emite "
            "EQUITY_RECONCILED/MISMATCH ni se estima un equity parcial"
        ),
        as_of=as_of,
    )


__all__ = [
    "MT5EquityReconciliationError",
    "MT5EquityReconciliationStatus",
    "MT5EquityReconciliationRequest",
    "MT5EquityReconciliationReport",
    "reconcile_mt5_equity",
]
