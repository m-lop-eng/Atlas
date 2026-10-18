"""ATLAS-MT5-ACCOUNTING-06E: resolver canonico de simbolos Atlas <-> MT5.

Contrato EXPLICITO y verificado para traducir entre el identificador de
instrumento de Atlas y el simbolo de MT5. NO usa heuristica alguna (ni sufijos,
ni prefijos, ni mayusculas, ni coincidencias aproximadas): solo un mapping
configurado y exacto.

Se construye validando el mapping; en particular rechaza mappings NO INYECTIVOS
(dos instrumentos Atlas al mismo simbolo MT5), de modo que el diccionario inverso
no pueda colapsar correspondencias en silencio. Un simbolo ausente NO produce una
identidad inferida: `to_mt5`/`to_atlas` devuelven `None`.

Vive en la capa `brokers` como modulo de dominio MT5 compartido, para que tanto el
adaptador como la logica de reconciliacion (capa `execution`) reutilicen UN solo
algoritmo sin crear dependencias circulares (el modulo no importa `execution`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

__all__ = ["MT5SymbolMappingError", "MT5SymbolResolver"]


class MT5SymbolMappingError(ValueError):
    """Mapping Atlas<->MT5 ausente, mal formado, ambiguo o no inyectivo."""


def _require_symbol(value: object, field: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise MT5SymbolMappingError(f"{field} debe ser str no vacio")
    if value == "" or value != value.strip():
        raise MT5SymbolMappingError(
            f"{field} invalido (vacio o con espacios): {value!r}"
        )
    return value


def _validate_mapping(
    mapping: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Valida un mapping Atlas->MT5 y deriva el inverso de forma inyectiva.

    Rechaza entradas vacias/mal formadas y simbolos MT5 repetidos. NOTA: las
    claves Atlas duplicadas ya no pueden detectarse si el llamante entrega un
    `dict` (Python ya las colapso); este contrato NO afirma detectarlas.
    """
    if not isinstance(mapping, Mapping):
        raise MT5SymbolMappingError("symbol_map debe ser un mapping")
    atlas_to_mt5: dict[str, str] = {}
    mt5_to_atlas: dict[str, str] = {}
    for atlas, mt5 in mapping.items():
        _require_symbol(atlas, "atlas_instrument")
        _require_symbol(mt5, "mt5_symbol")
        if atlas in atlas_to_mt5:
            raise MT5SymbolMappingError(f"instrumento Atlas duplicado: {atlas!r}")
        if mt5 in mt5_to_atlas:
            raise MT5SymbolMappingError(
                "mapping no inyectivo: el simbolo MT5 "
                f"{mt5!r} esta asignado a mas de un instrumento Atlas"
            )
        atlas_to_mt5[atlas] = mt5
        mt5_to_atlas[mt5] = atlas
    return atlas_to_mt5, mt5_to_atlas


class MT5SymbolResolver:
    """Traduccion exacta y bidireccional Atlas <-> MT5 (mapeo inyectivo).

    UNICA fuente de verdad: el mapping explicito Atlas->MT5. El inverso se DERIVA
    de el, de modo que es IMPOSIBLE construir una instancia con mapas directo e
    inverso contradictorios. Toda construccion valida el contrato (no existe una
    via publica que lo eluda).
    """

    __slots__ = ("_atlas_to_mt5", "_mt5_to_atlas")

    def __init__(self, mapping: Mapping[str, str]) -> None:
        atlas_to_mt5, mt5_to_atlas = _validate_mapping(mapping)
        self._atlas_to_mt5: Mapping[str, str] = MappingProxyType(atlas_to_mt5)
        self._mt5_to_atlas: Mapping[str, str] = MappingProxyType(mt5_to_atlas)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> "MT5SymbolResolver":
        """Fabrica validada (delega en el constructor, que tambien valida)."""
        return cls(mapping)

    @property
    def atlas_to_mt5(self) -> Mapping[str, str]:
        return self._atlas_to_mt5

    @property
    def mt5_to_atlas(self) -> Mapping[str, str]:
        return self._mt5_to_atlas

    def to_mt5(self, atlas_instrument: object) -> str | None:
        """Simbolo MT5 para un instrumento Atlas, o `None` si no esta mapeado."""
        if not isinstance(atlas_instrument, str):
            return None
        return self._atlas_to_mt5.get(atlas_instrument)

    def to_atlas(self, mt5_symbol: object) -> str | None:
        """Instrumento Atlas para un simbolo MT5, o `None` si no esta mapeado."""
        if not isinstance(mt5_symbol, str):
            return None
        return self._mt5_to_atlas.get(mt5_symbol)
