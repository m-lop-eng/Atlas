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
            monetario teórico total de la operación. Solo se usa cuando la
            validación NO recibe `point_value`.

    Dos modos de riesgo cuantificable:
        * Sizing (point_value provisto): cantidad = cap / (distancia*point_value);
          el riesgo monetario en stop es, por construcción, equity*risk_per_trade.
        * Estimador (externo): riesgo teórico total provisto por callable.

    Fail-safe (RULE-036): sin stop, sin precio y sin fuente de riesgo
    cuantificable la intención es rechazada.
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
        *,
        point_value: float | None = None,
    ) -> RiskDecision:
        """Evalúa una intención y devuelve una decisión determinista.

        Args:
            signal: Intención de trading de la estrategia.
            state: Estado vivo de cuenta.
            point_value: P&L monetario por unidad de precio del instrumento.
                Al proveerlo habilita el sizing fixed-fractional y la cantidad
                de la decisión deriva de RiskConfig sin estimador externo.
        """
        if signal.action is Action.NO_TRADE:
            return self._reject(
                "Señal NO_TRADE no genera operación", {"action_valid": False}
            )

        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def _check(name: str, ok: bool, reason: str) -> None:
            checks[name] = ok
            if not ok:
                reasons.append(reason)

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
        approved_quantity = 0.0

        if signal.reference_price is None or signal.stop_price is None:
            _check(
                "quantifiable_risk",
                False,
                "Riesgo no cuantificable: se requieren reference_price y stop_price",
            )
        elif point_value is not None:
            quantity, ok_reason = self._size_fixed_fractional(
                signal, risk_cap, point_value
            )
            if ok_reason is not None:
                _check("quantifiable_risk", False, ok_reason)
            else:
                _check("quantifiable_risk", True, "")
                _check("risk_per_trade", True, "")
                approved_quantity = quantity
        elif self.trade_risk_estimator is not None:
            _check("quantifiable_risk", True, "")
            theoretical_risk = self.trade_risk_estimator(signal)
            limit = self.config.max_strategy_risk * risk_cap
            _check(
                "risk_per_trade",
                theoretical_risk <= limit,
                f"Riesgo por operación excede el límite "
                f"({theoretical_risk:.2f} > {limit:.2f})",
            )
            if theoretical_risk > 0:
                approved_quantity = float(signal.target_quantity or 1)
        else:
            _check(
                "quantifiable_risk",
                False,
                "Riesgo no cuantificable: se requiere point_value o un "
                "trade_risk_estimator configurado",
            )

        if not reasons:
            final_quantity = approved_quantity if approved_quantity > 0 else float(
                signal.target_quantity or 1
            )
            return RiskDecision(
                decision=RiskDecisionState.APPROVED,
                approved_quantity=final_quantity,
                reason="OK",
                checks=checks,
            )

        return self._reject(" | ".join(reasons), checks)

    def _size_fixed_fractional(
        self,
        signal: Signal,
        risk_cap: float,
        point_value: float,
    ) -> tuple[float, str | None]:
        """Sizing fixed-fractional.

        Returns: (cantidad, None) si es viable; (0.0, motivo) si no.
        """
        distance = abs(signal.reference_price - signal.stop_price)
        if distance <= 0:
            return 0.0, "Stop sin distancia: riesgo indefinido"
        if point_value <= 0:
            return 0.0, "point_value debe ser positivo"
        unit_risk = distance * point_value
        quantity = risk_cap / unit_risk
        if quantity <= 0:
            return 0.0, "Riesgo por operación sin tamaño"
        return quantity, None