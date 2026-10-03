"""BT2 — Cierre de Milestone 2: oracle determinista + anti-lookahead.

Endurece la EVIDENCIA del `BacktestEngine` existente (no lo reconstruye):

  * BT2-A: oracle independiente y explicito (valores calculados a mano).
  * BT2-B: causalidad de indicadores (futuro no altera decisiones pasadas).
  * BT2-C: proteccion contra OHLC futuro (high/low extremos posteriores).
  * BT2-D: entrada en el open de t+1 (no en t).
  * BT2-E: stop/exit causal (solo informacion de la vela en curso).
  * BT2-F: determinismo completo (dos ejecuciones identicas).

Convencion determinista intrabar documentada (BT2-E): si dentro de la misma
vela `low <= stop` (LONG), la salida se marca al `stop` con razon "STOP" y
los costes se aplican sobre ese precio. No se inventa precision tick-a-tick.
"""

from __future__ import annotations

from typing import Any

import pytest

from backtesting.engine import BacktestEngine, ExecutionCosts
from execution.order_manager import OrderManager
from reporting.metrics import calculate_metrics
from risk.engine import RiskEngine
from risk.types import RiskConfig
from strategies.base.signal import Action, Signal
from strategies.base.strategy import BaseStrategy, StrategyMetadata

TS = "2026-01-01T00:00:00+00:00"


def _meta() -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id="bt2-demo",
        strategy_name="BT2 Demo",
        strategy_family="demo",
        market="crypto",
        instrument="BTCUSDT",
        timeframe="1h",
        version="1.0.0",
        parameter_set_version="p1",
        data_version="d1",
    )


class LongOnCloseAbove(BaseStrategy):
    """LONG cuando el cierre de la vela en curso supera `threshold`.

    Stop fijo a `stop_distance` por debajo del cierre de la vela de decision.
    Es determinista y solo mira el ultimo elemento recibido (`data[-1]`), lo
    que hace explicito el punto de decision causal.
    """

    def __init__(self, threshold: float, stop_distance: float = 10.0) -> None:
        self.metadata = _meta()
        self._threshold = threshold
        self._stop_distance = stop_distance

    def generate_signal(self, data) -> Signal:
        last = float(data[-1]["close"])
        action = Action.LONG if last > self._threshold else Action.NO_TRADE
        return Signal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=self.metadata.instrument,
            action=action,
            reference_price=last,
            stop_price=last - self._stop_distance,
        )


def _engine(**cost_kwargs) -> BacktestEngine:
    """Sizing estable: risk_cap=1.0, stop distance 10, pv 1 -> qty 0.1."""
    defaults = dict(initial_equity=100_000.0)
    defaults.update(cost_kwargs)
    return BacktestEngine(
        costs=ExecutionCosts(**defaults),
        risk_engine=RiskEngine(config=RiskConfig(risk_per_trade=1e-5)),
        order_manager=OrderManager(),
        point_value=1.0,
        min_size=0.05,
    )


