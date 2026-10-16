"""Modelo contable para cuentas MT5 apalancadas (Forex/CFD). ATLAS-MT5-ACCOUNTING-02/02B.

Modulo INDEPENDIENTE y de solo contabilidad. NO modifica ni sustituye
`papertrading.AccountLedger` (semantica spot/backtesting intacta), NO conecta con
MT5, NO envia ordenes y NO forma parte todavia del `LiveExecutionRuntime`.

Semantica (auditoria ATLAS-MT5-ACCOUNTING-01):
  * Balance/cash y credito se mantienen SEPARADOS de posiciones y margen.
  * La apertura/aumento de posicion NO toca el balance (no se debita el notional).
  * El P&L realizado se toma del campo monetario AUTORITATIVO del deal (`profit`).
  * Comision/swap/fee se contabilizan EXACTAMENTE UNA VEZ y con el signo reportado
    por el broker (no se invierte por intuicion). `DEAL_SWAP` PUEDE representar
    swap acumulado al cierre y algunas comisiones llegan como movimiento de
    balance independiente: el modelo NO asume que todo coste aparece en el fill;
    si un coste no puede atribuirse con los tipos actuales, queda bloqueado.
  * El P&L flotante y la equity interna NO se calculan (requieren valoracion): la
    equity es un valor OBSERVADO del broker; nunca se declara una equity validada.

Integridad (ATLAS-MT5-ACCOUNTING-02A/02B):
  * Validacion estricta de tipos y de importes (rechaza bool/str/NaN/Inf; NO
    sustituye invalidos por cero; `None` solo se admite como cero si el contrato
    tipado lo representa como ausente).
  * Aplicacion ATOMICA: se calcula el estado siguiente en locales y solo se
    confirma la mutacion tras validar TODO; ante cualquier error no hay cambio de
    estado y el modelo pasa a incompleto con motivo estable.
  * Identidad de cuenta robusta: la huella deriva del servidor + identificador
    COMPLETO (transitorio, nunca almacenado); el login enmascarado es solo
    presentacion. Cada deal porta su `account_fingerprint` y se rechaza si no
    coincide con la baseline.
  * Los eventos rechazados NUNCA quedan marcados como aplicados.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

MT5_ACCOUNTING_SCHEMA_VERSION = 1
# Version del payload de ESTADO serializable (snapshot del ledger).
#   * v2 (05B): anade la metadata de EPOCA contable (`epoch`).
#   * v3 (05C-INGESTION): anade el estado DURABLE de ingestion (`ingestion`:
#     cursor `last_successful_scan_to` + intervalo y status de la ultima
#     consulta exitosa).
# Los payloads de versiones anteriores NO se interpretan con defaults: se
# rechazan con `MT5AccountStateVersionError`.
MT5_ACCOUNTING_STATE_VERSION = 3

# Constantes oficiales MQL5 (ENUM_DEAL_TYPE / ENUM_DEAL_ENTRY), definidas localmente
# para no depender del paquete MetaTrader5 (import perezoso innecesario offline).
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_CREDIT = 3
DEAL_TYPE_CHARGE = 4
DEAL_TYPE_CORRECTION = 5
DEAL_TYPE_BONUS = 6
DEAL_TYPE_COMMISSION = 7
DEAL_TYPE_COMMISSION_DAILY = 8
DEAL_TYPE_COMMISSION_MONTHLY = 9
DEAL_TYPE_COMMISSION_AGENT_DAILY = 10
DEAL_TYPE_COMMISSION_AGENT_MONTHLY = 11
DEAL_TYPE_INTEREST = 12
DEAL_TYPE_BUY_CANCELED = 13
DEAL_TYPE_SELL_CANCELED = 14
DEAL_TYPE_DIVIDEND = 15
DEAL_TYPE_DIVIDEND_TAX = 16

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

_NON_COMMERCIAL_TYPES = frozenset(
    {
        DEAL_TYPE_CHARGE,
        DEAL_TYPE_CORRECTION,
        DEAL_TYPE_BONUS,
        DEAL_TYPE_COMMISSION,
        DEAL_TYPE_COMMISSION_DAILY,
        DEAL_TYPE_COMMISSION_MONTHLY,
        DEAL_TYPE_COMMISSION_AGENT_DAILY,
        DEAL_TYPE_COMMISSION_AGENT_MONTHLY,
        DEAL_TYPE_INTEREST,
        DEAL_TYPE_DIVIDEND,
        DEAL_TYPE_DIVIDEND_TAX,
    }
)

_VOL_EPS = 1e-9
_DEFAULT_MONEY_EPS = 1e-9

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MASKED_RE = re.compile(r"^\*+[0-9]{0,4}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{32}$")


class MT5AccountingError(ValueError):
    """Error base del modelo contable MT5."""


class BaselineError(MT5AccountingError):
    """Baseline ausente, invalida o de esquema incompatible."""


class InvalidDealError(MT5AccountingError):
    """Deal con tipos/campos mal formados (rechazo previo a cualquier mutacion)."""


class UnsupportedDealError(MT5AccountingError):
    """Deal no soportado en esta fase (bloqueo explicito)."""


class AccountingIncompleteError(MT5AccountingError):
    """La contabilidad no puede continuar de forma inequivoca (bloqueo)."""


class DealAccountMismatchError(MT5AccountingError):
    """El deal no pertenece a la cuenta de la baseline (o carece de huella)."""


class DealIdentityCollisionError(MT5AccountingError):
    """Mismo ticket de deal con contenido distinto (identidad inconsistente)."""


class MT5AccountStateVersionError(MT5AccountingError):
    """`state_version` del payload de estado no soportada (incompatible)."""


class AccountingEpochError(MT5AccountingError):
    """Metadata de epoca contable ausente, mal formada o contradictoria."""


class IngestionStateError(MT5AccountingError):
    """Estado de ingestion ausente, mal formado o incoherente."""


class DealEventKind(str, Enum):
    """Clasificacion explicita de un deal MT5."""

    TRADING_FILL = "TRADING_FILL"
    BALANCE_MOVEMENT = "BALANCE_MOVEMENT"
    CREDIT_MOVEMENT = "CREDIT_MOVEMENT"
    NON_COMMERCIAL = "NON_COMMERCIAL"
    UNSUPPORTED = "UNSUPPORTED"
    INCOMPLETE = "INCOMPLETE"


class AccountReconciliationStatus(str, Enum):
    """Resultado de comparar el balance/credito internos con el broker."""

    BALANCE_MISMATCH = "BALANCE_MISMATCH"
    # balance/credito/divisa coinciden, pero la EQUITY interna no es calculable:
    # la equity debe reconciliarse con la OBSERVADA del broker. NO es "OK".
    BALANCE_MATCH_EQUITY_UNVERIFIED = "BALANCE_MATCH_EQUITY_UNVERIFIED"


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_money(value: object, field: str) -> float:
    """Importe monetario ESTRICTO. `None`==ausente==0.0; cualquier otro valor no
    finito o no numerico (bool/str/objeto) se RECHAZA (no se sustituye por 0)."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MT5AccountingError(f"{field} no numerico: {type(value).__name__}")
    f = float(value)
    if not math.isfinite(f):
        raise MT5AccountingError(f"{field} no finito")
    return f


