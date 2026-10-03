"""Metadata de datasets (RULE-015 / 00_MASTER_SPECIFICATION.md §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    """Procedencia e identidad de un dataset.

    Todo dataset usado en investigación debe tener estos campos mínimos.
    Un resultado sin dataset versionado no es reproducible.
    """

    source: str
    instrument: str
    timeframe: str
    timezone: str
    start: str
    end: str
    version: str
    download_date: str | None = None
    asset_class: str | None = None
    data_format: str | None = None
    adjustments: str | None = None
    known_limitations: str | None = None
    acquired_at: str | None = None

    def describe(self) -> dict[str, str]:
        return {
            "source": self.source,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "timezone": self.timezone,
            "start": self.start,
            "end": self.end,
            "version": self.version,
            "download_date": self.download_date or "",
        }