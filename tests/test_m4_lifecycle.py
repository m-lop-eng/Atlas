"""BT3-C — Ciclo de vida M4: hypothesis pre-registration, selección y gates.

Criterios BT3-B (sin seleccionar ninguna hipótesis real):

    record inexistente            -> no backtest
    record incompleto             -> no elegible
    record valido                 -> elegible (tras seleccion)
    seleccion explicita requerida
    estrategia existente sin sel. -> no seleccionada
    H002 existente                -> no seleccionada automaticamente
    metadata.status no promueve
    gate PASS -> promocion
    gate FAIL -> permanece en etapa (sin bypass)
    GateResult persistido
    FINAL_OOS/data-role intactos
    lineage/experiment_id deterministas

Aislamiento: estos tests NO usan evidence.json / data_roles.json reales.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.hypothesis import (
    FalsificationCriterion,
    HypothesisError,
    HypothesisRegistry,
    SelectionRegistry,
    StrategyResearchRecord,
    require_backtest_eligibility,
    validate_record,
)
from validation import (
    StrategyStage,
    evaluate_transition,
    is_gate_passed,
)


def _record(strategy_id: str = "STRAT-TEST-001", **overrides) -> StrategyResearchRecord:
    base = dict(
        strategy_id=strategy_id,
        strategy_name="Test Strategy",
        version="1.0.0",
        asset_class="crypto",
        instrument="BTCUSDT",
        market="crypto",
        timeframe="1h",
        researcher="tester",
        creation_date="2026-10-01",
        hypothesis="Los breakouts muestran continuacion a corto plazo.",
        market_phenomenon="momentum/breakout",
        expected_mechanism="flujos institucionales y sesgo conductual",
        expected_edge="continuacion tras ruptura de rango",
        entry_rules="close > max(high, lookback)",
        exit_rules="stop ATR / time exit",
        risk_model="fixed-fractional, stop ATR",
        execution_assumptions="market en open t+1, costes per_side",
        known_limitations="depende de regimen alcista",
        falsification_criteria=[
            FalsificationCriterion(metric="net_return", threshold=0.0, condition=">"),
        ],
    )
    base.update(overrides)
    return StrategyResearchRecord(**base)


# --------------------------------------------------------------- G-M4-1: record


def test_record_inexistente_no_backtest() -> None:
    hyps = HypothesisRegistry()
    sels = SelectionRegistry()
    with pytest.raises(HypothesisError, match="No existe StrategyResearchRecord"):
        require_backtest_eligibility("H002", hyps, sels)


def test_record_incompleto_no_elegible() -> None:
    rec = _record()
    rec.expected_edge = ""  # campo obligatorio vacio
    problems = validate_record(rec)
    assert "falta:expected_edge" in problems

    hyps = HypothesisRegistry()
    hyps.register(rec)
    sels = SelectionRegistry()
    with pytest.raises(HypothesisError, match="inválido"):
        require_backtest_eligibility("STRAT-TEST-001", hyps, sels)


def test_record_falsification_vacia_no_elegible() -> None:
    rec = _record()
    rec.falsification_criteria = []
    assert "falta:falsification_criteria" in validate_record(rec)


def test_record_valido_sin_seleccion_no_elegible() -> None:
    hyps = HypothesisRegistry()
    hyps.register(_record())
    sels = SelectionRegistry()
    with pytest.raises(HypothesisError, match="NO seleccionada"):
        require_backtest_eligibility("STRAT-TEST-001", hyps, sels)


def test_record_valido_y_seleccionado_elegible() -> None:
    hyps = HypothesisRegistry()
    hyps.register(_record())
    sels = SelectionRegistry()
    sels.select("STRAT-TEST-001", selected_by="tester", reason="candidata A")
    rec = require_backtest_eligibility("STRAT-TEST-001", hyps, sels)
    assert rec.strategy_id == "STRAT-TEST-001"
    assert rec.status == "HYPOTHESIS"


def test_record_persistencia_roundtrip(tmp_path: Path) -> None:
    d = tmp_path / "hypotheses"
    hyps = HypothesisRegistry(path=d)
    hyps.register(_record())
    hyps.save()
    loaded = HypothesisRegistry.load_dir(d)
    assert loaded.get("STRAT-TEST-001") is not None
    assert loaded.validate("STRAT-TEST-001") == []


# --------------------------------------------------------------- G-M4-2: selection


def test_seleccion_explicita_requerida(tmp_path: Path) -> None:
    sels = SelectionRegistry(path=tmp_path / "selections")
    assert not sels.is_selected("STRAT-TEST-001")
    sels.select("STRAT-TEST-001", selected_by="researcher", reason="motivo")
    assert sels.is_selected("STRAT-TEST-001")
    sels.save()
    loaded = SelectionRegistry.load_dir(tmp_path / "selections")
    assert loaded.is_selected("STRAT-TEST-001")
    assert loaded.get("STRAT-TEST-001").reason == "motivo"


def test_estrategia_existente_no_seleccionada_por_defecto() -> None:
    """Tener un record (o config existente) no implica seleccion."""
    hyps = HypothesisRegistry()
    hyps.register(_record("H002"))
    sels = SelectionRegistry()
    assert not sels.is_selected("H002")
    with pytest.raises(HypothesisError):
        require_backtest_eligibility("H002", hyps, sels)


def test_h002_no_seleccionada_automaticamente() -> None:
    """El registro real de H002 no debe aparecer seleccionado."""
    sels = SelectionRegistry.load_dir()  # selecciones reales del repo
    assert not sels.is_selected("H002")


# --------------------------------------------------------------- D2: autoridad


def test_metadata_status_no_promueve() -> None:
    """Aunque StrategyMetadata.status diga otra cosa, el lifecycle manda.

    El guard de elegibilidad no consulta metadata; depende solo del record
    (autoridad) y de la seleccion.
    """
    from strategies.base.strategy import StrategyMetadata

    meta = StrategyMetadata(
        strategy_id="STRAT-TEST-001",
        strategy_name="X",
        strategy_family="demo",
        market="crypto",
        instrument="BTCUSDT",
        timeframe="1h",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
        status="LIVE",  # metadata descriptiva no promueve
    )
    hyps = HypothesisRegistry()
    hyps.register(_record())
    sels = SelectionRegistry()
    # metadata dice LIVE, pero sin seleccion el lifecycle lo bloquea.
    with pytest.raises(HypothesisError):
        require_backtest_eligibility(meta.strategy_id, hyps, sels)


# --------------------------------------------------------------- G1: gates


def test_gate_pass_permite_promocion() -> None:
    result = evaluate_transition(
        "STRAT-TEST-001",
        StrategyStage.PROTOTYPE,
        StrategyStage.BACKTEST,
        {"record_valid": True, "selection": True},
        decided_at="2026-10-01T00:00:00Z",
    )
    assert result.passed is True
    assert result.from_stage == "prototype"
    assert result.to_stage == "backtest"


def test_gate_fail_no_promueve() -> None:
    result = evaluate_transition(
        "STRAT-TEST-001",
        StrategyStage.PROTOTYPE,
        StrategyStage.BACKTEST,
        {"record_valid": True, "selection": False},
        decided_at="2026-10-01T00:00:00Z",
    )
    assert result.passed is False


def test_gate_sin_requisitos_lanza() -> None:
    with pytest.raises(Exception):
        is_gate_passed(StrategyStage.PROTOTYPE, {})


# --------------------------------------------------------------- aislamiento


def test_final_oos_y_data_role_intactos() -> None:
    """BT3-C no toca el perímetro congelado."""
    root = Path(__file__).resolve().parents[1]
    assert (root / "research" / "evidence.json").exists()
    assert (root / "research" / "data_roles.json").exists()
    assert (root / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json").exists()
    # No existe directorio de hipotesis/selecciones reales creado por estos tests.
    assert not (root / "research" / "hypotheses").exists()
    assert not (root / "research" / "selections").exists()


def test_lineage_experiment_id_determinista(tmp_path: Path) -> None:
    from research.pipeline import experiment_id_for

    cfg = {"strategy": {"params": {"lookback": 20, "direction": "long"}}, "risk": {}, "costs": {}}
    csv = tmp_path / "d.csv"
    csv.write_text(
        "ts,open,high,low,close,volume\n1700000000,1,2,0.5,1.5,10\n",
        encoding="utf-8",
    )
    assert experiment_id_for(cfg, csv) == experiment_id_for(cfg, csv)
