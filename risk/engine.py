"""RiskEngine: autoridad de veto sobre las intenciones de trading.

Flujo (03_RISK_MANAGEMENT.md §7, §40):
    Strategy → Signal → RiskEngine → APPROVED/REJECTED/... → OrderManager → Broker

Principios:
    * No trading when risk cannot be calculated reliably (fail-safe).
    * El RiskEngine es independiente de la lógica de la estrategia.
    * Todo intento de trading se evalúa contra TODOS los límites aplicables.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from strategies.base.signal import Action, Signal
from .position_sizing import maximum_monetary_risk
from .types import AccountRiskState, RiskConfig, RiskDecision, RiskDecisionState

TradeRiskEstimator = Callable[[Signal], float]


@dataclass(slots=True)
class RiskEngine:
    """Motor de riesgo determinista y comprobable.

    Args:
        config: Límites internos de la estrategia/cuenta.
        trade_risk_estimator: Función que convierte un Signal en el riesgo
            monetario teórico de la operación (incluye slippage y comisiones).
            Si no se provee, se rechaza por defecto ante señales sin un
            estimador explícito (fail-safe, RULE-036).
    """

    config: RiskConfig
    trade_risk_estimator: TradeRiskEstimator | None = None

    def _reject(self, reason: str, checks: dict[str, bool]) -> RiskDecision:
        return RiskDecision(
            decision=RiskDecisionState.REJECTED,
            approved_quantity=0,
            reason=reason,
            checks=checks,
        )

    def validate(
        self,
        signal: Signal,
        state: AccountRiskState,
    ) -> RiskDecision:
        """Evalúa una intención y devuelve una decisión determinista.

        Una señal sin riesgo cuantificable (precio o stop ausentes, o sin
        trade_risk_estimator) es rechazada: no se opera sin riesgo medible.
        """
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def _check(name: str, ok: bool, reason: str) -> None:
            checks[name] = ok
            if not ok:
                reasons.append(reason)

        if signal.action is Action.NO_TRADE:
            return self._reject(
                "Señal NO_TRADE no genera operación", {"action_valid": False}
            )

        _check("strategy_active", state.strategy_active, "Estrategia no activa")
        _check("account_active", state.account_active, "Cuenta no activa")
        _check("kill_switch", not state.kill_switch, "Kill switch activo")
        _check(
            "daily_loss",
            state.current_daily_pnl > -(self.config.max_daily_loss * state.equity),
            "Límite de pérdida diaria alcanzado",
        )
        _check(
            "drawdown",
            state.current_drawdown < self.config.max_drawdown,
            "Límite de drawdown alcanzado",
        )
        _check(
            "positions_slot",
            state.open_positions < self.config.max_open_positions,
            "Se alcanzó el máximo de posiciones abiertas",
        )

        risk_cap = maximum_monetary_risk(state.equity, self.config.risk_per_trade)

        if (
            signal.reference_price is None
            or signal.stop_price is None
            or self.trade_risk_estimator is None
        ):
            _check(
                "quantifiable_risk",
                False,
                "Riesgo no cuantificable: se requieren reference_price, stop_price "
                "y un trade_risk_estimator configurado",
            )
            theoretical_risk = 0.0
        else:
            distance = abs(signal.reference_price - signal.stop_price)
            if distance <= 0:
                _check(
                    "quantifiable_risk",
                    False,
                    "Stop sin distancia: riesgo indefinido",
                )
                theoretical_risk = 0.0
            else:
                theoretical_risk = self.trade_risk_estimator(signal)
                _check(
                    "risk_per_trade",
                    theoretical_risk <= self.config.max_strategy_risk * risk_cap,
                    f"Riesgo por operación excede el límite "
                    f"({theoretical_risk:.2f} > {self.config.max_strategy_risk * risk_cap:.2f})",
                )

        if not reasons:
            return RiskDecision(
                decision=RiskDecisionState.APPROVED,
                approved_quantity=signal.target_quantity or 1,
                reason="OK",
                checks=checks,
            )

        return self._reject(" | ".join(reasons), checks)