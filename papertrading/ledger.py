"""AccountLedger (F-06A): autoridad interna unica de cash/positions/realized.

Fase 1 (mini-spec F-03/F-06 v3): instrumentos spot/equity, `point_value = 1`,
una sola moneda de cuenta, sin margin/settlement/FX.

Responsabilidades:
  * poseer `cash`, `positions` (quantity dirigida + average entry) y
    `realized_pnl`;
  * aplicar fills de forma ATOMICA e IDEMPOTENTE por identidad estable de fill
    `(client_order_id, fill_sequence)`;
  * NO obtener capital inicial del broker: el bootstrap es explicito
    (`initial_cash`), provisto por configuracion.

NO implementa (corresponde a fases posteriores):
  * equity/unrealized/exposure derivados (F-06B);
  * ACCOUNT reconciliation esperada (F-06C);
  * margin/leverage/correlation/concentration/gates (F-03D/E);
  * comisiones distintas de cero (el modelo spot actual es 0.0, F-05).

El signo de `quantity` determina LONG (+) / SHORT (-). `realized_pnl` es estado
de reporting; NO se suma a equity (equity = cash + Σ quantity*mark, F-06B).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from execution.instrument import (
    FxMissingError,
    InstrumentSpec,
    InstrumentSpecError,
)

_EPS = 1e-9


class LedgerError(ValueError):
    """Fill no aplicable al modelo contable de Fase 1."""


def _valid_mark(price: object) -> bool:
    """Mark valido: numero, finito y > 0 (nunca NaN/Inf/0/negativo)."""
    if not isinstance(price, (int, float)):
        return False
    p = float(price)
    return math.isfinite(p) and p > 0.0


@dataclass(frozen=True, slots=True)
class LedgerPosition:
    """Posicion contable autoritativa (quantity dirigida, avg entry)."""

    instrument: str
    quantity: float
    avg_entry_price: float

    @property
    def is_long(self) -> bool:
        return self.quantity > 0


@dataclass(frozen=True, slots=True)
class ValuationSnapshot:
    """Estado DERIVADO de valoracion (F-06B). No es estado primario.

    `equity`/`unrealized_pnl`/exposure son funciones puras de
    cash+positions+marks; un cambio de mark NO muta cash/positions/realized.
    """

    equity: float
    unrealized_pnl: float
    gross_exposure: float
    net_exposure: float
    exposure_by_instrument: dict[str, float] = field(default_factory=dict)


@dataclass
class AccountLedger:
    """Ledger contable interno (autoridad unica de cash/positions/realized).

    Args:
        initial_cash: capital inicial EXPLICITO (configuracion; nunca del
            broker: eso reintroduciria F-06).
    """

    initial_cash: float
    account_currency: str = "USD"

    def __post_init__(self) -> None:
        if self.initial_cash is None or self.initial_cash < 0:
            raise LedgerError(
                f"initial_cash debe ser >= 0, recibido {self.initial_cash!r}"
            )
        if not self.account_currency or not str(self.account_currency).strip():
            raise LedgerError("account_currency no puede estar vacia")
        self._cash: float = float(self.initial_cash)
        self._realized_pnl: float = 0.0
        self._positions: dict[str, LedgerPosition] = {}
        self._applied_fills: set[tuple[str, int]] = set()
        self._specs: dict[str, InstrumentSpec] = {}

    # ----------------------------------------------------- metadata economica

    def register_instrument(self, spec: InstrumentSpec) -> None:
        """Registra la economia de un instrumento (unica fuente de metadata).

        MULTI-ASSET-03: el ledger NO asume `contract_size=1` silenciosamente
        cuando hay spec registrado. Un instrumento sin spec usa el path LEGACY
        explicito (contract_size=1, quote=account) solo para paper/test.
        """
        existing = self._specs.get(spec.atlas_instrument_id)
        if existing is not None and existing != spec:
            raise InstrumentSpecError(
                f"spec economico ya registrado para "
                f"{spec.atlas_instrument_id!r} y difiere"
            )
        self._specs[spec.atlas_instrument_id] = spec

    def spec(self, instrument: str) -> InstrumentSpec | None:
        return self._specs.get(instrument)

    # ------------------------------------------------------------- consulta

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def position(self, instrument: str) -> LedgerPosition | None:
        return self._positions.get(instrument)

    def positions(self) -> tuple[LedgerPosition, ...]:
        return tuple(self._positions.values())

    def is_applied(self, client_order_id: str, fill_sequence: int) -> bool:
        return (client_order_id, fill_sequence) in self._applied_fills

    # -------------------------------------------------------------- apply

    def apply_fill(
        self,
        *,
        instrument: str,
        side: str,
        quantity: float,
        price: float,
        client_order_id: str,
        fill_sequence: int,
        commission: float = 0.0,
        spec: InstrumentSpec | None = None,
        fx_rate: float | None = None,
    ) -> LedgerPosition | None:
        """Aplica un fill de forma atomica e idempotente.

        Idempotencia: `(client_order_id, fill_sequence)` identifica la
        ejecucion. Reintentar el mismo fill es un no-op (mismo resultado, sin
        mutar cash/posicion/realized de nuevo). Un fill DISTINTO de la misma
        orden (otra `fill_sequence`) SI se aplica.

        MULTI-ASSET-03: la contabilidad usa la economia del instrumento
        (`contract_size` × `FX(quote->account)`). Si `quote_currency !=
        account_currency` y falta `fx_rate` => `LedgerError` (fail-closed); NUNCA
        se asume FX=1. Un instrumento sin spec usa el path LEGACY explicito
        (contract_size=1, quote=account) solo para paper/test.

        Args:
            side: "BUY" o "SELL".
            quantity: magnitud positiva del fill (unidades de ejecucion).
            price: precio de ejecucion (quote currency).
            fill_sequence: identidad monotona del fill dentro de la orden.
            spec: economics del instrumento (si se provee, se registra).
            fx_rate: FX explicita quote->account cuando difieren.

        Raises:
            LedgerError: fill invalido o FX requerida no disponible.
        """
        if side not in ("BUY", "SELL"):
            raise LedgerError(f"side invalido: {side!r}")
        q = float(quantity)
        p = float(price)
        c = float(commission)
        if q != q or p != p or q <= 0 or p <= 0:  # NaN/<=0
            raise LedgerError(
                f"fill invalido: quantity={quantity!r} price={price!r}"
            )
        # LIVE-06: commission = importe monetario (moneda de cuenta), finito y
        # no negativo. Es un COSTE que reduce cash (afecta equity); no altera el
        # avg_entry ni el realized P&L (gross, por precio).
        if not math.isfinite(c) or c < 0:
            raise LedgerError(f"commission invalida: {commission!r}")

        multiplier = self._economic_multiplier(instrument, spec=spec, fx_rate=fx_rate)

        key = (client_order_id, int(fill_sequence))
        if key in self._applied_fills:
            # Idempotente: no-op; devuelve la posicion actual.
            return self._positions.get(instrument)

        # --- mutaciones (atomicas una vez pasadas todas las validaciones) ---
        signed = q if side == "BUY" else -q
        if side == "BUY":
            self._cash -= q * p * multiplier
        else:
            self._cash += q * p * multiplier
        # LIVE-06: la comision se aplica EXACTAMENTE UNA VEZ por fill (dentro del
        # guard de idempotencia). Un coste siempre reduce cash.
        self._cash -= c

        pos = self._positions.get(instrument)
        if pos is None:
            self._positions[instrument] = LedgerPosition(
                instrument=instrument, quantity=signed,
                avg_entry_price=p,
            )
        else:
            new_qty = pos.quantity + signed
            if abs(new_qty) <= _EPS:
                # cierre completo
                self._realize(pos, abs(pos.quantity), p, multiplier)
                del self._positions[instrument]
            elif (pos.quantity > 0 and signed > 0) or (pos.quantity < 0 and signed < 0):
                # MISMO sentido que la posicion: entrada adicional -> avg pond.
                avg = (
                    pos.quantity * pos.avg_entry_price + signed * p
                ) / new_qty
                self._positions[instrument] = LedgerPosition(
                    instrument=instrument, quantity=round(new_qty, 12),
                    avg_entry_price=avg,
                )
            else:
                # sentido contrario: reduccion parcial -> realiza P&L
                self._realize(pos, abs(signed), p, multiplier)
                self._positions[instrument] = LedgerPosition(
                    instrument=instrument, quantity=round(new_qty, 12),
                    avg_entry_price=pos.avg_entry_price,
                )

        self._applied_fills.add(key)
        return self._positions.get(instrument)

    def _economic_multiplier(
        self,
        instrument: str,
        *,
        spec: InstrumentSpec | None,
        fx_rate: float | None,
    ) -> float:
        """Devuelve `contract_size × FX(quote->account)` (fail-closed)."""
        if spec is not None:
            self.register_instrument(spec)
        resolved = spec or self._specs.get(instrument)
        contract_size = 1.0
        quote = self.account_currency
        if resolved is not None:
            contract_size = float(resolved.contract_size)
            quote = str(resolved.quote_currency)
        if quote == self.account_currency:
            return contract_size
        if fx_rate is None:
            raise LedgerError(
                f"FX requerida {quote}->{self.account_currency} para "
                f"{instrument!r}: no disponible (fail-closed)"
            )
        rate = float(fx_rate)
        if not math.isfinite(rate) or rate <= 0:
            raise LedgerError(f"fx_rate invalida: {fx_rate!r}")
        return contract_size * rate

    def _realize(
        self, pos: LedgerPosition, qty: float, price: float, multiplier: float = 1.0
    ) -> float:
        """Realiza P&L (gross) sobre la cantidad reducida/cerrada.

        pnl = sign * (price - avg_entry) * qty * contract_size * FX.
        La comision NO entra aqui (sigue separada, reduce cash).
        """
        if pos.is_long:
            pnl = (price - pos.avg_entry_price) * qty
        else:
            pnl = (pos.avg_entry_price - price) * qty
        pnl *= multiplier
        self._realized_pnl += pnl
        return pnl

    # ------------------------------------------------- derivaciones (F-06B)

    def _open_positions(self) -> tuple[LedgerPosition, ...]:
        return tuple(
            p for p in self._positions.values() if abs(p.quantity) > _EPS
        )

    def valuation(
        self,
        marks: Mapping[str, float],
        fx: Mapping[str, float] | None = None,
    ) -> "ValuationSnapshot | None":
        """Deriva equity/unrealized/exposure de cash+positions+marks.

        Retorna `None` (valoracion INCOMPLETA, fail-closed) si alguna posicion
        abierta carece de mark valido (ausente/NaN/Inf/<=0). Sin posiciones
        abiertas no se requiere mark: `equity = cash`, exposure 0.
        NO muta cash/positions/realized.

        MULTI-ASSET-03: valoracion por instrumento con `contract_size` y FX.
          * quote_currency == account_currency: FX no requerida;
          * distinto: requiere `fx[quote_currency]`; ausente => `None`
            (fail-closed), NUNCA FX=1 por defecto.
        Un instrumento sin spec registrado usa el path LEGACY (contract_size=1,
        quote=account) solo para paper/test.
        """
        open_positions = self._open_positions()
        by_instrument: dict[str, float] = {}
        unrealized = 0.0
        gross = 0.0
        net = 0.0
        for pos in open_positions:
            mark = marks.get(pos.instrument)
            if not _valid_mark(mark):
                return None  # fail-closed: no emitir valoracion completa
            m = float(mark)  # type: ignore[arg-type]
            resolved = self._specs.get(pos.instrument)
            contract_size = 1.0
            quote = self.account_currency
            if resolved is not None:
                contract_size = float(resolved.contract_size)
                quote = str(resolved.quote_currency)
            if quote == self.account_currency:
                rate = 1.0
            else:
                rate_val = None if fx is None else fx.get(quote)
                if rate_val is None:
                    return None  # FX requerida ausente: fail-closed
                rate = float(rate_val)
                if not math.isfinite(rate) or rate <= 0:
                    return None
            multiplier = contract_size * rate
            notional = pos.quantity * m * multiplier
            net += notional
            gross += abs(notional)
            unrealized += pos.quantity * (m - pos.avg_entry_price) * multiplier
            by_instrument[pos.instrument] = notional
        # `equity = cash + Σ quantity*mark*contract_size*FX` (sin abs).
        equity = self._cash + net
        return ValuationSnapshot(
            equity=equity,
            unrealized_pnl=unrealized,
            gross_exposure=gross,
            net_exposure=net,
            exposure_by_instrument=by_instrument,
        )

    def equity(
        self, marks: Mapping[str, float], fx: Mapping[str, float] | None = None
    ) -> float | None:
        val = self.valuation(marks, fx)
        return None if val is None else val.equity

    def unrealized_pnl(
        self, marks: Mapping[str, float], fx: Mapping[str, float] | None = None
    ) -> float | None:
        val = self.valuation(marks, fx)
        return None if val is None else val.unrealized_pnl

    def gross_exposure(
        self, marks: Mapping[str, float], fx: Mapping[str, float] | None = None
    ) -> float | None:
        val = self.valuation(marks, fx)
        return None if val is None else val.gross_exposure

    def net_exposure(
        self, marks: Mapping[str, float], fx: Mapping[str, float] | None = None
    ) -> float | None:
        val = self.valuation(marks, fx)
        return None if val is None else val.net_exposure

    # ------------------------------------------------------- snapshot/restore

    def snapshot(self) -> dict:
        # Orden CANONICO (por instrumento) para reproducibilidad independiente del
        # orden de registro/procesamiento (MULTI-ASSET-06).
        return {
            "initial_cash": self.initial_cash,
            "account_currency": self.account_currency,
            "cash": self._cash,
            "realized_pnl": self._realized_pnl,
            "positions": [
                {
                    "instrument": p.instrument,
                    "quantity": p.quantity,
                    "avg_entry_price": p.avg_entry_price,
                }
                for _, p in sorted(self._positions.items())
            ],
            "specs": [self._specs[k].to_dict() for k in sorted(self._specs)],
            "applied_fills": sorted(self._applied_fills),
        }

    def probe_snapshot(self, snap: dict) -> None:
        """Prevalida un snapshot de ledger SIN mutar el estado.

        Parsea la misma estructura que `load_snapshot` en locales; si el bloque
        es invalido lanza `LedgerError` sin efectos. Permite al llamante
        (p. ej. `PaperTradingEngine.load_snapshot`) garantizar atomicidad:
        prevalidar aqui y aplicar despues `load_snapshot` (no-fallable para el
        mismo input ya validado).
        """
        if not isinstance(snap, dict):
            raise LedgerError(f"ledger snapshot corrupto: {snap!r}")
        try:
            float(snap.get("cash", self.initial_cash))
            float(snap.get("realized_pnl", 0.0))
            for p in snap.get("positions", []):
                LedgerPosition(
                    instrument=p["instrument"],
                    quantity=p["quantity"],
                    avg_entry_price=p["avg_entry_price"],
                )
            for cid, seq in snap.get("applied_fills", []):
                (str(cid), int(seq))
            for spec in snap.get("specs", []):
                InstrumentSpec.from_dict(spec)
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"ledger snapshot corrupto: {exc}") from exc

    def load_snapshot(self, snap: dict) -> None:
        self._cash = snap.get("cash", self.initial_cash)
        self._realized_pnl = snap.get("realized_pnl", 0.0)
        if snap.get("account_currency"):
            self.account_currency = str(snap["account_currency"])
        self._positions = {
            p["instrument"]: LedgerPosition(
                instrument=p["instrument"],
                quantity=p["quantity"],
                avg_entry_price=p["avg_entry_price"],
            )
            for p in snap.get("positions", [])
        }
        self._specs = {
            s.atlas_instrument_id: s
            for s in (InstrumentSpec.from_dict(d) for d in snap.get("specs", []))
        }
        self._applied_fills = {
            (cid, int(seq)) for cid, seq in snap.get("applied_fills", [])
        }


__all__ = [
    "AccountLedger",
    "LedgerError",
    "LedgerPosition",
    "ValuationSnapshot",
]
