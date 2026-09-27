"""Backoff determinista para reconexión (S3).

Sin aleatoriedad: la secuencia de espera es una función pura de la política y
del número de intento, de modo que reconexiones y tests sean reproducibles.
`BackoffPolicy.delay_for(attempt)` = `min(base * factor^(attempt-1), max)`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Política de espera exponencial acotada y determinista.

    Attributes:
        base_delay_seconds: espera del primer intento (attempt=1).
        factor: multiplicador por intento.
        max_delay_seconds: techo de la espera.
    """

    base_delay_seconds: float = 0.5
    factor: float = 2.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds debe ser > 0")
        if self.factor < 1:
            raise ValueError("factor debe ser >= 1")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds debe ser >= base_delay_seconds")

    def delay_for(self, attempt: int) -> float:
        """Espera (segundos) antes del intento `attempt` (1-indexado)."""
        if attempt < 1:
            raise ValueError("attempt debe ser >= 1")
        delay = self.base_delay_seconds * (self.factor ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def sequence(self, count: int) -> list[float]:
        """Genera la secuencia de esperas para `count` intentos."""
        if count < 0:
            raise ValueError("count debe ser >= 0")
        return [self.delay_for(i) for i in range(1, count + 1)]


__all__ = ["BackoffPolicy"]
