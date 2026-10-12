"""LedgerApplier (MT5-DEMO-LIVE-READINESS-03A): fills del broker -> AccountLedger.

Normaliza los fills devueltos por el broker (`BrokerOrder.fills`, con identidad
estable `(client_order_id, fill_sequence)`) y los aplica AL `AccountLedger`
(autoridad contable). NO inventa precio, cantidad ni comision: si un envio
confirmado no trae fills definitivos, el runtime debe obtenerlos por lookup
(`get_order`) antes de contabilizar.

Garantias:
  * Idempotencia por `(client_order_id, fill_sequence)` (contrato LIVE-02/F-06A).
  * Un fill de un lote agregado sin detalle usa `fill_sequence=0` (misma regla
    que el motor paper), por lo que re-entrega del lote es no-op.
  * Comisiones: se aplican EXACTAMENTE UNA VEZ y solo si su moneda es
    representable en la moneda de cuenta (`CommissionPolicy`). Si no puede
    determinarse (moneda `None` o distinta), la comision NO se aplica (se deja
    visible como discrepancia) en vez de inventar FX.
  * CLOSE/REDUCE mantienen la no-inversion porque el lado/cantidad ya fueron
    validados por el coordinador y el adaptador; el ledger netea por signos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from brokers.models import BrokerFill, BrokerOrder
from papertrading.ledger import AccountLedger, LedgerPosition
from reconciliation.models import FillSnapshot


class CommissionCurrencyError(ValueError):
    """La comision no puede representarse en la moneda de cuenta (fail-safe)."""


@dataclass(frozen=True, slots=True)
class CommissionPolicy:
    """Moneda EXPLICITA en la que el broker expresa las comisiones.

    `currency=None` => NO verificada (no se puede asumir USD). El applier
    aplicara la comision solo si `currency == account_currency`; en otro caso
    la deja sin contabilizar y la reporta (`unverified_commission`).
    """

    currency: str | None = None

    def resolvable(self, *, account_currency: str) -> bool:
        return bool(self.currency) and self.currency == account_currency


@dataclass(slots=True)
class ApplyOutcome:
    """Resultado de aplicar los fills de una orden al ledger."""

    applied: tuple[tuple[str, int], ...] = ()
    skipped: tuple[tuple[str, int], ...] = ()
    applied_commission: float = 0.0
    unverified_commission: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.applied)


@dataclass(slots=True)
class LedgerApplier:
    """Aplica fills de broker al ledger con idempotencia durable."""

    ledger: AccountLedger
    account_currency: str
    commission_policy: CommissionPolicy = field(default_factory=CommissionPolicy)
    clock: Callable[[], object] | None = None
    # cid -> {fill_sequence: BrokerFill}. Incluye tambien los fills ya aplicados
    # (p.ej. rehidratados de un snapshot) para poder reconciliar; el dedup por
    # `fill_sequence` impide duplicar en `internal_fills`.
    _fills: dict[str, dict[int, BrokerFill]] = field(default_factory=dict, init=False)

    def _timestamp(self):
        if self.clock is None:
            from datetime import datetime, timezone

            return datetime.now(timezone.utc)
        return self.clock()

    @staticmethod
    def _effective_fills(broker_order: BrokerOrder, timestamp) -> list[BrokerFill]:
        fills = list(getattr(broker_order, "fills", ()) or ())
        if not fills:
            fq = float(getattr(broker_order, "filled_quantity", 0.0) or 0.0)
            ap = getattr(broker_order, "avg_fill_price", None)
            if fq > 0 and ap is not None:
                # Lote agregado sin detalle: identidad estable seq=0.
                fills = [
                    BrokerFill(
                        broker_order_id=str(getattr(broker_order, "broker_order_id", "")),
                        quantity=fq,
                        price=float(ap),
                        timestamp=timestamp,
                        commission=0.0,
                        fill_sequence=0,
                    )
                ]
        return fills

    def apply_broker_order(
        self, client_order_id: str, broker_order: BrokerOrder
    ) -> ApplyOutcome:
        """Aplica los fills de `broker_order` al ledger (idempotente).

        `client_order_id` es la identidad canonica de la operacion (OPEN/CANCEL
        no; CLOSE/REDUCE si). Un fill ya presente por `(cid, seq)` se SALTA.
        """
        cid = str(client_order_id)
        side = broker_order.side.value
        instrument = broker_order.instrument
        timestamp = self._timestamp()
        outcome = ApplyOutcome()
        applied: list[tuple[str, int]] = []
        skipped: list[tuple[str, int]] = []
        for f in self._effective_fills(broker_order, timestamp):
            seq = int(getattr(f, "fill_sequence", 0))
            key = (cid, seq)
            # Registrar SIEMPRE el fill (dedup por seq) para reconciliacion,
            # aunque ya estuviera aplicado (rehidratado de un snapshot).
            self._fills.setdefault(cid, {})[seq] = f
            if self.ledger.is_applied(cid, seq):
                skipped.append(key)
                continue
            commission = float(getattr(f, "commission", 0.0) or 0.0)
            commission_to_apply = 0.0
            if commission != 0.0:
                if self.commission_policy.resolvable(
                    account_currency=self.account_currency
                ):
                    commission_to_apply = commission
                    outcome.applied_commission += commission
                else:
                    # No se inventa FX: se deja sin contabilizar y visible.
                    outcome.unverified_commission += commission
            self.ledger.apply_fill(
                instrument=instrument,
                side=side,
                quantity=float(f.quantity),
                price=float(f.price),
                client_order_id=cid,
                fill_sequence=seq,
                commission=commission_to_apply,
            )
            applied.append(key)
        outcome.applied = tuple(applied)
        outcome.skipped = tuple(skipped)
        return outcome

    # ------------------------------------------------------- reconciliacion

    def internal_fills(self) -> tuple[FillSnapshot, ...]:
        """Vista agregada (por cid) de los fills contabilizados internamente."""
        out: list[FillSnapshot] = []
        for cid in sorted(self._fills):
            bucket = self._fills[cid]
            fills = [bucket[seq] for seq in sorted(bucket)]
            q = sum(float(f.quantity) for f in fills)
            if q <= 0:
                continue
            avg = sum(float(f.quantity) * float(f.price) for f in fills) / q
            out.append(
                FillSnapshot(
                    client_order_id=cid,
                    quantity=q,
                    avg_price=avg,
                    fill_count=len(fills),
                )
            )
        return tuple(out)

    def position_snapshots(self) -> tuple:
        from reconciliation.models import PositionSnapshot

        return tuple(
            PositionSnapshot(
                instrument=p.instrument,
                quantity=p.quantity,
                avg_entry_price=p.avg_entry_price,
            )
            for p in self.ledger.positions()
        )


__all__ = [
    "ApplyOutcome",
    "CommissionCurrencyError",
    "CommissionPolicy",
    "LedgerApplier",
]