def _valid_positive(value: object) -> bool:
    return _finite(value) and float(value) > 0.0


def _nonzero(value: object) -> bool:
    return _finite(value) and abs(float(value)) > _DEFAULT_MONEY_EPS


def account_fingerprint(*, server: str, account_id: int | str) -> str:
    """Huella NO secreta determinista = sha256(servidor | identificador COMPLETO).

    El identificador completo se usa SOLO de forma transitoria para calcular la
    huella: no se almacena ni se imprime. Es estable entre reinicios y distingue
    cuentas distintas del mismo servidor aunque compartan mascara de login.
    """
    if not isinstance(server, str) or not server.strip():
        raise MT5AccountingError("server invalido para la huella de cuenta")
    if isinstance(account_id, bool) or not isinstance(account_id, (int, str)):
        raise MT5AccountingError("account_id invalido para la huella de cuenta")
    raw = str(account_id).strip()
    if not raw:
        raise MT5AccountingError("account_id vacio para la huella de cuenta")
    material = f"{server.strip()}|{raw}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class BaselineOrigin(str, Enum):
    """Origen del estado inicial de una epoca contable."""

    # Estado inicial DECLARADO por observacion puntual de la cuenta: NO es una
    # reconstruccion del historial anterior.
    DECLARED = "DECLARED"
    # Reconstruccion historica completa: NO disponible con las APIs actuales.
    RECONSTRUCTED = "RECONSTRUCTED"


@dataclass(frozen=True, slots=True)
class AccountingEpoch:
    """Epoca contable: origen del estado inicial y estado de continuidad.

    `continuity_verified` es SIEMPRE `False` por defecto: crear y persistir una
    baseline NO demuestra continuidad historica desde el corte. Una baseline
    `DECLARED` no puede declararse con continuidad verificada (fail-closed).
    """

    origin: BaselineOrigin
    continuity_verified: bool = False

    def __post_init__(self) -> None:
        origin = self.origin
        if not isinstance(origin, BaselineOrigin):
            try:
                origin = BaselineOrigin(origin)
            except ValueError:
                raise AccountingEpochError(
                    f"BaselineOrigin invalido: {origin!r}"
                ) from None
            object.__setattr__(self, "origin", origin)
        if not isinstance(self.continuity_verified, bool):
            raise AccountingEpochError("continuity_verified debe ser bool")
        if origin is BaselineOrigin.RECONSTRUCTED:
            # RESERVADO: no existe reconstruccion historica verificable en el
            # contrato actual. Se rechaza en TODA la superficie publica (constructor,
            # serializacion y restauracion) con independencia de continuity_verified.
            raise AccountingEpochError(
                "BaselineOrigin.RECONSTRUCTED esta reservado: requiere una "
                "reconstruccion historica verificable (no disponible); no se infiere "
                "a partir de un snapshot"
            )
        if origin is BaselineOrigin.DECLARED and self.continuity_verified:
            raise AccountingEpochError(
                "una baseline DECLARED no puede declarar continuidad verificada"
            )


class IngestionScanStatus(str, Enum):
    """Resultado de la ULTIMA consulta de historial completada con exito.

    NO se acopla a `brokers.mt5` (capa broker): mismos valores semanticos
    (`HISTORY_COMPLETE`/`HISTORY_EMPTY_CONFIRMED`).
    """

    COMPLETE = "COMPLETE"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"


@dataclass(frozen=True, slots=True)
class IngestionState:
    """Estado DURABLE de ingestion historica (mismo snapshot que el ledger).

    Registra la ULTIMA consulta de `deal_history` completada con EXITO. NO es
    evidencia de cobertura historica global: `COMPLETE`/`EMPTY_CONFIRMED` solo
    describen el intervalo solicitado; no demuestran ausencia de huecos ni de
    eventos tardios, ni habilitan continuidad verificada.

    Invariantes:
      * Sin consultas previas: `from`/`to`/`status` a `None` (sin cursor ficticio).
      * `from`/`to` presentes juntos o ausentes juntos, normalizados a UTC.
      * `from <= to`.
      * `status ∈ {COMPLETE, EMPTY_CONFIRMED}`, presente si y solo si hay intervalo.
    """

    last_successful_scan_from: datetime | None = None
    last_successful_scan_to: datetime | None = None
    status: IngestionScanStatus | None = None

    def __post_init__(self) -> None:
        frm = self.last_successful_scan_from
        to = self.last_successful_scan_to
        status = self.status
        if (frm is None) != (to is None):
            raise IngestionStateError(
                "intervalo de ingesta parcial: from/to deben estar juntos o ausentes"
            )
        if frm is None:
            if status is not None:
                raise IngestionStateError(
                    "status de ingesta sin intervalo de consulta"
                )
            return
        if (
            not isinstance(frm, datetime)
            or frm.tzinfo is None
            or frm.utcoffset() is None
        ):
            raise IngestionStateError(
                "last_successful_scan_from debe ser datetime con zona (UTC), no naive"
            )
        if (
            not isinstance(to, datetime)
            or to.tzinfo is None
            or to.utcoffset() is None
        ):
            raise IngestionStateError(
                "last_successful_scan_to debe ser datetime con zona (UTC), no naive"
            )
        frm = frm.astimezone(timezone.utc)
        to = to.astimezone(timezone.utc)
        if frm > to:
            raise IngestionStateError(
                "last_successful_scan_from no puede ser posterior a _to"
            )
        if status is None:
            raise IngestionStateError("intervalo de ingesta sin status")
        if not isinstance(status, IngestionScanStatus):
            try:
                status = IngestionScanStatus(status)
            except ValueError:
                raise IngestionStateError(
                    f"status de ingesta invalido: {status!r}"
                ) from None
        object.__setattr__(self, "last_successful_scan_from", frm)
        object.__setattr__(self, "last_successful_scan_to", to)
        object.__setattr__(self, "status", status)

    @property
    def has_successful_scan(self) -> bool:
        return self.last_successful_scan_to is not None


