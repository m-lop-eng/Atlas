"""Modelos SQLAlchemy iniciales del sistema (00_MASTER_SPECIFICATION.md §41).

Estos modelos cubren el seguimiento de experimentos e instrumentos.
Siguientes iteraciones añadirán: signals, ordenes, trades, ejecuciones,
configuraciones versionadas (REST de estrategia).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Instrumento: id, ticker, asset class, exchange, lot/valor del punto."""

    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    asset_class: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    tick_size: Mapped[float | None] = mapped_column(Float)
    point_value: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StrategyRecord(Base):
    """Registro de estrategia desplegada (09_STRATEGY_LIFECYCLE.md §24)."""

    __tablename__ = "strategies"

    binary_hash: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    instruments: Mapped[str | None] = mapped_column(Text)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Experiment(Base):
    """Experimento de investigación reproducible (05_RESEARCH.md §10)."""

    __tablename__ = "experiments"

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str | None] = mapped_column(String(128))
    dataset_meta: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[str | None] = mapped_column(Text)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))