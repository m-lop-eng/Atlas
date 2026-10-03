"""Data Engine (00_MASTER_SPECIFICATION.md §10-11).

Responsable de adquisición, validación, limpieza, normalización y
almacenamiento. Toda dataset debe conservar metadata de procedencia.
El estándar interno de timestamps es UTC (RULE-017).
"""

from .loader import DataLoadError, read_ohlc_csv
from .metadata import DatasetMetadata
from .quality import (
    check_ohlc_integrity,
    check_required_columns,
    detect_duplicates,
    detect_timestamp_gaps,
    summarize_quality,
)
from .synthetic import generate_ohlc_bars, sha256_of_file, write_ohlc_csv

__all__ = [
    "DataLoadError",
    "DatasetMetadata",
    "read_ohlc_csv",
    "check_ohlc_integrity",
    "check_required_columns",
    "detect_duplicates",
    "detect_timestamp_gaps",
    "summarize_quality",
    "generate_ohlc_bars",
    "sha256_of_file",
    "write_ohlc_csv",
]