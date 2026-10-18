"""ATLAS-MT5-ACCOUNTING-06I-C2: motor OFFLINE de PnL por movimiento de precio.

Funcion PURA y determinista que estima SOLO el componente de PnL por movimiento
de precio de UNA posicion MT5, en la divisa de beneficio del instrumento
(`currency_profit`). NO llama al terminal, NO importa `MetaTrader5`, NO persiste,
NO ejecuta y NO usa estado global. NO calcula el PnL total, NO convierte divisas,
NO suma swaps/comisiones y NO reconcilia equity ni cambia readiness.

Alcance (06I-C2): solo modos LINEALES confirmados por MetaQuotes
(`Profit = (Close - Open) * ContractSize * Lots`). Quedan FUERA (sin soporte)
futuros, bonos, opciones, colateral, modos desconocidos y `None`.

El resultado es una ESTIMACION de identificacion/auditoria, NUNCA PnL realizado,
valor de cuenta ni equity reconciliado.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "SUPPORTED_LINEAR_PRICE_MODES",
    "MT5_PRICE_PNL_REASON_ORDER",
    "MT5PricePnlInput",
    "MT5PricePnlStatus",
    "MT5PricePnlMethod",
    "MT5PricePnlResult",
    "MT5PriceValuationError",
    "estimate_mt5_price_pnl",
]


# 06I-C2: whitelist economica EXACTA (coincidencia literal por nombre simbolico).
# NO es una tabla de enteros; NO se normaliza ni se infiere por similitud.
SUPPORTED_LINEAR_PRICE_MODES = frozenset(
    {
        "SYMBOL_CALC_MODE_FOREX",
        "SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE",
        "SYMBOL_CALC_MODE_CFD",
        "SYMBOL_CALC_MODE_CFDINDEX",
        "SYMBOL_CALC_MODE_CFDLEVERAGE",
        "SYMBOL_CALC_MODE_EXCH_STOCKS",
        "SYMBOL_CALC_MODE_EXCH_STOCKS_MOEX",
    }
)


# Precedencia DETERMINISTA de motivos de no verificacion (06I-C2 design audit).
MT5_PRICE_PNL_REASON_ORDER: tuple[str, ...] = (
    "INSTRUMENT_MISSING",
    "SYMBOL_MISSING",
    "INSTRUMENT_MISMATCH",
    "MODE_UNKNOWN",
    "MODE_NOT_WHITELISTED",
    "CURRENCY_PROFIT_MISSING",
    "QUANTITY_INVALID",
    "QUANTITY_ZERO",
    "AVG_ENTRY_PRICE_INVALID",
    "CONTRACT_SIZE_INVALID",
    "VALUATION_PRICE_MISSING",
    "PNL_NOT_FINITE",
)

_REASON_MESSAGES: dict[str, str] = {
    "INSTRUMENT_MISSING": "instrumento Atlas ausente o invalido",
    "SYMBOL_MISSING": "simbolo MT5 (posicion o marca) ausente o vacio",
    "INSTRUMENT_MISMATCH": "instrumento/simbolo de la marca no corresponde",
    "MODE_UNKNOWN": "nombre de modo de calculo ausente o invalido",
    "MODE_NOT_WHITELISTED": "modo de calculo fuera de la whitelist lineal",
    "CURRENCY_PROFIT_MISSING": "divisa de beneficio (currency_profit) invalida",
    "QUANTITY_INVALID": "quantity invalida (no numerica/finita)",
    "QUANTITY_ZERO": "quantity cero: no se valoriza",
    "AVG_ENTRY_PRICE_INVALID": "precio de apertura invalido",
    "CONTRACT_SIZE_INVALID": "tamano de contrato invalido (<=0 o no finito)",
    "VALUATION_PRICE_MISSING": "falta el lado de mercado requerido (bid/ask)",
    "PNL_NOT_FINITE": "resultado aritmetico no finito (overflow)",
}


class MT5PriceValuationError(ValueError):
    """Violacion del CONTRATO DE INVOCACION (no de datos).

    Se lanza EXCLUSIVAMENTE cuando el argumento no es `MT5PricePnlInput`. Los
    problemas de datos se devuelven como `UNVERIFIED`, nunca como excepcion.
    """


class MT5PricePnlStatus(str, Enum):
    """Estado de la estimacion de PnL por precio."""

    VALUED = "VALUED"
    UNVERIFIED = "UNVERIFIED"


class MT5PricePnlMethod(str, Enum):
    """Metodo de calculo soportado (solo modos lineales)."""

    LINEAR_PRICE_MOVE = "LINEAR_PRICE_MOVE"


@dataclass(frozen=True, slots=True)
class MT5PricePnlInput:
    """Entrada PURA (sin terminal). Solo datos ya observados y explicitos.

    NO incluye `last`, `swap`, tasas FX ni campos de equity: el motor no puede
    recurrir a ellos por accidente.
    """

    position_id: Any
    instrument: str | None
    mt5_symbol: str | None
    quantity: Any
    avg_entry_price: Any
    contract_size: Any
    currency_profit: str | None
    trade_calc_mode_name: str | None
    bid: Any
    ask: Any
    tick_time: datetime | None = None
    received_at: datetime | None = None
    mark_instrument: str | None = None
    mark_mt5_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class MT5PricePnlResult:
    """Resultado auditable de la estimacion de PnL por movimiento de precio.

    `price_pnl` es `None` siempre que el estado es `UNVERIFIED`. La divisa del
    resultado es `currency_profit` (sin conversion FX). `timestamp_verified`
    permanece `False` (06J definira la politica de frescura).
    """

    status: MT5PricePnlStatus
    reason_code: str | None
    reason_codes: tuple[str, ...]
    reason: str
    position_id: Any
    instrument: str | None
    mt5_symbol: str | None
    trade_calc_mode_name: str | None
    method: MT5PricePnlMethod | None
    quantity_lots: float | None
    avg_entry_price: float | None
    market_side: str | None
    valuation_price: float | None
    contract_size: float | None
    price_pnl: float | None
    result_currency: str | None
    tick_time: datetime | None
    received_at: datetime | None
    timestamp_verified: bool = False


def _finite(value: Any) -> float | None:
    """Float finito o `None`.

    Rechaza `None`, `bool`, no numericos, NaN e +/-Inf, y cualquier conversion
    que lance `TypeError`/`ValueError`/`OverflowError`. NUNCA sustituye por 0.
    Contrato equivalente a los helpers de validacion de posiciones/equity.
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


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def estimate_mt5_price_pnl(data: MT5PricePnlInput) -> MT5PricePnlResult:
    """Estima el PnL por movimiento de precio de UNA posicion MT5 (puro).

    Formula: `signed_quantity_lots * contract_size * (valuation_price - avg)`.
    `valuation_price` = `bid` si `quantity > 0`, `ask` si `quantity < 0`. El
    volumen ya lleva el signo: NO se aplica un segundo cambio de signo.

    Lanza `MT5PriceValuationError` SOLO si `data` no es `MT5PricePnlInput`.
    """
    if not isinstance(data, MT5PricePnlInput):
        raise MT5PriceValuationError(
            "estimate_mt5_price_pnl exige un MT5PricePnlInput"
        )

    reasons: set[str] = set()

    # ---- identidad de instrumento/simbolo -------------------------------
    instrument_ok = _is_nonempty_str(data.instrument)
    if not instrument_ok:
        reasons.add("INSTRUMENT_MISSING")

    sym_ok = _is_nonempty_str(data.mt5_symbol)
    mark_sym_ok = _is_nonempty_str(data.mark_mt5_symbol)
    if not (sym_ok and mark_sym_ok):
        reasons.add("SYMBOL_MISSING")
    elif data.mt5_symbol != data.mark_mt5_symbol:
        reasons.add("INSTRUMENT_MISMATCH")

    if instrument_ok:
        mark_inst = data.mark_instrument
        if not (isinstance(mark_inst, str) and mark_inst == data.instrument):
            reasons.add("INSTRUMENT_MISMATCH")

    # ---- modo de calculo ------------------------------------------------
    mode = data.trade_calc_mode_name
    mode_named = _is_nonempty_str(mode)
    mode_supported = False
    if not mode_named:
        reasons.add("MODE_UNKNOWN")
    elif mode in SUPPORTED_LINEAR_PRICE_MODES:
        mode_supported = True
    else:
        reasons.add("MODE_NOT_WHITELISTED")

    # ---- divisa de resultado -------------------------------------------
    currency_ok = _is_nonempty_str(data.currency_profit)
    if not currency_ok:
        reasons.add("CURRENCY_PROFIT_MISSING")

    # ---- operandos numericos -------------------------------------------
    qty = _finite(data.quantity)
    if qty is None:
        reasons.add("QUANTITY_INVALID")
    elif qty == 0.0:
        reasons.add("QUANTITY_ZERO")

    avg = _finite(data.avg_entry_price)
    if avg is None:
        reasons.add("AVG_ENTRY_PRICE_INVALID")

    contract = _finite(data.contract_size)
    if contract is None or contract <= 0.0:
        reasons.add("CONTRACT_SIZE_INVALID")

    # ---- precio de valoracion por direccion -----------------------------
    side: str | None = None
    valuation: float | None = None
    if qty is not None and qty != 0.0:
        if qty > 0:
            side = "BID"
            valuation = _finite(data.bid)
        else:
            side = "ASK"
            valuation = _finite(data.ask)
        if valuation is None:
            reasons.add("VALUATION_PRICE_MISSING")

    # ---- deteccion de overflow (solo si el modo es admitido) ------------
    arithmetic_ready = (
        mode_supported
        and qty is not None
        and qty != 0.0
        and avg is not None
        and contract is not None
        and contract > 0.0
        and valuation is not None
    )
    pnl: float | None = None
    if arithmetic_ready:
        product = qty * contract * (valuation - avg)
        if math.isfinite(product):
            pnl = product
        else:
            reasons.add("PNL_NOT_FINITE")

    ordered = tuple(c for c in MT5_PRICE_PNL_REASON_ORDER if c in reasons)
    result_currency = data.currency_profit if currency_ok else None
    method = (
        MT5PricePnlMethod.LINEAR_PRICE_MOVE if mode_supported else None
    )

    if ordered:
        return MT5PricePnlResult(
            status=MT5PricePnlStatus.UNVERIFIED,
            reason_code=ordered[0],
            reason_codes=ordered,
            reason=_REASON_MESSAGES.get(ordered[0], "no verificable"),
            position_id=data.position_id,
            instrument=data.instrument,
            mt5_symbol=data.mt5_symbol,
            trade_calc_mode_name=(
                data.trade_calc_mode_name if mode_named else None
            ),
            method=method,
            quantity_lots=None,
            avg_entry_price=None,
            market_side=None,
            valuation_price=None,
            contract_size=None,
            price_pnl=None,
            result_currency=result_currency,
            tick_time=data.tick_time,
            received_at=data.received_at,
            timestamp_verified=False,
        )

    # ---- VALUED: sin motivos y PnL finito -------------------------------
    assert pnl is not None  # invariante: sin motivos => aritmetica valida
    return MT5PricePnlResult(
        status=MT5PricePnlStatus.VALUED,
        reason_code=None,
        reason_codes=(),
        reason="pnl por movimiento de precio estimado",
        position_id=data.position_id,
        instrument=data.instrument,
        mt5_symbol=data.mt5_symbol,
        trade_calc_mode_name=data.trade_calc_mode_name,
        method=MT5PricePnlMethod.LINEAR_PRICE_MOVE,
        quantity_lots=qty,
        avg_entry_price=avg,
        market_side=side,
        valuation_price=valuation,
        contract_size=contract,
        price_pnl=pnl,
        result_currency=result_currency,
        tick_time=data.tick_time,
        received_at=data.received_at,
        timestamp_verified=False,
    )
