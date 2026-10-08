"""InstrumentSpec (MULTI-ASSET-03): representacion economica core de instrumento.

Contrato economico congelado (MULTI-ASSET-02):

    exposure_quote   = quantity * contract_size * price
    pnl_quote        = quantity * contract_size * Δprice
    exposure_account = exposure_quote * FX(quote_currency -> account_currency)

donde `quantity` son unidades de ejecucion segun el contrato del instrumento y
`contract_size` es el multiplicador economico (por unidad de precio y unidad de
quantity). `quote_currency == account_currency` => FX = 1 por equivalencia
monetaria explicita; distinto => conversion explicita requerida (sin FX =>
fail-closed). NUNCA se asume `quantity * price` universal ni FX = 1 por defecto.

Aqui vive SOLO la identidad + economia + reglas de cantidad del core. La
metadata de ejecucion del broker (digits, filling mode, ticket, execution mode,
tick value/contract metadata del broker) permanece en el adapter
(`brokers.mt5.MT5SymbolSpec`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class InstrumentSpecError(ValueError):
    """InstrumentSpec invalido o inconsistente (fail-closed)."""


class ExposureError(ValueError):
    """Parametros de exposicion invalidos (precio/fx no finito, etc.)."""


class FxMissingError(ExposureError):
    """Falta FX explicita entre quote_currency y account_currency (fail-closed)."""


class AssetClass(str, Enum):
    FOREX = "FOREX"
    METAL = "METAL"
    INDEX = "INDEX"
    COMMODITY = "COMMODITY"
    CRYPTO = "CRYPTO"
    EQUITY = "EQUITY"
    OTHER = "OTHER"


def _finite(value: object, *, name: str) -> float:
    if value is None or isinstance(value, bool):
        raise InstrumentSpecError(f"{name} debe ser numerico, recibido {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise InstrumentSpecError(f"{name} no numerico: {value!r}") from None
    if not math.isfinite(f):
        raise InstrumentSpecError(f"{name} no finito: {value!r}")
    return f


def _finite_positive(value: object, *, name: str) -> float:
    f = _finite(value, name=name)
    if f <= 0.0:
        raise InstrumentSpecError(f"{name} debe ser > 0, recibido {value!r}")
    return f


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Identidad + economia + reglas de cantidad de un instrumento (core).

    Attributes:
        atlas_instrument_id: identidad Atlas estable y portable entre brokers.
        asset_class: clasificacion (validacion/config/reporting; NO decide la
            formula economica, que es general).
        contract_size: multiplicador economico (por unidad de precio y unidad
            de quantity). Canonico.
        quote_currency: moneda en la que se expresan precio/exposicion.
        base_currency: moneda base (si aplica; informativo).
        quantity_min/step/max: restricciones del lattice de cantidad (opcionales;
            el broker suele ser la fuente autoritativa).
    """

    atlas_instrument_id: str
    asset_class: AssetClass = AssetClass.OTHER
    contract_size: float = 1.0
    quote_currency: str = "USD"
    base_currency: str | None = None
    quantity_min: float | None = None
    quantity_step: float | None = None
    quantity_max: float | None = None

    def __post_init__(self) -> None:
        if not self.atlas_instrument_id or not str(self.atlas_instrument_id).strip():
            raise InstrumentSpecError("atlas_instrument_id no puede estar vacio")
        if not isinstance(self.asset_class, AssetClass):
            try:
                object.__setattr__(self, "asset_class", AssetClass(str(self.asset_class)))
            except ValueError:
                raise InstrumentSpecError(
                    f"asset_class invalida: {self.asset_class!r}"
                ) from None
        object.__setattr__(
            self, "contract_size", _finite_positive(self.contract_size, name="contract_size")
        )
        for name in ("quote_currency",):
            value = getattr(self, name)
            if not value or not str(value).strip():
                raise InstrumentSpecError(f"{name} no puede estar vacia")
        if self.base_currency is not None:
            if not str(self.base_currency).strip():
                raise InstrumentSpecError("base_currency no puede ser vacia")
        lo, step, hi = self.quantity_min, self.quantity_step, self.quantity_max
        if lo is not None:
            object.__setattr__(self, "quantity_min", _finite(lo, name="quantity_min"))
            if self.quantity_min < 0.0:
                raise InstrumentSpecError("quantity_min debe ser >= 0")
        if step is not None:
            object.__setattr__(
                self, "quantity_step", _finite_positive(step, name="quantity_step")
            )
        if hi is not None:
            object.__setattr__(self, "quantity_max", _finite(hi, name="quantity_max"))
            if self.quantity_max <= 0.0:
                raise InstrumentSpecError("quantity_max debe ser > 0")
        if lo is not None and hi is not None and hi < lo:
            raise InstrumentSpecError("quantity_max debe ser >= quantity_min")

    @staticmethod
    def legacy(
        instrument: str,
        *,
        point_value: float = 1.0,
        currency: str = "USD",
    ) -> "InstrumentSpec":
        """Spec implicito para el paper/backtest LEGACY.

        `point_value` legacy es un ALIAS de `contract_size` (mismo concepto). El
        path legacy explicito: contract_size = point_value, quote = account.
        """
        return InstrumentSpec(
            atlas_instrument_id=str(instrument),
            asset_class=AssetClass.OTHER,
            contract_size=_finite_positive(point_value, name="point_value"),
            quote_currency=str(currency),
        )

    def to_dict(self) -> dict:
        return {
            "atlas_instrument_id": self.atlas_instrument_id,
            "asset_class": self.asset_class.value,
            "contract_size": self.contract_size,
            "quote_currency": self.quote_currency,
            "base_currency": self.base_currency,
            "quantity_min": self.quantity_min,
            "quantity_step": self.quantity_step,
            "quantity_max": self.quantity_max,
        }

    @staticmethod
    def from_dict(data: dict) -> "InstrumentSpec":
        try:
            return InstrumentSpec(
                atlas_instrument_id=str(data["atlas_instrument_id"]),
                asset_class=AssetClass(str(data.get("asset_class", "OTHER"))),
                contract_size=float(data.get("contract_size", 1.0)),
                quote_currency=str(data.get("quote_currency", "USD")),
                base_currency=data.get("base_currency"),
                quantity_min=data.get("quantity_min"),
                quantity_step=data.get("quantity_step"),
                quantity_max=data.get("quantity_max"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InstrumentSpecError(f"spec corrupta: {exc}") from exc


def _finite_exposure(value: object, *, name: str) -> float:
    if value is None or isinstance(value, bool):
        raise ExposureError(f"{name} debe ser numerico, recibido {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ExposureError(f"{name} no numerico: {value!r}") from None
    if not math.isfinite(f):
        raise ExposureError(f"{name} no finito: {value!r}")
    return f


def exposure_quote(spec: InstrumentSpec, quantity: float, price: float) -> float:
    """Exposicion en la moneda de cotizacion del instrumento.

    exposure_quote = quantity * contract_size * price.
    """
    q = _finite_exposure(quantity, name="quantity")
    p = _finite_exposure(price, name="price")
    if p <= 0.0:
        raise ExposureError(f"price debe ser > 0, recibido {price!r}")
    return q * spec.contract_size * p


def exposure_account(
    spec: InstrumentSpec,
    quantity: float,
    price: float,
    *,
    account_currency: str,
    fx_rate: float | None = None,
) -> float:
    """Exposicion convertida a la moneda de cuenta.

    `quote_currency == account_currency` => FX = 1 por equivalencia monetaria
    explicita (no se requiere `fx_rate`). Distinto => `fx_rate` obligatorio;
    ausente => `FxMissingError` (fail-closed). NUNCA FX = 1 por defecto.
    """
    if not account_currency or not str(account_currency).strip():
        raise ExposureError("account_currency no puede estar vacia")
    quote = exposure_quote(spec, quantity, price)
    if spec.quote_currency == account_currency:
        return quote
    if fx_rate is None:
        raise FxMissingError(
            f"FX requerida {spec.quote_currency}->{account_currency}: no disponible"
        )
    rate = _finite_exposure(fx_rate, name="fx_rate")
    if rate <= 0.0:
        raise ExposureError(f"fx_rate debe ser > 0, recibido {fx_rate!r}")
    return quote * rate


__all__ = [
    "AssetClass",
    "ExposureError",
    "FxMissingError",
    "InstrumentSpec",
    "InstrumentSpecError",
    "exposure_account",
    "exposure_quote",
]
