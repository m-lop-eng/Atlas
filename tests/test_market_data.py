"""Tests de datos de mercado real (sin red: solo piezas puras) y metadata."""

from __future__ import annotations

import json

import pytest

from data.market import epoch_of, write_market_dataset
from data.loader import read_ohlc_csv
from research.experiment import ExperimentStatus, ExperimentType, experiment_meta


def json_load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestExperimentMeta:
    def test_default_is_hypothesis(self) -> None:
        meta = experiment_meta({})
        assert meta == {
            "experiment_type": "HYPOTHESIS",
            "parent_experiment_id": None,
            "change": None,
            "hypothesis": None,
        }

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError):
            experiment_meta({"experiment": {"type": "MAGIC"}})

    def test_members(self) -> None:
        for name in ("HYPOTHESIS", "BASELINE", "ABLATION", "NULL_CONTROL",
                     "SENSITIVITY", "STRESS", "ROBUSTNESS", "OOS",
                     "WALK_FORWARD", "EXECUTION"):
            assert ExperimentType(name).value == name
        assert ExperimentStatus.RUNNING.value == "running"


class TestMarketData:
    def test_epoch_of(self) -> None:
        assert epoch_of("2026-08-01T00:00:00") == 1785542400
        assert epoch_of("2026-08-01") == 1785542400

    def test_manifest_roundtrip_with_volume(self, tmp_path) -> None:
        bars = [
            {"ts": "1785542400", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.6, "volume": 100.0},
            {"ts": "1785546000", "open": 1.6, "high": 1.8, "low": 1.4,
             "close": 1.5, "volume": 90.0},
        ]
        csv = tmp_path / "m.csv"
        manifest = tmp_path / "m.json"
        write_market_dataset(
            bars, csv, manifest, pair="XXBTZUSD", interval=60, since=1785542400
        )
        loaded = read_ohlc_csv(csv, volume_col="volume")
        assert len(loaded) == 2
        assert loaded[0]["volume"] == 100.0
        data = json_load(manifest)
        assert data["bars"] == 2
        assert data["pair"] == "XXBTZUSD"
        assert data["sha256"]