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
from .types import (
    AccountRiskState,
    ProjectedExposure,
    RiskConfig,
    RiskDecision,
    RiskDecisionState,
)

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

    Contrato canonico de `max_strategy_risk` (F-02): es el MAXIMO riesgo por
    operacion expresado como fraccion de equity. Se exige:
        risk_per_trade <= max_strategy_risk
    En el modo sizing se comprueba esta coherencia de configuracion; en el modo
    estimador el limite absoluto es `max_strategy_risk * equity` (NO
    `risk_cap * max_strategy_risk`).

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
        contract_size: float | None = None,
    ) -> RiskDecision:
        """Evalúa una intención y devuelve una decisión determinista.

        Args:
            signal: Intención de trading de la estrategia.
            state: Estado vivo de cuenta.
            point_value: P&L monetario por unidad de precio del instrumento
                (LEGACY). Alias de `contract_size` para paper/backtest históricos.
            contract_size: multiplicador económico canónico (MULTI-ASSET-03):
                `risk_per_unit = distance * contract_size`. Es la vía preferida;
                no debe combinarse con un `point_value` distinto.
        """
        if signal.action is Action.NO_TRADE:
            return self._reject(
                "Señal NO_TRADE no genera operación", {"action_valid": False}
            )

        if (
            point_value is not None
            and contract_size is not None
            and not self._same_multiplier(point_value, contract_size)
        ):
            raise ValueError(
                "point_value y contract_size incompatibles "
                f"({point_value!r} != {contract_size!r}): usa uno solo"
            )
        multiplier = contract_size if contract_size is not None else point_value

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

        # F-03: los gates de exposicion NO se evaluan aqui (estado actual). El
        # control material es el gate PROJECTED pre-fill
        # (`validate_projected_exposure`), invocado por el paper engine
        # inmediatamente antes del fill con el precio de ejecucion. Ver F-03.

        risk_cap = maximum_monetary_risk(state.equity, self.config.risk_per_trade)
        approved_quantity = 0.0

        if signal.reference_price is None or signal.stop_price is None:
            _check(
                "quantifiable_risk",
                False,
                "Riesgo no cuantificable: se requieren reference_price y stop_price",
            )
        elif multiplier is not None:
            quantity, ok_reason = self._size_fixed_fractional(
                signal, risk_cap, multiplier
            )
            if ok_reason is not None:
                _check("quantifiable_risk", False, ok_reason)
            else:
                _check("quantifiable_risk", True, "")
                # Contrato canonico F-02: `max_strategy_risk` es el MAXIMO
                # riesgo por operacion como fraccion de equity. El riesgo
                # objetivo por operacion es `risk_per_trade`; se exige
                #   risk_per_trade <= max_strategy_risk
                # (no se altera el algoritmo de sizing).
                _check(
                    "max_strategy_risk",
                    self.config.risk_per_trade <= self.config.max_strategy_risk,
                    "risk_per_trade excede max_strategy_risk: la configuracion "
                    "opera por encima del maximo de riesgo por operacion de la "
                    "estrategia",
                )
                approved_quantity = quantity
        elif self.trade_risk_estimator is not None:
            _check("quantifiable_risk", True, "")
            theoretical_risk = self.trade_risk_estimator(signal)
            # Contrato canonico F-02: limite absoluto por operacion relativo a
            # equity (no `risk_cap * max_strategy_risk`).
            limit = self.config.max_strategy_risk * state.equity
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

    def validate_projected_exposure(
        self, projected: ProjectedExposure
    ) -> RiskDecision:
        """Gate F-03 de enforcement PRE-FILL sobre exposicion PROYECTADA.

        Solo evalua los 4 controles de exposicion (gross/net/leverage/
        instrument concentration). NO hace sizing ni otros checks. El llamante
        (paper engine) lo invoca inmediatamente antes del fill, tras conocer la
        cantidad aprobada y el precio de ejecucion, con el estado proyectado
        derivado del ledger + la orden. El `approved_quantity` del resultado no
        aplica (el sizing ya ocurrio); usar `decision.approved`.
        """
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def _check(name: str, ok: bool, reason: str) -> None:
            checks[name] = ok
            if not ok:
                reasons.append(reason)

        self._check_exposure_gates(projected, _check)
        if reasons:
            return RiskDecision(
                decision=RiskDecisionState.REJECTED,
                approved_quantity=0,
                reason=" | ".join(reasons),
                checks=checks,
            )
        return RiskDecision(
            decision=RiskDecisionState.APPROVED,
            approved_quantity=0,
            reason="OK",
            checks=checks,
        )

    def _check_exposure_gates(
        self, projected: ProjectedExposure,
        _check: Callable[[str, bool, str], None],
    ) -> None:
        """Los 4 gates F-03 sobre metricas PROYECTADAS (autoritativas).

        `None` en el limite = gate deshabilitado (no se evalua). Una metrica
        `None` (proyeccion no disponible) con el gate activo es fail-closed:
        nunca se sustituye por cero. `equity <= 0` con gate relativo es
        fail-closed. NO usa `portfolio.compute_portfolio_risk` ni recalcula
        exposicion desde posiciones propias.
        """
        cfg = self.config

        if cfg.max_gross_exposure is not None:
            if projected.gross_exposure is None:
                _check(
                    "max_gross_exposure", False,
                    "Exposición bruta proyectada no disponible",
                )
            else:
                _check(
                    "max_gross_exposure",
                    projected.gross_exposure <= cfg.max_gross_exposure,
                    "Límite de exposición bruta proyectada alcanzado",
                )

        if cfg.max_net_exposure is not None:
            if projected.net_exposure is None:
                _check(
                    "max_net_exposure", False,
                    "Exposición neta proyectada no disponible",
                )
            else:
                # Net es SIGNED (LONG +/SHORT -): el limite aplica a la magnitud.
                _check(
                    "max_net_exposure",
                    abs(projected.net_exposure) <= cfg.max_net_exposure,
                    "Límite de exposición neta proyectada alcanzado",
                )

        if cfg.max_leverage is not None:
            if projected.equity <= 0 or projected.gross_exposure is None:
                _check(
                    "max_leverage", False,
                    "Leverage proyectado no evaluable (equity<=0 o gross no "
                    "disponible)",
                )
            else:
                _check(
                    "max_leverage",
                    projected.gross_exposure / projected.equity <= cfg.max_leverage,
                    "Límite de leverage proyectado alcanzado",
                )

        if cfg.max_instrument_concentration is not None:
            if projected.equity <= 0 or projected.exposure_by_instrument is None:
                _check(
                    "max_instrument_concentration", False,
                    "Concentración proyectada no evaluable (equity<=0 o "
                    "exposición no disponible)",
                )
            else:
                # Concentracion por instrumento = |notional_i| / equity; el gate
                # usa el MAXIMO entre instrumentos. Sin posiciones = 0.0 real.
                concentration = max(
                    (abs(v) / projected.equity
                     for v in projected.exposure_by_instrument.values()),
                    default=0.0,
                )
                _check(
                    "max_instrument_concentration",
                    concentration <= cfg.max_instrument_concentration,
                    "Límite de concentración por instrumento alcanzado",
                )

    def _size_fixed_fractional(
        self,
        signal: Signal,
        risk_cap: float,
        multiplier: float,
    ) -> tuple[float, str | None]:
        """Sizing fixed-fractional.

        `multiplier` = contract_size (MULTI-ASSET-03) o su alias legacy
        `point_value`: `risk_per_unit = distance * multiplier`.

        Returns: (cantidad, None) si es viable; (0.0, motivo) si no.
        """
        distance = abs(signal.reference_price - signal.stop_price)
        if distance <= 0:
            return 0.0, "Stop sin distancia: riesgo indefinido"
        if multiplier <= 0:
            return 0.0, "contract_size/point_value debe ser positivo"
        unit_risk = distance * multiplier
        quantity = risk_cap / unit_risk
        if quantity <= 0:
            return 0.0, "Riesgo por operación sin tamaño"
        return quantity, None

    @staticmethod
    def _same_multiplier(a: float, b: float) -> bool:
        import math

        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)