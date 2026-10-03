"""Tests del LiveDataEngine (B1): solo market data, sin ordenes.

La bateria exigida por el plan B1:

  1. barras normales (3 validas)  -> 3 ClosedBarEvent, estados/calidad OK
  2. duplicado (bar1, bar1, bar2) -> no se emite 2 veces la misma vela
  3. out-of-order (10, 12, 11)    -> comportamiento definido/rechazo
  4. gap (10, 11, 13)             -> GAP_DETECTED + DEGRADED, sin emitir
  5. vela incompleta (10 OPEN)    -> sin ClosedBarEvent (nada intrabar)
  6. cierre (un solo tick)        -> exactamente una emision
  7. cierre repetido              -> sin duplicar la estrategia
  8. stale feed                   -> CONNECTED -> STALE -> recovery
  9. reinicio                     -> sin falsa vela cerrada
 10. reconciliacion via Backfill  -> gap -> backfill -> CONNECTED
 11. contrato paper-safe          -> atlas_bar identico al backtest
 12. validaciones OHLC/timestamp  -> INVALID_* rechazados sin emision
"""

from __future__ import annotations

import pytest

from live.adapter import BackfillProvider, MarketDataAdapter
from live.engine import (
    DataQuality,
    EngineState,
    LiveDataEngine,
    WarmStartError,
)
from live.events import ClosedBarEvent, MarketDataEvent

HOUR = 3_600
BASE = 1_788_220_800  # 2026-09-01T00:00Z (vlaalida alineada a 1h)


def _ev(
    open_time: int,
    *,
    open: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 100.0,
    volume: float = 1_000.0,
    is_closed: bool = True,
    symbol: str = "BTCUSDT",
) -> MarketDataEvent:
    high = high if high is not None else max(open, close)
    low = low if low is not None else min(open, close)
    return MarketDataEvent(
        symbol=symbol,
        open_time=open_time,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
    )


class RememberingAdapter(MarketDataAdapter):
    """Adapter sintetico: cola de eventos + banderas de conexion."""

    def __init__(self, events: list[MarketDataEvent] | None = None) -> None:
        self.queue = list(events or [])
        self.connected = False
        self.subscribed: list[tuple[str, int]] = []
        self.connect_calls = 0

    def connect(self) -> None:
        self.connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        self.subscribed.append((symbol, interval_seconds))

    def receive(self) -> MarketDataEvent | None:
        if not self.connected:
            raise ConnectionError("adapter desconectado")
        if not self.queue:
            return None
        return self.queue.pop(0)


class FakeBackfill(BackfillProvider):
    def __init__(
        self, bars: list[MarketDataEvent], *, filter_start: bool = True
    ) -> None:
        self.bars = bars
        self.filter_start = filter_start
        self.calls: list[tuple[str, int, int]] = []

    def fetch_closed_bars(
        self, symbol: str, interval_seconds: int, start_open_time: int
    ) -> list[MarketDataEvent]:
        self.calls.append((symbol, interval_seconds, start_open_time))
        if not self.filter_start:
            return list(self.bars)
        return [b for b in self.bars if b.open_time >= start_open_time]


def _engine(
    events: list[MarketDataEvent] | None = None,
    *,
    backfill: BackfillProvider | None = None,
    handler: object | None = None,
    stale_timeout: float = 60.0,
) -> LiveDataEngine:
    adapter = RememberingAdapter(events)
    engine = LiveDataEngine(
        "BTCUSDT",
        HOUR,
        adapter,
        stale_timeout_seconds=stale_timeout,
        backfill_provider=backfill,
        on_closed_bar=handler,
    )
    engine.start()
    return engine


# ---------------------------------------------------------------------------
# 1-3-4-5. flujo normal, duplicado, out-of-order, gap e incompleta
# ---------------------------------------------------------------------------


