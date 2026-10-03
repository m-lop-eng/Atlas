"""Portfolio Engine (00_MASTER_SPECIFICATION.md §24).

Calcula exposición agregada para detectar riesgos indirectos
(p. ej. LONG NQ + LONG ES no son riesgos independientes).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Position:
    """Posición abierta mínima para el cálculo de exposición."""

    strategy_id: str
    instrument: str
    market: str
    quantity: int | float
    direction: str
    notional: float
    entry_price: float | None = None
    current_price: float | None = None


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """Resumen de exposición (03_RISK_MANAGEMENT.md §10)."""

    total_gross_exposure: float
    total_net_exposure: float
    long_exposure: float
    short_exposure: float
    positions_count: int
    exposure_by_instrument: dict[str, float] = field(default_factory=dict)
    exposure_by_market: dict[str, float] = field(default_factory=dict)
    exposure_by_strategy: dict[str, float] = field(default_factory=dict)


def compute_portfolio_risk(positions: list[Position]) -> PortfolioRisk:
    """Calcula la exposición agregada del portafolio.

    Nota: para el efecto promedio de correlación/mercado compartido se
    requiere una matriz de correlación (03 §12). Este cálculo expone las
    métricas de exposición básicas.
    """
    long = sum(p.notional for p in positions if p.direction == "LONG")
    short = sum(p.notional for p in positions if p.direction == "SHORT")

    by_instrument: dict[str, float] = {}
    by_market: dict[str, float] = {}
    by_strategy: dict[str, float] = {}
    for p in positions:
        by_instrument[p.instrument] = by_instrument.get(p.instrument, 0.0) + p.notional
        by_market[p.market] = by_market.get(p.market, 0.0) + p.notional
        by_strategy[p.strategy_id] = by_strategy.get(p.strategy_id, 0.0) + p.notional

    return PortfolioRisk(
        total_gross_exposure=long + short,
        total_net_exposure=abs(long - short),
        long_exposure=long,
        short_exposure=short,
        positions_count=len(positions),
        exposure_by_instrument=by_instrument,
        exposure_by_market=by_market,
        exposure_by_strategy=by_strategy,
    )