def _bar(open_, high, low, close, ts) -> dict[str, Any]:
    return {
        "ts": str(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


# ---------------------------------------------------------------------------
# BT2-A — Oracle determinista independiente (valores calculados a mano)
# ---------------------------------------------------------------------------


class TestOracleDeterminista:
    """Escenario: senal en t=1 (close 102 > 100) -> entrada open t=2 (=102).

    Stop = close(t=1) - 10 = 92. Salida por STOP en t=4 (low 90 < 92).
    Sizing: risk_cap = 100000*1e-5 = 1.0; distancia stop 10; pv 1 -> qty 0.1.
    per_side = 0.5 (slippage) + 0.25 (commission) = 0.75.
    Entrada LONG:   entry_raw=102, entry=102.75
    Salida  STOP:   exit_raw=92,  exit=91.25
    gross = (92-102)*0.1 = -1.0
    costs = 0.1 * 2 * 0.75 = 0.15
    net   = -1.0 - 0.15 = -1.15
    final_equity = 100000 - 1.15 = 99998.85
    """

    BARS = [
        _bar(100, 101, 99, 100, 1000),   # t=0: close 100 (no signal)
        _bar(101, 103, 100, 102, 2000),  # t=1: close 102 > 100 -> LONG (stop 92)
        _bar(102, 105, 101, 104, 3000),  # t=2: fill open 102
        _bar(104, 106, 103, 105, 4000),  # t=3: no stop (low 103 > 92)
        _bar(105, 106, 90, 91, 5000),    # t=4: low 90 <= 92 -> STOP @ 92
    ]

    def test_oracle_trade_fields(self) -> None:
        engine = _engine(
            slippage_per_side=0.5,
            commission_per_unit=0.25,
            spread_ticks=0.0,
            tick_size=0.0,
        )
        result = engine.run(LongOnCloseAbove(threshold=100.0), self.BARS)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t["side"] == "LONG"
        assert t["quantity"] == 0.1
        assert t["entry_index"] == 2
        assert t["entry_time"] == "3000"
        assert t["entry_raw"] == 102.0
        assert t["entry_price"] == 102.75
        assert t["exit_index"] == 4
        assert t["exit_time"] == "5000"
        assert t["exit_reason"] == "STOP"
        assert t["exit_raw"] == 92.0
        assert t["exit_price"] == 91.25
        assert t["gross_pnl"] == -1.0
        assert t["costs"] == pytest.approx(0.15)
        assert t["net_pnl"] == pytest.approx(-1.15)
        assert t["pnl"] == pytest.approx(-1.15)

    def test_oracle_equity_and_curve(self) -> None:
        engine = _engine(
            slippage_per_side=0.5, commission_per_unit=0.25
        )
        result = engine.run(LongOnCloseAbove(threshold=100.0), self.BARS)
        assert result.initial_equity == 100_000.0
        assert result.final_equity == pytest.approx(99_998.85)
        assert result.net_return == pytest.approx(-1.15 / 100_000.0)
        # Curva: punto inicial + un punto por barra (5 barras).
        assert len(result.equity_curve) == len(self.BARS) + 1
        assert result.equity_curve[0] == 100_000.0
        assert result.equity_curve[-1] == pytest.approx(99_998.85)

    def test_oracle_metrics(self) -> None:
        engine = _engine(slippage_per_side=0.5, commission_per_unit=0.25)
        result = engine.run(LongOnCloseAbove(threshold=100.0), self.BARS)
        m = calculate_metrics(result)
        assert m.num_trades == 1
        assert m.expectancy == pytest.approx(-1.15)
        assert m.win_rate == 0.0
        assert m.equity_final == pytest.approx(99_998.85)
        # `calculate_metrics` redondea net_return a 6 decimales (convencion).
        assert m.net_return == round(-1.15 / 100_000.0, 6)
        assert m.costs_total == pytest.approx(0.15)
        assert m.max_drawdown > 0.0
        # Sin ganancias: gross_wins=0, gross_losses>0 -> profit_factor 0.0.
        assert m.profit_factor == 0.0

    def test_oracle_sin_costes_equity(self) -> None:
        """Mismo escenario sin costes: gross = net = -1.0."""
        engine = _engine()
        result = engine.run(LongOnCloseAbove(threshold=100.0), self.BARS)
        assert result.trades[0]["gross_pnl"] == -1.0
        assert result.trades[0]["costs"] == 0.0
        assert result.trades[0]["net_pnl"] == -1.0
        assert result.final_equity == 99_999.0


# ---------------------------------------------------------------------------
# BT2-B — Causalidad de indicadores
# ---------------------------------------------------------------------------


class TestCausalidadIndicadores:
    def test_futuro_no_altera_decision_pasada(self) -> None:
        """Dos datasets identicos hasta t; difieren despues.

        Las decisiones correspondientes a t deben ser identicas: el motor solo
        pasa `bars[:i+1]` a la estrategia.
        """
        base = [
            _bar(100, 101, 99, 100, 1000),
            _bar(101, 103, 100, 102, 2000),  # decision LONG en t=1
        ]
        future_up = base + [
            _bar(102, 130, 101, 129, 3000),  # futuro alcista extremo
            _bar(129, 135, 128, 134, 4000),
        ]
        future_down = base + [
            _bar(102, 103, 60, 61, 3000),    # futuro bajista extremo
            _bar(61, 62, 20, 21, 4000),
        ]
        r_up = _engine().run(LongOnCloseAbove(threshold=100.0), future_up)
        r_down = _engine().run(LongOnCloseAbove(threshold=100.0), future_down)

        # La ENTRADA (decision en t=1, fill en open t=2) es identica.
        e_up, e_down = r_up.trades[0], r_down.trades[0]
        assert e_up["entry_time"] == e_down["entry_time"] == "3000"
        assert e_up["entry_raw"] == e_down["entry_raw"] == 102.0
        assert e_up["side"] == e_down["side"] == "LONG"
        assert e_up["quantity"] == e_down["quantity"] == 0.1


# ---------------------------------------------------------------------------
# BT2-C — Proteccion contra OHLC futuro
# ---------------------------------------------------------------------------


class TestOHLCFuturo:
    def test_high_low_futuro_no_afecta_entrada_ni_stop(self) -> None:
        """Una vela futura con high/low extremos no cambia lo anterior."""
        past = [
            _bar(100, 101, 99, 100, 1000),
            _bar(101, 103, 100, 102, 2000),  # LONG -> stop 92
            _bar(102, 103, 101, 102, 3000),  # fill open 102
        ]
        normal = past + [_bar(102, 103, 101, 102, 4000)]
        extreme = past + [_bar(102, 9999, -9999, 102, 4000)]  # high/low absurdos

        r_normal = _engine().run(LongOnCloseAbove(threshold=100.0), normal)
        r_extreme = _engine().run(LongOnCloseAbove(threshold=100.0), extreme)

        # La entrada no cambia.
        assert r_normal.trades[0]["entry_raw"] == r_extreme.trades[0]["entry_raw"]
        assert r_normal.trades[0]["entry_time"] == r_extreme.trades[0]["entry_time"]
        # El stop (92) se evalua SOLO sobre la vela en curso; en `normal` la
        # vela 4 (low 101) no lo toca, en `extreme` si (low -9999) -> no se usa
        # el high/low de una vela para reescribir el pasado, sino la propia vela.
        assert r_normal.trades[0]["exit_reason"] == "END"
        assert r_extreme.trades[0]["exit_reason"] == "STOP"


# ---------------------------------------------------------------------------
# BT2-D — Entrada en el open de t+1
# ---------------------------------------------------------------------------


class TestEntradaSiguienteApertura:
    def test_no_ejecuta_en_la_misma_vela_de_la_senal(self) -> None:
        bars = [
            _bar(100, 101, 99, 100, 1000),   # no signal
            _bar(100, 103, 100, 102, 2000),  # LONG (close 102); open=100
            _bar(102, 104, 101, 103, 3000),  # fill en open=102
        ]
        result = _engine().run(LongOnCloseAbove(threshold=100.0), bars)
        t = result.trades[0]
        # Si hubiera look-ahead, entraria en el open de t=1 (=100); el contrato
        # exige el open de t=2 (=102).
        assert t["entry_index"] == 2
        assert t["entry_raw"] == 102.0
        assert t["signal_time"] == "2000"
        assert t["entry_time"] == "3000"


# ---------------------------------------------------------------------------
# BT2-E — Stop/exit causal + convencion intrabar
# ---------------------------------------------------------------------------


class TestStopExitCausal:
    def test_stop_solo_usa_la_vela_en_curso(self) -> None:
        bars = [
            _bar(100, 101, 99, 100, 1000),
            _bar(101, 103, 100, 102, 2000),  # LONG, stop 92
            _bar(102, 103, 99, 100, 3000),   # fill open 102; low 99 > 92
            _bar(100, 101, 95, 96, 4000),    # low 95 > 92, no stop
            _bar(96, 97, 90, 91, 5000),      # low 90 <= 92 -> STOP @ 92
        ]
        result = _engine().run(LongOnCloseAbove(threshold=100.0), bars)
        t = result.trades[0]
        assert t["exit_reason"] == "STOP"
        assert t["exit_index"] == 4
        assert t["exit_raw"] == 92.0

    def test_convencion_intrabar_stop_prevalece(self) -> None:
        """Vela que toca stop y recupera: la salida determinista es el stop."""
        bars = [
            _bar(100, 101, 99, 100, 1000),
            _bar(101, 103, 100, 102, 2000),  # LONG, stop 92
            _bar(102, 103, 101, 102, 3000),  # fill open 102
            _bar(102, 120, 90, 118, 4000),   # toca low 90 (stop) y cierra 118
        ]
        result = _engine().run(LongOnCloseAbove(threshold=100.0), bars)
        t = result.trades[0]
        # Convencion determinista: STOP tiene prioridad sobre el cierre alto.
        assert t["exit_reason"] == "STOP"
        assert t["exit_raw"] == 92.0


# ---------------------------------------------------------------------------
# BT2-F — Determinismo completo
# ---------------------------------------------------------------------------


class TestDeterminismoCompleto:
    def test_dos_ejecuciones_identicas(self) -> None:
        bars = TestOracleDeterminista.BARS

        def run():
            engine = _engine(slippage_per_side=0.5, commission_per_unit=0.25)
            return engine.run(LongOnCloseAbove(threshold=100.0), bars)

        a, b = run(), run()
        assert a.trades == b.trades
        assert a.equity_curve == b.equity_curve
        assert a.final_equity == b.final_equity
        assert a.diagnostics == b.diagnostics
        assert calculate_metrics(a).to_dict() == calculate_metrics(b).to_dict()
