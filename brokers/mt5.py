"""MT5BrokerAdapter (LIVE-09): primer adapter real de Atlas (MetaTrader 5, DEMO).

Decision congelada (LIVE-09): proveedor = **MetaTrader 5**; primer entorno =
cuenta **DEMO**. **Produccion PROHIBIDA en este bloque**: el adapter no
conecta ni envia ordenes si `environment != demo` (fisicamente limitado por
codigo/config, sin fallback a demo ni a live).

Dependencia externa: el paquete `MetaTrader5` (oficial, wheel CPython 3.12
Windows x86-64). Queda **aislado** aqui: se inyecta como parametro (`mt5=...`)
o se importa de forma perezosa solo en `connect()`. La suite de tests corre
sin terminal MT5 (se inyecta un fake). Es una **optional dependency**
(`pip install "atlas[mt5]"`), no un requisito de desarrollo.

NO se crea una segunda jerarquia de brokers: implementa el `BrokerAdapter`
existente (`brokers/base.py`). No se toca `PaperBrokerAdapter`, el engine paper
ni A4/replay. El adapter se selecciona EXPLICITAMENTE; ningun default lo activa.

Invariantes clave (L-BROKER-MT5-*):
  * environment demo explicito; live no conecta/no submit (L-BROKER-MT5-1/2/18);
  * credenciales ausentes => no connect (L-BROKER-MT5-3);
  * secretos nunca en logs/repr/errores (L-BROKER-MT5-4);
  * symbol debe existir y ser operable (L-BROKER-MT5-5);
  * volume debe cumplir min/step/max SIN mutacion silenciosa (L-BROKER-MT5-6);
  * order_check success != ejecucion confirmada (L-BROKER-MT5-7);
  * send timeout/ambiguedad => UNKNOWN; UNKNOWN => no resubmit (8/9);
  * cid <-> ticket MT5 estable (magic + comment + mapping local) (10);
  * deal identity estable; duplicate deal => no-op (11/12);
  * commission observada preservada (13);
  * observed nunca sobrescribe ledger (14);
  * lookup timeout != NOT_FOUND (15); reconnect != recovered (16);
  * paper default unchanged (17); errores clasificados (19).

NO usa `pickle`. NO persiste ni registra secretos.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from execution.instrument import AssetClass, InstrumentSpec, InstrumentSpecError
from execution.intent_journal import (
    OPERATION_CANCEL,
    OPERATION_CLOSE,
    OPERATION_REDUCE,
)
from execution.order import (
    PURPOSE_CLOSE,
    PURPOSE_OPEN,
    PURPOSE_REDUCE,
    Order,
    OrderSide,
    OrderType,
)

from brokers.base import BrokerAdapter
from brokers.mt5_symbols import MT5SymbolResolver
from brokers.models import (
    AccountState,
    BrokerAuthError,
    BrokerConnectionError,
    BrokerDependencyError,
    BrokerError,
    BrokerFill,
    BrokerInsufficientFundsError,
    BrokerInvalidOrderError,
    BrokerMarketClosedError,
    BrokerOrder,
    BrokerOrderState,
    BrokerPermissionError,
    BrokerRateLimitError,
    HistoricalDeal,
    OrderNotFoundError,
    OrderRejectedError,
    OrderSubmission,
    Position,
    SubmitStatus,
)

# --------------------------------------------------------------- MT5 constantes
# Valores documentados de la API oficial (no dependemos del modulo inyectado
# para las constantes: solo para las funciones).
TRADE_ACTION_DEAL = 1
TRADE_ACTION_REMOVE = 3
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

SYMBOL_TRADE_MODE_FULL = 4
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

ORDER_STATE_PLACED = 1
ORDER_STATE_PARTIAL = 2
ORDER_STATE_FILLED = 3
ORDER_STATE_CANCELED = 4
ORDER_STATE_REJECTED = 5
ORDER_STATE_EXPIRED = 6

TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_CONNECTION = 10031

# Aceptada por el broker (la orden existe / se ejecuto). Semantica distinta por
# codigo: PLACED != fill; DONE_PARTIAL = fill parcial; DONE = fill completo.
_DONE_RETCODES = frozenset(
    {TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL}
)
# Resultado INCIERTO: timeout o conexion => UNKNOWN (lookup obligatorio).
_AMBIGUOUS_RETCODES = frozenset({TRADE_RETCODE_TIMEOUT, TRADE_RETCODE_CONNECTION})

# Rechazos inequivocos del broker (nunca se convierten en UNKNOWN).
_INVALID_RETCODES = frozenset(
    {10013, 10014, 10015, 10016, 10022, 10030, 10035, 10038}
)
_FUNDS_RETCODES = frozenset({10019})
_MARKET_CLOSED_RETCODES = frozenset({10018, 10017, 10026, 10027, 10032})
_RATE_LIMIT_RETCODES = frozenset({10024, 10033, 10034})
_PERMISSION_RETCODES = frozenset({10017, 10026, 10027, 10032, 10042, 10043, 10044})

MT5_COMMENT_MAX = 31


class MT5ConfigError(ValueError):
    """Configuracion MT5 invalida/ambigua (fail-closed)."""


class MT5Environment(str, Enum):
    DEMO = "demo"
    LIVE = "live"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MT5Credentials:
    """Credenciales MT5 (login/password/server) inyectadas desde el entorno.

    NUNCA se persisten en YAML ni se exponen en logs/repr/errores (LIVE-05).
    """

    login: int
    password: str
    server: str

    def __repr__(self) -> str:  # nunca mostrar el secreto
        return f"MT5Credentials(login=***, password=***, server=***)"

    __str__ = __repr__

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "MT5Credentials":
        import os

        source = os.environ if env is None else env
        raw_login = (source.get("MT5_LOGIN") or "").strip()
        password = source.get("MT5_PASSWORD") or ""
        server = (source.get("MT5_SERVER") or "").strip()
        missing = [
            name
            for name, value in (
                ("MT5_LOGIN", raw_login),
                ("MT5_PASSWORD", password),
                ("MT5_SERVER", server),
            )
            if not value
        ]
        if missing:
            raise BrokerAuthError(
                "credenciales MT5 ausentes: " + ", ".join(missing)
            )
        try:
            login = int(raw_login)
        except ValueError:
            raise BrokerAuthError("MT5_LOGIN no es un entero valido") from None
        return MT5Credentials(login=login, password=password, server=server)


@dataclass(frozen=True, slots=True)
class MT5Config:
    """Contrato NO secreto del adapter MT5 (operativo)."""

    provider: str = "mt5"
    environment: MT5Environment = MT5Environment.DEMO
    terminal_path: str | None = None
    symbol_map: Mapping[str, str] = field(default_factory=dict)
    magic_base: int = 0
    deviation: int = 20
    filling_mode: int = ORDER_FILLING_IOC
    comment_prefix: str = "atlas"
    # Timeout de conexion de `initialize()` (ms). None = sin timeout explicito
    # (controlado por el terminal). No se inventa socket timeout para las
    # operaciones posteriores: la API MT5 no lo expone.
    connection_timeout_ms: int | None = None
    # ATLAS-MT5-HEDGING-01: opt-in EXPLICITO para HEDGING restringido (una sola
    # posicion propiedad de Atlas por simbolo; sin LONG+SHORT simultaneos). Por
    # defecto DESHABILITADO; HEDGING NO se habilita solo por margin_mode.
    allow_restricted_hedging: bool = False

    def __post_init__(self) -> None:
        if self.provider != "mt5":
            raise MT5ConfigError(f"provider invalido: {self.provider!r}")
        if not isinstance(self.environment, MT5Environment):
            # Acepta string explicito, pero nunca un valor desconocido.
            try:
                object.__setattr__(
                    self, "environment", MT5Environment(str(self.environment))
                )
            except ValueError:
                raise MT5ConfigError(
                    f"environment invalido: {self.environment!r}"
                ) from None
        if self.deviation < 0:
            raise MT5ConfigError("deviation debe ser >= 0")
        if self.magic_base < 0:
            raise MT5ConfigError("magic_base debe ser >= 0")

    @staticmethod
    def from_mapping(data: Mapping[str, Any]) -> "MT5Config":
        """Construye/valida desde config externa (fail-closed)."""
        if not isinstance(data, Mapping):
            raise MT5ConfigError("config MT5 no es un mapping")
        env_raw = data.get("environment", MT5Environment.DEMO.value)
        try:
            environment = MT5Environment(str(env_raw))
        except ValueError:
            raise MT5ConfigError(f"environment invalido: {env_raw!r}") from None
        mapping = data.get("symbol_map") or {}
        if not isinstance(mapping, Mapping):
            raise MT5ConfigError("symbol_map debe ser un mapping")
        return MT5Config(
            provider=str(data.get("provider", "mt5")),
            environment=environment,
            terminal_path=data.get("terminal_path"),
            symbol_map={str(k): str(v) for k, v in mapping.items()},
            magic_base=int(data.get("magic_base", 0)),
            deviation=int(data.get("deviation", 20)),
            filling_mode=int(data.get("filling_mode", ORDER_FILLING_IOC)),
            comment_prefix=str(data.get("comment_prefix", "atlas")),
            connection_timeout_ms=(
                None
                if data.get("connection_timeout_ms") is None
                else int(data["connection_timeout_ms"])
            ),
            allow_restricted_hedging=bool(data.get("allow_restricted_hedging", False)),
        )


@dataclass(frozen=True, slots=True)
class MT5SymbolSpec:
    """Metadata real descubierta via `symbol_info()` (F-08-b material)."""

    strategy_instrument: str
    name: str
    visible: bool
    volume_min: float
    volume_step: float
    volume_max: float
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    currency_base: str
    currency_profit: str
    currency_margin: str
    trade_mode: int
    filling_mode: int
    digits: int


def instrument_spec_from_mt5(
    mt5_spec: MT5SymbolSpec,
    *,
    atlas_instrument_id: str,
    asset_class: AssetClass = AssetClass.OTHER,
    contract_size: float | None = None,
    quote_currency: str | None = None,
    base_currency: str | None = None,
) -> InstrumentSpec:
    """Construye/valida un `InstrumentSpec` Atlas desde metadata MT5.

    Precedence (MULTI-ASSET-02 §17):
      * Atlas aporta la SEMANTICA economica (`contract_size`, moneda de cotizacion).
      * El broker es AUTORITATIVO para el lattice de cantidad (volume_min/step/max).
      * Cross-validacion: si Atlas fija `contract_size` y el broker permite
        derivarlo (`tick_value/tick_size`), deben ser compatibles; mismatch =>
        `InstrumentSpecError` (fail-closed). Nunca overwrite silencioso.
    """
    if not atlas_instrument_id or not str(atlas_instrument_id).strip():
        raise InstrumentSpecError("atlas_instrument_id obligatorio")

    broker_quote = str(mt5_spec.currency_profit or "")
    if quote_currency is not None and broker_quote and quote_currency != broker_quote:
        raise InstrumentSpecError(
            "quote_currency mismatch: Atlas "
            f"{quote_currency!r} != MT5 currency_profit {broker_quote!r}"
        )
    quote = quote_currency or broker_quote or "USD"

    derived: float | None = None
    if mt5_spec.trade_tick_size > 0 and mt5_spec.trade_tick_value > 0:
        derived = mt5_spec.trade_tick_value / mt5_spec.trade_tick_size

    if contract_size is None:
        if derived is None:
            raise InstrumentSpecError(
                "contract_size no provisto y no derivable (tick metadata ausente)"
            )
        contract_size = derived
    elif derived is not None and not math.isclose(
        float(contract_size), derived, rel_tol=1e-3, abs_tol=1e-6
    ):
        raise InstrumentSpecError(
            "contract_size Atlas "
            f"{contract_size!r} incompatible con MT5 tick_value/tick_size "
            f"({derived!r})"
        )

    return InstrumentSpec(
        atlas_instrument_id=str(atlas_instrument_id),
        asset_class=asset_class,
        contract_size=float(contract_size),
        quote_currency=quote,
        base_currency=base_currency or (mt5_spec.currency_base or None),
        quantity_min=float(mt5_spec.volume_min),
        quantity_step=float(mt5_spec.volume_step),
        quantity_max=float(mt5_spec.volume_max),
    )


def _finite(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise BrokerError(f"valor no numerico del proveedor: {value!r}") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise BrokerError(f"valor no finito del proveedor: {value!r}")
    return f


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depende del runtime
        raise BrokerDependencyError(
            "el paquete MetaTrader5 no esta instalado (pip install \"atlas[mt5]\")"
        ) from exc
    return mt5


def read_mt5_symbol_spec(
    mt5,
    mt5_name: str,
    strategy_instrument: str,
    *,
    filling_mode_default: int,
    require_operable: bool = True,
    select_if_invisible: bool = True,
) -> MT5SymbolSpec:
    """Lee/valida la metadata de un simbolo MT5 (SOLO LECTURA de datos).

    Reutilizado por `MT5BrokerAdapter._symbol_spec` y por el probe de solo
    lectura. No envia ordenes.

    `select_if_invisible`:
      * True (adapter de ejecucion): si el simbolo no esta visible, lo selecciona
        en Market Watch (`symbol_select`) para poder operarlo. Es un EFECTO
        LATERAL sobre el terminal.
      * False (probe de solo lectura): NUNCA modifica el Market Watch; lee la
        metadata tal cual y refleja `visible` real (el llamante reporta la
        incompatibilidad si el simbolo no es visible/operable).

    `require_operable=False` permite inspeccionar un simbolo no operable.
    """
    info = mt5.symbol_info(mt5_name)
    if info is None:
        raise BrokerInvalidOrderError(f"simbolo MT5 inexistente: {mt5_name!r}")
    visible = bool(getattr(info, "visible", True))
    if not visible and select_if_invisible:
        try:
            mt5.symbol_select(mt5_name, True)
        except Exception:  # noqa: BLE001
            pass
        info = mt5.symbol_info(mt5_name)
        if info is None or not bool(getattr(info, "visible", False)):
            raise BrokerInvalidOrderError(
                f"simbolo MT5 no visible/operable: {mt5_name!r}"
            )
        visible = True
    trade_mode = int(getattr(info, "trade_mode", -1))
    if require_operable and trade_mode != SYMBOL_TRADE_MODE_FULL:
        raise BrokerInvalidOrderError(
            f"simbolo {mt5_name!r} no operable (trade_mode={trade_mode})"
        )
    spec = MT5SymbolSpec(
        strategy_instrument=strategy_instrument,
        name=mt5_name,
        visible=visible,
        volume_min=_finite(getattr(info, "volume_min")),
        volume_step=_finite(getattr(info, "volume_step")),
        volume_max=_finite(getattr(info, "volume_max")),
        point=_finite(getattr(info, "point")),
        trade_tick_size=_finite(getattr(info, "trade_tick_size", 0.0)),
        trade_tick_value=_finite(getattr(info, "trade_tick_value", 0.0)),
        trade_contract_size=_finite(getattr(info, "trade_contract_size", 0.0)),
        currency_base=str(getattr(info, "currency_base", "")),
        currency_profit=str(getattr(info, "currency_profit", "")),
        currency_margin=str(getattr(info, "currency_margin", "")),
        trade_mode=trade_mode,
        filling_mode=int(getattr(info, "filling_mode", filling_mode_default)),
        digits=int(getattr(info, "digits", 0)),
    )
    if spec.volume_min <= 0 or spec.volume_step <= 0 or spec.volume_max < spec.volume_min:
        raise BrokerInvalidOrderError(
            f"metadata de volumen invalida para {mt5_name!r}"
        )
    return spec


# MT5-DEMO-LIVE-READINESS-03A: estado EXPLICITO de una lectura de historial.
# Distingue historial completo, vacio confirmado y consulta incompleta/no
# disponible, para que un error historico NUNCA se confunda con "no hay fills".
HISTORY_COMPLETE = "COMPLETE"
HISTORY_EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
HISTORY_INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class OrderHistoryLookup:
    """Resultado explicito de reconstruir la historia de UNA orden.

    `status`:
      * `COMPLETE`: la orden y TODOS sus fills se leyeron del broker.
      * `EMPTY_CONFIRMED`: el broker (consulta limpia) no conoce la orden.
      * `INCOMPLETE`: la consulta fallo, fue ambigua o no pudo confirmarse el
        detalle de fills: NO se puede contabilizar ni afirmar ausencia.
    """

    client_order_id: str
    status: str
    order: "BrokerOrder | None" = None
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.status == HISTORY_COMPLETE


class MT5DealHistoryError(ValueError):
    """Intervalo temporal invalido para la consulta de deals historicos."""


@dataclass(frozen=True, slots=True)
class DealHistoryResult:
    """Resultado explicito de consultar deals historicos por INTERVALO temporal.

    `status`:
      * `COMPLETE`: la consulta se valido; `deals` es el conjunto de deals del
        intervalo solicitado (posiblemente no vacio).
      * `EMPTY_CONFIRMED`: la consulta fue correcta y no devolvio ningun deal.
      * `INCOMPLETE`: la consulta fallo (None/excepcion) o el resultado no pudo
        validarse/completarse; `deals` queda vacio y NUNCA debe interpretarse
        como "sin deals".

    IMPORTANTE: `COMPLETE` describe SOLO la consulta del intervalo solicitado; no
    afirma completitud historica global. La API subyacente no ofrece paginacion y
    los deals pueden aparecer con retraso.
    """

    date_from: datetime
    date_to: datetime
    status: str
    deals: tuple[HistoricalDeal, ...] = ()
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.status == HISTORY_COMPLETE


class MT5AccountObservationError(BrokerError):
    """La cuenta MT5 no expone los campos de identidad/economicos requeridos."""


@dataclass(frozen=True, slots=True)
class MT5AccountSnapshot:
    """Observacion READ-ONLY de la cuenta MT5 (identidad + economicos basicos).

    Fuente unica: `MetaTrader5.account_info()` (lectura). `credit` se lee del campo
    reportado y NO se sustituye por cero si falta. NO calcula equity de mercado
    completa: `equity` es el valor reportado por el broker.
    """

    server: str
    # El login completo se conserva para el calculo de identidad/huella, pero NO
    # se incluye en la representacion publica (repr) para no exponerlo.
    login: str = field(repr=False)
    login_masked: str
    currency: str
    margin_mode: int | None
    balance: float
    credit: float
    equity: float


class MT5BrokerAdapter(BrokerAdapter):
    """Adapter real MT5 (DEMO-only en LIVE-09).

    Args:
        config: contrato operativo no secreto.
        credentials: login/password/server desde entorno (LIVE-05).
        mt5: modulo MT5 inyectable (por defecto import perezoso del real).
        clock: reloj inyectable (UTC-aware).
    """

    def __init__(
        self,
        config: MT5Config,
        credentials: MT5Credentials,
        *,
        mt5: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(config, MT5Config):
            raise MT5ConfigError("config debe ser MT5Config")
        if not isinstance(credentials, MT5Credentials):
            raise BrokerAuthError("credentials debe ser MT5Credentials")
        self.config = config
        self._credentials = credentials
        self._clock = clock
        self._mt5 = mt5
        self._state = "DISCONNECTED"
        self._cid_to_ticket: dict[str, int] = {}
        self._ticket_to_cid: dict[int, str] = {}
        self._specs: dict[str, MT5SymbolSpec] = {}
        # MT5-DEMO-EXECUTION-FOUNDATION-01: gate de capacidad OPCIONAL. Si se
        # enlaza (`bind_execution_gate`), `submit_order` exige que el gate
        # conozca el `client_order_id` (lo aprueba SOLO el coordinador de
        # ejecucion). Sin gate (default) el comportamiento LIVE-09 es intacto.
        self._execution_gate = None
        # 06E: UN solo resolver validado (rechaza mappings no inyectivos). El
        # diccionario inverso se DERIVA de el, evitando dos algoritmos inversos.
        self._symbol_resolver = MT5SymbolResolver.from_mapping(config.symbol_map)
        self._reverse_symbol_map = dict(self._symbol_resolver.mt5_to_atlas)

    # ------------------------------------------------------------- propiedades

    @property
    def state(self) -> str:
        """Estado interno detallado (disconnected/connecting/ready/failed/...)."""
        return self._state

    @property
    def environment(self) -> MT5Environment:
        return self.config.environment

    # Marcador de CAPACIDAD: el adaptador MT5 EXIGE un gate de admision contable
    # fail-closed en el runtime. NO es un flag configurable: no puede desactivarse
    # desde configuracion (evita habilitar un runtime aparentemente listo sin
    # contabilidad MT5).
    requires_accounting_admission: bool = True

    @property
    def symbol_resolver(self) -> MT5SymbolResolver:
        """Resolver canonico Atlas<->MT5 validado (compartido con la conciliacion)."""
        return self._symbol_resolver

    # -------------------------------------------------------------- lifecycle

    def _module(self):
        if self._mt5 is None:
            self._mt5 = _load_mt5()
        return self._mt5

    def _require_demo(self) -> None:
        if self.config.environment is not MT5Environment.DEMO:
            raise BrokerPermissionError(
                "LIVE-09: solo entorno DEMO; produccion/live PROHIBIDA "
                "(no se conecta ni se opera)"
            )

    def _require_ready(self) -> None:
        if self._state != "READY":
            raise BrokerConnectionError(
                f"MT5 no listo (estado={self._state!r})"
            )

    def connect(self) -> None:
        """Valida config/credenciales, inicializa terminal y autentica."""
        self._require_demo()
        self._state = "CONNECTING"
        mt5 = self._module()
        kwargs: dict[str, Any] = {
            "login": self._credentials.login,
            "password": self._credentials.password,
            "server": self._credentials.server,
        }
        if self.config.terminal_path:
            kwargs["path"] = self.config.terminal_path
        if self.config.connection_timeout_ms is not None:
            kwargs["timeout"] = int(self.config.connection_timeout_ms)
        try:
            ok = bool(mt5.initialize(**kwargs))
        except BrokerError:
            self._state = "FAILED"
            raise
        except Exception as exc:  # noqa: BLE001 - transporte/módulo
            self._state = "FAILED"
            raise BrokerConnectionError(
                f"MT5 initialize fallo ({type(exc).__name__})"
            ) from exc
        if not ok:
            self._state = "FAILED"
            raise self._classify_connect_error()
        # READY exige CAPACIDAD REAL de trading automatico, no solo sesion:
        # terminal_info presente + account_info valido + trade_allowed +
        # trade_expert. Cualquier duda => fail-closed (nunca health=True falso).
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is None or account is None:
            self._state = "FAILED"
            raise BrokerAuthError("MT5 inicializado pero sin sesion de cuenta")
        if not bool(getattr(account, "trade_allowed", False)):
            self._state = "FAILED"
            raise BrokerPermissionError(
                "MT5: trade_allowed=False (cuenta/terminal no permite operar)"
            )
        if not bool(getattr(account, "trade_expert", False)):
            self._state = "FAILED"
            raise BrokerPermissionError(
                "MT5: trade_expert=False (trading automatico/EA no permitido)"
            )
        self._state = "READY"

    def disconnect(self) -> None:
        if self._state in ("DISCONNECTED", "FAILED"):
            self._state = "DISCONNECTED"
            return
        try:
            self._module().shutdown()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        self._state = "DISCONNECTED"

    def health(self) -> bool:
        return self._state == "READY"

    # ------------------------------------------- ejecucion coordinada (aditivo)

    def bind_execution_gate(self, gate) -> None:
        """Enlaza (una sola vez) un `ExecutionGate` obligatorio para `submit_order`.

        Endurecido (H1): el enlace es una operacion de API PUBLICA de una sola
        asignacion; no permite desactivar ni sustituir el gate de forma
        accidental:

          * `None` se rechaza: no existe "desenlazar" por API publica.
          * un objeto sin `check(client_order_id)` llamable se rechaza.
          * si YA hay un gate enlazado, un segundo enlace falla SIN reemplazarlo
            (una llamada fallida nunca muta el estado).

        Sin gate enlazado el comportamiento LIVE-09 es intacto (compatibilidad
        heredada); ese modo NO esta autorizado para la ruta coordinada y no debe
        activarse desde un runner de ejecucion. Con gate, `submit_order` rechaza
        cualquier `client_order_id` que el coordinador (poseedor del token) no
        haya aprobado; no cambia la semantica de submit.
        """
        if gate is None:
            raise BrokerError(
                "bind_execution_gate rechaza None: no se puede desenlazar el "
                "gate por API publica (fail-closed)"
            )
        if not callable(getattr(gate, "check", None)):
            raise BrokerError(
                "bind_execution_gate exige un gate con check(client_order_id) "
                "llamable (interfaz de comprobacion esperada)"
            )
        if self._execution_gate is not None:
            raise BrokerError(
                "ya existe un ExecutionGate enlazado: el enlace es de una sola "
                "asignacion (no se sustituye)"
            )
        self._execution_gate = gate

    def remember_identity(self, client_order_id: str, broker_order_id: int | str) -> None:
        """Prima la correlacion `cid<->ticket` desde una fuente fiable (journal).

        Usado en recovery para NO reconstruir `client_order_id` desde comentarios
        MT5 (posiblemente truncados). No inventa tickets: exige un id ya conocido.
        """
        self._remember(str(client_order_id), int(broker_order_id))

    def margin_mode(self) -> int | None:
        """`margin_mode` de la cuenta (0=NETTING, 2=HEDGING) o `None` si no READY."""
        if self._state != "READY":
            return None
        info = self._module().account_info()
        if info is None:
            return None
        value = getattr(info, "margin_mode", None)
        return int(value) if isinstance(value, int) else None

    def _verified_account_margin_mode(self) -> int:
        """Modo de cuenta VERIFICADO antes de autorizar un envio (fail-closed).

        ATLAS-MT5-HEDGING-02E: NUNCA asume NETTING ni ningun modo compatible como
        valor por defecto. Si `account_info()` es `None`, lanza, no expone
        `margin_mode`, o el valor es ausente/no entero/no reconocido, se rechaza
        la operacion ANTES de `order_check`/`order_send`.
        """
        try:
            info = self._module().account_info()
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001 - transporte/consulta incierta
            raise BrokerConnectionError(
                f"account_info fallo ({type(exc).__name__}): modo no verificable"
            ) from exc
        if info is None:
            raise BrokerConnectionError(
                "account_info() sin respuesta: modo de cuenta no verificable"
            )
        raw = getattr(info, "margin_mode", None)
        if raw is None:
            raise BrokerPermissionError(
                "cuenta sin margin_mode: modo de cuenta no verificable"
            )
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise BrokerPermissionError(f"margin_mode no entero: {raw!r}")
        mode = int(raw)
        if mode not in (
            ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
            ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        ):
            raise BrokerPermissionError(f"margin_mode no reconocido: {mode}")
        return mode

    def _classify_connect_error(self) -> BrokerError:
        try:
            error = self._module().last_error()
        except Exception:  # noqa: BLE001
            error = None
        code = error[0] if isinstance(error, (tuple, list)) and error else None
        if code == -6:
            return BrokerAuthError("autenticacion MT5 fallida")
        if code == -4:
            return BrokerDependencyError("terminal MT5 no encontrado")
        return BrokerConnectionError(f"MT5 initialize devolvio False (code={code})")

    # ------------------------------------------------------------------ cuenta

    def get_account_state(self) -> AccountState:
        self._require_ready()
        info = self._module().account_info()
        if info is None:
            # Nunca convertir a cuenta vacia: fail-safe.
            raise BrokerConnectionError("account_info() no disponible")
        return AccountState(
            account_id=str(getattr(info, "login", self._credentials.login)),
            cash=_finite(getattr(info, "balance")),
            equity=_finite(getattr(info, "equity")),
            # MT5 no expone "buying power" como tal: se mapea margin_free.
            buying_power=_finite(getattr(info, "margin_free")),
            currency=str(getattr(info, "currency", "")),
            as_of=self._clock(),
        )

    def read_account_snapshot(self) -> MT5AccountSnapshot:
        """Observa identidad + economicos basicos de la cuenta (SOLO LECTURA).

        Requiere estado READY. Falla cerrado si el terminal no expone servidor,
        login, divisa o credito: NO se inventan valores por defecto (en particular,
        `credit` ausente NO se sustituye por cero).
        """
        self._require_ready()
        try:
            info = self._module().account_info()
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001 - transporte/consulta incierta
            raise BrokerConnectionError(
                f"account_info fallo ({type(exc).__name__})"
            ) from exc
        if info is None:
            raise BrokerConnectionError("account_info() no disponible")
        server = getattr(info, "server", None)
        if not isinstance(server, str) or not server.strip():
            raise MT5AccountObservationError("servidor no observable en account_info()")
        login = getattr(info, "login", None)
        if login is None or isinstance(login, bool):
            raise MT5AccountObservationError("login no observable en account_info()")
        currency = getattr(info, "currency", None)
        if not isinstance(currency, str) or not currency.strip():
            raise MT5AccountObservationError("divisa no observable en account_info()")
        credit = getattr(info, "credit", None)
        if credit is None:
            raise MT5AccountObservationError("credit no observable en account_info()")
        raw_mode = getattr(info, "margin_mode", None)
        margin_mode = (
            int(raw_mode)
            if isinstance(raw_mode, int) and not isinstance(raw_mode, bool)
            else None
        )
        return MT5AccountSnapshot(
            server=server.strip(),
            login=str(login),
            login_masked="***" + str(login)[-4:],
            currency=currency.strip(),
            margin_mode=margin_mode,
            balance=_finite(getattr(info, "balance")),
            credit=_finite(credit),
            equity=_finite(getattr(info, "equity")),
        )

    # ---------------------------------------------------------------- simbolos

    def _symbol_spec(self, strategy_instrument: str) -> MT5SymbolSpec:
        cached = self._specs.get(strategy_instrument)
        if cached is not None:
            return cached
        mt5_name = self._symbol_resolver.to_mt5(strategy_instrument)
        if not mt5_name:
            raise BrokerInvalidOrderError(
                f"sin mapping explicito Atlas->MT5 para {strategy_instrument!r}"
            )
        spec = read_mt5_symbol_spec(
            self._module(),
            mt5_name,
            strategy_instrument,
            filling_mode_default=self.config.filling_mode,
            require_operable=True,
        )
        self._specs[strategy_instrument] = spec
        return spec

    def _validate_volume(self, spec: MT5SymbolSpec, quantity: float) -> None:
        """Valida volumen contra min/step/max. NUNCA muta la cantidad."""
        qty = _finite(quantity)
        if qty <= 0:
            raise BrokerInvalidOrderError(f"quantity invalida: {quantity!r}")
        if qty < spec.volume_min - 1e-9:
            raise BrokerInvalidOrderError(
                f"quantity {qty} < volume_min {spec.volume_min}"
            )
        if qty > spec.volume_max + 1e-9:
            raise BrokerInvalidOrderError(
                f"quantity {qty} > volume_max {spec.volume_max}"
            )
        steps = (qty - spec.volume_min) / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            raise BrokerInvalidOrderError(
                f"quantity {qty} no alineada a volume_step {spec.volume_step}"
            )

    # -------------------------------------------------------- submit / lookup

    def _magic_for(self, client_order_id: str) -> int:
        crc = zlib.crc32(client_order_id.encode("utf-8")) & 0x7FFFFFFF
        return (self.config.magic_base + crc) % 2_000_000_000

    def _comment_for(self, client_order_id: str) -> str:
        raw = f"{self.config.comment_prefix}:{client_order_id}"
        return raw[:MT5_COMMENT_MAX]

    def _prepare_reduce_only(self, order: Order, spec: MT5SymbolSpec) -> int:
        """Valida un cierre/reduccion explicito (reduce_only=True).

        NETTING/EXCHANGE: permitido si existe posicion abierta del simbolo, el
        sentido es el contrario y el volumen no excede la exposicion (no puede
        invertir). Devuelve el ticket de posicion para el campo `position`.

        HEDGING: RECHAZADO en esta fase (contract gap: el dominio no transporta
        broker_position_id de forma inequivoca).
        """
        mt5 = self._module()
        account = mt5.account_info()
        margin_mode = int(getattr(account, "margin_mode", 0)) if account is not None else 0
        if margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            # ATLAS-MT5-HEDGING-01: HEDGING SOLO con opt-in restringido y
            # objetivo de posicion explicito (identidad estable), verificando
            # simbolo/direccion/volumen contra la posicion VIVA y usando su
            # POSITION_TICKET actual. Sin opt-in o sin objetivo => fail-closed.
            return self._prepare_reduce_only_hedging(order, spec)
        try:
            rows = self._rows_or_raise(
                mt5.positions_get(symbol=spec.name), "positions_get(symbol)"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        if not rows:
            raise BrokerInvalidOrderError(
                "reduce_only sin posicion abierta del simbolo"
            )
        long_vol = sum(
            _finite(getattr(p, "volume")) for p in rows
            if int(getattr(p, "type", ORDER_TYPE_BUY)) == ORDER_TYPE_BUY
        )
        short_vol = sum(
            _finite(getattr(p, "volume")) for p in rows
            if int(getattr(p, "type", ORDER_TYPE_BUY)) == ORDER_TYPE_SELL
        )
        net = long_vol - short_vol
        if abs(net) <= 1e-12:
            raise BrokerInvalidOrderError("reduce_only sin posicion neta")
        required = OrderSide.SELL if net > 0 else OrderSide.BUY
        exposure = abs(net)
        if order.side is not required:
            raise BrokerInvalidOrderError(
                "reduce_only debe ser contrario a la posicion (no convertir en "
                "entrada opuesta)"
            )
        if float(order.quantity) > exposure + 1e-9:
            raise BrokerInvalidOrderError(
                "reduce_only excede la posicion abierta (no puede invertir)"
            )
        return int(getattr(rows[0], "ticket", 0))

    def _prepare_reduce_only_hedging(self, order: Order, spec: MT5SymbolSpec) -> int:
        """Cierre/reduccion DIRIGIDO en HEDGING restringido (opt-in).

        Exige `allow_restricted_hedging`, un `target_position_id` explicito
        (POSITION_IDENTIFIER) y valida contra la POSICION VIVA: simbolo,
        direccion contraria y volumen <= volumen de la posicion objetivo. Usa el
        POSITION_TICKET ACTUAL en el campo `position`. Nunca selecciona filas
        por indice ni por ticket minimo. Fail-closed ante ambiguedad.
        """
        if not self.config.allow_restricted_hedging:
            raise BrokerPermissionError(
                "HEDGING rechazado: requiere opt-in explicito "
                "(allow_restricted_hedging=True)"
            )
        target = order.target_position_id
        if not target or not str(target).strip():
            raise BrokerInvalidOrderError(
                "HEDGING restringido exige target_position_id explicito "
                "(POSITION_IDENTIFIER); no se selecciona posicion por indice"
            )
        mt5 = self._module()
        try:
            rows = self._rows_or_raise(
                mt5.positions_get(symbol=spec.name), "positions_get(symbol)"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        matching = [
            p for p in rows
            if getattr(p, "identifier", None) is not None
            and str(getattr(p, "identifier")) == str(target)
        ]
        if not matching:
            raise BrokerInvalidOrderError(
                "posicion objetivo (POSITION_IDENTIFIER) no existe en el broker"
            )
        if len(matching) > 1:
            raise BrokerInvalidOrderError(
                "POSITION_IDENTIFIER ambiguo: multiples posiciones coinciden"
            )
        pos = matching[0]
        volume = _finite(getattr(pos, "volume"))
        ptype = int(getattr(pos, "type", ORDER_TYPE_BUY))
        required = OrderSide.SELL if ptype == ORDER_TYPE_BUY else OrderSide.BUY
        if order.side is not required:
            raise BrokerInvalidOrderError(
                "reduce_only debe ser contrario a la posicion objetivo "
                "(no convertir en apertura opuesta)"
            )
        if float(order.quantity) > volume + 1e-9:
            raise BrokerInvalidOrderError(
                "reduce_only excede el volumen de la posicion objetivo"
            )
        ticket = getattr(pos, "ticket", None)
        if ticket is None:
            raise BrokerInvalidOrderError(
                "posicion objetivo sin POSITION_TICKET actual determinable"
            )
        return int(ticket)

    def submit_order(self, order: Order) -> OrderSubmission:
        self._require_demo()
        self._require_ready()
        cid = order.client_order_id
        if not cid:
            raise BrokerInvalidOrderError("client_order_id obligatorio")
        # ATLAS-MT5-HEDGING-02B/02E (H-3): el modo de cuenta se VERIFICA antes de
        # cualquier envio. Modo no verificable (account_info None/excepcion/ausente)
        # o no reconocido => fail-closed (nunca se asume NETTING). La ruta DIRECTA
        # tampoco elude el opt-in HEDGING, con independencia del ExecutionGate.
        mode = self._verified_account_margin_mode()
        if (
            mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
            and not self.config.allow_restricted_hedging
        ):
            raise BrokerPermissionError(
                "cuenta HEDGING sin opt-in restringido "
                "(allow_restricted_hedging=False): operacion rechazada"
            )
        # MT5-DEMO-EXECUTION-FOUNDATION-01 / 02B: solo envia si el gate de
        # capacidad enlazado autoriza esta operacion. El TIPO se deriva del
        # `purpose` tipado (NO de reduce_only): OPEN usa target=cid; CLOSE/REDUCE
        # usan target=instrument (posicion). Sin gate (default) => LIVE-09 intacto.
        if self._execution_gate is not None:
            purpose = str(getattr(order, "purpose", PURPOSE_OPEN))
            if purpose == PURPOSE_CLOSE:
                self._execution_gate.check_operation(
                    OPERATION_CLOSE, op_id=cid, target=str(order.instrument)
                )
            elif purpose == PURPOSE_REDUCE:
                self._execution_gate.check_operation(
                    OPERATION_REDUCE, op_id=cid, target=str(order.instrument)
                )
            else:
                self._execution_gate.check(cid)
        if order.order_type is not OrderType.MARKET:
            raise BrokerInvalidOrderError(
                f"tipo de orden no soportado por MT5 en LIVE-09: {order.order_type}"
            )
        spec = self._symbol_spec(order.instrument)
        self._validate_volume(spec, order.quantity)
        # reduce_only NO se ignora: un cierre no es una entrada MARKET opuesta.
        position_ticket: int | None = None
        if order.reduce_only:
            position_ticket = self._prepare_reduce_only(order, spec)
        mt5 = self._module()
        tick = mt5.symbol_info_tick(spec.name)
        if tick is None:
            raise BrokerConnectionError(f"sin tick para {spec.name!r}")
        if order.side is OrderSide.BUY:
            order_type = ORDER_TYPE_BUY
            price = _finite(getattr(tick, "ask"))
        else:
            order_type = ORDER_TYPE_SELL
            price = _finite(getattr(tick, "bid"))
        request = {
            "action": TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(order.quantity),
            "type": order_type,
            "price": price,
            "deviation": self.config.deviation,
            "magic": self._magic_for(cid),
            "comment": self._comment_for(cid),
            "type_time": ORDER_TIME_GTC,
            "type_filling": spec.filling_mode,
        }
        if position_ticket is not None:
            # Cierre explicito: el campo `position` identifica la posicion.
            request["position"] = position_ticket
        # order_check NO confirma ejecucion: solo valida la peticion. Un fallo
        # aqui implica que NO se ha enviado (no hay ambiguedad de submit).
        try:
            check = mt5.order_check(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"order_check fallo ({type(exc).__name__})"
            ) from exc
        if check is None:
            raise BrokerConnectionError("order_check sin respuesta")
        check_code = int(getattr(check, "retcode", -1))
        if check_code != 0:
            return OrderSubmission(
                client_order_id=cid,
                status=SubmitStatus.REJECTED,
                broker_order=None,
                detail=f"order_check retcode={check_code}",
            )
        # A partir de aqui el envio puede ocurrir: el limite de ambiguedad.
        try:
            result = mt5.order_send(request)
        except Exception:  # noqa: BLE001 - transporte post-send incierto
            return OrderSubmission(
                client_order_id=cid,
                status=SubmitStatus.UNKNOWN,
                broker_order=None,
                detail="order_send: respuesta incierta",
            )
        if result is None:
            return OrderSubmission(
                client_order_id=cid,
                status=SubmitStatus.UNKNOWN,
                broker_order=None,
                detail="order_send sin respuesta",
            )
        retcode = int(getattr(result, "retcode", -1))
        if retcode in _DONE_RETCODES:
            # Semantica distinta por codigo, derivada del estado real via lookup:
            #   DONE -> FILLED; DONE_PARTIAL -> PARTIALLY_FILLED; PLACED -> SUBMITTED
            # (PLACED NO afirma fill).
            ticket = self._ticket_from_result(result)
            if ticket is not None:
                self._remember(cid, ticket)
                broker_order = self._build_broker_order(cid, ticket)
            else:
                broker_order = self._provisional_broker_order(cid, order, result)
            return OrderSubmission(
                client_order_id=cid,
                status=SubmitStatus.CONFIRMED,
                broker_order=broker_order,
                detail=f"retcode={retcode}",
            )
        if retcode in _AMBIGUOUS_RETCODES:
            return OrderSubmission(
                client_order_id=cid,
                status=SubmitStatus.UNKNOWN,
                broker_order=None,
                detail=f"retcode incierto={retcode}",
            )
        return OrderSubmission(
            client_order_id=cid,
            status=SubmitStatus.REJECTED,
            broker_order=None,
            detail=self._reject_detail(retcode),
        )

    def _ticket_from_result(self, result: Any) -> int | None:
        for field_name in ("order", "deal"):
            value = getattr(result, field_name, None)
            if value:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def _reject_detail(retcode: int) -> str:
        # Nunca incluir texto crudo del broker (puede contener datos sensibles).
        return f"rechazo MT5 retcode={retcode}"

    def _remember(self, cid: str, ticket: int) -> None:
        self._cid_to_ticket[cid] = ticket
        self._ticket_to_cid[ticket] = cid

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        self._require_ready()
        ticket = self._cid_to_ticket.get(client_order_id)
        if ticket is None:
            matches = self._discover_tickets(client_order_id)
            if not matches:
                return None  # NOT_FOUND (autoritativo tras scan limpio)
            if len(matches) > 1:
                # Ambiguedad: no se puede correlacionar de forma fiable.
                return self._indeterminate_broker_order(client_order_id)
            ticket = matches[0]
            self._remember(client_order_id, ticket)
        order = self._build_broker_order(client_order_id, ticket)
        if order is None:
            return self._indeterminate_broker_order(client_order_id)
        return order

    def lookup_order_history(self, client_order_id: str) -> OrderHistoryLookup:
        """Reconstruye la historia de una orden con estado EXPLICITO (03A).

        A diferencia de `get_order` (que devuelve `None` para NOT_FOUND y un
        `BrokerOrder` UNKNOWN para ambiguedad), este metodo NUNCA convierte un
        fallo de consulta historica en "sin fills": devuelve `INCOMPLETE`. Asi el
        runtime puede decidir reconstruir (COMPLETE) o fallar de forma cerrada.
        """
        self._require_ready()
        cid = str(client_order_id)
        ticket = self._cid_to_ticket.get(cid)
        if ticket is None:
            try:
                matches = self._discover_tickets(cid)
            except BrokerConnectionError as exc:
                return OrderHistoryLookup(cid, HISTORY_INCOMPLETE, detail=str(exc))
            if not matches:
                return OrderHistoryLookup(cid, HISTORY_EMPTY_CONFIRMED)
            if len(matches) > 1:
                return OrderHistoryLookup(
                    cid, HISTORY_INCOMPLETE,
                    detail="correlacion ambigua (multiples tickets)",
                )
            ticket = matches[0]
            self._remember(cid, ticket)
        try:
            order = self._build_broker_order(cid, ticket)
        except BrokerConnectionError as exc:
            return OrderHistoryLookup(cid, HISTORY_INCOMPLETE, detail=str(exc))
        if order is None:
            return OrderHistoryLookup(
                cid, HISTORY_INCOMPLETE, detail="sin fila de orden/pocision en broker"
            )
        # Una orden terminalmente ejecutada DEBE traer detalle de fills; su
        # ausencia indica historial de deals incompleto (no "0 fills").
        if order.state in (
            BrokerOrderState.FILLED,
            BrokerOrderState.PARTIALLY_FILLED,
        ) and not order.fills:
            return OrderHistoryLookup(
                cid, HISTORY_INCOMPLETE, order=order,
                detail="estado con fills pero sin detalle de deals",
            )
        return OrderHistoryLookup(cid, HISTORY_COMPLETE, order=order)

    def deal_history(
        self, date_from: datetime, date_to: datetime
    ) -> DealHistoryResult:
        """Deals historicos por intervalo temporal (SOLO LECTURA).

        Contrato:
          * Valida que `date_from`/`date_to` sean datetimes con zona horaria y
            los convierte a UTC de forma EXPLICITA (nunca reinterpreta naive);
            exige `date_from <= date_to`.
          * Consulta `history_deals_get(date_from, date_to)` SIN filtro de
            simbolo ni de orden (no excluye movimientos de cuenta ni `order == 0`).
          * `None`/excepcion => `INCOMPLETE` (nunca "vacio"). La coleccion vacia
            confirmada => `EMPTY_CONFIRMED`.
          * Valida cada registro antes de emitir `COMPLETE`; un registro
            malformado => `INCOMPLETE`. NO descarta deals por `type` desconocido
            (no clasifica economicamente aqui).
          * Deduplica por `ticket`: identico => colapsa; contradictorio =>
            `INCOMPLETE`.
          * Orden determinista por `(time, time_msc, ticket)`.

        NO afirma paginacion ni completitud historica global; la futura ingestion
        debera admitir intervalos SOLAPADOS y deduplicar por identidad de deal.
        """
        self._require_ready()
        frm = self._require_utc_datetime(date_from, "date_from")
        to = self._require_utc_datetime(date_to, "date_to")
        if frm > to:
            raise MT5DealHistoryError("date_from no puede ser posterior a date_to")

        mt5 = self._module()
        try:
            rows = mt5.history_deals_get(frm, to)
        except Exception as exc:  # noqa: BLE001 - transporte/consulta incierta
            return DealHistoryResult(
                frm, to, HISTORY_INCOMPLETE, (),
                f"consulta fallo ({type(exc).__name__})",
            )
        if rows is None:
            return DealHistoryResult(
                frm, to, HISTORY_INCOMPLETE, (),
                f"sin respuesta ({self._last_error_text()})",
            )
        try:
            raw = list(rows)
        except TypeError:
            return DealHistoryResult(
                frm, to, HISTORY_INCOMPLETE, (), "resultado no iterable"
            )

        by_ticket: dict[int, HistoricalDeal] = {}
        for row in raw:
            try:
                deal = self._historical_deal(row)
            except MT5DealHistoryError as exc:
                return DealHistoryResult(
                    frm, to, HISTORY_INCOMPLETE, (), f"registro invalido: {exc}"
                )
            prev = by_ticket.get(deal.ticket)
            if prev is None:
                by_ticket[deal.ticket] = deal
            elif prev != deal:
                return DealHistoryResult(
                    frm, to, HISTORY_INCOMPLETE, (),
                    "ticket duplicado contradictorio",
                )
            # ticket identico repetido => colapsa (no se elige variante).
        if not by_ticket:
            return DealHistoryResult(frm, to, HISTORY_EMPTY_CONFIRMED, ())
        ordered = tuple(
            sorted(by_ticket.values(), key=lambda d: (d.time, d.time_msc, d.ticket))
        )
        return DealHistoryResult(frm, to, HISTORY_COMPLETE, ordered)

    @staticmethod
    def _require_utc_datetime(value: object, field: str) -> datetime:
        """Exige datetime con zona y lo normaliza a UTC (sin reinterpretar naive)."""
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise MT5DealHistoryError(
                f"{field} debe ser datetime con zona horaria (UTC), no naive"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _historical_deal(row: Any) -> HistoricalDeal:
        """Normaliza/valida UN registro de `history_deals_get` (fail-closed)."""

        def _int(name: str) -> int:
            # OBLIGATORIO y ESTRICTO: solo `int` real. Rechaza ausencia/None,
            # bool (subtipo de int), float (aunque sea 5.0: no se trunca ni se
            # pierde identidad) y str: no hay coercion implicita ni defaults.
            if not hasattr(row, name):
                raise MT5DealHistoryError(f"{name} ausente")
            v = getattr(row, name)
            if v is None:
                raise MT5DealHistoryError(f"{name} ausente")
            if isinstance(v, bool) or not isinstance(v, int):
                raise MT5DealHistoryError(f"{name} no entero (tipo {type(v).__name__})")
            return v

        def _num(name: str) -> float:
            # Importe OBLIGATORIO: ausente/None => error (nunca 0.0 por defecto).
            v = getattr(row, name, None)
            if v is None:
                raise MT5DealHistoryError(f"{name} ausente")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise MT5DealHistoryError(f"{name} no numerico")
            f = float(v)
            if not math.isfinite(f):
                raise MT5DealHistoryError(f"{name} no finito")
            return f

        ticket = _int("ticket")
        if ticket <= 0:
            raise MT5DealHistoryError("ticket invalido")
        order = _int("order")  # obligatorio (0 = movimiento sin orden: legitimo)
        time_raw = _int("time")  # obligatorio
        if time_raw <= 0:
            raise MT5DealHistoryError("time invalido")
        time_msc = _int("time_msc")  # obligatorio
        deal_type = _int("type")  # obligatorio
        entry = _int("entry")  # obligatorio (0 = entrada: legitimo)
        position_id = _int("position_id")  # obligatorio (0 = sin posicion: legitimo)
        symbol_raw = getattr(row, "symbol", None)
        symbol = None if symbol_raw is None or not str(symbol_raw).strip() else str(symbol_raw)
        return HistoricalDeal(
            ticket=ticket,
            order=order,
            time=datetime.fromtimestamp(time_raw, tz=timezone.utc),
            time_msc=time_msc,
            type=deal_type,
            entry=entry,
            position_id=position_id,
            symbol=symbol,
            volume=_num("volume"),
            price=_num("price"),
            profit=_num("profit"),
            commission=_num("commission"),
            swap=_num("swap"),
            fee=_num("fee"),
        )

    def _discover_tickets(self, client_order_id: str) -> list[int]:
        magic = self._magic_for(client_order_id)
        comment = self._comment_for(client_order_id)
        candidates = list(self._safe_orders_get()) + list(
            self._safe_history_orders_get()
        )
        exact = [
            int(o.ticket)
            for o in candidates
            if int(getattr(o, "magic", -1)) == magic
            and str(getattr(o, "comment", "")) == comment
        ]
        if exact:
            return sorted(set(exact))
        by_magic = [
            int(o.ticket)
            for o in candidates
            if int(getattr(o, "magic", -1)) == magic
        ]
        return sorted(set(by_magic))

    def get_open_orders(self) -> list[BrokerOrder]:
        self._require_ready()
        out: list[BrokerOrder] = []
        for raw in self._safe_orders_get():
            ticket = int(getattr(raw, "ticket", 0))
            cid = self._ticket_to_cid.get(ticket)
            if cid is None:
                comment = str(getattr(raw, "comment", "") or "")
                cid = comment or f"MT5:{ticket}"
            order = self._build_broker_order(cid, ticket)
            if order is not None:
                out.append(order)
        return out

    def get_positions(self) -> list[Position]:
        self._require_ready()
        mt5 = self._module()
        try:
            raw_positions = self._rows_or_raise(
                mt5.positions_get(), "positions_get"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        # Deteccion hedging: multiples posiciones por simbolo no soportadas aun.
        account = mt5.account_info()
        margin_mode = int(getattr(account, "margin_mode", 0)) if account else 0
        if margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            by_symbol: dict[str, int] = {}
            for p in raw_positions:
                by_symbol[str(p.symbol)] = by_symbol.get(str(p.symbol), 0) + 1
            if any(count > 1 for count in by_symbol.values()):
                raise BrokerError(
                    "cuenta HEDGING con multiples posiciones por simbolo: "
                    "contract gap (no soportado en LIVE-09)"
                )
        positions: list[Position] = []
        for p in raw_positions:
            positions.append(self._position_view(p))
        return positions

    def _position_view(self, p: Any) -> Position:
        """Vista de posicion con identidad MT5 EXPLICITA (03/HEDGING-01).

        `ticket`=POSITION_TICKET (actual), `position_id`=POSITION_IDENTIFIER
        (estable), `magic`/`comment` auxiliares. NO se confunden entre si.
        """
        symbol = str(getattr(p, "symbol", ""))
        volume = _finite(getattr(p, "volume"))
        ptype = int(getattr(p, "type", 0))
        sign = 1.0 if ptype == ORDER_TYPE_BUY else -1.0
        ticket = getattr(p, "ticket", None)
        identifier = getattr(p, "identifier", None)
        return Position(
            # 06E: NO se inventa identidad Atlas. Si el simbolo MT5 no esta
            # mapeado, `instrument` queda None (fail-closed) y se conserva el
            # simbolo MT5 original en `mt5_symbol`.
            instrument=self._symbol_resolver.to_atlas(symbol),
            mt5_symbol=symbol,
            quantity=sign * volume,
            avg_entry_price=_finite(getattr(p, "price_open")),
            updated_at=self._position_time(getattr(p, "time", 0)),
            ticket=None if ticket is None else str(ticket),
            position_id=(
                None
                if identifier is None
                else str(identifier)
            ),
            magic=(
                int(getattr(p, "magic"))
                if getattr(p, "magic", None) is not None
                else None
            ),
            comment=str(getattr(p, "comment", "") or ""),
        )

    def resolve_position(self, position_id: str) -> Position | None:
        """Devuelve la POSICION VIVA por su POSITION_IDENTIFIER estable.

        Consulta `positions_get()` y empareja por `POSITION_IDENTIFIER` (nunca
        por indice ni ticket minimo). `None` si no existe. Falla cerrado ante
        consulta incompleta (`BrokerConnectionError`).
        """
        target = str(position_id)
        try:
            rows = self._rows_or_raise(
                self._module().positions_get(), "positions_get"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        for p in rows:
            ident = getattr(p, "identifier", None)
            if ident is not None and str(ident) == target:
                return self._position_view(p)
        return None

    def _position_time(self, epoch: Any) -> datetime:
        try:
            value = int(epoch)
            if value > 0:
                return datetime.fromtimestamp(value, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
        return self._clock()

    def cancel_order(
        self, client_order_id: str, *, operation_id: str | None = None
    ) -> BrokerOrder:
        """Cancela una orden pendiente.

        Compatibilidad heredada (LIVE-09): sin gate enlazado, `operation_id` se
        ignora y el comportamiento es el de siempre (NO es ruta coordinada
        segura; no debe usarse desde un runner live).

        Modo coordinado (MT5-DEMO-EXECUTION-CONTROLS-02A): si hay un
        `ExecutionGate` enlazado, la operacion exige autorizacion de capacidad de
        tipo `CANCEL`, ligada a `(operation_id, client_order_id)`. Sin `operation_id`
        o sin autorizacion valida => se impide la llamada al broker. El gate lo
        posee SOLO el `LiveExecutionCoordinator`; un caller con el adapter no
        puede cancelar sin su autorizacion. NO cierra/reduce posiciones (fuera de
        alcance 02A).
        """
        self._require_demo()
        self._require_ready()
        if self._execution_gate is not None:
            if not operation_id or not str(operation_id).strip():
                raise BrokerError(
                    "cancelacion coordinada (gate enlazado) requiere operation_id: "
                    "sin identidad de operacion no se llama al broker"
                )
            self._execution_gate.check_operation(
                OPERATION_CANCEL,
                op_id=str(operation_id),
                target=client_order_id,
            )
        ticket = self._cid_to_ticket.get(client_order_id)
        if ticket is None:
            matches = self._discover_tickets(client_order_id)
            if not matches:
                raise OrderNotFoundError(
                    f"MT5 no conoce la orden {client_order_id!r}"
                )
            if len(matches) > 1:
                raise BrokerConnectionError(
                    "correlacion ambigua: no se puede cancelar de forma fiable"
                )
            ticket = matches[0]
            self._remember(client_order_id, ticket)
        mt5 = self._module()
        request = {"action": TRADE_ACTION_REMOVE, "order": ticket}
        try:
            result = mt5.order_send(request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"cancel: respuesta incierta ({type(exc).__name__})"
            ) from exc
        if result is None:
            raise BrokerConnectionError("cancel: sin respuesta (incierto)")
        retcode = int(getattr(result, "retcode", -1))
        if retcode in _AMBIGUOUS_RETCODES:
            raise BrokerConnectionError(
                f"cancel: resultado incierto (retcode={retcode})"
            )
        if retcode not in _DONE_RETCODES:
            raise OrderRejectedError(f"cancel rechazado (retcode={retcode})")
        order = self._build_broker_order(client_order_id, ticket)
        if order is None:
            raise BrokerConnectionError("cancel: estado no determinable")
        if order.state not in (
            BrokerOrderState.CANCELLED,
            BrokerOrderState.FILLED,
        ):
            order = replace(order, state=BrokerOrderState.CANCELLED)
        return order

    # ------------------------------------------------------- construccion vista

    def _last_error_text(self) -> str:
        """Diagnostico SANITIZADO de `last_error()` (nunca credenciales)."""
        try:
            err = self._module().last_error()
        except Exception:  # noqa: BLE001 - diagnostico best-effort
            return "last_error no disponible"
        if isinstance(err, (tuple, list)) and err:
            code = err[0]
            desc = err[1] if len(err) > 1 else ""
            # Solo codigo + descripcion corta; jamas datos de cuenta/credenciales.
            return f"last_error={code}:{str(desc)[:80]}"
        return f"last_error={err!r}"

    def _rows_or_raise(self, rows, what: str) -> list[Any]:
        """MetaTrader5 devuelve `None` en error. Un `None` es FALLO, no vacio.

        Distingue explicitamente: `None` (error de la API, diagnostico via
        `last_error()`) y retorno no iterable (fallo) se convierten en
        `BrokerConnectionError`; una coleccion vacia SIN error se preserva como
        vacia (nunca se confunde con un fallo).
        """
        if rows is None:
            raise BrokerConnectionError(
                f"{what} devolvio None (fallo de la API; {self._last_error_text()})"
            )
        try:
            return list(rows)
        except TypeError as exc:
            raise BrokerConnectionError(
                f"{what} retorno no iterable ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc

    def _safe_orders_get(self) -> list[Any]:
        try:
            return self._rows_or_raise(self._module().orders_get(), "orders_get")
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"orders_get fallo ({type(exc).__name__}; {self._last_error_text()})"
            ) from exc

    def _safe_history_orders_get(self) -> list[Any]:
        # 03A: un fallo de consulta historica NO se convierte en `[]`. Un `[]`
        # silencioso haria que `get_order`/`lookup_order_history` interpretasen
        # un error (None/excepcion) como "el broker no conoce la orden"
        # (NOT_FOUND), perdiendo fills reales. Se propaga LOOKUP_UNCERTAIN.
        try:
            return self._rows_or_raise(
                self._module().history_orders_get(), "history_orders_get"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"history_orders_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc

    def _open_order(self, ticket: int) -> Any | None:
        try:
            rows = self._rows_or_raise(
                self._module().orders_get(ticket=ticket), "orders_get(ticket)"
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"orders_get(ticket) fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        return rows[0] if rows else None

    def _history_order(self, ticket: int) -> Any | None:
        try:
            rows = self._rows_or_raise(
                self._module().history_orders_get(ticket=ticket),
                "history_orders_get(ticket)",
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"history_orders_get fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        return rows[-1] if rows else None

    def _deals_for(self, ticket: int) -> list[Any]:
        try:
            # API MT5: el filtro por ticket de ORDEN (DEAL_ORDER) es `ticket=`
            # (no existe `order=`). None/error => fallo, nunca historial vacio.
            rows = self._rows_or_raise(
                self._module().history_deals_get(ticket=ticket),
                "history_deals_get(ticket)",
            )
        except BrokerConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"history_deals_get(ticket) fallo ({type(exc).__name__}; "
                f"{self._last_error_text()})"
            ) from exc
        return sorted(
            rows,
            key=lambda d: (int(getattr(d, "time", 0)), int(getattr(d, "ticket", 0))),
        )

    def _build_broker_order(self, cid: str, ticket: int) -> BrokerOrder | None:
        open_order = self._open_order(ticket)
        raw = open_order if open_order is not None else self._history_order(ticket)
        if raw is None:
            return None
        deals = self._deals_for(ticket)
        fills = tuple(self._deal_to_fill(d) for d in deals)
        filled = sum(f.quantity for f in fills)
        avg = (
            sum(f.quantity * f.price for f in fills) / filled if filled > 0 else None
        )
        if open_order is not None:
            state = (
                BrokerOrderState.PARTIALLY_FILLED
                if filled > 0
                else BrokerOrderState.SUBMITTED
            )
        else:
            state = self._state_from_mt5(int(getattr(raw, "state", -1)))
            if fills and state in (BrokerOrderState.UNKNOWN, BrokerOrderState.SUBMITTED):
                state = (
                    BrokerOrderState.FILLED
                    if filled >= _finite(getattr(raw, "volume_initial", 0.0)) - 1e-9
                    else BrokerOrderState.PARTIALLY_FILLED
                )
        quantity = _finite(
            getattr(raw, "volume_initial", getattr(raw, "volume_current", filled))
        )
        return BrokerOrder(
            client_order_id=cid,
            broker_order_id=str(ticket),
            instrument=self._reverse_symbol_map.get(
                str(getattr(raw, "symbol", "")), str(getattr(raw, "symbol", ""))
            ),
            side=(
                OrderSide.BUY
                if int(getattr(raw, "type", ORDER_TYPE_BUY)) in (ORDER_TYPE_BUY, 2)
                else OrderSide.SELL
            ),
            order_type=OrderType.MARKET,
            quantity=quantity,
            state=state,
            filled_quantity=filled,
            avg_fill_price=avg,
            reject_reason=None,
            fills=fills,
        )

    @staticmethod
    def _deal_to_fill(deal: Any) -> BrokerFill:
        # fill_sequence = ticket del deal (identidad ESTABLE y unica; no se
        # reenumera en cada poll). commission = importe monetario del deal.
        return BrokerFill(
            broker_order_id=str(getattr(deal, "order", "")),
            quantity=_finite(getattr(deal, "volume")),
            price=_finite(getattr(deal, "price")),
            timestamp=datetime.fromtimestamp(
                int(getattr(deal, "time", 0)), tz=timezone.utc
            ),
            commission=_finite(getattr(deal, "commission", 0.0)),
            fill_sequence=int(getattr(deal, "ticket", 0)),
        )

    @staticmethod
    def _state_from_mt5(state: int) -> BrokerOrderState:
        return {
            ORDER_STATE_PLACED: BrokerOrderState.SUBMITTED,
            ORDER_STATE_PARTIAL: BrokerOrderState.PARTIALLY_FILLED,
            ORDER_STATE_FILLED: BrokerOrderState.FILLED,
            ORDER_STATE_CANCELED: BrokerOrderState.CANCELLED,
            ORDER_STATE_REJECTED: BrokerOrderState.REJECTED,
            ORDER_STATE_EXPIRED: BrokerOrderState.EXPIRED,
        }.get(state, BrokerOrderState.UNKNOWN)

    def _indeterminate_broker_order(self, cid: str) -> BrokerOrder:
        return BrokerOrder(
            client_order_id=cid,
            broker_order_id="",
            instrument="",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=0.0,
            state=BrokerOrderState.UNKNOWN,
            fills=(),
        )

    def _provisional_broker_order(
        self, cid: str, order: Order, result: Any
    ) -> BrokerOrder:
        volume = _finite(getattr(result, "volume", order.quantity))
        price = _finite(getattr(result, "price", 0.0))
        return BrokerOrder(
            client_order_id=cid,
            broker_order_id=str(self._ticket_from_result(result) or ""),
            instrument=order.instrument,
            side=order.side,
            order_type=order.order_type,
            quantity=volume,
            state=BrokerOrderState.SUBMITTED,
            fills=(),
        )


__all__ = [
    "DealHistoryResult",
    "MT5AccountObservationError",
    "MT5AccountSnapshot",
    "MT5BrokerAdapter",
    "MT5Config",
    "MT5ConfigError",
    "MT5Credentials",
    "MT5DealHistoryError",
    "MT5Environment",
    "MT5SymbolSpec",
    "instrument_spec_from_mt5",
    "read_mt5_symbol_spec",
]
