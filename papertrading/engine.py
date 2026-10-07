"""PaperTradingEngine (B7): primer integración completa del sistema, en paper.

Cadena por barra (misma semántica que el BacktestEngine, §backtesting/engine):

    MarketData -> LiveDataEngine -> [ClosedBarEvent]
        -> sesión (guard de entrada)
        -> señal sobre la barra CERRADA en t (misma interfaz que backtest)
        -> RiskEngine (veto)
        -> Kill Switch (can_trade) -> OrderManager -> PaperBrokerAdapter
        -> fills/posición/equity
        -> Reconciliación (B4) -> Monitoring (B6) -> Kill Switch (B5)  ↺

Temporalidad anti-lookahead (idéntica al backtest):
    decisión sobre la barra cerrada en t -> la orden se ejecuta en el OPEN
    de la barra t+1. La estrategia recibe solo la ventana de barras cerradas
    hasta t (prefijo completo, mismo objeto lista).

Solo PaperBrokerAdapter: el engine exige un broker con `price_source`
(capa paper); nunca se envía a un broker real. No liquida posiciones por
sesión CLOSED (solo stop/time). Reiniciable sin duplicar exposición
(`signed_signals` persistido). Audit trail completo con IDs.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable

from brokers.base import BrokerAdapter
from brokers.models import (
    BrokerConnectionError,
    BrokerError,
    BrokerFill,
    BrokerOrderState,
    OrderNotFoundError,
    SubmitStatus,
)
from brokers.paper import BrokerSnapshotError
from execution.order import Fill, OrderSide, OrderStatus, OrderType
from execution.order_manager import OrderManager, OrderStateError
from killswitches.coordinator import KillSwitchCoordinator
from live.events import ClosedBarEvent
from monitoring.engine import MonitoringEngine, apply_to_kill_switch
from monitoring.models import HealthStatus
from monitoring.wiring import (
    collect,
    observe_broker,
    observe_data_feed,
    observe_execution,
    observe_reconciliation,
    observe_reconciliation_report,
    observe_session,
    observe_system,
)
from reconciliation.engine import ReconciliationEngine
from reconciliation.models import (
    AccountSnapshot,
    FillSnapshot,
    InternalSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    ReconciliationStatus,
)
from reconciliation.orchestrator import (
    reconcile_with_broker,
    resolve_unknown_orders,
)
from risk.types import AccountRiskState, ProjectedExposure
from sessions.manager import MarketSessionManager
from strategies.base.signal import Action
from strategies.base.strategy import BaseStrategy

from monitoring import utc_now
from papertrading.ledger import AccountLedger, LedgerPosition
from papertrading.models import (
    PendingEntry,
    PaperAuditEntry,
    PaperEngineConfig,
    PaperEvent,
    PaperPosition,
    PaperTrade,
)

_EPS = 1e-9


def _dt_from_open(open_time: int) -> datetime:
    return datetime.fromtimestamp(open_time, tz=timezone.utc)


def _valid_price(price: object) -> bool:
    """Precio de ejecucion/mark valido: numero finito y > 0."""
    if not isinstance(price, (int, float)):
        return False
    p = float(price)
    return math.isfinite(p) and p > 0.0


class PaperTradingEngine:
    """Orquestador paper: consume ClosedBarEvent y orquesta todo el stack."""

    def __init__(
        self,
        strategy: BaseStrategy,
        order_manager: OrderManager,
        risk_engine,
        broker: BrokerAdapter,
        sessions: MarketSessionManager,
        reconciliation_engine: ReconciliationEngine,
        kill_switch: KillSwitchCoordinator,
        monitoring: MonitoringEngine,
        *,
        clock: Callable[[], datetime] = utc_now,
        run_id: str = "PAPER",
        config: PaperEngineConfig | None = None,
        initial_cash: float | None = None,
        ledger: "AccountLedger | None" = None,
    ) -> None:
        if not hasattr(broker, "price_source"):
            raise TypeError(
                "B7: el unico broker admitido es PaperBrokerAdapter "
                "(objeto con atributo `price_source`); nunca un broker real"
            )
        self.strategy = strategy
        self.order_manager = order_manager
        self.risk_engine = risk_engine
        self.broker = broker
        self.sessions = sessions
        self.reconciliation = reconciliation_engine
        self.kill_switch = kill_switch
        self.monitoring = monitoring
        self._clock = clock
        self.run_id = run_id
        self.config = config or PaperEngineConfig()

        # F-06A: AccountLedger = autoridad interna unica de cash/positions/
        # realized. El bootstrap es EXPLICITO (configuracion), nunca del broker.
        if ledger is not None:
            self._ledger = ledger
        else:
            if initial_cash is None:
                raise TypeError(
                    "B7/F-06A: PaperTradingEngine requiere `initial_cash` "
                    "(o un `ledger`) explicito; el capital inicial NO se lee "
                    "del broker."
                )
            self._ledger = AccountLedger(initial_cash=initial_cash)

        self._bars: list[dict[str, Any]] = []
        self._audit: list[PaperAuditEntry] = []
        self._seq = 0
        self._pending: PendingEntry | None = None
        self._signed: set[str] = set()
        # F-06A: `_position` es una proyeccion DERIVADA del ledger (metadata
        # operativa: stop/cid/tiempos) — NO es autoridad contable. La autoridad
        # de quantity/avg_entry/cash/realized es `self._ledger`.
        self._position: PaperPosition | None = None
        self._pending_resolution: set[str] = set()
        self._order_fills: dict[str, tuple] = {}

        self._last_open_time: int | None = None
        self._interval_seconds: int | None = None
        self._last_bar_close_dt: datetime | None = None
        self._last_report = None
        self._last_report_at: datetime | None = None
        self._last_monitoring = None

        self._last_equity = 0.0
        self._last_cash = 0.0
        self._peak_equity = 0.0
        self._day_key: date | None = None
        self._day_start_equity: float | None = None
        self._day_pnl = 0.0
        self._equity_curve: list = []
        self._trades: list[PaperTrade] = []

        self._last_broker_success: datetime | None = None
        self._broker_errors = 0
        self._last_exit_reason: str | None = None
        # LIVE-04 (G-MD-C): observacion del feed de market data (B1). Por defecto
        # paper/test = CONNECTED/HEALTHY; el ForwardRunner/streaming inyecta el
        # estado REAL de `LiveDataEngine` (no se hardcodea HEALTHY).
        self._data_feed_state = "CONNECTED"
        self._data_feed_quality = "HEALTHY"

        # Paper: el engine siempre arranca con el broker simulado conectado.
        # Un test de bloqueo desconecta el adapter después del arranque.
        broker.connect()

        self._audit_rec(PaperEvent.READY, {"run_id": run_id})

    # ------------------------------------------------------------- auditoría

    def _audit_rec(self, event: PaperEvent, refs: dict) -> None:
        self._seq += 1
        self._audit.append(
            PaperAuditEntry(
                id=f"AUD-{self._seq:06d}",
                at=self._clock(),
                event=event.value,
                refs=dict(refs),
            )
        )

    @property
    def audit_log(self) -> tuple[PaperAuditEntry, ...]:
        return tuple(self._audit)

    def trail(self, ref_key: str, ref_value: str) -> tuple[PaperAuditEntry, ...]:
        """Reconstruye una operación de extremo a extremo por ID correlacionado.

        `ref_key` "signal_id" correlaciona la entrada (signal_id == cid ==
        position_id de la posición creada); los eventos de salida referencian
        el `position_id`, que es el mismo cid. Se matchea por VALOR para que
        la cadena entrada->posición->salida se reconstruya completa.
        """
        return tuple(
            e for e in self._audit if e.refs.get(ref_key) == ref_value
            or ref_value in e.refs.values()
        )

    # ------------------------------------------------------------- API pública

    @property
    def position(self) -> PaperPosition | None:
        return self._position

    @property
    def ledger(self) -> AccountLedger:
        """Autoridad interna contable (F-06A)."""
        return self._ledger

    @property
    def realized_pnl(self) -> float:
        """Realized P&L autoritativo (delegado al ledger)."""
        return self._ledger.realized_pnl

    @property
    def trades(self) -> tuple[PaperTrade, ...]:
        return tuple(self._trades)

    @property
    def equity_curve(self) -> tuple:
        return tuple(self._equity_curve)

    @property
    def last_report(self):
        return self._last_report

    @property
    def last_monitoring(self):
        return self._last_monitoring

    def _bar_rejection_reason(
        self, closed: ClosedBarEvent, now: datetime
    ) -> str | None:
        """LIVE-03: validez estructural + temporal de una barra, antes de procesar.

        Bloquea (antes de cualquier mutacion de estado/ledger) barras:
          * OHLC no finito / no positivo / incoherente, volumen invalido;
          * timestamp ausente/no positivo/no alineado al intervalo;
          * timestamp **futuro** (la vela aun no habria cerrado respecto al
            reloj autoritativo `self._clock()`).

        NO implementa freshness (antiguedad maxima): requiere decision de
        configuracion (ver contrato LIVE-03). Devuelve `None` si la barra es
        valida. Epoch-based => independiente del timezone local.
        """
        # LIVE-03: identidad de market data (instrumento/intervalo esperados).
        if closed.symbol != self.config.instrument:
            return "wrong_instrument"
        if (
            self._interval_seconds is not None
            and closed.interval_seconds != self._interval_seconds
        ):
            return "wrong_interval"
        vals = (closed.open, closed.high, closed.low, closed.close, closed.volume)
        if any(
            not isinstance(v, (int, float)) or not math.isfinite(float(v))
            for v in vals
        ):
            return "invalid_ohlc"
        if closed.open <= 0 or closed.close <= 0 or closed.volume < 0:
            return "invalid_ohlc"
        if (
            closed.high < max(closed.open, closed.close)
            or closed.low > min(closed.open, closed.close)
            or closed.low > closed.high
        ):
            return "invalid_ohlc"
        if (
            closed.interval_seconds <= 0
            or closed.open_time <= 0
            or closed.open_time % closed.interval_seconds != 0
        ):
            return "invalid_timestamp"
        if closed.close_time > int(now.timestamp()):
            return "future_timestamp"
        return None

    def _freshness_rejection(self, now: datetime) -> str | None:
        """LIVE-03: freshness del market data en el punto de submit (G-MD-D).

        `data_reference_time` = cierre de la ultima vela cerrada
        (`_last_bar_close_dt`); `now_authoritative` = `now`. Fresh si
        `now - reference <= data_max_age_s` (configuracion EXISTENTE en
        `MonitorConfig`; no se inventa un valor nuevo). Fail-closed si no hay
        referencia valida. Devuelve el motivo de rechazo o `None`.
        """
        ref = self._last_bar_close_dt
        if ref is None:
            return "no_market_data"
        age = (now - ref).total_seconds()
        if age < 0:
            return "future_market_data"
        if age > self.monitoring.config.data_max_age_s:
            return "stale_market_data"
        return None

    def on_closed_bar(self, closed: ClosedBarEvent) -> None:
        """Único punto de entrada: una barra ya cerrada (emisión de B1)."""
        # LIVE-03: rechazar datos invalidos/futuros ANTES de cualquier
        # procesamiento: una barra invalida NUNCA debe llegar a submit_order ni
        # mutar ledger/posicion.
        rejection = self._bar_rejection_reason(closed, self._clock())
        if rejection is not None:
            self._audit_rec(
                PaperEvent.BAR_SKIPPED,
                {"reason": rejection, "bar_open_time": closed.open_time},
            )
            return
        if (
            self._last_open_time is not None
            and closed.open_time <= self._last_open_time
        ):
            # Restart / replay: nunca reprocesar, nunca duplicar exposición.
            self._audit_rec(
                PaperEvent.RESTART_SKIP,
                {"bar_open_time": closed.open_time, "last_open_time": self._last_open_time},
            )
            return
        self._last_open_time = closed.open_time
        self._interval_seconds = closed.interval_seconds
        bar = closed.atlas_bar
        self._bars.append(bar)
        self._last_bar_close_dt = _dt_from_open(closed.close_time)
        self._audit_rec(
            PaperEvent.BAR_CLOSED,
            {
                "instrument": closed.symbol,
                "bar_open_time": closed.open_time,
                "open": bar["open"],
                "close": bar["close"],
            },
        )

        now = self._clock()
        self._execute_pending(bar, now)      # entrada decidida en t-1 al open de t
        self._evaluate_exit(bar, now)        # stop/time sobre la barra t
        self._mark(bar, closed, now)         # equity al cierre de t
        self._resolve_pending_submissions()  # adopción de fills UNKNOWN (sin duplicar)
        self._on_signal(bar, now)            # decisión sobre barra t -> open t+1
        self._post_bar(now)                  # reconciliación/monitoring

    # ------------------------------------------------------- ejecución entrada

    def _execute_pending(self, bar: dict, now: datetime) -> None:
        pe = self._pending
        if pe is None:
            return
        session = self.sessions.get_session(pe.signal.instrument, pe.bar_open_time)
        if not session.can_open_positions:
            self._audit_rec(
                PaperEvent.ENTRY_SKIPPED_SESSION,
                {"signal_id": pe.cid, "session_state": session.state.value},
            )
            self._pending = None
            return
        if not self.kill_switch.status().can_trade:
            self._audit_rec(
                PaperEvent.ENTRY_SKIPPED_KILL_SWITCH, {"signal_id": pe.cid}
            )
            self._pending = None
            return
        if not self._broker_healthy():
            self._audit_rec(
                PaperEvent.ENTRY_SKIPPED_BROKER, {"signal_id": pe.cid}
            )
            self._pending = None
            return
        # LIVE-03: revalidacion EXPLICITA de freshness inmediatamente antes del
        # submit (G-MD-D/TOCTOU). No depende de `_post_bar`/monitoring/kill switch:
        # si el dato quedo stale entre la senal y la ejecucion, se bloquea aqui.
        market_rejection = self._freshness_rejection(now)
        if market_rejection is not None:
            self._audit_rec(
                PaperEvent.RISK_REJECTED,
                {"signal_id": pe.cid, "reason": market_rejection,
                 "stage": "market_data_pre_submit"},
            )
            self._pending = None
            return
        # F-03: enforcement PROJECTED pre-fill. Ultimo punto antes del fill, con
        # el precio de ejecucion disponible (open de t+1 en paper MARKET). Si
        # algun gate projected activo se viola (o la metrica no esta disponible
        # => fail-closed), la orden NO llega al broker ni al ledger.
        execution_price = float(bar["open"])
        signed_qty = (
            pe.decision.approved_quantity
            if pe.signal.action is Action.LONG
            else -pe.decision.approved_quantity
        )
        projected = self._projected_exposure(
            instrument=pe.signal.instrument,
            signed_qty=signed_qty,
            execution_price=execution_price,
        )
        projected_decision = self.risk_engine.validate_projected_exposure(
            projected
        )
        if not projected_decision.approved:
            self._audit_rec(
                PaperEvent.RISK_REJECTED,
                {"signal_id": pe.cid, "reason": projected_decision.reason,
                 "stage": "projected_pre_fill"},
            )
            self._pending = None
            return
        try:
            self._submit_entry(pe, bar, now)
        finally:
            self._pending = None

    def _submit_entry(self, pe: PendingEntry, bar: dict, now: datetime) -> None:
        signal = pe.signal
        side = OrderSide.BUY if signal.action is Action.LONG else OrderSide.SELL
        self._set_price(float(bar["open"]))
        try:
            order = self.order_manager.create_order(
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                account_id=self.config.account_id,
                instrument=signal.instrument,
                side=side,
                quantity=pe.decision.approved_quantity,
                order_type=OrderType.MARKET,
                client_order_id=pe.cid,
                signal_id=pe.cid,
                stop_price=signal.stop_price,
            )
        except OrderStateError:
            # Idempotencia B7: el cid ya existe -> no duplicar exposición.
            self._audit_rec(PaperEvent.ENTRY_SKIPPED_KILL_SWITCH, {"signal_id": pe.cid})
            return
        self.order_manager.approve(order.order_id)
        self.order_manager.submit(order.order_id)
        self._signed.add(pe.cid)
        self._audit_rec(
            PaperEvent.ENTRY_SUBMITTED,
            {
                "signal_id": pe.cid,
                "order_id": order.order_id,
                "bar_open_time": bar["ts"],
                "open": float(bar["open"]),
            },
        )
        sub = self.broker.submit_order(order)
        self._note_broker_success(now)
        if sub.status is SubmitStatus.CONFIRMED and sub.broker_order is not None:
            self._apply_fills(pe.cid, sub.broker_order, now)
        elif sub.status is SubmitStatus.REJECTED:
            self.order_manager.reject(order.order_id, sub.detail or "rechazada")
            self._audit_rec(
                PaperEvent.ENTRY_REJECTED_BROKER,
                {"signal_id": pe.cid, "order_id": order.order_id, "detail": sub.detail},
            )
        elif sub.status is SubmitStatus.UNKNOWN:
            # LIVE-07 (recovery): la incertidumbre del submit debe reflejarse en
            # el estado INTERNO de la orden (UNKNOWN transitorio, S-04-a). Asi
            # Monitoring/EXECUTION la observa (>0 UNKNOWN => no autoriza) y la
            # reconciliacion la trata como UNKNOWN (no MISSING_BROKER). NO hay
            # resubmit ciego: la unica salida es la resolucion por lookup.
            self.order_manager.mark_unknown(order.order_id)
            self._pending_resolution.add(pe.cid)
            self._audit_rec(
                PaperEvent.SUBMISSION_UNKNOWN,
                {"signal_id": pe.cid, "order_id": order.order_id},
            )

    # ----------------------------------------------------------- salidas

    def _evaluate_exit(self, bar: dict, now: datetime) -> None:
        pos = self._position
        if pos is None:
            return
        low, high, close = (float(bar["low"]), float(bar["high"]), float(bar["close"]))
        stop = pos.stop_price
        hit_stop = (pos.is_long and stop is not None and low <= stop) or (
            not pos.is_long and stop is not None and high >= stop
        )
        if hit_stop:
            reason = "STOP"
            price = stop
        elif self.config.max_bars_in_trade is not None:
            # Basado en open_time/intervalo: correcto también tras un restart.
            elapsed = int(bar["ts"]) - pos.entry_open_time
            bars_held = (
                elapsed / self._interval_seconds if self._interval_seconds else 0
            )
            if bars_held < self.config.max_bars_in_trade:
                return
            reason = "TIME"
            price = close
        else:
            return
        self._audit_rec(
            PaperEvent.EXIT_STOP if reason == "STOP" else PaperEvent.EXIT_TIME,
            {"position_id": pos.cid, "price": price},
        )
        self._close_position(pos, price, reason, now)

    def _close_position(
        self, pos: PaperPosition, price: float, reason: str, now: datetime
    ) -> None:
        self._last_exit_reason = reason
        reduce_side = OrderSide.SELL if pos.is_long else OrderSide.BUY
        exit_cid = f"EXIT:{pos.cid}"
        self._set_price(price)
        order = self.order_manager.create_order(
            strategy_id=getattr(self.strategy.metadata, "strategy_id", "?"),
            strategy_version=getattr(self.strategy.metadata, "version", ""),
            account_id=self.config.account_id,
            instrument=pos.instrument,
            side=reduce_side,
            quantity=abs(pos.quantity),
            order_type=OrderType.MARKET,
            client_order_id=exit_cid,
            signal_id=pos.cid,
            reduce_only=True,
        )
        self.order_manager.approve(order.order_id)
        self.order_manager.submit(order.order_id)
        self._signed.add(exit_cid)
        self._audit_rec(
            PaperEvent.EXIT_SUBMITTED,
            {
                "position_id": pos.cid,
                "order_id": order.order_id,
                "exit_cid": exit_cid,
                "reason": reason,
            },
        )
        sub = self.broker.submit_order(order)
        self._note_broker_success(now)
        if sub.status is SubmitStatus.CONFIRMED and sub.broker_order is not None:
            self._apply_fills(exit_cid, sub.broker_order, now)
            trade = self._trades[-1]
            self._audit_rec(
                PaperEvent.EXIT_FILLED,
                {
                    "position_id": pos.cid,
                    "order_id": order.order_id,
                    "exit_cid": exit_cid,
                    "exit_reason": reason,
                    "exit_price": trade.exit_price,
                    "pnl": trade.pnl,
                },
            )
        else:
            # El cierre no llegó al broker: faill-safe, se mantiene la señal de
            # salida en auditoría; el engine queda bloqueado por monitoring.
            self._audit_rec(
                PaperEvent.BROKER_ERROR,
                {"position_id": pos.cid, "exit_cid": exit_cid, "detail": "exit sin confirmar"},
            )

    # ------------------------------------------------------------- fills

    def _record_broker_order_id(self, cid: str, broker_order_id) -> None:
        """Correlaciona `client_order_id <-> broker_order_id` en la orden interna.

        LIVE-02: la identidad estable interna es `client_order_id`; el
        `broker_order_id` observado se registra en cuanto el broker lo asigna
        (submit confirmado o resolucion por lookup). Metadata, no transicion.
        """
        if not broker_order_id:
            return
        order = next(
            (o for o in self.order_manager.orders.values() if o.client_order_id == cid),
            None,
        )
        if order is not None:
            self.order_manager.set_broker_order_id(order.order_id, broker_order_id)

    def _apply_fills(self, cid: str, broker_order, now: datetime) -> None:
        order = next(
            (o for o in self.order_manager.orders.values() if o.client_order_id == cid),
            None,
        )
        if order is None:
            return
        # LIVE-02: registrar el id del broker en cuanto se conoce (correlacion).
        self._record_broker_order_id(
            cid, getattr(broker_order, "broker_order_id", None)
        )
        fills = list(getattr(broker_order, "fills", ()) or ())
        if not fills and broker_order.filled_quantity > 0 and broker_order.avg_fill_price:
            # Broker sin detalle de fills pero con agregado autoritativo.
            fills = [
                BrokerFill(
                    broker_order_id=broker_order.broker_order_id,
                    quantity=broker_order.filled_quantity,
                    price=broker_order.avg_fill_price,
                    timestamp=now,
                )
            ]
        if not fills:
            return
        # LIVE-02: idempotencia de fills. Si TODOS los fills del lote ya estan
        # aplicados al ledger (misma identidad `(cid, fill_sequence)`), no
        # re-aplicar: protege reconnect, polling y re-entrega del mismo lote
        # (evita `apply_fill` sobre orden terminal y auditoria duplicada).
        if all(
            self._ledger.is_applied(cid, int(getattr(f, "fill_sequence", i)))
            for i, f in enumerate(fills)
        ):
            return
        qty = sum(f.quantity for f in fills)
        vwap = sum(f.quantity * f.price for f in fills) / qty
        if qty <= 0:
            return
        fill_ts = fills[-1].timestamp
        commission = sum(getattr(f, "commission", 0.0) for f in fills)
        order = self.order_manager.apply_fill(
            order.order_id,
            Fill(
                order_id=order.order_id,
                quantity=qty,
                price=vwap,
                timestamp=fill_ts,
                commission=commission,
            ),
        )
        self._order_fills[cid] = self._order_fills.get(cid, ()) + tuple(fills)
        is_exit = cid.startswith("EXIT:")
        refs = {"order_id": order.order_id, "quantity": qty, "price": vwap}
        if is_exit:
            refs["exit_cid"] = cid
        self._audit_rec(
            PaperEvent.EXIT_FILLED if is_exit else PaperEvent.ENTRY_FILLED, refs
        )
        self._apply_fill_to_ledger(order, cid, fills, vwap, qty, fill_ts)

    def _apply_fill_to_ledger(
        self, order, cid: str, fills: list, vwap: float, qty: float, at: datetime
    ) -> None:
        """Aplica el fill al ledger (autoridad) y proyecta `PaperPosition`.

        La contabilidad (cash/posición/realized) la realiza `AccountLedger`. El
        engine solo mantiene la proyección `PaperPosition` (metadata operativa:
        stop/cid/tiempos) y los eventos de audit/trades.

        Idempotencia (F-06A): la identidad es `(client_order_id, fill_sequence)`.
        Se aplica CADA fill del lote individualmente al ledger (fuente unica de
        idempotencia): todos los `(cid, fill_sequence)` quedan registrados, de
        modo que reaplicar el mismo lote es completamente idempotente. La
        proyeccion `PaperPosition`/audit se calcula una vez sobre el neto.
        Un broker agregado sin detalle de fills usa `(cid, 0)`.
        """
        before = self._ledger.position(order.instrument)
        # `all_applied` = todos los fills del lote ya estaban registrados.
        all_applied = True
        for i, f in enumerate(fills):
            seq = int(getattr(f, "fill_sequence", i))
            if not self._ledger.is_applied(cid, seq):
                all_applied = False
                self._ledger.apply_fill(
                    instrument=order.instrument,
                    side=order.side.value,
                    quantity=float(f.quantity),
                    price=float(f.price),
                    client_order_id=cid,
                    fill_sequence=seq,
                    # LIVE-06: commission OBSERVADA del fill (importe monetario);
                    # antes se descartaba (0.0). Idempotente por (cid, seq).
                    commission=float(getattr(f, "commission", 0.0) or 0.0),
                )
        already = all_applied
        after = self._ledger.position(order.instrument)

        # --- proyeccion PaperPosition (metadata operativa) ---
        if after is None:
            # cierre completo
            if before is not None:
                self._trades.append(
                    PaperTrade(
                        cid=before and self._position.cid if self._position else cid,
                        strategy_id=str(order.strategy_id or ""),
                        instrument=order.instrument,
                        side="LONG" if before.is_long else "SHORT",
                        quantity=abs(before.quantity),
                        entry_price=before.avg_entry_price,
                        entry_time=self._position.opened_at if self._position else at,
                        entry_open_time=(
                            self._position.entry_open_time if self._position else 0
                        ),
                        exit_price=vwap,
                        exit_time=at,
                        exit_open_time=int(self._last_open_time or 0),
                        exit_reason=self._last_exit_reason or "EXIT",
                        pnl=(
                            (vwap - before.avg_entry_price) * abs(before.quantity)
                            if before.is_long
                            else (before.avg_entry_price - vwap) * abs(before.quantity)
                        ),
                        stop_price=(
                            self._position.stop_price if self._position else None
                        ),
                    )
                )
                cid_meta = self._position.cid if self._position else cid
                self._last_exit_reason = None
                self._position = None
                self._audit_rec(
                    PaperEvent.POSITION_CLOSED, {"position_id": cid_meta, "exit": vwap}
                )
            if already:
                return
            return

        if self._position is None:
            self._position = PaperPosition(
                cid=order.signal_id,
                instrument=order.instrument,
                quantity=after.quantity,
                avg_entry_price=after.avg_entry_price,
                stop_price=order.stop_price,
                opened_at=at,
                entry_open_time=int(self._last_open_time or 0),
                entry_bar_index=len(self._bars) - 1,
            )
            if not already:
                self._audit_rec(
                    PaperEvent.POSITION_OPENED,
                    {"position_id": self._position.cid, "quantity": after.quantity,
                     "entry": after.avg_entry_price},
                )
            return

        # actualiza metadata de la proyeccion (quantity/avg del ledger)
        prev_qty = self._position.quantity
        self._position = replace(
            self._position,
            quantity=after.quantity,
            avg_entry_price=after.avg_entry_price,
        )
        if not already:
            reduced = abs(after.quantity) < abs(prev_qty)
            if reduced:
                self._audit_rec(
                    PaperEvent.POSITION_UPDATED,
                    {"position_id": self._position.cid, "quantity": after.quantity},
                )

    def _realize(self, pos: PaperPosition, qty: float, price: float) -> float:
        """Compatibilidad: el P&L realizado vive en el ledger (F-06A)."""
        return self._ledger._realize(
            LedgerPosition(
                instrument=pos.instrument,
                quantity=pos.quantity,
                avg_entry_price=pos.avg_entry_price,
            ),
            qty,
            price,
        )

    # ---------------------------------------------------------- señales/riesgo

    def _on_signal(self, bar: dict, now: datetime) -> None:
        # La estrategia recibe CADA barra cerrada (misma interfaz que el
        # backtest). Sus señales se descartan cuando el engine no puede abrir.
        signal = self.strategy.generate_signal(self._bars)
        if signal.action is Action.NO_TRADE:
            return
        if self._position is not None:
            self._audit_rec(PaperEvent.SIGNAL_WHILE_IN_POSITION, {})
            return
        if self._pending_resolution:
            # Fail-safe B7: con un submission UNKNOWN sin resolver no se
            # encadena una segunda exposición sobre el mismo instrumento.
            self._audit_rec(
                PaperEvent.ENTRY_SKIPPED_BROKER,
                {"detail": "submission UNKNOWN sin resolver"},
            )
            return
        self._audit_rec(
            PaperEvent.SIGNAL,
            {
                "strategy_id": signal.strategy_id,
                "instrument": signal.instrument,
                "action": signal.action.value,
                "bar_open_time": bar["ts"],
            },
        )
        decision = self.risk_engine.validate(
            signal, self._risk_state(), point_value=self.config.point_value
        )
        self._audit_rec(
            PaperEvent.RISK_DECISION,
            {
                "strategy_id": signal.strategy_id,
                "approved": decision.approved,
                "quantity": decision.approved_quantity,
                "reason": decision.reason,
            },
        )
        if not decision.approved:
            self._audit_rec(PaperEvent.RISK_REJECTED, {"reason": decision.reason})
            return
        if decision.approved_quantity < self.config.min_size:
            self._audit_rec(PaperEvent.RISK_REJECTED, {"reason": "min_size"})
            return
        cid = f"{signal.strategy_id}:{signal.instrument}:{bar['ts']}"
        if cid in self._signed:
            return  # guardia de reintento de restart: sin duplicación
        self._pending = PendingEntry(
            cid=cid,
            signal=signal,
            decision=decision,
            bar_open_time=int(bar["ts"]),
            staged_at=now,
        )
        self._audit_rec(
            PaperEvent.ENTRY_STAGED,
            {"signal_id": cid, "bar_open_time": bar["ts"]},
        )

    def _risk_state(self) -> AccountRiskState:
        peak = self._peak_equity or 1.0
        dd = 0.0 if peak <= 0 else max(0.0, 1.0 - self._last_equity / peak)
        return AccountRiskState(
            equity=self._last_equity,
            balance=self._last_cash,
            open_positions=0 if self._position is None else 1,
            current_daily_pnl=self._day_pnl,
            current_drawdown=dd,
            kill_switch=False,
            account_active=True,
            strategy_active=True,
        )

    def _projected_exposure(
        self, *, instrument: str, signed_qty: float, execution_price: float
    ) -> ProjectedExposure:
        """Metricas de exposicion PROYECTADA (post-trade) para el gate pre-fill.

        Deriva del ledger (autoridad) + la orden propuesta + el precio de
        ejecucion. Value object efimero; NO se persiste ni crea una segunda
        autoridad. Para el instrumento de la orden usa `execution_price`; para
        posiciones existentes, los marks del runtime (`_marks`). Si falta un
        mark/precio valido => metricas `None` (fail-closed si el gate esta
        activo). `equity` = equity de referencia pre-trade del ledger.
        """
        marks = self._marks()
        current_val = self._ledger.valuation(marks)
        equity = (
            current_val.equity if current_val is not None else self._ledger.cash
        )
        projected_qty: dict[str, float] = {
            p.instrument: p.quantity for p in self._ledger.positions()
        }
        projected_qty[instrument] = (
            projected_qty.get(instrument, 0.0) + signed_qty
        )
        by_instrument: dict[str, float] = {}
        for i, q in projected_qty.items():
            if abs(q) <= _EPS:
                continue
            price = execution_price if i == instrument else marks.get(i)
            if not _valid_price(price):
                return ProjectedExposure(
                    equity=equity,
                    gross_exposure=None,
                    net_exposure=None,
                    exposure_by_instrument=None,
                )
            by_instrument[i] = q * float(price)
        gross = sum(abs(v) for v in by_instrument.values())
        net = sum(by_instrument.values())
        return ProjectedExposure(
            equity=equity,
            gross_exposure=gross,
            net_exposure=net,
            exposure_by_instrument=by_instrument,
        )

    # ---------------------------------------------------------- mark / equity

    def _mark(self, bar: dict, closed: ClosedBarEvent, now: datetime) -> None:
        # F-06B: la valoracion interna se DERIVA del ledger + mark del runtime,
        # NO de `broker.get_account_state()` (el expected interno no depende del
        # broker). El broker sigue alimentando solo el `price_source` de fills.
        self._set_price(float(bar["close"]))
        self._note_broker_success(now)
        marks = self._marks()
        val = self._ledger.valuation(marks)
        if val is None:
            # Fail-closed: posicion abierta sin mark valido -> valoracion
            # incompleta. NO se emite equity falsa; no se muta accounting.
            self._audit_rec(
                PaperEvent.BROKER_ERROR, {"detail": "mark invalido/ausente"}
            )
            return
        self._last_cash = self._ledger.cash
        self._last_equity = val.equity
        self._equity_curve.append([closed.open_time, self._last_equity])
        self._peak_equity = max(self._peak_equity, self._last_equity)
        day = _dt_from_open(closed.close_time).date()
        if self._day_key != day:
            self._day_key = day
            self._day_start_equity = self._last_equity
        self._day_pnl = self._last_equity - (self._day_start_equity or 0.0)

    def _marks(self) -> dict[str, float]:
        """Marks por instrumento para la valoracion (runtime de mercado).

        En paper, el precio de la barra en curso es el mark vigente del
        instrumento (misma referencia que usa el PaperBroker para fills).
        """
        try:
            return {self.config.instrument: float(self.broker.price_source(
                self.config.instrument
            ))}
        except Exception:  # noqa: BLE001 - price_source no disponible
            return {}

    # ------------------------------------------------------- broker helpers

    def _set_price(self, price: float) -> None:
        self.broker.price_source = lambda _instrument: price

    def _broker_healthy(self) -> bool:
        try:
            return bool(self.broker.health())
        except BrokerError:
            return False

    def _note_broker_success(self, now: datetime) -> None:
        self._last_broker_success = now
        self._broker_errors = 0

    def note_data_feed(self, state: str, quality: str) -> None:
        """LIVE-04 (G-MD-C): registra el estado REAL del feed de market data.

        Lo inyecta el ForwardRunner/streaming con el estado de `LiveDataEngine`
        (`engine.state`/`engine.quality`). Monitoring/kill switch dejan de
        asumir CONNECTED/HEALTHY cuando el feed esta stale/degraded/failed.
        """
        self._data_feed_state = state
        self._data_feed_quality = quality

    def _resolve_pending_submissions(self) -> None:
        """Resuelve submissions UNKNOWN por broker lookup.

        LIVE-02: distingue explicitamente (no mezclar):
          * **NOT_FOUND** (`get_order` -> None, o `OrderNotFoundError`): el broker
            respondio que NO conoce la orden. NO es un timeout/error, NO confirma
            cancelacion, NO es evidencia de que el submit no ocurrio. Se mantiene
            UNKNOWN/pending (fail-safe); sin resubmit; sin mutacion terminal.
          * **LOOKUP_UNCERTAIN** (`BrokerConnectionError`/transporte): no se pudo
            determinar el estado. Permanece pending; sin resubmit.
          * **INDETERMINATE** (`state is UNKNOWN`): el broker respondio pero el
            estado no es determinable. Permanece pending.
          * **RESUELTO**: estado/fills determinables -> se adopta.
        """
        for cid in list(self._pending_resolution):
            try:
                bro = self.broker.get_order(cid)
            except OrderNotFoundError:
                # NOT_FOUND explicito (algunos adapters lo lanzan en vez de None).
                continue
            except BrokerConnectionError:
                # LOOKUP_UNCERTAIN: sin respuesta; reintentar en la proxima.
                continue
            except BrokerError:
                continue
            if bro is None:
                # NOT_FOUND: el broker no conoce la orden. Fail-safe: permanece
                # UNKNOWN/pending (NO terminal, NO resubmit, NO cancel implicito).
                continue
            if bro.state is BrokerOrderState.UNKNOWN:
                # INDETERMINATE: el broker respondio, estado no determinable.
                continue
            self._pending_resolution.discard(cid)
            # LIVE-02: registrar el id del broker al resolver por lookup.
            self._record_broker_order_id(cid, getattr(bro, "broker_order_id", None))
            order = next(
                (
                    o
                    for o in self.order_manager.orders.values()
                    if o.client_order_id == cid
                ),
                None,
            )
            has_fill = bool(bro.fills) or float(
                getattr(bro, "filled_quantity", 0.0) or 0.0
            ) > 0
            if has_fill:
                self._apply_fills(cid, bro, self._clock())
                self._audit_rec(
                    PaperEvent.SUBMISSION_RESOLVED,
                    {"signal_id": cid, "broker_state": bro.state.value},
                )
            elif bro.state is BrokerOrderState.REJECTED:
                # LIVE-07: fijar el estado interno desde la verdad del broker.
                # UNKNOWN es transitorio; NUNCA se infiere localmente REJECTED ni
                # CANCELLED por ausencia de respuesta.
                if order is not None and order.status is OrderStatus.UNKNOWN:
                    self.order_manager.reject(
                        order.order_id,
                        bro.reject_reason or "rechazada por broker",
                    )
                self._audit_rec(
                    PaperEvent.ENTRY_REJECTED_BROKER,
                    {"signal_id": cid, "broker_state": bro.state.value},
                )
            elif bro.state is BrokerOrderState.CANCELLED:
                if order is not None and order.status is OrderStatus.UNKNOWN:
                    self.order_manager.cancel(order.order_id)
                self._audit_rec(
                    PaperEvent.ENTRY_REJECTED_BROKER,
                    {"signal_id": cid, "broker_state": bro.state.value},
                )
            elif bro.state is BrokerOrderState.SUBMITTED:
                # Broker confirma la orden viva y sin fills: UNKNOWN -> SUBMITTED.
                if order is not None and order.status is OrderStatus.UNKNOWN:
                    self.order_manager.submit(order.order_id)

    # -------------------------------------------------------- reconciliación

    def _internal_snapshot(self) -> InternalSnapshot:
        orders: list[OrderSnapshot] = []
        fills: list[FillSnapshot] = []
        for o in self.order_manager.orders.values():
            orders.append(
                OrderSnapshot(
                    client_order_id=o.client_order_id,
                    instrument=o.instrument,
                    quantity=o.quantity,
                    state=o.status.value,
                    filled_quantity=o.fill_quantity,
                    avg_fill_price=o.average_fill_price,
                    # LIVE-06: id observado del broker (None hasta conocerlo).
                    broker_order_id=o.broker_order_id,
                )
            )
            fl = self._order_fills.get(o.client_order_id, ())
            if fl:
                q = sum(f.quantity for f in fl)
                vw = sum(f.quantity * f.price for f in fl) / q if q > 0 else None
                fills.append(
                    FillSnapshot(
                        client_order_id=o.client_order_id,
                        quantity=q,
                        avg_price=vw,
                        fill_count=len(fl),
                    )
                )
        pos = self._position
        positions = (
            (PositionSnapshot(instrument=pos.instrument, quantity=pos.quantity, avg_entry_price=pos.avg_entry_price),)
            if pos is not None
            else ()
        )
        # F-06C: la cuenta ESPERADA (expected) procede del ledger interno
        # (autoridad contable), NO del broker. El broker alimenta únicamente el
        # lado OBSERVED/actual (`broker_state_from_adapter`); así ACCOUNT deja de
        # ser tautológico y una divergencia real ledger vs broker se detecta.
        # Mark: el mismo del runtime (`_marks()`, fijado por `_mark()`), sin
        # persistirlo ni cachearlo aparte. Si la valoración no es completa
        # (posición abierta sin mark válido) -> `account=None` (UNKNOWN), sin
        # fabricar equity ni consultar al broker.
        val = self._ledger.valuation(self._marks())
        account = (
            None
            if val is None
            else AccountSnapshot(
                self.config.account_id, self._ledger.cash, val.equity,
                # LIVE-06: moneda canonica esperada (config; NO del broker).
                currency=self.config.currency,
            )
        )
        return InternalSnapshot(
            orders=tuple(orders), fills=tuple(fills), positions=positions, account=account
        )

    def reconcile(self):
        """Ejecuta reconciliación (B4) y devuelve el reporte (solo lectura)."""
        internal = resolve_unknown_orders(self._internal_snapshot(), self.broker)
        report = reconcile_with_broker(self.reconciliation, internal, self.broker)
        self._last_report = report
        self._last_report_at = self._clock()
        self._audit_rec(
            PaperEvent.RECONCILIATION,
            {"status": report.status.value, "diffs": len(report.diffs)},
        )
        return report

    # ------------------------------------------------------------ monitoring

    def run_monitoring(self, now: datetime | None = None):
        """Alimenta Monitoring (B6) y aplica al Kill Switch (B5): detecta, decide."""
        now = now or self._clock()
        data_last = self._last_bar_close_dt
        # LIVE-04 (G-MD-C): usa el estado REAL del feed (inyectado por el
        # ForwardRunner/streaming), no un HEALTHY/CONNECTED hardcodeado.
        data_obs = observe_data_feed(
            self._data_feed_state, self._data_feed_quality, last_event_at=data_last
        ) if data_last is not None else observe_data_feed(
            self._data_feed_state, self._data_feed_quality, None
        )
        broker_obs = observe_broker(
            self.broker,
            last_success=self._last_broker_success,
            consecutive_errors=self._broker_errors,
        )
        recon_obs = (
            observe_reconciliation_report(self._last_report, self._last_report_at)
            if self._last_report is not None
            else observe_reconciliation(None, None)
        )
        session = self.sessions.get_session(self.config.instrument, self._clock())
        ses_obs = observe_session(session.state.value, self._clock())
        sys_obs = observe_system(now, heartbeat_at=now, persistence_ok=True, unhandled_errors=0)
        ex_obs = observe_execution(self.order_manager.orders.values())
        observations = collect(
            data_feed=data_obs,
            broker=broker_obs,
            reconciliation=recon_obs,
            session=ses_obs,
            system=sys_obs,
            execution=ex_obs,
        )
        snapshot = self.monitoring.evaluate(observations)
        apply_to_kill_switch(self.kill_switch, snapshot)
        self._last_monitoring = snapshot
        self._audit_rec(
            PaperEvent.MONITORING,
            {
                status.value: [
                    r.component.value for r in snapshot.results
                    if r.status is status
                ]
                for status in HealthStatus
            },
        )
        return snapshot

    def _post_bar(self, now: datetime) -> None:
        self.reconcile()
        self.run_monitoring(now)

    # ------------------------------------------------------- snapshot/restore

    def snapshot(self) -> dict:
        """Estado serializable JSON para restart sin duplicar exposición.

        F-06C-restart: el snapshot es un envelope con un `checkpoint_id` comun a
        los bloques `ledger` (expected, estado interno) y `broker` (observed,
        estado del broker). Ambos bloques siguen siendo semanticamente
        independientes; el `checkpoint_id` solo garantiza que pertenecen al mismo
        instante logico (nunca `ledger @ N` con `broker @ N-1`).
        """
        pos = self._position
        position = None
        if pos is not None:
            position = {
                "cid": pos.cid,
                "instrument": pos.instrument,
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "stop_price": pos.stop_price,
                "opened_at": pos.opened_at.isoformat(),
                "entry_open_time": pos.entry_open_time,
                "entry_bar_index": pos.entry_bar_index,
            }
        # Checkpoint logico: reutiliza la secuencia de auditoria (unica y
        # monotona en el run) en lugar de inventar un nuevo contador.
        checkpoint_id = f"{self.run_id}:{self._seq}"
        ledger_block = self._ledger.snapshot()
        ledger_block["checkpoint_id"] = checkpoint_id
        broker_block = self.broker.snapshot(checkpoint_id=checkpoint_id)
        return {
            "version": 1,
            "checkpoint_id": checkpoint_id,
            "run_id": self.run_id,
            "instrument": self.config.instrument,
            "account_id": self.config.account_id,
            "last_open_time": self._last_open_time,
            "signed_signals": sorted(self._signed),
            "position": position,
            "ledger": ledger_block,
            "broker": broker_block,
            "realized_pnl": self._ledger.realized_pnl,
            "peak_equity": self._peak_equity,
            "day_key": self._day_key.isoformat() if self._day_key else None,
            "day_start_equity": self._day_start_equity,
            "day_pnl": self._day_pnl,
            "equity_curve": [list(p) for p in self._equity_curve],
            "trades": [
                {
                    "cid": t.cid,
                    "strategy_id": t.strategy_id,
                    "instrument": t.instrument,
                    "side": t.side,
                    "quantity": t.quantity,
                    "entry_price": t.entry_price,
                    "entry_time": t.entry_time.isoformat(),
                    "entry_open_time": t.entry_open_time,
                    "exit_price": t.exit_price,
                    "exit_time": t.exit_time.isoformat(),
                    "exit_open_time": t.exit_open_time,
                    "exit_reason": t.exit_reason,
                    "pnl": t.pnl,
                    "stop_price": t.stop_price,
                }
                for t in self._trades
            ],
        }

    def load_snapshot(self, snap: dict) -> None:
        # ---------------------------------------------------------- PREVALIDAR
        # F-06C-restart: TODO se valida/parsea en locales ANTES de mutar el
        # engine. Si algo falla, el engine queda EXACTAMENTE como estaba
        # (ledger, `_position`, broker y metadata intactos). Al final se aplica
        # el restore con operaciones ya deterministicamente no-fallables.
        p = snap.get("position")
        new_position = None
        if p is not None:
            new_position = PaperPosition(
                cid=p["cid"],
                instrument=p["instrument"],
                quantity=p["quantity"],
                avg_entry_price=p["avg_entry_price"],
                stop_price=p["stop_price"],
                opened_at=datetime.fromisoformat(p["opened_at"]),
                entry_open_time=p["entry_open_time"],
                entry_bar_index=p["entry_bar_index"],
            )
        # F-06A: snapshot legacy (sin `ledger`) se reconstruye desde
        # `realized_pnl`/posicion preservando el contrato.
        has_ledger = "ledger" in snap
        checkpoint_id = None
        if has_ledger:
            ledger_block = snap["ledger"]
            # F-06C-restart: rehidrata el estado OBSERVED del broker desde su
            # propio bloque (nunca desde el ledger). Un restart sin bloque
            # `broker` NO crea un broker fresco en silencio: fail-closed.
            checkpoint_id = snap.get("checkpoint_id")
            if checkpoint_id is None:
                raise BrokerSnapshotError(
                    "restart sin checkpoint_id: snapshot no broker-restore-capable"
                )
            ledger_cp = ledger_block.get("checkpoint_id")
            if ledger_cp != checkpoint_id:
                raise BrokerSnapshotError(
                    f"checkpoint mismatch: ledger={ledger_cp!r} "
                    f"!= engine={checkpoint_id!r}"
                )
            if "broker" not in snap:
                raise BrokerSnapshotError(
                    "restart con ledger pero sin bloque broker: legacy no "
                    "broker-restore-capable (F-06C-restart); no se inventa un "
                    "broker desde el ledger."
                )
            # Prevalida el bloque broker SIN mutar (parseo puro).
            self.broker.validate_snapshot(
                snap["broker"], checkpoint_id=checkpoint_id
            )
        else:
            ledger_block = {
                "cash": self._ledger.cash,
                "realized_pnl": snap.get("realized_pnl", 0.0),
                "positions": (
                    [
                        {
                            "instrument": p.instrument,
                            "quantity": p.quantity,
                            "avg_entry_price": p.avg_entry_price,
                        }
                    ]
                    if p is not None
                    else []
                ),
                "applied_fills": [],
            }
        # Prevalida el bloque ledger en una instancia TEMPORAL: si el bloque
        # esta corrupto, falla aqui sin haber tocado el ledger real.
        self._ledger.probe_snapshot(ledger_block)
        # Prevalida la metadata restante parseandola en locales (day_key,
        # equity_curve, trades): un dataclass invalido falla aqui, sin mutar.
        dk = snap.get("day_key")
        new_day_key = date.fromisoformat(dk) if dk else None
        new_equity_curve = [list(pt) for pt in snap.get("equity_curve", [])]
        new_trades = [
            PaperTrade(
                cid=t["cid"],
                strategy_id=t["strategy_id"],
                instrument=t["instrument"],
                side=t["side"],
                quantity=t["quantity"],
                entry_price=t["entry_price"],
                entry_time=datetime.fromisoformat(t["entry_time"]),
                entry_open_time=t["entry_open_time"],
                exit_price=t["exit_price"],
                exit_time=datetime.fromisoformat(t["exit_time"]),
                exit_open_time=t["exit_open_time"],
                exit_reason=t["exit_reason"],
                pnl=t["pnl"],
                stop_price=t["stop_price"],
            )
            for t in snap.get("trades", [])
        ]

        # ------------------------------------------------------------ APLICAR
        # A partir de aqui, con todo prevalidado, las operaciones son
        # deterministicamente no-fallables (mismo input que la prevalidacion).
        self._position = new_position
        self._ledger.load_snapshot(ledger_block)
        if has_ledger:
            self.broker.load_snapshot(
                snap["broker"], checkpoint_id=checkpoint_id
            )
        self._last_open_time = snap.get("last_open_time")
        self._signed = set(snap.get("signed_signals", ()))
        self._peak_equity = snap.get("peak_equity", 0.0)
        self._day_key = new_day_key
        self._day_start_equity = snap.get("day_start_equity")
        self._day_pnl = snap.get("day_pnl", 0.0)
        self._equity_curve = new_equity_curve
        self._trades = new_trades
        self._audit_rec(PaperEvent.READY, {"run_id": self.run_id, "restored": True})