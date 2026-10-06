"""Tests LIVE-05: secretos, entorno y configuracion de produccion (contrato).

Congela:
  * higiene de repositorio: archivos de secretos (`.env*`, `.credentials*`)
    NUNCA versionados; `.env.example` si versionado;
  * separacion paper/development/production via `ATLAS_ENV` (fail-closed);
  * la configuracion YAML NO contiene secretos (solo parametros funcionales);
  * el arranque live real (adapter + credenciales + permisos) queda **DEFERRED**:
    no se simula seguridad con mocks.

No incluye valores que parezcan credenciales reales.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from config.loader import ConfigError, EnvName, load_config, load_environment

ROOT = Path(__file__).resolve().parents[1]

SECRET_ENV_FILES = (
    ".env",
    ".env.production",
    ".env.live",
    ".env.paper",
    ".env.local",
    ".env.staging",
    ".credentials",
)
SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "passphrase",
    "token",
    "private_key",
    "password",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    )


class TestRepoHygiene:
    @pytest.mark.parametrize("name", SECRET_ENV_FILES)
    def test_secret_files_are_gitignored(self, name):
        r = _git("check-ignore", "-q", name)
        assert r.returncode == 0, f"{name} deberia estar ignorado por .gitignore"

    def test_env_example_is_versioned(self):
        # El ejemplo NO debe ignorarse (debe versionarse).
        assert _git("check-ignore", "-q", ".env.example").returncode != 0

    def test_no_tracked_secret_files(self):
        tracked = _git("ls-files").stdout.splitlines()
        for f in tracked:
            low = f.lower()
            assert low == ".env.example" or not low.startswith(".env."), f
            assert "credential" not in low, f


class TestEnvironmentSeparation:
    def test_default_environment_is_development(self, monkeypatch):
        monkeypatch.delenv("ATLAS_ENV", raising=False)
        assert load_environment() is EnvName.DEVELOPMENT

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("development", EnvName.DEVELOPMENT),
            ("paper", EnvName.PAPER),
            ("production", EnvName.PRODUCTION),
        ],
    )
    def test_explicit_environment(self, monkeypatch, value, expected):
        monkeypatch.setenv("ATLAS_ENV", value)
        assert load_environment() is expected

    def test_ambiguous_environment_fails_closed(self, monkeypatch):
        monkeypatch.setenv("ATLAS_ENV", "live")  # no es un EnvName valido
        with pytest.raises(ConfigError):
            load_environment()

    def test_config_loaded_per_environment(self):
        for env in (EnvName.DEVELOPMENT, EnvName.PAPER, EnvName.PRODUCTION):
            cfg = load_config(env)
            assert cfg["environment"] == env.value


class TestNoSecretsInConfig:
    def test_config_yaml_has_no_secret_fields(self):
        for path in (ROOT / "config").rglob("*.yaml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            text = repr(data).lower()
            for hint in SECRET_KEY_HINTS:
                assert hint not in text, f"{path}: posible secreto '{hint}'"

    def test_forward_builder_config_has_no_secrets(self):
        for name in (
            "config/paper/settings.yaml",
            "config/final_oos/config.yaml",
        ):
            data = yaml.safe_load((ROOT / name).read_text(encoding="utf-8")) or {}
            assert "secret" not in repr(data).lower()
            assert "password" not in repr(data).lower()
