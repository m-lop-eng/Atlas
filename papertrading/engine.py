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

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable

from brokers.base import BrokerAdapter
from brokers.models import BrokerError, BrokerFill, BrokerOrderState, SubmitStatus
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
from risk.types import AccountRiskState
from sessions.manager import MarketSessionManager
from strategies.base.signal import Action
from strategies.base.strategy import BaseStrategy

from monitoring import utc_now
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

        self._bars: list[dict[str, Any]] = []
        self._audit: list[PaperAuditEntry] = []
        self._seq = 0
        self._pending: PendingEntry | None = None
        self._signed: set[str] = set()
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
        self._realized_pnl = 0.0
        self._peak_equity = 0.0
        self._day_key: date | None = None
        self._day_start_equity: float | None = None
        self._day_pnl = 0.0
        self._equity_curve: list = []
        self._trades: list[PaperTrade] = []

        self._last_broker_success: datetime | None = None
        self._broker_errors = 0
        self._last_exit_reason: str | None = None

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

    def on_closed_bar(self, closed: ClosedBarEvent) -> None:
        """Único punto de entrada: una barra ya cerrada (emisión de B1)."""
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

    def _apply_fills(self, cid: str, broker_order, now: datetime) -> None:
        order = next(
            (o for o in self.order_manager.orders.values() if o.client_order_id == cid),
            None,
        )
        if order is None:
            return
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
        self._update_position(order, vwap, qty, fill_ts)

    def _update_position(
        self, order, price: float, qty: float, at: datetime
    ) -> None:
        signed = qty if order.side is OrderSide.BUY else -qty
        pos = self._position
        if pos is None:
            self._position = PaperPosition(
                cid=order.signal_id,
                instrument=order.instrument,
                quantity=signed,
                avg_entry_price=price,
                stop_price=order.stop_price,
                opened_at=at,
                entry_open_time=int(self._last_open_time or 0),
                entry_bar_index=len(self._bars) - 1,
            )
            self._audit_rec(
                PaperEvent.POSITION_OPENED,
                {"position_id": self._position.cid, "quantity": signed, "entry": price},
            )
            return
        new_qty = pos.quantity + signed
        if abs(new_qty) <= _EPS:  # cierre completo
            closed_qty = abs(pos.quantity)
            pnl = self._realize(pos, closed_qty, price)
            self._trades.append(
                PaperTrade(
                    cid=pos.cid,
                    strategy_id=str(order.strategy_id or ""),
                    instrument=pos.instrument,
                    side="LONG" if pos.is_long else "SHORT",
                    quantity=closed_qty,
                    entry_price=pos.avg_entry_price,
                    entry_time=pos.opened_at,
                    entry_open_time=pos.entry_open_time,
                    exit_price=price,
                    exit_time=at,
                    exit_open_time=int(self._last_open_time or 0),
                    exit_reason=self._last_exit_reason or "EXIT",
                    pnl=pnl,
                    stop_price=pos.stop_price,
                )
            )
            self._last_exit_reason = None
            self._position = None
            self._audit_rec(
                PaperEvent.POSITION_CLOSED,
                {"position_id": pos.cid, "exit": price, "pnl": pnl},
            )
            return
        # Mismo sentido (entrada adicional) o reducción parcial.
        if (pos.is_long and new_qty > 0) or (not pos.is_long and new_qty < 0):
            added = abs(signed)
            avg = (
                pos.quantity * pos.avg_entry_price + signed * price
            ) / new_qty
            self._position = replace(
                pos, quantity=round(new_qty, 12), avg_entry_price=avg
            )
        else:
            pnl = self._realize(pos, abs(signed), price)
            self._realized_pnl += pnl
            self._position = replace(pos, quantity=round(new_qty, 12))
            self._audit_rec(
                PaperEvent.POSITION_UPDATED,
                {"position_id": pos.cid, "quantity": new_qty, "pnl": pnl},
            )

    def _realize(self, pos: PaperPosition, qty: float, price: float) -> float:
        if pos.is_long:
            pnl = (price - pos.avg_entry_price) * qty * self.config.point_value
        else:
            pnl = (pos.avg_entry_price - price) * qty * self.config.point_value
        self._realized_pnl += pnl
        return pnl

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

    # ---------------------------------------------------------- mark / equity

    def _mark(self, bar: dict, closed: ClosedBarEvent, now: datetime) -> None:
        try:
            self._set_price(float(bar["close"]))
            acct = self.broker.get_account_state()
        except BrokerError:
            self._broker_errors += 1
            self._audit_rec(PaperEvent.BROKER_ERROR, {"detail": "mark sin broker"})
            return
        self._note_broker_success(now)
        self._last_cash = acct.cash
        self._last_equity = acct.equity
        self._equity_curve.append([closed.open_time, self._last_equity])
        self._peak_equity = max(self._peak_equity, self._last_equity)
        day = _dt_from_open(closed.close_time).date()
        if self._day_key != day:
            self._day_key = day
            self._day_start_equity = self._last_equity
        self._day_pnl = self._last_equity - (self._day_start_equity or 0.0)

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

    def _resolve_pending_submissions(self) -> None:
        for cid in list(self._pending_resolution):
            try:
                bro = self.broker.get_order(cid)
            except BrokerError:
                continue
            if bro is None or bro.state is BrokerOrderState.UNKNOWN:
                continue
            self._pending_resolution.discard(cid)
            if bro.fills:
                self._apply_fills(cid, bro, self._clock())
                self._audit_rec(
                    PaperEvent.SUBMISSION_RESOLVED,
                    {"signal_id": cid, "broker_state": bro.state.value},
                )
            elif bro.state is BrokerOrderState.REJECTED or bro.state is BrokerOrderState.CANCELLED:
                self._audit_rec(
                    PaperEvent.ENTRY_REJECTED_BROKER,
                    {"signal_id": cid, "broker_state": bro.state.value},
                )

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
        account = None
        try:
            acct = self.broker.get_account_state()
            account = AccountSnapshot(acct.account_id, acct.cash, acct.equity)
        except BrokerError:
            account = None
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
        data_obs = observe_data_feed(
            "CONNECTED", "HEALTHY", last_event_at=data_last
        ) if data_last is not None else observe_data_feed("CONNECTED", "HEALTHY", None)
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
        """Estado serializable JSON para restart sin duplicar exposición."""
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
        return {
            "version": 1,
            "run_id": self.run_id,
            "instrument": self.config.instrument,
            "account_id": self.config.account_id,
            "last_open_time": self._last_open_time,
            "signed_signals": sorted(self._signed),
            "position": position,
            "realized_pnl": self._realized_pnl,
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
        self._last_open_time = snap.get("last_open_time")
        self._signed = set(snap.get("signed_signals", ()))
        p = snap.get("position")
        if p is not None:
            self._position = PaperPosition(
                cid=p["cid"],
                instrument=p["instrument"],
                quantity=p["quantity"],
                avg_entry_price=p["avg_entry_price"],
                stop_price=p["stop_price"],
                opened_at=datetime.fromisoformat(p["opened_at"]),
                entry_open_time=p["entry_open_time"],
                entry_bar_index=p["entry_bar_index"],
            )
        else:
            self._position = None
        self._realized_pnl = snap.get("realized_pnl", 0.0)
        self._peak_equity = snap.get("peak_equity", 0.0)
        dk = snap.get("day_key")
        self._day_key = date.fromisoformat(dk) if dk else None
        self._day_start_equity = snap.get("day_start_equity")
        self._day_pnl = snap.get("day_pnl", 0.0)
        self._equity_curve = [list(p) for p in snap.get("equity_curve", [])]
        self._trades = [
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
        self._audit_rec(PaperEvent.READY, {"run_id": self.run_id, "restored": True})