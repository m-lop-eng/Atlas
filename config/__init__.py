"""Configuración (00_MASTER_SPECIFICATION.md §36-38)."""

from .loader import (
    CONFIG_DIR,
    ConfigError,
    EnvName,
    load_config,
    load_environment,
)

__all__ = ["CONFIG_DIR", "ConfigError", "EnvName", "load_config", "load_environment"]