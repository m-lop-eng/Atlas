"""Tests de validación de calidad de datos (data/quality.py)."""

from data.quality import (
    check_ohlc_integrity,
    detect_duplicates,
    detect_timestamp_gaps,
    summarize_quality,
)


def _clean_bars(n: int = 4, start: int = 1000) -> list[dict]:
    return [
        {"ts": str(start + i * 60), "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i}
        for i in range(n)
    ]


class TestOhlcIntegrity:
    def test_clean_passes(self) -> None:
        issues = check_ohlc_integrity(_clean_bars())
        assert issues == []

    def test_high_below_close_detected(self) -> None:
        bars = _clean_bars()
        bars[1]["high"] = 95.0
        issues = check_ohlc_integrity(bars)
        assert any(i.issue == "ohlc_high" for i in issues)

    def test_low_above_open_detected(self) -> None:
        bars = _clean_bars()
        bars[1]["low"] = 200.0
        issues = check_ohlc_integrity(bars)
        assert any(i.issue == "ohlc_low" for i in issues)

    def test_malformed_fields(self) -> None:
        bars = _clean_bars()
        bars[0]["high"] = "no-float"
        issues = check_ohlc_integrity(bars)
        assert any(i.issue == "campos_malformados" for i in issues)


class TestDuplicates:
    def test_detect_duplicate_ts(self) -> None:
        bars = _clean_bars()
        bars[2]["ts"] = bars[1]["ts"]
        issues = detect_duplicates(bars)
        assert any(i.issue == "ts_duplicado" for i in issues)

    def test_no_duplicates(self) -> None:
        issues = detect_duplicates(_clean_bars())
        assert issues == []


class TestGaps:
    def test_gap_detected(self) -> None:
        bars = _clean_bars(start=0)
        bars[2]["ts"] = "10000"
        issues = detect_timestamp_gaps(bars, expected_seconds=60)
        assert any(i.issue == "gap" for i in issues)

    def test_no_gap_within_tolerance(self) -> None:
        bars = _clean_bars(start=0)
        issues = detect_timestamp_gaps(bars, expected_seconds=60, tolerance_seconds=30)
        assert issues == []


class TestSummary:
    def test_clean_data_ok(self) -> None:
        summary = summarize_quality(_clean_bars(start=0), expected_interval_seconds=60)
        assert summary.ok
        assert summary.rows == 4

    def test_dirty_data_not_ok(self) -> None:
        bars = _clean_bars()
        bars[0]["high"] = 50.0
        summary = summarize_quality(bars, expected_interval_seconds=60)
        assert not summary.ok
        assert summary.issues

    def test_missing_columns_reported(self) -> None:
        bars = [{"ts": "1", "open": 1, "high": 1, "low": 1}]
        summary = summarize_quality(bars)
        assert any(i.issue == "columnas" for i in summary.issues)