class TestNormalFlow:
    def test_three_valid_bars_emit_three_closed_events(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        e1 = engine.process(_ev(BASE))
        e2 = engine.process(_ev(BASE + HOUR))
        e3 = engine.process(_ev(BASE + 2 * HOUR))

        assert [e.open_time for e in (e1, e2, e3)] == [
            BASE,
            BASE + HOUR,
            BASE + 2 * HOUR,
        ]
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY
        assert len(received) == 3
        assert [e.emission_sequence for e in received] == [1, 2, 3]
        # sequencia monotona: denominador de idempotencia
        assert received[0].emission_sequence < received[1].emission_sequence
        assert received[0].close_time == received[0].open_time + HOUR

    def test_bar_requires_open_and_close_ticks(self) -> None:
        engine = _engine()
        assert engine.process(_ev(BASE, is_closed=False)) is None
        assert engine.process(_ev(BASE + HOUR, is_closed=False)) is None
        assert engine.closed_bars == []

    def test_open_then_close_single_emission(self) -> None:
        engine = _engine()
        assert engine.process(_ev(BASE, is_closed=False)) is None
        out = engine.process(_ev(BASE))
        assert out is not None
        assert out.open_time == BASE
        assert engine.closed_bars == [out]


class TestDuplicates:
    def test_duplicate_bar_not_emitted_twice(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        assert engine.process(_ev(BASE)) is not None
        e2 = engine.process(_ev(BASE))
        assert e2 is None  # duplicado rechazado
        e3 = engine.process(_ev(BASE + HOUR))
        assert e3 is not None

        assert [e.open_time for e in engine.closed_bars] == [BASE, BASE + HOUR]
        assert len(received) == 2  # la estrategia solo vio barras validas
        assert any(q == DataQuality.DUPLICATE for q, _ in engine.issues)

    def test_duplicate_close_no_strategy_repeat(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        engine.process(_ev(BASE))
        engine.process(_ev(BASE))
        engine.process(_ev(BASE))
        assert len(received) == 1  # una sola notificacion a la estrategia


class TestOutOfOrder:
    def test_gap_marks_degraded_and_rejects(self) -> None:
        engine = _engine()
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + HOUR))
        # falta BASE+2*HOUR; llega BASE+3*HOUR
        e = engine.process(_ev(BASE + 3 * HOUR))
        assert e is None
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.GAP_DETECTED

    def test_out_of_order_rejected(self) -> None:
        engine = _engine()
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + 2 * HOUR))  # avanzo (gap implicito)
        e = engine.process(_ev(BASE + HOUR))  # tarde
        assert e is None
        assert any(q == DataQuality.GAP_DETECTED for q, _ in engine.issues)

    def test_stale_bar_not_emitted(self) -> None:
        engine = _engine()
        engine.process(_ev(BASE))  # emite barra 10:00
        e = engine.process(_ev(BASE + 2 * HOUR))  # barra 12:00 salta la 11:00
        assert e is None
        # la barra 11:00 nunca existio en el stream; no se fabrica
        assert [b.open_time for b in engine.closed_bars] == [BASE]


# ---------------------------------------------------------------------------
# 6-7. cierre exacto y cierre repetido
# ---------------------------------------------------------------------------


class TestCloseSemantics:
    def test_close_exactly_once(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        engine.process(_ev(BASE, is_closed=False))
        engine.process(_ev(BASE))
        assert len(engine.closed_bars) == 1
        assert len(received) == 1

    def test_repeated_close_no_duplicate(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        engine.process(_ev(BASE))
        engine.process(_ev(BASE))
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + HOUR))
        engine.process(_ev(BASE + HOUR))
        assert len(received) == 2
        assert [b.open_time for b in received] == [BASE, BASE + HOUR]


# ---------------------------------------------------------------------------
# 8-9. stale feed y reinicio sin falsa vela cerrada
# ---------------------------------------------------------------------------


