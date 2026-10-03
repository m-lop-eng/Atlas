"""Tests del portfolio engine (portfolio/engine.py)."""

from portfolio.engine import Position, compute_portfolio_risk


def _pos(direction: str, notional: float, instrument: str = "NQ", market: str = "futures") -> Position:
    return Position(
        strategy_id="strat-1",
        instrument=instrument,
        market=market,
        quantity=1,
        direction=direction,
        notional=notional,
    )


class TestPortfolioRisk:
    def test_long_short_exposures(self) -> None:
        positions = [_pos("LONG", 10_000), _pos("SHORT", 4_000)]
        risk = compute_portfolio_risk(positions)
        assert risk.long_exposure == 10_000
        assert risk.short_exposure == 4_000
        assert risk.total_gross_exposure == 14_000
        assert risk.total_net_exposure == 6_000
        assert risk.positions_count == 2

    def test_empty_portfolio(self) -> None:
        risk = compute_portfolio_risk([])
        assert risk.total_gross_exposure == 0.0
        assert risk.positions_count == 0

    def test_aggregation_by_key(self) -> None:
        positions = [
            _pos("LONG", 5_000, instrument="NQ", market="futures"),
            _pos("LONG", 3_000, instrument="NQ", market="futures"),
            _pos("LONG", 2_000, instrument="ES", market="futures"),
        ]
        risk = compute_portfolio_risk(positions)
        assert risk.exposure_by_instrument["NQ"] == 8_000
        assert risk.exposure_by_market["futures"] == 10_000
        assert risk.exposure_by_strategy["strat-1"] == 10_000