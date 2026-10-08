"""Normalizacion de cantidad al lattice del instrumento (MULTI-ASSET-03).

Contrato (congelado en MULTI-ASSET-02, implementado aqui):

    target_quantity -> floor_to_step -> min/max validation -> effective_quantity

Reglas:
  * sin `quantity_step`: solo min/max (reject si fuera de rango);
  * con `quantity_step`: FLOOR hacia abajo (nunca round-up);
  * el floor se calcula respecto de cero (`floor(target/step)*step`) y luego se
    exige `effective >= quantity_min`; si el floor cae por debajo del minimo se
    RECHAZA (no se sube a min);
  * target > quantity_max => reject (sin cap silencioso);
  * NUNCA aumenta la cantidad pedida (por tanto nunca aumenta el riesgo);
  * determinista y exacto via `Decimal` (convencion del dominio de sizing).

Esta operacion es de SIZING (riesgo), no un parche del broker adapter: el
adapter sigue validando sus propias constraints.
"""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from execution.instrument import InstrumentSpec


class QuantityError(ValueError):
    """Cantidad no valida para el lattice del instrumento (fail-closed)."""


def _dec(value: float, *, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise QuantityError(f"{name} debe ser numerico, recibido {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise QuantityError(f"{name} no numerico: {value!r}") from None
    if not math.isfinite(f):
        raise QuantityError(f"{name} no finito: {value!r}")
    try:
        return Decimal(str(f))
    except InvalidOperation:  # pragma: no cover - defensivo
        raise QuantityError(f"{name} no convertible a Decimal: {value!r}") from None


def normalize_quantity(spec: InstrumentSpec, target_quantity: float) -> float:
    """Devuelve la cantidad efectiva (floor) o lanza `QuantityError`.

    No muta `target_quantity`. Nunca redondea hacia arriba.
    """
    target = _dec(target_quantity, name="target_quantity")
    if target <= 0:
        raise QuantityError(f"target_quantity debe ser > 0, recibido {target_quantity!r}")

    lo = None if spec.quantity_min is None else _dec(spec.quantity_min, name="quantity_min")
    step = None if spec.quantity_step is None else _dec(
        spec.quantity_step, name="quantity_step"
    )
    hi = None if spec.quantity_max is None else _dec(spec.quantity_max, name="quantity_max")

    if lo is not None and target < lo:
        raise QuantityError(
            f"target {target} < quantity_min {lo}"
        )
    if hi is not None and target > hi:
        raise QuantityError(
            f"target {target} > quantity_max {hi} (sin cap silencioso)"
        )

    effective = target
    if step is not None:
        steps = (target / step).to_integral_value(rounding=ROUND_FLOOR)
        if steps < 0:  # pragma: no cover - target>0 => steps>=0
            steps = Decimal(0)
        effective = steps * step

    if lo is not None and effective < lo:
        raise QuantityError(
            f"floor {effective} < quantity_min {lo} (reject, sin round-up)"
        )
    if hi is not None and effective > hi:  # pragma: no cover - effective<=target<=hi
        raise QuantityError(f"floor {effective} > quantity_max {hi}")
    if effective <= 0:
        raise QuantityError(f"cantidad efectiva no positiva: {effective}")

    return float(effective)


__all__ = ["QuantityError", "normalize_quantity"]