class TestStaleAndRestart:
    def test_stale_detected_and_recovers(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 1_000.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        adapter = RememberingAdapter([])
        engine = LiveDataEngine(
            "BTCUSDT",
            HOUR,
            adapter,
            stale_timeout_seconds=5.0,
            clock=clock,
        )
        engine.start()
        engine.process(_ev(BASE))  # datos frescos en t=1000
        assert engine.state == EngineState.CONNECTED

        clock.now = 1_100.0  # 100s sin datos > timeout 5s
        engine.check_stale()
        assert engine.state == EngineState.STALE
        assert engine.quality == DataQuality.STALE

        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY

    def test_restart_without_false_closed_bar(self) -> None:
        adapter = RememberingAdapter([])
        engine = LiveDataEngine("BTCUSDT", HOUR, adapter)
        engine.start()
        engine.process(_ev(BASE, is_closed=False))  # vela abierta
        engine.stop()
        # la vela abierta no se convierte en barra cerrada
        assert engine.closed_bars == []

        engine.start()
        assert engine.state == EngineState.CONNECTED
        e = engine.process(_ev(BASE))
        assert e is not None  # sigue contando secuencias sin duplicar
        assert len(engine.closed_bars) == 1


# ---------------------------------------------------------------------------
# 10. reconciliacion via backfill tras un gap
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_gap_then_backfill_then_connected(self) -> None:
        backfill = FakeBackfill([_ev(BASE + 2 * HOUR)])
        engine = _engine(
            [],
            backfill=backfill,
        )
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + HOUR))
        engine.process(_ev(BASE + 3 * HOUR))  # gap, falta BASE+2*HOUR
        assert engine.state == EngineState.DEGRADED

        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY
        # la barra faltante se entrego exactamente una vez
        assert [b.open_time for b in engine.closed_bars] == [
            BASE,
            BASE + HOUR,
            BASE + 2 * HOUR,
        ]
        assert backfill.calls[0][0:3] == ("BTCUSDT", HOUR, BASE + 2 * HOUR)

    def test_reconcile_requires_provider(self) -> None:
        engine = _engine([])
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + 3 * HOUR))  # gap
        with pytest.raises(RuntimeError):
            engine.reconcile()

    def test_reconcile_only_from_degraded(self) -> None:
        engine = _engine([])
        with pytest.raises(RuntimeError):
            engine.reconcile()


# ---------------------------------------------------------------------------
# 11. contrato paper-safe: misma representacion que el backtest
# ---------------------------------------------------------------------------


