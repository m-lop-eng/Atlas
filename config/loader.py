"""Carga de configuración por entorno (00_MASTER_SPECIFICATION.md §36).

Entornos: development / paper / production.
NUNCA cargar una configuración de producción contaminada por variables de
desarrollo: el loader rechaza entornos desconocidos y exige archivo YAML
válido. La selección viene de la variable ATLAS_ENV (.env / entorno OS).
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent


class EnvName(str, Enum):
    DEVELOPMENT = "development"
    PAPER = "paper"
    PRODUCTION = "production"


class ConfigError(ValueError):
    pass


def load_environment() -> EnvName:
    """Resuelve el entorno activo a partir de ATLAS_ENV."""
    raw = os.getenv("ATLAS_ENV", EnvName.DEVELOPMENT.value)
    try:
        return EnvName(raw.lower())
    except ValueError:
        raise ConfigError(
            f"ATLAS_ENV inválido: {raw!r}. Válidos: "
            + ", ".join(e.value for e in EnvName)
        ) from None


def load_config(env: EnvName | str | None = None) -> dict:
    """Carga el settings.yaml del entorno.

    Fallos catastróficos: ningún valor por defecto silencioso para métricas
    de riesgo en producción. Un YAML ausente o malformado es un error que
    detiene el arranque del motor.
    """
    if isinstance(env, str):
        try:
            env = EnvName(env.lower())
        except ValueError:
            raise ConfigError(
                f"Entorno inválido: {env!r}. Válidos: "
                + ", ".join(e.value for e in EnvName)
            ) from None
    else:
        env = env or load_environment()
    path = CONFIG_DIR / env.value / "settings.yaml"
    if not path.exists():
        raise ConfigError(f"Configuración no encontrada: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido en {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Configuración {path} no es un diccionario")

    data.setdefault("environment", env.value)
    return data