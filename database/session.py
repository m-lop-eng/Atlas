"""Sesión y conexión PostgreSQL desde variables de entorno.

Variables requeridas (ver .env.example):
    ATLAS_POSTGRES_HOST / PORT / DB / USER / PASSWORD

El entorno de desarrollo nunca debe poder escribir en producción:
la URL se construye exclusivamente a partir de estas variables.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def postgres_url_from_env() -> str:
    host = os.getenv("ATLAS_POSTGRES_HOST", "localhost")
    port = os.getenv("ATLAS_POSTGRES_PORT", "5432")
    db = os.getenv("ATLAS_POSTGRES_DB", "atlas")
    user = os.getenv("ATLAS_POSTGRES_USER", "atlas")
    password = os.getenv("ATLAS_POSTGRES_PASSWORD", "")

    if "ATLAS_POSTGRES_HOST" in os.environ and not password:
        raise RuntimeError(
            "ATLAS_POSTGRES_PASSWORD no configurada: no se construirá una "
            "conexión con credenciales vacías"
        )

    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def create_engine_from_env() -> object:
    return create_engine(postgres_url_from_env(), pool_pre_ping=True)


def create_session_factory(engine=None) -> sessionmaker[Session]:
    engine = engine or create_engine_from_env()
    return sessionmaker(bind=engine, expire_on_commit=False)