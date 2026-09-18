"""Tests del loader de datos OHLC (data/loader.py)."""

from __future__ import annotations

import pytest

from data.loader import DataLoadError, read_ohlc_csv
from data.synthetic import write_ohlc_csv


def _write_csv(tmp_path, rows: list[str]) -> str:
    path = tmp_path / "dataset.csv"
    path.write_text("\n".join(rows), encoding="utf-8")
    return str(path)


HEADER = "ts,open,high,low,close"


class TestReadOhlcCsv:
    def test_reads_all_rows(self, tmp_path) -> None:
        path = _write_csv(
            tmp_path,
            [HEADER, "1,10,11,9,10.5", "2,10.5,12,10,11.5"],
        )
        bars = read_ohlc_csv(path)
        assert len(bars) == 2
        assert bars[0]["ts"] == "1"
        assert bars[0]["open"] == 10.0
        assert bars[1]["close"] == 11.5

    def test_sorts_by_timestamp(self, tmp_path) -> None:
        path = _write_csv(
            tmp_path,
            [HEADER, "2,10.5,12,10,11.5", "1,10,11,9,10.5"],
        )
        bars = read_ohlc_csv(path)
        assert [b["ts"] for b in bars] == ["1", "2"]

    def test_volume_optional_by_default(self, tmp_path) -> None:
        path = _write_csv(
            tmp_path,
            [HEADER, "1,10,11,9,10.5"],
        )
        assert read_ohlc_csv(path)

    def test_volume_required_when_requested(self, tmp_path) -> None:
        path = _write_csv(
            tmp_path,
            [HEADER, "1,10,11,9,10.5"],
        )
        with pytest.raises(DataLoadError, match="Faltan columnas"):
            read_ohlc_csv(path, volume_col="volume")

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(DataLoadError, match="no encontrado"):
            read_ohlc_csv(str(tmp_path / "no-existe.csv"))

    def test_missing_column_raises(self, tmp_path) -> None:
        path = _write_csv(tmp_path, ["ts,open,low,close", "1,10,9,10.5"])
        with pytest.raises(DataLoadError, match="high"):
            read_ohlc_csv(path)

    def test_corrupt_row_raises(self, tmp_path) -> None:
        path = _write_csv(tmp_path, [HEADER, "1,no-numero,11,9,10.5"])
        with pytest.raises(DataLoadError, match="corrupta"):
            read_ohlc_csv(path)

    def test_empty_file_raises(self, tmp_path) -> None:
        path = _write_csv(tmp_path, [""])
        with pytest.raises(DataLoadError, match="vac"):
            read_ohlc_csv(path)

    def test_roundtrip_with_synthetic_writer(self, tmp_path) -> None:
        from data.synthetic import generate_ohlc_bars

        bars = generate_ohlc_bars(n_bars=30, seed=1)
        path = write_ohlc_csv(bars, tmp_path / "sync.csv")
        loaded = read_ohlc_csv(path)
        assert len(loaded) == 30