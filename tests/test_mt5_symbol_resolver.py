"""ATLAS-MT5-ACCOUNTING-06E: tests del resolver canonico de simbolos Atlas<->MT5.

Offline, sin MT5 real. Cubre validacion del mapping, conversiones exactas en ambos
sentidos, ausencia de heuristicas y la canonicalizacion del adaptador.
"""

from __future__ import annotations

import pytest

from brokers.mt5_symbols import MT5SymbolMappingError, MT5SymbolResolver
from tests.test_mt5_execution_foundation import INSTRUMENT, _adapter, _fake


class TestResolverConstruction:
    def test_valid_mapping_both_directions(self):
        r = MT5SymbolResolver.from_mapping({"EURUSD": "EURUSD.a", "XAUUSD": "GOLD"})
        assert r.to_mt5("EURUSD") == "EURUSD.a"
        assert r.to_mt5("XAUUSD") == "GOLD"
        assert r.to_atlas("EURUSD.a") == "EURUSD"
        assert r.to_atlas("GOLD") == "XAUUSD"

    def test_suffix_alias_only_via_explicit_mapping(self):
        r = MT5SymbolResolver.from_mapping({"EURUSD": "EURUSD.raw"})
        assert r.to_atlas("EURUSD.raw") == "EURUSD"
        # Sin heuristica de sufijos: el simbolo sin mapping no se infiere.
        assert r.to_atlas("EURUSD") is None
        assert r.to_atlas("EURUSD.") is None

    def test_duplicate_mt5_symbol_rejected(self):
        with pytest.raises(MT5SymbolMappingError):
            MT5SymbolResolver.from_mapping({"EURUSD": "X", "GBPUSD": "X"})

    def test_empty_mapping_is_valid_and_infers_nothing(self):
        r = MT5SymbolResolver.from_mapping({})
        assert r.to_mt5("EURUSD") is None
        assert r.to_atlas("EURUSD") is None

    @pytest.mark.parametrize(
        "mapping",
        [
            {"": "X"},
            {"EURUSD": ""},
            {" EURUSD": "X"},
            {"EURUSD": " X"},
            {1: "X"},
            {"EURUSD": 2},
            {"EURUSD": None},
        ],
    )
    def test_malformed_mapping_rejected(self, mapping):
        with pytest.raises(MT5SymbolMappingError):
            MT5SymbolResolver.from_mapping(mapping)

    def test_non_mapping_rejected(self):
        with pytest.raises(MT5SymbolMappingError):
            MT5SymbolResolver.from_mapping([("EURUSD", "EURUSD")])  # type: ignore[arg-type]


class TestResolverLookups:
    def setup_method(self, _):
        self.r = MT5SymbolResolver.from_mapping({"EURUSD": "EURUSD", "GBPUSD": "BP"})

    def test_unconfigured_returns_none_both_directions(self):
        assert self.r.to_mt5("USDJPY") is None
        assert self.r.to_atlas("JPY") is None

    def test_non_string_inputs_return_none(self):
        assert self.r.to_mt5(None) is None
        assert self.r.to_mt5(123) is None
        assert self.r.to_atlas(None) is None
        assert self.r.to_atlas(True) is None

    def test_no_case_or_prefix_heuristics(self):
        assert self.r.to_atlas("eurusd") is None  # case-sensitive
        assert self.r.to_atlas("BP.x") is None     # no prefix stripping otherwise


class TestConstructorHardening:
    def test_public_constructor_single_mapping_valid(self):
        r = MT5SymbolResolver({"EURUSD": "EURUSD.a", "XAU": "GOLD"})
        assert r.to_mt5("EURUSD") == "EURUSD.a"
        assert r.to_atlas("GOLD") == "XAU"
        assert MT5SymbolResolver.from_mapping({"EURUSD": "EURUSD.a"}).to_atlas(
            "EURUSD.a"
        ) == "EURUSD"

    def test_contradictory_two_map_construction_unavailable(self):
        # Ya no existe una via publica para pasar dos mapas independientes que
        # puedan contradecirse: el constructor acepta UN solo mapping.
        with pytest.raises(TypeError):
            MT5SymbolResolver({"A": "X"}, {"Z": "A"})  # type: ignore[call-arg]

    def test_inverse_maps_derived_and_consistent(self):
        r = MT5SymbolResolver({"A": "X", "B": "Y"})
        assert dict(r.atlas_to_mt5) == {"A": "X", "B": "Y"}
        assert dict(r.mt5_to_atlas) == {"X": "A", "Y": "B"}

    def test_exposed_maps_cannot_be_mutated(self):
        r = MT5SymbolResolver({"A": "X"})
        with pytest.raises(TypeError):
            r.atlas_to_mt5["A"] = "Z"  # type: ignore[index]
        with pytest.raises(TypeError):
            r.mt5_to_atlas["X"] = "B"  # type: ignore[index]
        with pytest.raises(TypeError):
            del r.atlas_to_mt5["A"]  # type: ignore[attr-defined]

    def test_public_constructor_validates_non_injective(self):
        with pytest.raises(MT5SymbolMappingError):
            MT5SymbolResolver({"A": "X", "B": "X"})

    @pytest.mark.parametrize("mapping", [{"": "X"}, {"A": ""}, {"A": None}, {"A": 3}])
    def test_public_constructor_validates_symbols(self, mapping):
        with pytest.raises(MT5SymbolMappingError):
            MT5SymbolResolver(mapping)

    def test_no_attribute_slots_patch(self):
        r = MT5SymbolResolver({"A": "X"})
        with pytest.raises(AttributeError):
            r.extra = "nope"  # type: ignore[attr-defined]


class TestAdapterCanonicalization:
    def test_adapter_exposes_resolver(self):
        adapter = _adapter(_fake())
        assert adapter.symbol_resolver.to_mt5(INSTRUMENT) == INSTRUMENT

    def test_position_carries_provenance_and_canonical_instrument(self):
        fake = _fake()
        fake.add_position(1001, symbol=INSTRUMENT)
        adapter = _adapter(fake)
        pos = adapter.get_positions()[0]
        assert pos.mt5_symbol == INSTRUMENT          # procedencia preservada
        assert pos.instrument == INSTRUMENT          # canonicalizado

    def test_unmapped_position_symbol_is_not_invented(self):
        fake = _fake()
        fake.add_position(2002, symbol="EURUSD.off")
        adapter = _adapter(fake)
        pos = adapter.get_positions()[0]
        assert pos.mt5_symbol == "EURUSD.off"
        assert pos.instrument is None                # fail-closed, sin invencion