class TestPaperSafeContract:
    def test_atlas_bar_matches_backtest_contract(self) -> None:
        engine = _engine([])
        bar = engine.process(_ev(BASE, open=100.0, high=105.0, low=99.0, close=104.0))
        assert bar is not None
        assert bar.atlas_bar == {
            "ts": str(BASE),
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1_000.0,
        }

    def test_engine_only_delivers_closed_bars(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        # ticks intrabar: nunca llegan a la estrategia
        engine.process(_ev(BASE, is_closed=False, close=100.0))
        engine.process(_ev(BASE, is_closed=False, close=101.0))
        engine.process(_ev(BASE, is_closed=False, close=102.0))
        assert received == []
        assert engine.closed_bars == []
        # la mitad de la vela no es una vela: sin estado derivado
        engine.process(_ev(BASE, is_closed=False, close=103.0))
        assert engine.closed_bars == []


# ---------------------------------------------------------------------------
# 12. validaciones de calidad
# ---------------------------------------------------------------------------


class TestQualityValidation:
    def test_invalid_timestamp_rejected(self) -> None:
        engine = _engine([])
        e = engine.process(_ev(BASE + 1_800))  # no alineado a 1h
        assert e is None
        assert any(q == DataQuality.INVALID_TIMESTAMP for q, _ in engine.issues)

    def test_invalid_ohlc_rejected(self) -> None:
        engine = _engine([])
        e = engine.process(_ev(BASE, high=90.0, low=80.0, close=95.0))
        assert e is None
        assert any(q == DataQuality.INVALID_OHLC for q, _ in engine.issues)

    def test_wrong_symbol_rejected(self) -> None:
        engine = _engine([])
        e = engine.process(_ev(BASE, symbol="ETHUSDT"))
        assert e is None
        assert any(q == DataQuality.INVALID_TIMESTAMP for q, _ in engine.issues)


# ---------------------------------------------------------------------------
# polling y gestor de conexion
# ---------------------------------------------------------------------------


class TestPolling:
    def test_poll_processes_queue(self) -> None:
        adapter = RememberingAdapter(
            [_ev(BASE), _ev(BASE + HOUR), _ev(BASE + 2 * HOUR)]
        )
        engine = LiveDataEngine("BTCUSDT", HOUR, adapter)
        engine.start()
        n = engine.poll()
        assert n == 3
        assert len(engine.closed_bars) == 3

    def test_reconnect_after_connection_loss(self) -> None:
        adapter = RememberingAdapter([_ev(BASE)])
        engine = LiveDataEngine("BTCUSDT", HOUR, adapter)
        engine.start()
        assert engine.state == EngineState.CONNECTED
        assert engine.poll() == 1  # consume BASE

        # stream caido: el adapter lanza ConnectionError
        adapter.connected = False
        n = engine.poll()
        assert n == 0
        assert engine.state == EngineState.RECONNECTING

        adapter.queue.append(_ev(BASE + HOUR))
        adapter.connected = True
        engine.reconnect()
        assert engine.state == EngineState.CONNECTED
        n = engine.poll()
        assert n == 1
        processed = engine.process(_ev(BASE + 2 * HOUR))
        assert processed is not None


# ---------------------------------------------------------------------------
# 13. contrato de reconcile(): el backfill es una frontera de confianza
# ---------------------------------------------------------------------------


class TestReconcileContract:
    """reconcile() valida el backfill y NUNCA marca HEALTHY con datos invalidos."""

    @staticmethod
    def _degraded(backfill_bars, *, filter_start: bool = True):
        provider = FakeBackfill(backfill_bars, filter_start=filter_start)
        engine = _engine([], backfill=provider)
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + HOUR))
        engine.process(_ev(BASE + 3 * HOUR))  # gap: falta BASE + 2*HOUR
        assert engine.state == EngineState.DEGRADED
        return engine, provider

    def test_duplicate_backfill_not_emitted_twice(self) -> None:
        engine, _ = self._degraded([_ev(BASE + 2 * HOUR), _ev(BASE + 2 * HOUR)])
        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.DUPLICATE
        assert [b.open_time for b in engine.closed_bars] == [
            BASE,
            BASE + HOUR,
            BASE + 2 * HOUR,
        ]

    def test_out_of_order_backfill_rejected(self) -> None:
        engine, _ = self._degraded(
            [_ev(BASE + 2 * HOUR), _ev(BASE + HOUR)], filter_start=False
        )
        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.OUT_OF_ORDER

    def test_incomplete_bar_rejected(self) -> None:
        engine, _ = self._degraded([_ev(BASE + 2 * HOUR, is_closed=False)])
        replayed = engine.reconcile()
        assert replayed == []
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.INCOMPLETE_BAR
        assert BASE + 2 * HOUR not in [b.open_time for b in engine.closed_bars]

    def test_internal_gap_rejected(self) -> None:
        engine, _ = self._degraded([_ev(BASE + 2 * HOUR), _ev(BASE + 4 * HOUR)])
        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.GAP_DETECTED

    def test_wrong_timestamp_alignment_rejected(self) -> None:
        engine, _ = self._degraded([_ev(BASE + 2 * HOUR + 1_800)])
        assert engine.reconcile() == []
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.INVALID_TIMESTAMP

    def test_invalid_ohlc_rejected(self) -> None:
        engine, _ = self._degraded(
            [_ev(BASE + 2 * HOUR, open=100.0, high=99.0, low=98.0, close=100.0)]
        )
        assert engine.reconcile() == []
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.INVALID_OHLC

    def test_empty_backfill_stays_degraded(self) -> None:
        engine, _ = self._degraded([])
        assert engine.reconcile() == []
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.GAP_DETECTED

    def test_valid_backfill_recovers(self) -> None:
        engine, _ = self._degraded([_ev(BASE + 2 * HOUR)])
        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY

    def test_invalid_then_valid_recovery(self) -> None:
        engine, provider = self._degraded([_ev(BASE + 2 * HOUR, is_closed=False)])
        assert engine.reconcile() == []
        assert engine.state == EngineState.DEGRADED

        provider.bars = [_ev(BASE + 2 * HOUR)]  # backfill corregido
        replayed = engine.reconcile()
        assert [b.open_time for b in replayed] == [BASE + 2 * HOUR]
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY
        assert [b.open_time for b in engine.closed_bars] == [
            BASE,
            BASE + HOUR,
            BASE + 2 * HOUR,
        ]


# ---------------------------------------------------------------------------
# 14. warm_start: siembra del ancla SIN emitir/callback/reconcile
# ---------------------------------------------------------------------------


def _closed(
    open_time: int,
    *,
    seq: int = 1,
    symbol: str = "BTCUSDT",
    interval_seconds: int = HOUR,
    open: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 100.0,
    volume: float = 1_000.0,
) -> ClosedBarEvent:
    high = high if high is not None else max(open, close)
    low = low if low is not None else min(open, close)
    return ClosedBarEvent(
        symbol=symbol,
        open_time=open_time,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        interval_seconds=interval_seconds,
        emission_sequence=seq,
    )


