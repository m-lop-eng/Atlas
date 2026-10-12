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
from execution.intent_journal import OPERATION_CANCEL
from execution.order import Order, OrderSide, OrderType

from brokers.base import BrokerAdapter
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
        self._reverse_symbol_map = {
            mt5_symbol: atlas for atlas, mt5_symbol in config.symbol_map.items()
        }

    # ------------------------------------------------------------- propiedades

    @property
    def state(self) -> str:
        """Estado interno detallado (disconnected/connecting/ready/failed/...)."""
        return self._state

    @property
    def environment(self) -> MT5Environment:
        return self.config.environment

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

    # ---------------------------------------------------------------- simbolos

    def _symbol_spec(self, strategy_instrument: str) -> MT5SymbolSpec:
        cached = self._specs.get(strategy_instrument)
        if cached is not None:
            return cached
        mt5_name = self.config.symbol_map.get(strategy_instrument)
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
            raise BrokerPermissionError(
                "LIVE-09: reduce_only no soportado en cuentas HEDGING "
                "(contract gap: falta broker_position_id inequivoco); no se envia"
            )
        try:
            rows = list(mt5.positions_get(symbol=spec.name) or ())
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__})"
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

    def submit_order(self, order: Order) -> OrderSubmission:
        self._require_demo()
        self._require_ready()
        cid = order.client_order_id
        if not cid:
            raise BrokerInvalidOrderError("client_order_id obligatorio")
        # MT5-DEMO-EXECUTION-FOUNDATION-01: solo envia si el gate de capacidad
        # enlazado autoriza este cid. Sin gate (default) => LIVE-09 intacto.
        if self._execution_gate is not None:
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
            raw_positions = list(mt5.positions_get() or ())
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"positions_get fallo ({type(exc).__name__})"
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
            symbol = str(getattr(p, "symbol", ""))
            volume = _finite(getattr(p, "volume"))
            ptype = int(getattr(p, "type", 0))
            sign = 1.0 if ptype == ORDER_TYPE_BUY else -1.0
            positions.append(
                Position(
                    instrument=self._reverse_symbol_map.get(symbol, symbol),
                    quantity=sign * volume,
                    avg_entry_price=_finite(getattr(p, "price_open")),
                    updated_at=self._position_time(getattr(p, "time", 0)),
                )
            )
        return positions

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

    def _safe_orders_get(self) -> list[Any]:
        try:
            return list(self._module().orders_get() or ())
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"orders_get fallo ({type(exc).__name__})"
            ) from exc

    def _safe_history_orders_get(self) -> list[Any]:
        try:
            return list(self._module().history_orders_get() or ())
        except Exception:  # noqa: BLE001 - historial opcional para discovery
            return []

    def _open_order(self, ticket: int) -> Any | None:
        try:
            rows = self._module().orders_get(ticket=ticket) or ()
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"orders_get(ticket) fallo ({type(exc).__name__})"
            ) from exc
        return rows[0] if rows else None

    def _history_order(self, ticket: int) -> Any | None:
        try:
            rows = self._module().history_orders_get(ticket=ticket) or ()
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"history_orders_get fallo ({type(exc).__name__})"
            ) from exc
        return rows[-1] if rows else None

    def _deals_for(self, ticket: int) -> list[Any]:
        try:
            rows = self._module().history_deals_get(order=ticket) or ()
        except Exception as exc:  # noqa: BLE001
            raise BrokerConnectionError(
                f"history_deals_get fallo ({type(exc).__name__})"
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
    "MT5BrokerAdapter",
    "MT5Config",
    "MT5ConfigError",
    "MT5Credentials",
    "MT5Environment",
    "MT5SymbolSpec",
    "instrument_spec_from_mt5",
    "read_mt5_symbol_spec",
]
