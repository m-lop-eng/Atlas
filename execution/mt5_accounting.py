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
from datetime import datetime, timedelta
from enum import Enum

MT5_ACCOUNTING_SCHEMA_VERSION = 1

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


class MT5AccountLedger:
    """Ledger contable MT5 apalancado (Forex/CFD), separado del ledger spot."""

    def __init__(
        self,
        baseline: MT5AccountBaseline,
        *,
        reconciliation_tolerance_abs: float = _DEFAULT_MONEY_EPS,
        reconciliation_tolerance_rel: float = _DEFAULT_MONEY_EPS,
    ) -> None:
        if not isinstance(baseline, MT5AccountBaseline):
            raise BaselineError("MT5AccountLedger exige una MT5AccountBaseline explicita")
        for name, value in (
            ("reconciliation_tolerance_abs", reconciliation_tolerance_abs),
            ("reconciliation_tolerance_rel", reconciliation_tolerance_rel),
        ):
            if not _finite(value) or float(value) < 0.0:
                raise MT5AccountingError(f"{name} debe ser finito y >= 0")
        self._baseline = baseline
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
            credit += amount
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
