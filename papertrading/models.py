"""Modelos de Paper Trading (B7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from monitoring.models import MonitorConfig
from risk.types import RiskConfig


class PaperEvent(str, Enum):
    """Tipos de evento del audit trail (traza reconstruible de extremo a extremo)."""

    READY = "READY"
    BAR_CLOSED = "BAR_CLOSED"
    BAR_SKIPPED = "BAR_SKIPPED"
    SIGNAL = "SIGNAL"
    SIGNAL_WHILE_IN_POSITION = "SIGNAL_WHILE_IN_POSITION"
    RISK_DECISION = "RISK_DECISION"
    RISK_REJECTED = "RISK_REJECTED"
    ENTRY_STAGED = "ENTRY_STAGED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_FILLED = "ENTRY_FILLED"
    ENTRY_SKIPPED_SESSION = "ENTRY_SKIPPED_SESSION"
    ENTRY_SKIPPED_KILL_SWITCH = "ENTRY_SKIPPED_KILL_SWITCH"
    ENTRY_SKIPPED_BROKER = "ENTRY_SKIPPED_BROKER"
    ENTRY_REJECTED_BROKER = "ENTRY_REJECTED_BROKER"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    SUBMISSION_RESOLVED = "SUBMISSION_RESOLVED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"
    EXIT_STOP = "EXIT_STOP"
    EXIT_TIME = "EXIT_TIME"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    EXIT_FILLED = "EXIT_FILLED"
    RECONCILIATION = "RECONCILIATION"
    MONITORING = "MONITORING"
    BROKER_ERROR = "BROKER_ERROR"
    RESTART_SKIP = "RESTART_SKIP"


@dataclass(frozen=True, slots=True)
class PaperAuditEntry:
    """Entrada del audit trail con IDs que correlacionan una operación."""

    id: str
    at: datetime
    event: str
    refs: dict


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """Decisión tomada sobre la barra `t` que se ejecutará en el open de `t+1`."""

    cid: str
    signal: object
    decision: object
    bar_open_time: int
    staged_at: datetime


@dataclass(frozen=True, slots=True)
class PaperPosition:
    """Posición interna del paper engine (quantity dirigida: +long / -short)."""

    cid: str
    instrument: str
    quantity: float
    avg_entry_price: float
    stop_price: float | None
    opened_at: datetime
    entry_open_time: int
    entry_bar_index: int

    @property
    def is_long(self) -> bool:
        return self.quantity > 0


@dataclass(frozen=True, slots=True)
class PaperTrade:
    """Operación cerrada (para P&L/equity determinista y audit)."""

    cid: str
    strategy_id: str
    instrument: str
    side: str
    quantity: float
    entry_price: float
    entry_time: datetime
    entry_open_time: int
    exit_price: float
    exit_time: datetime
    exit_open_time: int
    exit_reason: str
    pnl: float
    stop_price: float | None


@dataclass(frozen=True, slots=True)
class PaperEngineConfig:
    """Configuración del engine paper (B7: solo PaperBrokerAdapter)."""

    account_id: str = "PAPER-01"
    instrument: str = "BTCUSDT"
    point_value: float = 1.0
    min_size: float = 0.0
    max_bars_in_trade: int | None = None
    risk: RiskConfig = field(default_factory=RiskConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)