"""Tests de configuración por entorno (config/)."""

import pytest

from config import (
    CONFIG_DIR,
    ConfigError,
    EnvName,
    load_config,
    load_environment,
)


class TestEnvironmentResolution:
    def test_default_is_development(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAS_ENV", raising=False)
        assert load_environment() == EnvName.DEVELOPMENT

    def test_reads_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAS_ENV", "paper")
        assert load_environment() == EnvName.PAPER

    def test_unknown_env_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAS_ENV", "qa")
        with pytest.raises(ConfigError, match="ATLAS_ENV"):
            load_environment()


class TestLoadConfig:
    def test_load_development(self) -> None:
        cfg = load_config(EnvName.DEVELOPMENT)
        assert cfg["environment"] == "development"
        assert cfg["risk"]["risk_per_trade"] == 0.0025

    def test_load_string_env(self) -> None:
        cfg = load_config("production")
        assert cfg["environment"] == "production"

    def test_paper_has_initial_equity(self) -> None:
        cfg = load_config("paper")
        assert cfg["paper_account"]["initial_equity"] == 100_000

    def test_invalid_env_name_raises(self) -> None:
        with pytest.raises(ConfigError, match="Entorno inválido"):
            load_config("staging")

    def test_directories_exist(self) -> None:
        for name in ("development", "paper", "production"):
            assert (CONFIG_DIR / name / "settings.yaml").exists(), name