@dataclass(frozen=True, slots=True)
class MT5AccountBaseline:
    """Baseline explicita y revisable de una cuenta MT5.

    Dominio de importes (politica por campo):
      * `balance`: finito; se PERMITE negativo (estado observado legitimo que el
        modelo debe preservar sin clamp ni conversion).
      * `credit`: finito y NO negativo (una facilidad de credito no es negativa;
        un cargo se refleja como movimiento de balance). Se rechaza negativo.
    """

    schema_version: int
    account_fingerprint: str
    server: str
    account_currency: str
    baseline_at: datetime
    coverage_from: datetime
    balance: float
    credit: float
    login_masked: str
    leverage: int | None = None
    trade_mode: int = 0
    margin_mode: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != MT5_ACCOUNTING_SCHEMA_VERSION:
            raise BaselineError(
                f"baseline schema no soportada: {self.schema_version!r} "
                f"(esperada {MT5_ACCOUNTING_SCHEMA_VERSION})"
            )
        if not isinstance(self.account_fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
            self.account_fingerprint
        ):
            raise BaselineError("account_fingerprint invalido (hex de 32)")
        if not isinstance(self.server, str) or not self.server.strip():
            raise BaselineError("server debe ser str no vacio")
        if not isinstance(self.account_currency, str) or not _CURRENCY_RE.fullmatch(
            self.account_currency
        ):
            raise BaselineError(
                "account_currency invalida (formato requerido: 3 letras mayusculas)"
            )
        if not isinstance(self.login_masked, str) or not _MASKED_RE.fullmatch(
            self.login_masked
        ):
            raise BaselineError("login_masked invalido (formato enmascarado)")
        for name, value in (("baseline_at", self.baseline_at), ("coverage_from", self.coverage_from)):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise BaselineError(f"{name} debe ser datetime con zona horaria")
            if value.utcoffset() != timedelta(0):
                raise BaselineError(f"{name} debe ser UTC (offset 0)")
        if self.coverage_from != self.baseline_at:
            raise BaselineError(
                "coverage_from debe coincidir con baseline_at (cobertura desde la baseline)"
            )
        if not _finite(self.balance):
            raise BaselineError(f"balance invalido: {self.balance!r}")
        if not _finite(self.credit):
            raise BaselineError(f"credit invalido: {self.credit!r}")
        if float(self.credit) < 0.0:
            raise BaselineError("credit no puede ser negativo (politica del modelo)")
        if self.leverage is not None and (
            isinstance(self.leverage, bool) or not isinstance(self.leverage, int)
        ):
            raise BaselineError("leverage debe ser int o None")
        for name, value in (("trade_mode", self.trade_mode), ("margin_mode", self.margin_mode)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise BaselineError(f"{name} debe ser int")


@dataclass(frozen=True, slots=True)
class MT5Position:
    """Posicion MT5 identificada por POSITION_IDENTIFIER (estable)."""

    position_id: int
    symbol: str
    quantity: float  # dirigida: +long / -short
    avg_price: float


@dataclass(frozen=True, slots=True)
class MovementRecord:
    """Movimiento NO-fill (balance/credito)."""

    deal_key: tuple[str, int]
    kind: DealEventKind
    amount: float
    time: datetime


@dataclass(frozen=True, slots=True)
class BrokerAccountObservation:
    """Valores OBSERVADOS del broker (no calculados internamente)."""

    balance: float
    equity: float
    credit: float
    margin: float
    margin_free: float
    currency: str


@dataclass(frozen=True, slots=True)
class MT5Deal:
    """Deal MT5 normalizado (campos MQL5 relevantes).

    `account_fingerprint` identifica la CUENTA VERIFICADA que produjo el deal y es
    obligatoria. Los campos monetarios se consumen con el signo reportado.
    """

    account_fingerprint: str
    ticket: int
    order: int
    type: int
    entry: int | None
    position_id: int | None
    symbol: str | None
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    time: datetime


@dataclass(frozen=True, slots=True)
class DealEvent:
    """Resultado de clasificar un deal."""

    kind: DealEventKind
    deal: MT5Deal
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DealApplyResult:
    """Resultado de aplicar (o reconocer como idempotente) un deal."""

    kind: DealEventKind
    deal_key: tuple[str, int]
    applied: bool
    balance: float
    credit: float


@dataclass(frozen=True, slots=True)
class _PlannedState:
    """Estado siguiente calculado en locales (no aplicado aun)."""

    balance: float
    credit: float
    realized_pnl: float
    positions: dict[int, MT5Position]
    movement: MovementRecord | None


def _validate_deal(deal: MT5Deal) -> None:
    """Validacion estricta de tipos/estructura (sin conversion implicita)."""
    if isinstance(deal.account_fingerprint, bool) or not isinstance(
        deal.account_fingerprint, str
    ):
        raise InvalidDealError("account_fingerprint debe ser str")
    if isinstance(deal.ticket, bool) or not isinstance(deal.ticket, int):
        raise InvalidDealError("ticket debe ser int")
    if deal.ticket <= 0:
        raise InvalidDealError("ticket debe ser > 0")
    if isinstance(deal.order, bool) or not isinstance(deal.order, int):
        raise InvalidDealError("order debe ser int")
    if isinstance(deal.type, bool) or not isinstance(deal.type, int):
        raise InvalidDealError("type debe ser int")
    if deal.entry is not None and (
        isinstance(deal.entry, bool) or not isinstance(deal.entry, int)
    ):
        raise InvalidDealError("entry debe ser int o None")
    if deal.position_id is not None and (
        isinstance(deal.position_id, bool) or not isinstance(deal.position_id, int)
    ):
        raise InvalidDealError("position_id debe ser int o None")
    if deal.symbol is not None and not isinstance(deal.symbol, str):
        raise InvalidDealError("symbol debe ser str o None")
    for name in ("volume", "price"):
        v = getattr(deal, name)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise InvalidDealError(f"{name} debe ser numerico")
    if not isinstance(deal.time, datetime) or deal.time.tzinfo is None:
        raise InvalidDealError("time debe ser datetime con zona horaria")


def classify_deal(deal: MT5Deal) -> DealEvent:
    """Clasifica un deal MT5 SIN asumir `DEAL_ORDER == 0` como criterio unico."""
    if not isinstance(deal, MT5Deal):
        raise MT5AccountingError("classify_deal exige un MT5Deal normalizado")

    t = deal.type

    if t in (DEAL_TYPE_BUY, DEAL_TYPE_SELL):
        if deal.entry is None or isinstance(deal.entry, bool) or not isinstance(
            deal.entry, int
        ):
            return DealEvent(DealEventKind.UNSUPPORTED, deal, "entry ausente/invalido")
        entry = deal.entry
        if entry in (DEAL_ENTRY_IN, DEAL_ENTRY_OUT):
            if (
                isinstance(deal.position_id, bool)
                or not isinstance(deal.position_id, int)
                or deal.position_id <= 0
            ):
                return DealEvent(
                    DealEventKind.UNSUPPORTED, deal,
                    "fill de trading sin POSITION_IDENTIFIER verificable",
                )
            if not isinstance(deal.symbol, str) or not deal.symbol.strip():
                return DealEvent(
                    DealEventKind.UNSUPPORTED, deal, "fill de trading sin simbolo"
                )
            if not _valid_positive(deal.volume):
                return DealEvent(
                    DealEventKind.UNSUPPORTED, deal, "volumen de fill invalido"
                )
            if not _valid_positive(deal.price):
                return DealEvent(
                    DealEventKind.UNSUPPORTED, deal, "precio de fill invalido"
                )
            return DealEvent(DealEventKind.TRADING_FILL, deal)
        if entry in (DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY):
            return DealEvent(
                DealEventKind.UNSUPPORTED, deal,
                f"entry {entry} (INOUT/OUT_BY) no soportado en esta fase",
            )
        return DealEvent(
            DealEventKind.UNSUPPORTED, deal, f"entry desconocido: {entry!r}"
        )

    if t in (DEAL_TYPE_BUY_CANCELED, DEAL_TYPE_SELL_CANCELED):
        return DealEvent(
            DealEventKind.UNSUPPORTED, deal, "deal cancelado: no contabilizable"
        )

    if t in (DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT):
        if (
            _nonzero(deal.commission)
            or _nonzero(deal.swap)
            or _nonzero(deal.fee)
            or _nonzero(deal.volume)
            or _nonzero(deal.price)
        ):
            return DealEvent(
                DealEventKind.INCOMPLETE, deal,
                "movimiento con commission/swap/fee/volume/price inesperados",
            )
        if not _finite(deal.profit):
            return DealEvent(
                DealEventKind.INCOMPLETE, deal, "movimiento sin importe valido"
            )
        kind = (
            DealEventKind.BALANCE_MOVEMENT
            if t == DEAL_TYPE_BALANCE
            else DealEventKind.CREDIT_MOVEMENT
        )
        return DealEvent(kind, deal)

    if t in _NON_COMMERCIAL_TYPES:
        return DealEvent(
            DealEventKind.NON_COMMERCIAL, deal,
            f"tipo no comercial {t}: requiere politica de contabilizacion",
        )

    return DealEvent(DealEventKind.UNSUPPORTED, deal, f"tipo de deal desconocido: {t}")


# --------------------------------------------------------------------------
# Serializacion estricta de estado (para persistencia/restauracion offline).
# NO se usa reflexion sobre atributos privados desde fuera del modelo: estas
# funciones y los metodos `to_snapshot`/`from_snapshot` son la interfaz publica.
# --------------------------------------------------------------------------


def _require_number(value: object, field: str) -> float:
    """Numero finito real (rechaza bool/str/None/NaN/Inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MT5AccountingError(f"{field} debe ser numerico")
    f = float(value)
    if not math.isfinite(f):
        raise MT5AccountingError(f"{field} no finito")
    return f


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise MT5AccountingError(f"{field} debe ser str ISO-8601")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MT5AccountingError(f"{field} ISO-8601 invalido") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise MT5AccountingError(f"{field} sin zona horaria")
    return dt


def _epoch_to_dict(epoch: AccountingEpoch) -> dict:
    return {
        "origin": epoch.origin.value,
        "continuity_verified": epoch.continuity_verified,
    }


def _epoch_from_dict(d: object) -> AccountingEpoch:
    if not isinstance(d, dict):
        raise AccountingEpochError("epoch del snapshot no es dict")
    if "origin" not in d:
        raise AccountingEpochError("epoch sin `origin`")
    if "continuity_verified" not in d:
        raise AccountingEpochError("epoch sin `continuity_verified`")
    try:
        origin = BaselineOrigin(d["origin"])
    except ValueError as exc:
        raise AccountingEpochError(f"epoch origin invalido: {d['origin']!r}") from exc
    if not isinstance(d["continuity_verified"], bool):
        raise AccountingEpochError("continuity_verified debe ser bool")
    return AccountingEpoch(
        origin=origin, continuity_verified=d["continuity_verified"]
    )


def _ingestion_to_dict(state: IngestionState) -> dict:
    return {
        "last_successful_scan_from": (
            None
            if state.last_successful_scan_from is None
            else state.last_successful_scan_from.isoformat()
        ),
        "last_successful_scan_to": (
            None
            if state.last_successful_scan_to is None
            else state.last_successful_scan_to.isoformat()
        ),
        "status": None if state.status is None else state.status.value,
    }


def _ingestion_from_dict(d: object) -> IngestionState:
    if not isinstance(d, dict):
        raise IngestionStateError("ingestion del snapshot no es dict")
    for key in ("last_successful_scan_from", "last_successful_scan_to", "status"):
        if key not in d:
            raise IngestionStateError(f"ingestion sin `{key}`")
    frm_raw = d["last_successful_scan_from"]
    to_raw = d["last_successful_scan_to"]
    status_raw = d["status"]
    frm = (
        None
        if frm_raw is None
        else _parse_utc(frm_raw, "last_successful_scan_from")
    )
    to = (
        None
        if to_raw is None
        else _parse_utc(to_raw, "last_successful_scan_to")
    )
    return IngestionState(
        last_successful_scan_from=frm,
        last_successful_scan_to=to,
        status=None if status_raw is None else status_raw,
    )


def _baseline_to_dict(baseline: MT5AccountBaseline) -> dict:
    return {
        "schema_version": baseline.schema_version,
        "account_fingerprint": baseline.account_fingerprint,
        "server": baseline.server,
        "account_currency": baseline.account_currency,
        "baseline_at": baseline.baseline_at.isoformat(),
        "coverage_from": baseline.coverage_from.isoformat(),
        "balance": baseline.balance,
        "credit": baseline.credit,
        "login_masked": baseline.login_masked,
        "leverage": baseline.leverage,
        "trade_mode": baseline.trade_mode,
        "margin_mode": baseline.margin_mode,
    }


def _baseline_from_dict(d: object) -> MT5AccountBaseline:
    if not isinstance(d, dict):
        raise MT5AccountingError("baseline del snapshot no es dict")
    required = (
        "schema_version", "account_fingerprint", "server", "account_currency",
        "baseline_at", "coverage_from", "balance", "credit", "login_masked",
    )
    missing = [k for k in required if k not in d]
    if missing:
        raise MT5AccountingError(f"baseline sin campos: {missing}")
    return MT5AccountBaseline(
        schema_version=d["schema_version"],
        account_fingerprint=d["account_fingerprint"],
        server=d["server"],
        account_currency=d["account_currency"],
        baseline_at=_parse_utc(d["baseline_at"], "baseline_at"),
        coverage_from=_parse_utc(d["coverage_from"], "coverage_from"),
        balance=d["balance"],
        credit=d["credit"],
        login_masked=d["login_masked"],
        leverage=d.get("leverage"),
        trade_mode=d.get("trade_mode", 0),
        margin_mode=d.get("margin_mode", 0),
    )


def _deal_to_dict(deal: MT5Deal) -> dict:
    return {
        "account_fingerprint": deal.account_fingerprint,
        "ticket": deal.ticket,
        "order": deal.order,
        "type": deal.type,
        "entry": deal.entry,
        "position_id": deal.position_id,
        "symbol": deal.symbol,
        "volume": deal.volume,
        "price": deal.price,
        "profit": deal.profit,
        "commission": deal.commission,
        "swap": deal.swap,
        "fee": deal.fee,
        "time": deal.time.isoformat(),
    }


def _deal_from_dict(d: object) -> MT5Deal:
    if not isinstance(d, dict):
        raise MT5AccountingError("deal del snapshot no es dict")
    required = (
        "account_fingerprint", "ticket", "order", "type", "volume", "price",
        "profit", "commission", "swap", "fee", "time",
    )
    missing = [k for k in required if k not in d]
    if missing:
        raise MT5AccountingError(f"deal sin campos: {missing}")
    deal = MT5Deal(
        account_fingerprint=d["account_fingerprint"],
        ticket=d["ticket"],
        order=d["order"],
        type=d["type"],
        entry=d.get("entry"),
        position_id=d.get("position_id"),
        symbol=d.get("symbol"),
        volume=d["volume"],
        price=d["price"],
        profit=d["profit"],
        commission=d["commission"],
        swap=d["swap"],
        fee=d["fee"],
        time=_parse_utc(d["time"], "deal.time"),
    )
    _validate_deal(deal)
    return deal


# Allowlist de clases de evento persistibles (H3). Un evento NO comercial,
# no soportado o incompleto NUNCA puede restaurarse como aplicado.
_ALLOWED_PROCESSED_KINDS = frozenset(
    {
        DealEventKind.TRADING_FILL,
        DealEventKind.BALANCE_MOVEMENT,
        DealEventKind.CREDIT_MOVEMENT,
    }
)
_ALLOWED_MOVEMENT_KINDS = frozenset(
    {DealEventKind.BALANCE_MOVEMENT, DealEventKind.CREDIT_MOVEMENT}
)

# Tolerancias deterministas para la verificacion de estado derivado (H4).
_STATE_ABS_EPS = 1e-9
_STATE_REL_EPS = 1e-9


def _approx_equal(a: float, b: float) -> bool:
    return math.isclose(
        float(a), float(b), rel_tol=_STATE_REL_EPS, abs_tol=_STATE_ABS_EPS
    )


def _parse_state_positions(raw: object) -> dict[int, MT5Position]:
    if not isinstance(raw, (list, tuple)):
        raise MT5AccountingError("positions debe ser lista")
    positions: dict[int, MT5Position] = {}
    for p in raw:
        if not isinstance(p, dict):
            raise MT5AccountingError("posicion no es dict")
        pid = p.get("position_id")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise MT5AccountingError("position_id invalido")
        if pid in positions:
            raise MT5AccountingError(f"position_id duplicado: {pid}")
        symbol = p.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise MT5AccountingError("posicion sin simbolo")
        quantity = _require_number(p.get("quantity"), "quantity")
        if abs(quantity) <= _VOL_EPS:
            raise MT5AccountingError("posicion con cantidad nula")
        avg_price = _require_number(p.get("avg_price"), "avg_price")
        if avg_price <= 0.0:
            raise MT5AccountingError("posicion con avg_price invalido")
        positions[pid] = MT5Position(pid, symbol, quantity, avg_price)
    return positions


def _parse_state_movements(
    raw: object,
) -> tuple[list[MovementRecord], dict[tuple[str, int], MovementRecord]]:
    if not isinstance(raw, (list, tuple)):
        raise MT5AccountingError("movements debe ser lista")
    movements: list[MovementRecord] = []
    by_key: dict[tuple[str, int], MovementRecord] = {}
    for m in raw:
        if not isinstance(m, dict):
            raise MT5AccountingError("movimiento no es dict")
        dk = m.get("deal_key")
        if not isinstance(dk, (list, tuple)) or len(dk) != 2:
            raise MT5AccountingError("movimiento con deal_key invalido")
        fp, ticket = dk[0], dk[1]
        if not isinstance(fp, str) or isinstance(ticket, bool) or not isinstance(
            ticket, int
        ):
            raise MT5AccountingError("movimiento con deal_key mal tipado")
        key = (fp, ticket)
        if key in by_key:
            raise MT5AccountingError("movimiento duplicado")
        try:
            kind = DealEventKind(m.get("kind"))
        except ValueError as exc:
            raise MT5AccountingError("movimiento con kind invalido") from exc
        if kind not in _ALLOWED_MOVEMENT_KINDS:
            raise MT5AccountingError(
                "movimiento con clase no admisible (H3)"
            )
        amount = _require_number(m.get("amount"), "movement.amount")
        time = _parse_utc(m.get("time"), "movement.time")
        rec = MovementRecord(key, kind, amount, time)
        movements.append(rec)
        by_key[key] = rec
    return movements, by_key


def _parse_state_processed(
    raw: object, fingerprint: str
) -> tuple[list[tuple[MT5Deal, DealEventKind]], dict[tuple[str, int], tuple[MT5Deal, DealEventKind]]]:
    if not isinstance(raw, (list, tuple)):
        raise MT5AccountingError("processed debe ser lista")
    ordered: list[tuple[MT5Deal, DealEventKind]] = []
    mapping: dict[tuple[str, int], tuple[MT5Deal, DealEventKind]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise MT5AccountingError("entrada processed no es dict")
        try:
            kind = DealEventKind(entry.get("kind"))
        except ValueError as exc:
            raise MT5AccountingError("processed con kind invalido") from exc
        if kind not in _ALLOWED_PROCESSED_KINDS:
            raise MT5AccountingError("evento procesado con clase no admisible (H3)")
        deal = _deal_from_dict(entry.get("deal"))
        if deal.account_fingerprint != fingerprint:
            raise MT5AccountingError("deal procesado de otra cuenta")
        if classify_deal(deal).kind is not kind:
            raise MT5AccountingError("kind de deal procesado incoherente")
        key = (fingerprint, deal.ticket)
        if key in mapping:
            raise MT5AccountingError("ticket de deal procesado duplicado")
        mapping[key] = (deal, kind)
        ordered.append((deal, kind))
    return ordered, mapping


def _check_movement_event_consistency(
    movements: list[MovementRecord],
    by_key: dict[tuple[str, int], MovementRecord],
    processed: dict[tuple[str, int], tuple[MT5Deal, DealEventKind]],
) -> None:
    """Biyeccion movimiento <-> evento BALANCE/CREDIT procesado (H3)."""
    for key, rec in by_key.items():
        pe = processed.get(key)
        if pe is None:
            raise MT5AccountingError("movimiento sin evento procesado correspondiente")
        pdeal, pkind = pe
        if pkind is not rec.kind:
            raise MT5AccountingError("clase de movimiento no coincide con el evento")
        if not _approx_equal(_require_money(pdeal.profit, "profit"), rec.amount):
            raise MT5AccountingError("importe de movimiento no coincide con el evento")
        if pdeal.time != rec.time:
            raise MT5AccountingError("timestamp de movimiento no coincide con el evento")
    for key, (pdeal, pkind) in processed.items():
        if pkind in _ALLOWED_MOVEMENT_KINDS and key not in by_key:
            raise MT5AccountingError("evento BALANCE/CREDIT sin movimiento asociado")


def _replay_and_compare(
    ledger: "MT5AccountLedger",
    *,
    baseline: MT5AccountBaseline,
    balance: float,
    credit: float,
    realized: float,
    positions: dict[int, MT5Position],
    movements: list[MovementRecord],
) -> None:
    """Reproduce el estado aplicando los deals con las reglas del modelo y lo
    compara con el persistido (H4). Determinista, no mutante sobre el resultado."""
    replay = MT5AccountLedger(baseline)
    for deal, _kind in ledger.processed_deals_in_order():
        try:
            replay.apply_deal(deal)
        except MT5AccountingError as exc:
            raise MT5AccountingError(
                f"estado persistido no reproducible: {exc}"
            ) from exc
    if not _approx_equal(replay.balance, balance):
        raise MT5AccountingError("balance persistido no reproducible (H4)")
    if not _approx_equal(replay.credit, credit):
        raise MT5AccountingError("credito persistido no reproducible (H4)")
    if not _approx_equal(replay.realized_pnl, realized):
        raise MT5AccountingError("realized_pnl persistido no reproducible (H4)")
    derived_pos = {p.position_id: p for p in replay.positions()}
    if set(derived_pos) != set(positions):
        raise MT5AccountingError("posiciones persistidas no reproducibles (H4)")
    for pid, dp in derived_pos.items():
        ep = positions[pid]
        if dp.symbol != ep.symbol or not _approx_equal(
            dp.quantity, ep.quantity
        ) or not _approx_equal(dp.avg_price, ep.avg_price):
            raise MT5AccountingError(
                f"posicion {pid} persistida no reproducible (H4)"
            )
    derived_mov = replay.movement_records()
    if len(derived_mov) != len(movements):
        raise MT5AccountingError("movimientos persistidos no reproducibles (H4)")
    for dm, pm in zip(derived_mov, movements):
        if (
            dm.deal_key != pm.deal_key
            or dm.kind is not pm.kind
            or not _approx_equal(dm.amount, pm.amount)
            or dm.time != pm.time
        ):
            raise MT5AccountingError(
                "movimiento persistido no reproducible (H4)"
            )


class MT5AccountLedger:
    """Ledger contable MT5 apalancado (Forex/CFD), separado del ledger spot."""

    def __init__(
        self,
        baseline: MT5AccountBaseline,
        *,
        epoch: AccountingEpoch | None = None,
        ingestion: IngestionState | None = None,
        reconciliation_tolerance_abs: float = _DEFAULT_MONEY_EPS,
        reconciliation_tolerance_rel: float = _DEFAULT_MONEY_EPS,
    ) -> None:
        if not isinstance(baseline, MT5AccountBaseline):
            raise BaselineError("MT5AccountLedger exige una MT5AccountBaseline explicita")
        if epoch is None:
            # Default seguro: epoca DECLARADA y continuidad NO verificada. Nunca
            # DECLARED+verified ni RECONSTRUCTED por defecto.
            epoch = AccountingEpoch(BaselineOrigin.DECLARED)
        elif not isinstance(epoch, AccountingEpoch):
            raise AccountingEpochError("epoch debe ser AccountingEpoch")
        if ingestion is None:
            # Sin consultas exitosas previas: SIN cursor ficticio.
            ingestion = IngestionState()
        elif not isinstance(ingestion, IngestionState):
            raise IngestionStateError("ingestion debe ser IngestionState")
        for name, value in (
            ("reconciliation_tolerance_abs", reconciliation_tolerance_abs),
            ("reconciliation_tolerance_rel", reconciliation_tolerance_rel),
        ):
            if not _finite(value) or float(value) < 0.0:
                raise MT5AccountingError(f"{name} debe ser finito y >= 0")
        self._baseline = baseline
        self._epoch = epoch
        self._ingestion = ingestion
        self._balance = float(baseline.balance)
        self._credit = float(baseline.credit)
        self._realized_pnl = 0.0
        self._positions: dict[int, MT5Position] = {}
        self._processed: dict[tuple[str, int], tuple[MT5Deal, DealEventKind]] = {}
        self._movements: list[MovementRecord] = []
        self._complete = True
        self._incomplete_reason: str | None = None
        self._observed: BrokerAccountObservation | None = None
        self._tol_abs = float(reconciliation_tolerance_abs)
        self._tol_rel = float(reconciliation_tolerance_rel)

    # ------------------------------------------------------------- consulta

    @property
    def baseline(self) -> MT5AccountBaseline:
        return self._baseline

    @property
    def account_fingerprint(self) -> str:
        return self._baseline.account_fingerprint

    @property
    def account_currency(self) -> str:
        return self._baseline.account_currency

    @property
    def epoch(self) -> AccountingEpoch:
        return self._epoch

    @property
    def origin(self) -> BaselineOrigin:
        return self._epoch.origin

    @property
    def continuity_verified(self) -> bool:
        return self._epoch.continuity_verified

    @property
    def ingestion(self) -> IngestionState:
        return self._ingestion

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def credit(self) -> float:
        return self._credit

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def floating_pnl(self) -> float | None:
        """NO calculable en esta fase (requiere valoracion/marks)."""
        return None

    def valuation_available(self) -> bool:
        """Esta fase NO calcula equity interna; se reconcilia con el broker."""
        return False

    @property
    def observed(self) -> BrokerAccountObservation | None:
        return self._observed

    @property
    def is_complete(self) -> bool:
        return self._complete

    @property
    def incomplete_reason(self) -> str | None:
        return self._incomplete_reason

    def positions(self) -> tuple[MT5Position, ...]:
        return tuple(self._positions[k] for k in sorted(self._positions))

    def movement_records(self) -> tuple[MovementRecord, ...]:
        return tuple(self._movements)

    def processed_deal_count(self) -> int:
        return len(self._processed)

    def processed_deals(self) -> tuple[tuple[MT5Deal, DealEventKind], ...]:
        """Deals aplicados (payload, clase) ordenados por clave `(fp, ticket)`."""
        return tuple(self._processed[k] for k in sorted(self._processed))

    def processed_deals_in_order(self) -> tuple[tuple[MT5Deal, DealEventKind], ...]:
        """Deals aplicados en ORDEN DE APLICACION (relevante para el replay)."""
        return tuple(self._processed.values())

    # ------------------------------------------------- serializacion/estado

    def to_snapshot(self) -> dict:
        """Payload de estado serializable (sin credenciales ni id completo)."""
        return {
            "state_version": MT5_ACCOUNTING_STATE_VERSION,
            "baseline": _baseline_to_dict(self._baseline),
            "epoch": _epoch_to_dict(self._epoch),
            "ingestion": _ingestion_to_dict(self._ingestion),
            "balance": self._balance,
            "credit": self._credit,
            "realized_pnl": self._realized_pnl,
            "positions": [
                {
                    "position_id": p.position_id,
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                }
                for p in self.positions()
            ],
            "movements": [
                {
                    "deal_key": [m.deal_key[0], m.deal_key[1]],
                    "kind": m.kind.value,
                    "amount": m.amount,
                    "time": m.time.isoformat(),
                }
                for m in self._movements
            ],
            # En ORDEN DE APLICACION (el replay depende de la secuencia).
            "processed": [
                {"kind": kind.value, "deal": _deal_to_dict(deal)}
                for deal, kind in self._processed.values()
            ],
            "is_complete": self._complete,
            "incomplete_reason": self._incomplete_reason,
        }

    @classmethod
    def from_snapshot(cls, snapshot: object) -> "MT5AccountLedger":
        """Reconstruye un ledger desde un payload de estado VALIDADO.

        Verifica (H3) la allowlist de clases de evento y la biyeccion
        movimiento<->evento, y (H4) recompone el estado derivado reaplicando los
        deals en su orden con las MISMAS reglas del modelo. Todo se valida en
        locales ANTES de construir/exponer el ledger restaurado: un snapshot
        invalido no produce objeto parcial ni muta nada.
        """
        if not isinstance(snapshot, dict):
            raise MT5AccountingError("snapshot no es dict")
        if snapshot.get("state_version") != MT5_ACCOUNTING_STATE_VERSION:
            raise MT5AccountStateVersionError(
                f"state_version no soportada: {snapshot.get('state_version')!r} "
                f"(esperada {MT5_ACCOUNTING_STATE_VERSION}); no se interpreta con defaults"
            )
        baseline = _baseline_from_dict(snapshot.get("baseline"))
        epoch = _epoch_from_dict(snapshot.get("epoch"))
        ingestion = _ingestion_from_dict(snapshot.get("ingestion"))

        balance = _require_number(snapshot.get("balance"), "balance")
        credit = _require_number(snapshot.get("credit"), "credit")
        if credit < 0.0:
            raise MT5AccountingError("credit restaurado negativo (fuera de dominio)")
        realized = _require_number(snapshot.get("realized_pnl"), "realized_pnl")

        positions = _parse_state_positions(snapshot.get("positions", []))
        movements, movements_by_key = _parse_state_movements(
            snapshot.get("movements", [])
        )
        ordered, processed = _parse_state_processed(
            snapshot.get("processed", []), baseline.account_fingerprint
        )
        _check_movement_event_consistency(movements, movements_by_key, processed)

        complete = snapshot.get("is_complete")
        if not isinstance(complete, bool):
            raise MT5AccountingError("is_complete debe ser bool")
        reason = snapshot.get("incomplete_reason")
        if reason is not None and not isinstance(reason, str):
            raise MT5AccountingError("incomplete_reason debe ser str o None")
        if complete and reason is not None:
            raise MT5AccountingError("is_complete=True con incomplete_reason")
        if not complete and not reason:
            raise MT5AccountingError("is_complete=False sin motivo")

        # H4: recomputar el estado desde los eventos aceptados (mismas reglas).
        result = cls(baseline)
        result._processed = dict(processed)  # orden de insercion = orden persistido
        _replay_and_compare(
            result,
            baseline=baseline,
            balance=balance,
            credit=credit,
            realized=realized,
            positions=positions,
            movements=movements,
        )

        # Solo tras validar todo se confirma el estado restaurado.
        result._balance = balance
        result._credit = credit
        result._realized_pnl = realized
        result._positions = positions
        result._movements = movements
        result._complete = complete
        result._incomplete_reason = reason
        result._epoch = epoch
        result._ingestion = ingestion
        return result

    # -------------------------------------------------------------- apply

    def apply_deal(self, deal: MT5Deal) -> DealApplyResult:
        """Aplica un deal de forma ATOMICA e idempotente por `(fingerprint, ticket)`.

        Garantias:
          * Validacion estricta de tipos y de importes ANTES de mutar nada.
          * El nuevo estado se calcula en locales; la mutacion se confirma solo
            tras validar TODO. Ante cualquier error: sin cambios, evento NO
            marcado como aplicado, y modelo INCOMPLETO (bloqueo posterior).
          * Un deal de OTRA cuenta (huella distinta o ausente) se rechaza.
        """
        if not isinstance(deal, MT5Deal):
            raise MT5AccountingError("apply_deal exige un MT5Deal normalizado")

        try:
            _validate_deal(deal)
        except InvalidDealError as exc:
            self._mark_incomplete(str(exc))
            raise

        fingerprint = deal.account_fingerprint
        if not fingerprint.strip():
            self._mark_incomplete("deal sin huella de cuenta")
            raise DealAccountMismatchError("deal sin huella de cuenta")
        if fingerprint != self._baseline.account_fingerprint:
            self._mark_incomplete("deal de otra cuenta (huella divergente)")
            raise DealAccountMismatchError("deal de otra cuenta")

        key = (self._baseline.account_fingerprint, deal.ticket)
        prior = self._processed.get(key)
        if prior is not None:
            prior_deal, prior_kind = prior
            if prior_deal != deal:
                self._mark_incomplete("conflicto de identidad de deal")
                raise DealIdentityCollisionError(
                    "ticket ya procesado con contenido distinto"
                )
            return DealApplyResult(
                kind=prior_kind, deal_key=key, applied=False,
                balance=self._balance, credit=self._credit,
            )

        if not self._complete:
            raise AccountingIncompleteError(
                f"contabilidad incompleta: {self._incomplete_reason}; aplicacion bloqueada"
            )

        event = classify_deal(deal)
        if event.kind in (DealEventKind.UNSUPPORTED, DealEventKind.INCOMPLETE):
            self._mark_incomplete(event.reason or "deal no soportado")
            exc = (
                UnsupportedDealError
                if event.kind is DealEventKind.UNSUPPORTED
                else AccountingIncompleteError
            )
            raise exc(event.reason or "deal no soportado")
        if event.kind is DealEventKind.NON_COMMERCIAL:
            # Reconocido pero NO aplicable: NO se marca como aplicado.
            self._mark_incomplete(event.reason or "movimiento no comercial sin politica")
            raise AccountingIncompleteError(
                event.reason or "movimiento no comercial sin politica"
            )

        try:
            plan = self._plan(event, deal, key)
        except AccountingIncompleteError as exc:
            self._mark_incomplete(str(exc))
            raise
        except MT5AccountingError as exc:
            self._mark_incomplete(f"deal rechazado en calculo: {type(exc).__name__}")
            raise

        # Commit ATOMICO: solo aqui se muta el estado.
        self._balance = plan.balance
        self._credit = plan.credit
        self._realized_pnl = plan.realized_pnl
        self._positions = plan.positions
        if plan.movement is not None:
            self._movements.append(plan.movement)
        self._processed[key] = (deal, event.kind)
        return DealApplyResult(
            kind=event.kind, deal_key=key, applied=True,
            balance=self._balance, credit=self._credit,
        )

    def _plan(self, event: DealEvent, deal: MT5Deal, key: tuple[str, int]) -> _PlannedState:
        """Calcula el estado siguiente SIN mutar `self`."""
        balance = self._balance
        credit = self._credit
        realized = self._realized_pnl
        positions = dict(self._positions)
        movement: MovementRecord | None = None

        if event.kind is DealEventKind.TRADING_FILL:
            money, profit = self._plan_trading_fill(deal, positions)
            balance += money
            realized += profit
        elif event.kind is DealEventKind.BALANCE_MOVEMENT:
            amount = _require_money(deal.profit, "profit")
            balance += amount
            movement = MovementRecord(key, event.kind, amount, deal.time)
        elif event.kind is DealEventKind.CREDIT_MOVEMENT:
            amount = _require_money(deal.profit, "profit")
            new_credit = credit + amount
            if new_credit < 0.0:
                # Dominio del modelo: el credito no puede quedar negativo.
                raise AccountingIncompleteError(
                    "movimiento de credito deja credito negativo"
                )
            credit = new_credit
            movement = MovementRecord(key, event.kind, amount, deal.time)
        else:  # pragma: no cover - el llamante filtra los demas tipos
            raise AccountingIncompleteError(f"evento no aplicable: {event.kind}")

        return _PlannedState(
            balance=balance, credit=credit, realized_pnl=realized,
            positions=positions, movement=movement,
        )

    def _plan_trading_fill(
        self, deal: MT5Deal, positions: dict[int, MT5Position]
    ) -> tuple[float, float]:
        """Calcula el efecto del fill sobre `positions` (copia) y devuelve
        `(delta_balance, delta_realized)`. No muta `self`."""
        # 1) Importes monetarios estrictos ANTES de tocar la posicion (copia).
        profit = _require_money(deal.profit, "profit")
        commission = _require_money(deal.commission, "commission")
        swap = _require_money(deal.swap, "swap")
        fee = _require_money(deal.fee, "fee")

        pid = deal.position_id
        symbol = deal.symbol
        volume = float(deal.volume)
        price = float(deal.price)
        signed = volume if deal.type == DEAL_TYPE_BUY else -volume

        pos = positions.get(pid)
        if deal.entry == DEAL_ENTRY_IN:
            if pos is None:
                positions[pid] = MT5Position(pid, symbol, signed, price)
            else:
                if pos.symbol != symbol:
                    raise AccountingIncompleteError(
                        f"posicion {pid} con simbolo divergente"
                    )
                same_dir = (pos.quantity > 0 and signed > 0) or (
                    pos.quantity < 0 and signed < 0
                )
                if not same_dir:
                    raise AccountingIncompleteError(
                        f"entrada IN incoherente con el signo de la posicion {pid}"
                    )
                new_qty = pos.quantity + signed
                avg = (pos.quantity * pos.avg_price + signed * price) / new_qty
                positions[pid] = MT5Position(pid, symbol, new_qty, avg)
        else:  # DEAL_ENTRY_OUT
            if pos is None:
                raise AccountingIncompleteError(
                    f"salida OUT sin posicion previa {pid}: secuencia no reconstruible"
                )
            if pos.symbol != symbol:
                raise AccountingIncompleteError(
                    f"posicion {pid} con simbolo divergente"
                )
            same_dir = (pos.quantity > 0 and signed < 0) or (
                pos.quantity < 0 and signed > 0
            )
            if not same_dir:
                raise AccountingIncompleteError(
                    f"salida OUT incoherente con el signo de la posicion {pid}"
                )
            remaining = pos.quantity + signed
            if abs(remaining) <= _VOL_EPS:
                del positions[pid]
            elif (pos.quantity > 0) != (remaining > 0):
                raise AccountingIncompleteError(
                    f"salida OUT excede el volumen de la posicion {pid}"
                )
            else:
                positions[pid] = MT5Position(pid, symbol, remaining, pos.avg_price)

        return (profit + commission + swap + fee), profit

    def _mark_incomplete(self, reason: str) -> None:
        self._complete = False
        self._incomplete_reason = reason

    # --------------------------------------------------- reconciliacion broker

    def _within_tolerance(self, internal: float, observed: float) -> bool:
        scale = max(abs(internal), abs(observed))
        tol = max(self._tol_abs, self._tol_rel * scale)
        return abs(internal - observed) <= tol

    def observe_broker_account(
        self,
        *,
        balance: float,
        equity: float,
        credit: float,
        margin: float,
        margin_free: float,
        currency: str,
    ) -> AccountReconciliationStatus:
        """Registra valores OBSERVADOS del broker y compara balance/credito.

        Tolerancia determinista y configurable: `max(abs, rel*escala)`. NUNCA
        convierte `BALANCE_MATCH_EQUITY_UNVERIFIED` en validacion completa: la
        equity interna no es calculable en esta fase.
        """
        self._observed = BrokerAccountObservation(
            balance=float(balance), equity=float(equity), credit=float(credit),
            margin=float(margin), margin_free=float(margin_free),
            currency=str(currency),
        )
        if not self._within_tolerance(float(balance), self._balance):
            return AccountReconciliationStatus.BALANCE_MISMATCH
        if not self._within_tolerance(float(credit), self._credit):
            return AccountReconciliationStatus.BALANCE_MISMATCH
        if currency and str(currency) != self._baseline.account_currency:
            return AccountReconciliationStatus.BALANCE_MISMATCH
        return AccountReconciliationStatus.BALANCE_MATCH_EQUITY_UNVERIFIED
