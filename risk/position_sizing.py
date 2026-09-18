"""Position sizing basado en riesgo (03_RISK_MANAGEMENT.md §6).

Modelo inicial: Fixed Fractional / Risk Based.

    Position Size = Maximum Monetary Risk / Monetary Loss Per Unit at Stop

El tamaño se deriva del riesgo definido, no de una cantidad arbitraria.
"""

from __future__ import annotations

from decimal import Decimal


class PositionSizingError(ValueError):
    """Error de dimensionamiento: el riesgo no puede materializarse."""


def maximum_monetary_risk(equity: float, risk_fraction: float) -> float:
    """Capital máximo arriesgable en una operación.

    Args:
        equity: Capital de cuenta actual.
        risk_fraction: Fracción de riesgo (default 0.25% = 0.0025).

    Raises:
        PositionSizingError: si los argumentos no permiten calcular el riesgo.
    """
    if equity is None or equity <= 0:
        raise PositionSizingError(f"equity debe ser positivo, recibido {equity}")
    if risk_fraction is None or not 0.0 < risk_fraction < 1.0:
        raise PositionSizingError(
            f"risk_fraction debe estar en (0, 1), recibido {risk_fraction}"
        )
    return equity * risk_fraction


def position_size_from_stop(
    equity: float,
    risk_fraction: float,
    stop_distance: float,
    value_per_unit: float,
) -> float:
    """Cantidad de unidades (contratos/lotes/shares) para respetar el riesgo.

    Args:
        equity: Capital de cuenta actual.
        risk_fraction: Fracción de riesgo por operación.
        stop_distance: Distancia entre entrada y stop en unidades de precio (>0).
        value_per_unit: P&L monetario por unidad de precio (p. ej. $50 por punto).

    Raises:
        PositionSizingError: si la distancia de stop o el valor por unidad
            son inválidos, o el riesgo es demasiado pequeño para operar.
    """
    risk_cap = maximum_monetary_risk(equity, risk_fraction)
    stop_distance = float(Decimal(str(stop_distance)))
    value_per_unit = float(Decimal(str(value_per_unit)))

    if stop_distance <= 0:
        raise PositionSizingError(
            f"stop_distance debe ser positivo, recibido {stop_distance}"
        )
    if value_per_unit <= 0:
        raise PositionSizingError(
            f"value_per_unit debe ser positivo, recibido {value_per_unit}"
        )

    loss_per_unit = stop_distance * value_per_unit
    quantity = risk_cap / loss_per_unit

    if quantity <= 0:
        raise PositionSizingError("El riesgo calculado no permite operar")
    return quantity


def position_size_with_stop_at(
    equity: float,
    risk_fraction: float,
    entry_price: float,
    stop_price: float,
    value_per_unit: float,
    direction: str = "LONG",
) -> float:
    """Cantidad para un stop dado como precio absoluto (mayor claridad).

    Los stops en dirección contraria a la entrada (stop de protección) tienen
    distancia positiva; se normaliza internamente.
    """
    if direction not in ("LONG", "SHORT"):
        raise PositionSizingError(f"direction inválida: {direction}")
    if entry_price <= 0 or stop_price <= 0:
        raise PositionSizingError("entry_price y stop_price deben ser positivos")

    if direction == "LONG" and stop_price >= entry_price:
        raise PositionSizingError("En LONG el stop debe estar por debajo de la entrada")
    if direction == "SHORT" and stop_price <= entry_price:
        raise PositionSizingError("En SHORT el stop debe estar por encima de la entrada")

    distance = abs(entry_price - stop_price)
    return position_size_from_stop(
        equity=equity,
        risk_fraction=risk_fraction,
        stop_distance=distance,
        value_per_unit=value_per_unit,
    )