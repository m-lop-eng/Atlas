"""Tests de position_sizing (risk/position_sizing.py)."""

import pytest

from risk.position_sizing import (
    PositionSizingError,
    maximum_monetary_risk,
    position_size_from_stop,
    position_size_with_stop_at,
)


class TestMaximumMonetaryRisk:
    def test_basic(self) -> None:
        assert maximum_monetary_risk(100_000, 0.0025) == 250.0

    def test_small_equity(self) -> None:
        assert maximum_monetary_risk(1000, 0.05) == 50.0

    def test_equity_zero_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="positivo"):
            maximum_monetary_risk(0, 0.0025)

    def test_equity_negative_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="positivo"):
            maximum_monetary_risk(-1000, 0.0025)

    def test_risk_fraction_zero_raises(self) -> None:
        with pytest.raises(PositionSizingError, match=r"\(0, 1\)"):
            maximum_monetary_risk(100_000, 0.0)

    def test_risk_fraction_one_raises(self) -> None:
        with pytest.raises(PositionSizingError, match=r"\(0, 1\)"):
            maximum_monetary_risk(100_000, 1.0)

    def test_risk_fraction_above_one_raises(self) -> None:
        with pytest.raises(PositionSizingError):
            maximum_monetary_risk(100_000, 1.5)


class TestPositionSizeFromStop:
    def test_basic(self) -> None:
        size = position_size_from_stop(100_000, 0.0025, 5.0, 50.0)
        assert abs(size - 1.0) < 1e-10

    def test_larger_distance_fewer_units(self) -> None:
        size_small = position_size_from_stop(100_000, 0.0025, 2.0, 50.0)
        size_large = position_size_from_stop(100_000, 0.0025, 10.0, 50.0)
        assert size_small > size_large

    def test_stop_zero_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="positivo"):
            position_size_from_stop(100_000, 0.0025, 0.0, 50.0)

    def test_stop_negative_raises(self) -> None:
        with pytest.raises(PositionSizingError):
            position_size_from_stop(100_000, 0.0025, -5.0, 50.0)

    def test_value_per_unit_zero_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="positivo"):
            position_size_from_stop(100_000, 0.0025, 5.0, 0.0)


class TestPositionSizeWithStopAt:
    def test_long_stop_below(self) -> None:
        size = position_size_with_stop_at(100_000, 0.0025, 100.0, 95.0, 50.0, "LONG")
        assert abs(size - 1.0) < 1e-10

    def test_short_stop_above(self) -> None:
        size = position_size_with_stop_at(100_000, 0.0025, 100.0, 105.0, 50.0, "SHORT")
        assert abs(size - 1.0) < 1e-10

    def test_long_stop_above_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="debajo"):
            position_size_with_stop_at(100_000, 0.0025, 100.0, 105.0, 50.0, "LONG")

    def test_short_stop_below_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="encima"):
            position_size_with_stop_at(100_000, 0.0025, 100.0, 95.0, 50.0, "SHORT")

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="inválida"):
            position_size_with_stop_at(100_000, 0.0025, 100.0, 95.0, 50.0, "LONG2")

    def test_entry_zero_raises(self) -> None:
        with pytest.raises(PositionSizingError, match="positivos"):
            position_size_with_stop_at(100_000, 0.0025, 0.0, 95.0, 50.0, "LONG")