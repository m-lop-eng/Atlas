"""Motor de reconciliación puro (B4).

Compara `InternalSnapshot` (lo que Atlas cree) contra `BrokerSnapshot` (lo
que el broker declara) y produce un `ReconciliationReport` clasificando CADA
entidad. Es un motor de OBSERVACIÓN: no cancela, no cierra, no reenvía, no
modifica el broker (06 §59, §67, §68).

Flujo que impone: OBSERVAR → COMPARAR → CLASIFICAR → BLOQUEAR SI ES
NECESARIO. La resolución es SIEMPRE explícita y posterior (orquestador).

Regla crítica: cualquier discrepancia material (MISMATCH / MISSING_* /
UNKNOWN) => `trading_allowed = False`; las posiciones existentes NO se tocan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from reconciliation.models import (
    AccountSnapshot,
    BrokerSnapshot,
    DiffItem,
    DiscrepancyClass,
    Domain,
    InternalSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    ReconciliationReport,
    ReconciliationStatus,
)


def _approx(a: float | None, b: float | None, tol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


@dataclass(slots=True)
class ReconciliationEngine:
    """Compara estado interno vs broker de forma determinista.

    Attributes:
        tolerance: Tolerancia relativa/absoluta para comparar precios,
            cantidades y cash/equity.
    """

    tolerance: float = 1e-9

    # ------------------------------------------------------------- registers

    def reconcile(
        self, internal: InternalSnapshot, broker: BrokerSnapshot
    ) -> ReconciliationReport:
        if not broker.available:
            return ReconciliationReport(
                status=ReconciliationStatus.RECONCILIATION_UNKNOWN,
                diffs=(
                    DiffItem(
                        domain=Domain.SYSTEM,
                        key="broker",
                        classification=DiscrepancyClass.UNKNOWN,
                        detail="broker no disponible: estado incierto",
                    ),
                ),
                broker_available=False,
            )

        diffs = [
            *self._reconcile_orders(internal, broker),
            *self._reconcile_fills(internal, broker),
            *self._reconcile_positions(internal, broker),
            *self._reconcile_account(internal, broker),
        ]
        status = self._classify(diffs)
        return ReconciliationReport(
            status=status,
            diffs=tuple(diffs),
            broker_available=True,
        )

    def _classify(self, diffs: list[DiffItem]) -> ReconciliationStatus:
        if not diffs:
            return ReconciliationStatus.RECONCILIATION_OK
        if any(
            d.classification == DiscrepancyClass.UNKNOWN for d in diffs
        ):
            return ReconciliationStatus.RECONCILIATION_UNKNOWN
        if any(d.classification.blocks for d in diffs):
            return ReconciliationStatus.RECONCILIATION_BLOCKED
        return ReconciliationStatus.RECONCILIATION_OK

    # ---------------------------------------------------------------- orders

    def _reconcile_orders(
        self, internal: InternalSnapshot, broker: BrokerSnapshot
    ) -> list[DiffItem]:
        diffs: list[DiffItem] = []
        internal_keys = {o.client_order_id for o in internal.orders}
        broker_keys = {o.client_order_id for o in broker.orders}

        for key in sorted(broker_keys - internal_keys):
            diffs.append(
                DiffItem(
                    domain=Domain.ORDER,
                    key=key,
                    classification=DiscrepancyClass.MISSING_INTERNAL,
                    detail="orden en broker desconocida para Atlas",
                    broker_value=self._broker_order(broker, key),
                )
            )

        for order in sorted(internal.orders, key=lambda o: o.client_order_id):
            key = order.client_order_id
            broker_order = self._broker_order(broker, key)
            if broker_order is None:
                if order.state == "UNKNOWN":
                    # La orden interna es UNKNOWN: no podemos afirmar que el
                    # broker carece de ella (puede existir vía get_order).
                    # Clasificar UNKNOWN y resolver de forma explícita.
                    diffs.append(
                        DiffItem(
                            domain=Domain.ORDER,
                            key=key,
                            classification=DiscrepancyClass.UNKNOWN,
                            detail="orden interna UNKNOWN: resolver con "
                                   "get_order antes de asumir que el broker "
                                   "no la tiene",
                            internal_value=order.state,
                        )
                    )
                elif _order_terminal(order.state):
                    # Terminal y ausente en broker es lo esperado (fill ya
                    # contabilizada); pendiente y ausente es anomalía.
                    diffs.append(
                        DiffItem(
                            domain=Domain.ORDER,
                            key=key,
                            classification=DiscrepancyClass.MATCH,
                            detail="orden terminal correctamente ausente "
                                   "del broker",
                        )
                    )
                else:
                    diffs.append(
                        DiffItem(
                            domain=Domain.ORDER,
                            key=key,
                            classification=DiscrepancyClass.MISSING_BROKER,
                            detail="orden interna pendiente que el broker "
                                   "no encuentra",
                            internal_value=order.state,
                            broker_value=None,
                        )
                    )
                continue

            if order.state == "UNKNOWN" or broker_order.state == "UNKNOWN":
                diffs.append(
                    DiffItem(
                        domain=Domain.ORDER,
                        key=key,
                        classification=DiscrepancyClass.UNKNOWN,
                        detail="estado UNKNOWN: resolver antes de asumir "
                               "cualquier cosa",
                        internal_value=order.state,
                        broker_value=broker_order.state,
                    )
                )
                continue

            if order.state != broker_order.state:
                diffs.append(
                    DiffItem(
                        domain=Domain.ORDER,
                        key=key,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="estado divergente",
                        internal_value=order.state,
                        broker_value=broker_order.state,
                    )
                )
                continue

            if order.state in ("SUBMITTED", "PARTIALLY_FILLED") and not (
                _approx(
                    order.quantity, broker_order.quantity, self.tolerance
                )
            ):
                diffs.append(
                    DiffItem(
                        domain=Domain.ORDER,
                        key=key,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="cantidad solicitada divergente",
                        internal_value=order.quantity,
                        broker_value=broker_order.quantity,
                    )
                )
                continue

            diffs.append(
                DiffItem(
                    domain=Domain.ORDER,
                    key=key,
                    classification=DiscrepancyClass.MATCH,
                    detail="orden alineada",
                    internal_value=order.state,
                    broker_value=broker_order.state,
                )
            )
        return diffs

    # ----------------------------------------------------------------- fills

    def _reconcile_fills(
        self, internal: InternalSnapshot, broker: BrokerSnapshot
    ) -> list[DiffItem]:
        diffs: list[DiffItem] = []
        internal_fills = {f.client_order_id: f for f in internal.fills}
        broker_fills = {f.client_order_id: f for f in broker.fills}

        for key in sorted(set(internal_fills) | set(broker_fills)):
            int_fill = internal_fills.get(key)
            brk_fill = broker_fills.get(key)

            if int_fill is None:
                diffs.append(
                    DiffItem(
                        domain=Domain.FILL,
                        key=key,
                        classification=DiscrepancyClass.MISSING_INTERNAL,
                        detail="fills en broker que Atlas no tiene "
                               "(posibles fills duplicados/inesperados)",
                        broker_value=brk_fill.quantity,
                    )
                )
                continue

            if brk_fill is None:
                diffs.append(
                    DiffItem(
                        domain=Domain.FILL,
                        key=key,
                        classification=DiscrepancyClass.MISSING_BROKER,
                        detail="fills internos que el broker no reporta "
                               "(posibles fills faltantes)",
                        internal_value=int_fill.quantity,
                    )
                )
                continue

            if not _approx(
                int_fill.quantity, brk_fill.quantity, self.tolerance
            ):
                diffs.append(
                    DiffItem(
                        domain=Domain.FILL,
                        key=key,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="cantidad acumulada divergente",
                        internal_value=int_fill.quantity,
                        broker_value=brk_fill.quantity,
                    )
                )
                continue

            if not _approx(
                int_fill.avg_price, brk_fill.avg_price, self.tolerance
            ):
                diffs.append(
                    DiffItem(
                        domain=Domain.FILL,
                        key=key,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="precio medio divergente",
                        internal_value=int_fill.avg_price,
                        broker_value=brk_fill.avg_price,
                    )
                )
                continue

            if int_fill.fill_count != brk_fill.fill_count:
                diffs.append(
                    DiffItem(
                        domain=Domain.FILL,
                        key=key,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="número de fills divergente "
                               "(fills duplicados o faltantes)",
                        internal_value=int_fill.fill_count,
                        broker_value=brk_fill.fill_count,
                    )
                )
                continue

            diffs.append(
                DiffItem(
                    domain=Domain.FILL,
                    key=key,
                    classification=DiscrepancyClass.MATCH,
                    detail="fills alineados",
                    internal_value=int_fill.quantity,
                    broker_value=brk_fill.quantity,
                )
            )
        return diffs

    # -------------------------------------------------------------- positions

    def _reconcile_positions(
        self, internal: InternalSnapshot, broker: BrokerSnapshot
    ) -> list[DiffItem]:
        diffs: list[DiffItem] = []
        internal_pos = {p.instrument: p for p in internal.positions}
        broker_pos = {p.instrument: p for p in broker.positions}

        for instrument in sorted(
            set(internal_pos) | set(broker_pos),
            key=lambda k: (k or ""),
        ):
            int_pos = internal_pos.get(instrument)
            brk_pos = broker_pos.get(instrument)

            if int_pos is None:
                if brk_pos is not None and abs(brk_pos.quantity) > 0:
                    diffs.append(
                        DiffItem(
                            domain=Domain.POSITION,
                            key=instrument,
                            classification=(
                                DiscrepancyClass.MISSING_INTERNAL
                            ),
                            detail="posición inesperada en broker",
                            broker_value=brk_pos.quantity,
                        )
                    )
                continue

            if brk_pos is None:
                if abs(int_pos.quantity) > 0:
                    diffs.append(
                        DiffItem(
                            domain=Domain.POSITION,
                            key=instrument,
                            classification=(DiscrepancyClass.MISSING_BROKER),
                            detail="posición interna que el broker no "
                                   "reporta",
                            internal_value=int_pos.quantity,
                        )
                    )
                continue

            if _sign(int_pos.quantity) != _sign(brk_pos.quantity):
                diffs.append(
                    DiffItem(
                        domain=Domain.POSITION,
                        key=instrument,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="dirección divergente",
                        internal_value=int_pos.quantity,
                        broker_value=brk_pos.quantity,
                    )
                )
                continue

            if not _approx(
                int_pos.quantity, brk_pos.quantity, self.tolerance
            ):
                diffs.append(
                    DiffItem(
                        domain=Domain.POSITION,
                        key=instrument,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="cantidad divergente",
                        internal_value=int_pos.quantity,
                        broker_value=brk_pos.quantity,
                    )
                )
                continue

            if not _approx(
                int_pos.avg_entry_price,
                brk_pos.avg_entry_price,
                self.tolerance,
            ):
                diffs.append(
                    DiffItem(
                        domain=Domain.POSITION,
                        key=instrument,
                        classification=DiscrepancyClass.MISMATCH,
                        detail="precio medio de entrada divergente",
                        internal_value=int_pos.avg_entry_price,
                        broker_value=brk_pos.avg_entry_price,
                    )
                )
                continue

            diffs.append(
                DiffItem(
                    domain=Domain.POSITION,
                    key=instrument,
                    classification=DiscrepancyClass.MATCH,
                    detail="posición alineada",
                    internal_value=int_pos.quantity,
                    broker_value=brk_pos.quantity,
                )
            )
        return diffs

    # ---------------------------------------------------------------- account

    def _reconcile_account(
        self, internal: InternalSnapshot, broker: BrokerSnapshot
    ) -> list[DiffItem]:
        if internal.account is None and broker.account is None:
            # Sin cuenta referenciada en ningún lado: no hay nada que
            # verificar (por ejemplo, reconciliación de solo órdenes).
            return []

        if broker.account is None:
            return [
                DiffItem(
                    domain=Domain.ACCOUNT,
                    key="account",
                    classification=DiscrepancyClass.UNKNOWN,
                    detail="estado de cuenta no disponible",
                )
            ]
        if internal.account is None:
            return [
                DiffItem(
                    domain=Domain.ACCOUNT,
                    key="account",
                    classification=DiscrepancyClass.UNKNOWN,
                    detail="cuenta interna no disponible para comparar",
                )
            ]

        diffs: list[DiffItem] = []
        checks = (
            ("cash", internal.account.cash, broker.account.cash),
            ("equity", internal.account.equity, broker.account.equity),
        )
        aligned = True
        for field, int_val, brk_val in checks:
            if not _approx(int_val, brk_val, self.tolerance):
                aligned = False
                diffs.append(
                    DiffItem(
                        domain=Domain.ACCOUNT,
                        key=f"account/{field}",
                        classification=DiscrepancyClass.MISMATCH,
                        detail="valor de cuenta divergente",
                        internal_value=int_val,
                        broker_value=brk_val,
                    )
                )
        if aligned:
            diffs.append(
                DiffItem(
                    domain=Domain.ACCOUNT,
                    key="account",
                    classification=DiscrepancyClass.MATCH,
                    detail="cuenta alineada",
                    internal_value=internal.account.cash,
                    broker_value=broker.account.cash,
                )
            )
        return diffs

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _broker_order(
        broker: BrokerSnapshot, client_order_id: str
    ) -> OrderSnapshot | None:
        for o in broker.orders:
            if o.client_order_id == client_order_id:
                return o
        return None


def _order_terminal(state: str) -> bool:
    return state in (
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "FAILED",
    )


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0