class TestWarmStart:
    def test_cero_emision_ni_callback(self) -> None:
        received: list[ClosedBarEvent] = []
        engine = _engine(handler=received.append)
        engine.warm_start(_closed(BASE, seq=7))
        assert engine.closed_bars == []
        assert received == []
        assert engine.state == EngineState.CONNECTED
        assert engine.quality == DataQuality.HEALTHY

    def test_continuidad_emission_sequence(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE, seq=7))
        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert out.emission_sequence == 8

    def test_continuidad_normal(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE))
        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert [b.open_time for b in engine.closed_bars] == [BASE + HOUR]

    def test_gap_degraded(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE))
        out = engine.process(_ev(BASE + 3 * HOUR))
        assert out is None
        assert engine.state == EngineState.DEGRADED
        assert engine.quality == DataQuality.GAP_DETECTED
        assert engine.closed_bars == []

    def test_duplicate_tras_seed(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE))
        out = engine.process(_ev(BASE))
        assert out is None
        assert any(q == DataQuality.DUPLICATE for q, _ in engine.issues)
        assert engine.state == EngineState.CONNECTED
        assert engine.closed_bars == []

    def test_out_of_order_antes_del_seed(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE))
        out = engine.process(_ev(BASE - HOUR))
        assert out is None
        assert any(q == DataQuality.OUT_OF_ORDER for q, _ in engine.issues)
        assert engine.state == EngineState.CONNECTED
        assert engine.closed_bars == []

    def test_idempotencia_mismo_ancla(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE, seq=7))
        engine.warm_start(_closed(BASE, seq=7))
        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert out.emission_sequence == 8

    def test_conflicto_de_ancla(self) -> None:
        engine = _engine()
        engine.warm_start(_closed(BASE, seq=7))
        with pytest.raises(RuntimeError):
            engine.warm_start(_closed(BASE + HOUR, seq=8))  # posterior
        with pytest.raises(RuntimeError):
            engine.warm_start(_closed(BASE - HOUR, seq=8))  # anterior
        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert out.emission_sequence == 8

    def test_tipo_invalido(self) -> None:
        engine = _engine()
        with pytest.raises(WarmStartError):
            engine.warm_start(_ev(BASE))  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad",
        [
            _closed(BASE, symbol="ETHUSDT"),
            _closed(BASE, interval_seconds=60),
            _closed(BASE + 1_800),
            _closed(BASE, open=100.0, high=99.0, low=98.0, close=100.0),
            _closed(BASE, seq=-1),
        ],
    )
    def test_validaciones_atomicas(self, bad: ClosedBarEvent) -> None:
        engine = _engine()
        with pytest.raises(WarmStartError):
            engine.warm_start(bad)
        # sin efectos parciales: el engine sigue utilizable como fresco
        engine.warm_start(_closed(BASE, seq=3))
        out = engine.process(_ev(BASE + HOUR))
        assert out is not None
        assert out.emission_sequence == 4

    def test_estado_no_conectado(self) -> None:
        adapter = RememberingAdapter([])
        engine = LiveDataEngine("BTCUSDT", HOUR, adapter)
        with pytest.raises(RuntimeError):
            engine.warm_start(_closed(BASE))

    def test_vela_en_curso(self) -> None:
        engine = _engine()
        engine.process(_ev(BASE, is_closed=False))
        with pytest.raises(RuntimeError):
            engine.warm_start(_closed(BASE))
        assert engine.closed_bars == []

    def test_estado_degraded(self) -> None:
        engine = _engine()
        engine.process(_ev(BASE))
        engine.process(_ev(BASE + 3 * HOUR))
        assert engine.state == EngineState.DEGRADED
        with pytest.raises(RuntimeError):
            engine.warm_start(_closed(BASE + 2 * HOUR))

    def test_cero_llamadas_al_backfill(self) -> None:
        backfill = FakeBackfill([_ev(BASE + HOUR)])
        engine = _engine([], backfill=backfill)
        engine.warm_start(_closed(BASE))
        assert backfill.calls == []
        assert engine.state == EngineState.CONNECTED

    def test_no_modifica_last_data_at(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 1_000.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        adapter = RememberingAdapter([])
        engine = LiveDataEngine(
            "BTCUSDT", HOUR, adapter, stale_timeout_seconds=5.0, clock=clock
        )
        engine.start()
        engine.warm_start(_closed(BASE))
        clock.now = 1_100.0
        engine.check_stale()
        assert engine.state == EngineState.CONNECTED