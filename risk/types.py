"""Tipos y estados de la capa de riesgo."""

from __future__ import annotations

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
    max_strategy_risk: float = 0.01
    max_daily_loss: float = 0.01
    max_drawdown: float = 0.05
    warning_level: float = 0.5
    soft_limit_level: float = 0.75

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
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    margin_usage: float = 0.0
    kill_switch: bool = False
    account_active: bool = True
    strategy_active: bool = True


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