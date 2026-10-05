"""Tipos y estados de la capa de riesgo."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RiskDecisionState(str, Enum):
    """Estados de decisión del RiskEngine (03_RISK_MANAGEMENT.md §39)."""

    APPROVED = "APPROVED"
    APPROVED_WITH_REDUCED_SIZE = "APPROVED_WITH_REDUCED_SIZE"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    HALTED = "HALTED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Resultado determinista del RiskEngine para una intención de trading.

    Attributes:
        decision: Estado final.
        approved_quantity: Cantidad finalmente aprobada (0 si rechazado).
        reason: Motivo legible (o código del check que falló).
        checks: Resultado de cada check ejecutado.
        at: Timestamp de la decisión.
    """

    decision: RiskDecisionState
    approved_quantity: int | float = 0
    reason: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.decision in (
            RiskDecisionState.APPROVED,
            RiskDecisionState.APPROVED_WITH_REDUCED_SIZE,
        )


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Configuración de límites por estrategia/account (03_RISK_MANAGEMENT.md §13).

    Son límites INTERNOS configurables. Nunca deben superar restricciones
    externas verificadas (prop firm).
    """

    risk_per_trade: float = 0.0025
    max_open_positions: int = 3
    # Maximo riesgo por operacion como fraccion de equity (contrato F-02:
    # `risk_per_trade <= max_strategy_risk`).
    max_strategy_risk: float = 0.01
    max_daily_loss: float = 0.01
    max_drawdown: float = 0.05
    warning_level: float = 0.5
    soft_limit_level: float = 0.75
    # F-03 (paper phase 1): limites de exposicion/leverage/concentracion.
    # `None` = gate DESHABILITADO (no se evalua). Sin valores por defecto:
    # un limite `0.0` NO significa "deshabilitado". Unidades: gross/net = USD;
    # leverage = ratio; concentration = fraccion de equity.
    max_gross_exposure: float | None = None
    max_net_exposure: float | None = None
    max_leverage: float | None = None
    max_instrument_concentration: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "risk_per_trade",
            "max_strategy_risk",
            "max_daily_loss",
            "max_drawdown",
        ):
            value = getattr(self, name)
            if not 0.0 < value < 1.0:
                raise ValueError(f"{name} debe estar en (0, 1), recibido {value}")
        for name in (
            "max_gross_exposure",
            "max_net_exposure",
            "max_leverage",
            "max_instrument_concentration",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not (isinstance(value, (int, float)) and math.isfinite(value)
                    and value > 0.0):
                raise ValueError(
                    f"{name} debe ser > 0 (o None para deshabilitar), "
                    f"recibido {value!r}"
                )


@dataclass(slots=True)
class AccountRiskState:
    """Estado vivo de la cuenta/es del RiskEngine para valorar una intención.

    Los valores son verificados/reconciliados contra el broker en producción
    (06_EXECUTION_OPERATION.md §12). En backtest/paper los provee el simulador.
    """

    equity: float
    balance: float = 0.0
    open_positions: int = 0
    current_daily_pnl: float = 0.0
    current_drawdown: float = 0.0
    # margin: INFRASTRUCTURE GAP (F-03 fase 1 no lo implementa); se conserva el
    # campo existente sin usarlo como metrica de gate.
    margin_usage: float = 0.0
    kill_switch: bool = False
    account_active: bool = True
    strategy_active: bool = True


@dataclass(frozen=True, slots=True)
class ProjectedExposure:
    """Metricas de exposicion PROYECTADA (post-trade) para el gate F-03 pre-fill.

    Value object EFIMERO e inmutable: se deriva exclusivamente de
    `AccountLedger.valuation(marks)` + la orden propuesta + el precio de
    ejecucion. NO es una autoridad de estado (no persiste, no guarda
    posiciones); el `RiskEngine` solo lo consume para comparar con los limites.

    `None` = metrica NO DISPONIBLE (valuacion incompleta / precio de ejecucion
    invalido) -> fail-closed si el gate correspondiente esta activo. Nunca se usa
    `0.0` para representar ausencia de dato.
    """

    equity: float
    gross_exposure: float | None
    net_exposure: float | None
    exposure_by_instrument: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class TradeRisk:
    """Riesgo monetario teórico de una operación.

    Total = Stop Loss Risk + Expected Slippage + Estimated Commission
    (03_RISK_MANAGEMENT.md §7).
    """

    stop_risk: float
    expected_slippage: float = 0.0
    estimated_commission: float = 0.0
    fees: float = 0.0
    quantity: int | float = 1

    @property
    def total(self) -> float:
        return self.stop_risk + self.expected_slippage + self.estimated_commission + self.fees