"""Validación de calidad de datos de mercado (RULE-016).

Funciones puras y deterministas. Descubrimiento de problemas, no
modificación de datos: cualquier hallazgo se registra y se documenta.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

REQUIRED_OHLC_COLUMNS = ("ts", "open", "high", "low", "close")


@dataclass(slots=True)
class QualityIssue:
    issue: str
    row: int
    detail: str


@dataclass(slots=True)
class QualitySummary:
    rows: int
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def check_required_columns(bars: list[dict]) -> list[str]:
    """Campos obligatorios (ts y OHLC) ausentes en la primera barra."""
    if not bars:
        return ["dataset vacío"]
    missing = [c for c in REQUIRED_OHLC_COLUMNS if c not in bars[0]]
    return missing


def check_ohlc_integrity(bars: list[dict]) -> list[QualityIssue]:
    """High >= max(open, close) y Low <= min(open, close)."""
    issues: list[QualityIssue] = []
    for i, bar in enumerate(bars):
        try:
            high, low, open_, close = (
                float(bar["high"]),
                float(bar["low"]),
                float(bar["open"]),
                float(bar["close"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(QualityIssue("campos_malformados", i, str(exc)))
            continue
        if high < max(open_, close):
            issues.append(QualityIssue("ohlc_high", i, f"high={high} < max(o,c)"))
        if low > min(open_, close):
            issues.append(QualityIssue("ohlc_low", i, f"low={low} > min(o,c)"))
    return issues


def detect_duplicates(bars: list[dict]) -> list[QualityIssue]:
    """Timestamps duplicados (posible gap o doble descarga)."""
    seen: Counter = Counter(bar.get("ts") for bar in bars)
    return [
        QualityIssue("ts_duplicado", i, f"ts={bar.get('ts')}")
        for i, bar in enumerate(bars)
        if seen[bar.get("ts")] > 1
    ]


def detect_timestamp_gaps(
    bars: list[dict], expected_seconds: int, tolerance_seconds: int = 0
) -> list[QualityIssue]:
    """Gaps superiores al intervalo esperado (más tolerancia)."""
    issues: list[QualityIssue] = []
    prev: object = None
    for i, bar in enumerate(bars):
        cur = bar.get("ts")
        if prev is not None and cur is not None:
            try:
                delta = _ts_delta_seconds(prev, cur)
                limit = expected_seconds + tolerance_seconds
                if delta > limit:
                    issues.append(
                        QualityIssue("gap", i, f"delta={delta}s > límite {limit}s")
                    )
            except (TypeError, ValueError):
                issues.append(QualityIssue("ts_invalido", i, f"ts={cur!r}"))
        prev = cur
    return issues


def summarize_quality(
    bars: list[dict],
    expected_interval_seconds: int | None = None,
    tolerance_seconds: int = 0,
) -> QualitySummary:
    """Ejecuta todas las comprobaciones y consolida el resultado."""
    summary = QualitySummary(rows=len(bars))
    missing = check_required_columns(bars)
    if missing:
        summary.issues.append(
            QualityIssue("columnas", 0, f"faltan columnas: {', '.join(missing)}")
        )

    summary.issues.extend(check_ohlc_integrity(bars))
    summary.issues.extend(detect_duplicates(bars))
    if expected_interval_seconds is not None:
        summary.issues.extend(
            detect_timestamp_gaps(bars, expected_interval_seconds, tolerance_seconds)
        )
    return summary


def _ts_delta_seconds(a, b) -> float:
    """Diferencia en segundos entre dos representaciones de timestamp.

    Los epoch timestamps son UTC por definicion: se parsean con tz=UTC para que
    el calculo NO dependa del timezone/DST de la maquina (de lo contrario un
    salto de horario local falsifica gaps de +-3600s en barras 1h).
    """
    from datetime import datetime, timezone

    def parse(value):
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            if value.isdigit():
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise TypeError(f"timestamp no soportado: {type(value)}")

    return (parse(b) - parse(a)).total_seconds()