"""ReconnectManager (S3): resiliencia de conexión, aislada de la integridad.

Alcance S3: detectar desconexión/timeout/heartbeat perdido, reconectar con
backoff determinista y límite de intentos, re-suscribir tras reconectar, evitar
conexiones duplicadas y no republicar el mismo cierre tras una reconexión.

PRINCIPIO CRÍTICO — recuperación de transporte ≠ autorización para operar:
cuando el transporte se recupera, este manager NO declara los datos íntegros.
Un hueco pudo ocurrir durante la caída; `data_integrity_unknown` pasa a True en
cualquier reconexión y SOLO un proceso explícito de reconciliación (S4,
`confirm_data_integrity()`) puede limpiarlo. El manager nunca lo limpia por sí
mismo.

Fuera de S3: backfill/reconciliación de velas (S4), integración con
`LiveDataEngine`/B1 y PaperRunner.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from streaming.adapter import StreamingMarketDataAdapter
from streaming.backoff import BackoffPolicy
from streaming.clock import utc_now
from streaming.errors import (
    StreamingConnectionError,
    StreamingError,
    StreamingHeartbeatError,
    StreamingReconnectError,
    StreamingSubscribeError,
    StreamingTimeoutError,
)
from streaming.events import (
    StreamConnected,
    StreamData,
    StreamDisconnected,
    StreamEvent,
)

# Errores que indican pérdida de transporte (disparan reconexión).
_RECOVERABLE = (
    StreamingConnectionError,
    StreamingHeartbeatError,
    StreamingTimeoutError,
)


class ReconnectState(str, Enum):
    """Estados explícitos del ciclo de reconexión."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    WAITING_BACKOFF = "WAITING_BACKOFF"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Política de reintentos por episodio de caída.

    Attributes:
        max_attempts: intentos de reconexión antes de FAILED.
        backoff: política de espera determinista.
    """

    max_attempts: int = 5
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser >= 1")


class _DeliveredGuard:
    """Recuerda los últimos cierres entregados para no repetirlos (bounded)."""

    def __init__(self, maxlen: int = 64) -> None:
        self._maxlen = maxlen
        self._order: deque[tuple[str, int]] = deque()
        self._seen: set[tuple[str, int]] = set()

    def is_duplicate(self, key: tuple[str, int]) -> bool:
        return key in self._seen

    def remember(self, key: tuple[str, int]) -> None:
        if key in self._seen:
            return
        if len(self._order) >= self._maxlen:
            self._seen.discard(self._order.popleft())
        self._order.append(key)
        self._seen.add(key)


class ReconnectManager:
    """Orquesta reconexión + re-suscripción sobre un `StreamingMarketDataAdapter`.

    Args:
        adapter: adapter subyacente (S2 Binance o un fake en tests).
        policy: límite de intentos y backoff.
        heartbeat_timeout_seconds: inactividad tolerada antes de sondear.
        pong_timeout_seconds: plazo para un latido/respuesta tras el sondeo.
        poll_interval_seconds: espera entre sondeos de actividad.
        clock: reloj monótono inyectable (para timeouts deterministas).
        wall_clock: reloj UTC-aware inyectable (timestamps de eventos).
        sleep: función de espera inyectable (tests deterministas).
        on_state_change: callback de observabilidad de cada transición.
    """

    def __init__(
        self,
        adapter: StreamingMarketDataAdapter,
        *,
        policy: ReconnectPolicy = ReconnectPolicy(),
        heartbeat_timeout_seconds: float = 30.0,
        pong_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        on_state_change: Callable[[ReconnectState], None] | None = None,
    ) -> None:
        if heartbeat_timeout_seconds <= 0 or pong_timeout_seconds <= 0:
            raise ValueError("los timeouts de heartbeat deben ser > 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds debe ser > 0")
        self._adapter = adapter
        self._policy = policy
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._pong_timeout = pong_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._on_state_change = on_state_change

        self._state = ReconnectState.DISCONNECTED
        self._subscription: tuple[str, int] | None = None
        self._last_activity: float | None = None
        self._data_integrity_unknown = False
        self._recovering = False
        self._pending: deque[StreamEvent] = deque()
        self._delivered = _DeliveredGuard()
        self._last_closed_bar: tuple[str, int] | None = None

        self.attempts = 0
        self.last_error: str | None = None

    # ---------------------------------------------------------- propiedades

    @property
    def provider_name(self) -> str:
        return self._adapter.provider_name

    @property
    def state(self) -> ReconnectState:
        return self._state

    @property
    def subscription(self) -> tuple[str, int] | None:
        return self._subscription

    @property
    def data_integrity_unknown(self) -> bool:
        """True si hubo reconexión sin reconciliación posterior (S4)."""
        return self._data_integrity_unknown

    @property
    def last_closed_bar(self) -> tuple[str, int] | None:
        """Última vela cerrada entregada `(symbol, open_time)` o None (S4)."""
        return self._last_closed_bar

    # ---------------------------------------------------------- ciclo de vida

    def connect(self) -> None:
        if self._state in (ReconnectState.CONNECTED, ReconnectState.CONNECTING):
            return  # evita conexiones duplicadas
        if self._state is ReconnectState.CLOSED:
            raise StreamingConnectionError("manager CLOSED; use un nuevo manager")
        self._set_state(ReconnectState.CONNECTING)
        try:
            self._adapter.connect()
            if self._subscription is not None:
                self._adapter.subscribe(*self._subscription)
        except StreamingConnectionError as exc:
            self._set_state(ReconnectState.DISCONNECTED)
            self.last_error = str(exc)
            raise
        self._set_state(ReconnectState.CONNECTED)
        self._last_activity = self._clock()
        self._pending.append(
            StreamConnected(
                received_at=self._wall_clock(), provider=self._adapter.provider_name
            )
        )

    def subscribe(self, symbol: str, interval_seconds: int) -> None:
        if not symbol or interval_seconds <= 0:
            raise StreamingSubscribeError(
                f"suscripción inválida: symbol={symbol!r} interval={interval_seconds}"
            )
        self._subscription = (symbol, interval_seconds)
        if self._state is ReconnectState.CONNECTED:
            self._adapter.subscribe(symbol, interval_seconds)

    def receive(self) -> StreamEvent | None:
        if self._state is ReconnectState.CLOSED:
            raise StreamingConnectionError("manager CLOSED")
        if self._state is ReconnectState.FAILED:
            raise StreamingReconnectError(self.last_error or "reconexión agotada")
        if self._pending:
            return self._pending.popleft()  # eventos de conexión auditables
        if self._state is not ReconnectState.CONNECTED:
            raise StreamingConnectionError(
                f"receive() en estado {self._state.value}"
            )

        if self._is_heartbeat_due():
            self._adapter.heartbeat()
            event = self._wait_activity()
            if event is None:
                self._recover(StreamingHeartbeatError("heartbeat perdido"))
                return self._drain_one()
            return self._dedup(event)

        try:
            event = self._adapter.receive()
        except _RECOVERABLE as exc:
            self._recover(str(exc))
            return self._drain_one()
        if event is not None:
            self._last_activity = self._clock()
        return self._dedup(event)

    def disconnect(self) -> None:
        try:
            self._adapter.disconnect()
        finally:
            self._set_state(ReconnectState.CLOSED)
            self._subscription = None

    def confirm_data_integrity(self) -> None:
        """Marca la integridad como confirmada tras reconciliación (S4).

        El manager NUNCA llama a esto por sí solo: recuperar el transporte no
        equivale a recuperar la integridad de los datos.
        """
        self._data_integrity_unknown = False

    # --------------------------------------------------------------- interno

    def _set_state(self, state: ReconnectState) -> None:
        if state is not self._state:
            self._state = state
            if self._on_state_change is not None:
                self._on_state_change(state)

    def _is_heartbeat_due(self) -> bool:
        if self._last_activity is None:
            return False
        return self._clock() - self._last_activity >= self._heartbeat_timeout

    def _wait_activity(self) -> StreamEvent | None:
        deadline = self._clock() + self._pong_timeout
        while self._clock() < deadline:
            try:
                event = self._adapter.receive()
            except _RECOVERABLE:
                return None
            if event is not None:
                self._last_activity = self._clock()
                return event
            self._sleep(self._poll_interval)
        return None

    def _recover(self, reason: str) -> None:
        if self._recovering:
            raise StreamingReconnectError("recuperación anidada no permitida")
        self._recovering = True
        try:
            self.last_error = reason
            # La caída implica posible hueco: integridad NO confirmada.
            self._data_integrity_unknown = True
            self._pending.append(
                StreamDisconnected(
                    received_at=self._wall_clock(), reason=reason, expected=False
                )
            )
            for attempt in range(1, self._policy.max_attempts + 1):
                self.attempts = attempt
                self._set_state(ReconnectState.WAITING_BACKOFF)
                self._sleep(self._policy.backoff.delay_for(attempt))
                self._set_state(ReconnectState.RECONNECTING)
                if self._try_connect():
                    self.attempts = 0
                    self._set_state(ReconnectState.CONNECTED)
                    self._last_activity = self._clock()
                    self._pending.append(
                        StreamConnected(
                            received_at=self._wall_clock(),
                            provider=self._adapter.provider_name,
                        )
                    )
                    return
            self._set_state(ReconnectState.FAILED)
            raise StreamingReconnectError(
                f"reconexión agotada tras {self._policy.max_attempts} intentos; "
                f"último error: {self.last_error}"
            )
        finally:
            self._recovering = False

    def _try_connect(self) -> bool:
        try:
            self._adapter.disconnect()
        except StreamingError:
            pass
        try:
            self._adapter.connect()
            if self._subscription is not None:
                self._adapter.subscribe(*self._subscription)
        except StreamingError as exc:
            self.last_error = str(exc)
            return False
        return True

    def _dedup(self, event: StreamEvent | None) -> StreamEvent | None:
        """No republica el mismo cierre; los intrabar sí pasan (updates)."""
        if isinstance(event, StreamData) and event.event.is_closed:
            key = (event.event.symbol, event.event.open_time)
            if self._delivered.is_duplicate(key):
                return None
            self._delivered.remember(key)
            self._last_closed_bar = key
        return event

    def _drain_one(self) -> StreamEvent | None:
        return self._pending.popleft() if self._pending else None


__all__ = ["ReconnectManager", "ReconnectPolicy", "ReconnectState"]
