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

    def __post_init__(self) -> None:
        if self.initial_cash is None or self.initial_cash < 0:
            raise LedgerError(
                f"initial_cash debe ser >= 0, recibido {self.initial_cash!r}"
            )
        self._cash: float = float(self.initial_cash)
        self._realized_pnl: float = 0.0
        self._positions: dict[str, LedgerPosition] = {}
        self._applied_fills: set[tuple[str, int]] = set()

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
    ) -> LedgerPosition | None:
        """Aplica un fill de forma atomica e idempotente.

        Idempotencia: `(client_order_id, fill_sequence)` identifica la
        ejecucion. Reintentar el mismo fill es un no-op (mismo resultado, sin
        mutar cash/posicion/realized de nuevo). Un fill DISTINTO de la misma
        orden (otra `fill_sequence`) SI se aplica.

        Args:
            side: "BUY" o "SELL".
            quantity: magnitud positiva del fill.
            price: precio de ejecucion.
            fill_sequence: indice monotono del fill dentro de la orden (origen
                de ejecucion). Identidad estable; NO generado al releer.

        Raises:
            LedgerError: fill invalido (cantidad/precio no finito o <= 0,
                side desconocido) o fill no aplicable al modelo.
        """
        if side not in ("BUY", "SELL"):
            raise LedgerError(f"side invalido: {side!r}")
        q = float(quantity)
        p = float(price)
        if q != q or p != p or q <= 0 or p <= 0:  # NaN/<=0
            raise LedgerError(
                f"fill invalido: quantity={quantity!r} price={price!r}"
            )

        key = (client_order_id, int(fill_sequence))
        if key in self._applied_fills:
            # Idempotente: no-op; devuelve la posicion actual.
            return self._positions.get(instrument)

        # --- mutaciones (atomicas una vez pasadas todas las validaciones) ---
        signed = q if side == "BUY" else -q
        if side == "BUY":
            self._cash -= q * p
        else:
            self._cash += q * p

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
                self._realize(pos, abs(pos.quantity), p)
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
                self._realize(pos, abs(signed), p)
                self._positions[instrument] = LedgerPosition(
                    instrument=instrument, quantity=round(new_qty, 12),
                    avg_entry_price=pos.avg_entry_price,
                )

        self._applied_fills.add(key)
        return self._positions.get(instrument)

    def _realize(self, pos: LedgerPosition, qty: float, price: float) -> float:
        """Realiza P&L sobre la cantidad reducida/cerrada (point_value=1)."""
        if pos.is_long:
            pnl = (price - pos.avg_entry_price) * qty
        else:
            pnl = (pos.avg_entry_price - price) * qty
        self._realized_pnl += pnl
        return pnl

    # ------------------------------------------------- derivaciones (F-06B)

    def _open_positions(self) -> tuple[LedgerPosition, ...]:
        return tuple(
            p for p in self._positions.values() if abs(p.quantity) > _EPS
        )

    def valuation(self, marks: Mapping[str, float]) -> "ValuationSnapshot | None":
        """Deriva equity/unrealized/exposure de cash+positions+marks.

        Retorna `None` (valoracion INCOMPLETA, fail-closed) si alguna posicion
        abierta carece de mark valido (ausente/NaN/Inf/<=0). Sin posiciones
        abiertas no se requiere mark: `equity = cash`, exposure 0.
        NO muta cash/positions/realized.

        Fase 1: spot/equity, `point_value = 1`, moneda unica USD.
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
            notional = pos.quantity * m
            net += notional
            gross += abs(notional)
            unrealized += pos.quantity * (m - pos.avg_entry_price)
            by_instrument[pos.instrument] = notional
        # `equity = cash + Σ quantity*mark` (sin abs; conserva F-05).
        equity = self._cash + net
        return ValuationSnapshot(
            equity=equity,
            unrealized_pnl=unrealized,
            gross_exposure=gross,
            net_exposure=net,
            exposure_by_instrument=by_instrument,
        )

    def equity(self, marks: Mapping[str, float]) -> float | None:
        val = self.valuation(marks)
        return None if val is None else val.equity

    def unrealized_pnl(self, marks: Mapping[str, float]) -> float | None:
        val = self.valuation(marks)
        return None if val is None else val.unrealized_pnl

    def gross_exposure(self, marks: Mapping[str, float]) -> float | None:
        val = self.valuation(marks)
        return None if val is None else val.gross_exposure

    def net_exposure(self, marks: Mapping[str, float]) -> float | None:
        val = self.valuation(marks)
        return None if val is None else val.net_exposure

    # ------------------------------------------------------- snapshot/restore

    def snapshot(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "cash": self._cash,
            "realized_pnl": self._realized_pnl,
            "positions": [
                {
                    "instrument": p.instrument,
                    "quantity": p.quantity,
                    "avg_entry_price": p.avg_entry_price,
                }
                for p in self._positions.values()
            ],
            "applied_fills": sorted(self._applied_fills),
        }

    def load_snapshot(self, snap: dict) -> None:
        self._cash = snap.get("cash", self.initial_cash)
        self._realized_pnl = snap.get("realized_pnl", 0.0)
        self._positions = {
            p["instrument"]: LedgerPosition(
                instrument=p["instrument"],
                quantity=p["quantity"],
                avg_entry_price=p["avg_entry_price"],
            )
            for p in snap.get("positions", [])
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
