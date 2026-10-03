"""Tests deterministas de backfill + reconciliación de integridad (S4).

Batería S4:

  1.  sin hueco (resume == esperado) -> HEALTHY
  2.  backfill completo -> HEALTHY, ordenado
  3.  backfill vacío -> UNRECOVERABLE (hueco)
  4.  backfill parcial -> UNRECOVERABLE con hueco listado
  5.  duplicado idéntico -> deduplicado, sigue HEALTHY
  6.  conflicto (mismo t, OHLC distinto) -> BLOCKED
  7.  timestamp desalineado -> BLOCKED
  8.  OHLC inválido -> BLOCKED
  9.  símbolo inesperado -> BLOCKED
 10.  vela no cerrada -> BLOCKED
 11.  provider caído -> BLOCKED, sin confirmar
 12.  sin vela previa -> UNKNOWN
 13.  extremo superior no acotado -> UNKNOWN
 14.  solape (resume < esperado) -> DEGRADED
 15.  barrels fuera de rango ignorados
 16.  coordinator: HEALTHY confirma y habilita operar
 17.  coordinator: fallo NO confirma (sin atajo) y reintento puede confirmar
 18.  evidence() serializable
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live.adapter import BackfillProvider
from live.events import MarketDataEvent
from streaming import (
    IntegrityCoordinator,
    IntegrityReconciler,
    IntegrityStatus,
    ReconnectManager,
    ReconnectState,
    StreamConnected,
    StreamData,
    StreamDisconnected,
    StreamingConnectionError,
    StreamingMarketDataAdapter,
)

FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = 1_788_220_800
HOUR = 3_600
SYMBOL = "BTCUSDT"


# --------------------------------------------------------------- utilidades


def _bar(
    open_time: int,
    *,
    symbol: str = SYMBOL,
    o: float = 1.0,
    h: float = 2.0,
    l: float = 0.5,
    c: float = 1.5,
    v: float = 1.0,
    closed: bool = True,
) -> MarketDataEvent:
    return MarketDataEvent(
        symbol=symbol,
        open_time=open_time,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        is_closed=closed,
    )


class FakeBackfillProvider(BackfillProvider):
    def __init__(self, bars=None, *, error=None) -> None:
        self.bars = list(bars or [])
        self.error = error
        self.calls: list[tuple[str, int, int]] = []

    def fetch_closed_bars(self, symbol, interval_seconds, start_open_time):
        self.calls.append((symbol, interval_seconds, start_open_time))
        if self.error is not None:
            raise self.error
        return [b for b in self.bars if b.open_time >= start_open_time]


def _reconciler(provider) -> IntegrityReconciler:
    return IntegrityReconciler(provider, SYMBOL, HOUR)


# --------------------------------------------------------------- reconciler


def test_sin_hueco_healthy() -> None:
    provider = FakeBackfillProvider()
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + HOUR
    )
    assert report.status is IntegrityStatus.HEALTHY
    assert report.expected_start == BASE + HOUR
    assert report.recovered == ()
    assert provider.calls == []  # no hay hueco: no se llama al backfill


def test_backfill_completo_healthy_ordenado() -> None:
    provider = FakeBackfillProvider([_bar(BASE + 2 * HOUR), _bar(BASE + HOUR)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 3 * HOUR
    )
    assert report.status is IntegrityStatus.HEALTHY
    assert [b.open_time for b in report.recovered] == [BASE + HOUR, BASE + 2 * HOUR]
    assert provider.calls == [(SYMBOL, HOUR, BASE + HOUR)]


def test_backfill_vacio_unrecoverable() -> None:
    report = _reconciler(FakeBackfillProvider()).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 3 * HOUR
    )
    assert report.status is IntegrityStatus.UNRECOVERABLE
    assert report.missing == (BASE + HOUR, BASE + 2 * HOUR)
    assert not report.ok


def test_backfill_parcial_unrecoverable() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 3 * HOUR
    )
    assert report.status is IntegrityStatus.UNRECOVERABLE
    assert report.missing == (BASE + 2 * HOUR,)
    assert [b.open_time for b in report.recovered] == [BASE + HOUR]


def test_duplicado_identico_deduplicado_healthy() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR), _bar(BASE + HOUR)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.HEALTHY
    assert report.duplicates == (BASE + HOUR,)
    assert len(report.recovered) == 1


def test_conflicto_blocked() -> None:
    provider = FakeBackfillProvider(
        [_bar(BASE + HOUR, c=1.5), _bar(BASE + HOUR, c=1.6)]
    )
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED
    assert report.conflicts == (BASE + HOUR,)


def test_timestamp_desalineado_blocked() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR + 1)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 3 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED


def test_ohlc_invalido_blocked() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR, h=1.0)])  # high < close
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED


def test_simbolo_inesperado_blocked() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR, symbol="ETHUSDT")])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED


def test_vela_no_cerrada_blocked() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR, closed=False)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED


def test_provider_caido_blocked() -> None:
    provider = FakeBackfillProvider(error=ConnectionError("sin red"))
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.BLOCKED
    assert not report.ok


def test_sin_vela_previa_unknown() -> None:
    report = _reconciler(FakeBackfillProvider()).reconcile(
        last_open_time=None, resume_open_time=BASE + HOUR
    )
    assert report.status is IntegrityStatus.UNKNOWN
    assert not report.ok


def test_extremo_no_acotado_unknown() -> None:
    report = _reconciler(FakeBackfillProvider()).reconcile(
        last_open_time=BASE, resume_open_time=None
    )
    assert report.status is IntegrityStatus.UNKNOWN
    assert not report.ok


def test_solape_degraded() -> None:
    report = _reconciler(FakeBackfillProvider()).reconcile(
        last_open_time=BASE, resume_open_time=BASE  # == last, anterior al esperado
    )
    assert report.status is IntegrityStatus.DEGRADED
    assert not report.ok


def test_barras_fuera_de_rango_ignoradas() -> None:
    provider = FakeBackfillProvider(
        [_bar(BASE), _bar(BASE + HOUR), _bar(BASE + 3 * HOUR)]
    )
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    assert report.status is IntegrityStatus.HEALTHY
    assert [b.open_time for b in report.recovered] == [BASE + HOUR]


def test_evidence_serializable() -> None:
    provider = FakeBackfillProvider([_bar(BASE + HOUR)])
    report = _reconciler(provider).reconcile(
        last_open_time=BASE, resume_open_time=BASE + 2 * HOUR
    )
    ev = report.evidence()
    assert ev["status"] == "HEALTHY"
    assert ev["recovered"] == 1
    assert ev["recovered_first"] == BASE + HOUR
    assert ev["recovered_last"] == BASE + HOUR


# --------------------------------------------------------------- coordinator


class FakeAdapter(StreamingMarketDataAdapter):
    def __init__(self, script=None, *, name: str = "fake") -> None:
        self._script = list(script or [])
        self._name = name
        self._connected = False
        self._subscription = None

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def state(self):  # type: ignore[override]
        return "CONNECTED" if self._connected else "DISCONNECTED"

    @property
    def subscription(self):
        return self._subscription

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbol, interval_seconds) -> None:
        if not self._connected:
            raise StreamingConnectionError("no conectado")
        self._subscription = (symbol, interval_seconds)

    def receive(self):
        if not self._connected:
            raise StreamingConnectionError("no conectado")
        if not self._script:
            return None
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _sdata(open_time: int, *, closed: bool = True) -> StreamData:
    return StreamData(
        received_at=FIXED_NOW,
        event=MarketDataEvent(
            symbol=SYMBOL,
            open_time=open_time,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=1.0,
            is_closed=closed,
        ),
    )


def _reconnected_manager(script) -> ReconnectManager:
    manager = ReconnectManager(
        FakeAdapter(script),
        clock=lambda: 0.0,
        wall_clock=lambda: FIXED_NOW,
        sleep=lambda _s: None,
    )
    manager.subscribe(SYMBOL, HOUR)
    manager.connect()
    manager.receive()  # StreamConnected
    return manager


def test_coordinator_healthy_confirma_y_habilita() -> None:
    manager = _reconnected_manager([_sdata(BASE), StreamingConnectionError("drop")])
    assert isinstance(manager.receive(), StreamData)  # última vela antes de caer
    out = manager.receive()  # caída -> reconexión
    assert isinstance(out, StreamDisconnected)
    assert manager.data_integrity_unknown is True
    assert manager.last_closed_bar == (SYMBOL, BASE)

    provider = FakeBackfillProvider([_bar(BASE + HOUR)])
    coordinator = IntegrityCoordinator(manager, _reconciler(provider))
    assert coordinator.pending is True
    assert coordinator.can_operate is False

    report = coordinator.reconcile_data_integrity(resume_open_time=BASE + 2 * HOUR)
    assert report.status is IntegrityStatus.HEALTHY
    assert manager.data_integrity_unknown is False
    assert coordinator.can_operate is True
    assert manager.state is ReconnectState.CONNECTED


def test_coordinator_sin_atajo_fallo_no_confirma_y_reintenta() -> None:
    manager = _reconnected_manager([_sdata(BASE), StreamingConnectionError("drop")])
    manager.receive()  # vela
    manager.receive()  # caída -> reconexión

    provider = FakeBackfillProvider()  # vacío: hueco no recuperable
    coordinator = IntegrityCoordinator(manager, _reconciler(provider))

    report = coordinator.reconcile_data_integrity(resume_open_time=BASE + 2 * HOUR)
    assert report.status is IntegrityStatus.UNRECOVERABLE
    # SIN ATAJO: el fallo del backfill NO confirma integridad ni autoriza
    assert manager.data_integrity_unknown is True
    assert coordinator.can_operate is False

    # Reintento con datos disponibles: ahora sí demuestra y confirma
    provider.bars = [_bar(BASE + HOUR)]
    report2 = coordinator.reconcile_data_integrity(resume_open_time=BASE + 2 * HOUR)
    assert report2.status is IntegrityStatus.HEALTHY
    assert manager.data_integrity_unknown is False
    assert coordinator.can_operate is True
