"""Paper Trading (B7): integración paper-first del sistema Atlas completo.

Solo PaperBrokerAdapter (nunca capital real). Misma semántica temporal que el
backtest: decisión sobre la barra cerrada en `t`, ejecución en el open de
`t+1`; audit trail reconstruible; reiniciable sin duplicar exposición.
"""

from papertrading.engine import PaperTradingEngine
from papertrading.models import (
    PaperAuditEntry,
    PaperEngineConfig,
    PaperEvent,
    PaperPosition,
    PaperTrade,
    PendingEntry,
)
from papertrading.replay import ReplayMarketDataAdapter

__all__ = [
    "PaperTradingEngine",
    "ReplayMarketDataAdapter",
    "PaperAuditEntry",
    "PaperEngineConfig",
    "PaperEvent",
    "PaperPosition",
    "PaperTrade",
    "PendingEntry",
]