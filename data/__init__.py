"""Data Engine (00_MASTER_SPECIFICATION.md §10-11).

Responsable de adquisición, validación, limpieza, normalización y
almacenamiento. Toda dataset debe conservar metadata de procedencia.
El estándar interno de timestamps es UTC (RULE-017).
"""

from .metadata import DatasetMetadata
from .quality import (
    check_ohlc_integrity,
    check_required_columns,
    detect_duplicates,
    detect_timestamp_gaps,
    summarize_quality,
)

__all__ = [
    "DatasetMetadata",
    "check_ohlc_integrity",
    "check_required_columns",
    "detect_duplicates",
    "detect_timestamp_gaps",
    "summarize_quality",
]