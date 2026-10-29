"""ATLAS-MT5-HEDGING-01/02: HEDGING restringido con FakeMT5.

Politica: <= 1 posicion propiedad de Atlas por simbolo, sin LONG+SHORT
simultaneos, sin adopcion de posiciones externas; fail-closed ante ambiguedad.

Exclusivamente FakeMT5: sin terminal real, sin credenciales, sin ordenes reales.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_mt5_execution_foundation import (
    CLOSE_TIME,
    INSTRUMENT,
    NOW,
    OPEN_TIME,
    ORDER_STATE_FILLED,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    FakeKillSwitch,
    FakeMT5,
    FakeSessions,
    _spec,
    passing_admission,
)

from execution.intent_journal import (
    IntentJournal,
    IntentJournalCorruptionError,
    IntentJournalDuplicateError,
    IntentJournalSchemaError,
    OrderIntent,
)
from execution.live_execution import (
    ExecutionBlockedError,
    LiveExecutionCoordinator,
    MarketDataReference,
    UnresolvedIntentsError,
    UnsupportedAccountModeError,
)
from execution.order import Order, OrderSide, OrderType
from execution.order_manager import OrderManager
from brokers.models import (
    BrokerConnectionError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderState,
    BrokerPermissionError,
    SubmitStatus,
)

from execution.live_runtime import LiveExecutionRuntime
from execution.ledger_store import LedgerStore
from papertrading.ledger import AccountLedger

from killswitches.coordinator import KillSwitchCoordinator
from risk.types import AccountRiskState

from brokers.mt5 import (
    HISTORY_COMPLETE,
    HISTORY_INCOMPLETE,
    MT5BrokerAdapter,
    MT5Config,
    MT5Credentials,
    MT5Environment,
    ORDER_STATE_PARTIAL,
    OrderHistoryLookup,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_REMOVE,
)

UTC_NOW = NOW
PASSWORD_CANARY = "not-a-real-password"


# --------------------------------------------------------------------------
# FakeMT5 HEDGING: posiciones con identidad (identifier/ticket)
# --------------------------------------------------------------------------


class HedgingFakeMT5(FakeMT5):
    """FakeMT5 con margin_mode=HEDGING y posiciones identificables.

    `positions_get` expone POSITION_TICKET (`ticket`) y POSITION_IDENTIFIER
    (`identifier`) por separado. `order_send` abre posiciones (identifier =
    ticket de la orden de apertura) o reduce/cierra la posicion indicada en
    `request["position"]`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.account.margin_mode = 2  # HEDGING
        self._hedges: list[dict] = []
        self._order_seq = 3000
        # Controles realistas para la confirmacion posterior al envio.
        self.partial_fraction = 1.0      # fraccion del volumen solicitado ejecutada
        self.leave_position_on_fill = False  # orden FILLED pero posicion sin tocar
        self.fill_without_deal = False   # posicion reducida pero sin deal (INCOMPLETE)
        self.fail_positions_on_send = False  # la consulta de posiciones falla tras enviar
        self.positions_failed = False

    def add_hedge_position(self, *, identifier, ticket, symbol=INSTRUMENT,
                           type=ORDER_TYPE_BUY, volume=0.5, price=1.1,
                           magic=0, comment="") -> None:
        self._hedges.append({
            "ticket": int(ticket), "identifier": int(identifier), "symbol": symbol,
            "type": int(type), "volume": float(volume), "price_open": float(price),
            "magic": magic, "comment": comment, "time": 1_700_000_000,
        })

    def positions_get(self, symbol=None, ticket=None):
        if self.positions_failed:
            return None  # fallo de consulta => el adapter falla cerrado
        rows = [
            SimpleNamespace(
                ticket=p["ticket"], identifier=p["identifier"], symbol=p["symbol"],
                type=p["type"], volume=p["volume"], price_open=p["price_open"],
                magic=p["magic"], comment=p["comment"], time=p["time"],
            )
            for p in self._hedges
        ]
        if symbol is not None:
            rows = [r for r in rows if r.symbol == symbol]
        if ticket is not None:
            rows = [r for r in rows if r.ticket == ticket]
        return tuple(rows)

    def order_send(self, request):
        self.last_request = dict(request)
        if request.get("action") == TRADE_ACTION_REMOVE:
            return self._send_remove(request)
        self.send_count += 1
        if self.send_mode == "none":
            return None
        if self.send_mode == "raise":
            raise RuntimeError("transporte")
        if self.send_mode == "reject":
            return SimpleNamespace(retcode=10006, order=0, deal=0, volume=0.0, price=0.0)
        if self.fail_positions_on_send:
            self.positions_failed = True
        self._order_seq += 1
        order_ticket = self._order_seq
        deal_ticket = self._order_seq + 1
        self._order_seq += 1
        volume = float(request["volume"])
        price = float(request["price"])
        otype = int(request["type"])
        pos_ticket = request.get("position")
        filled = volume * self.partial_fraction
        if pos_ticket is not None:
            pos = next((p for p in self._hedges if p["ticket"] == int(pos_ticket)), None)
            if pos is None:
                return SimpleNamespace(retcode=10013, order=0, deal=0, volume=0.0, price=0.0)
            filled = min(filled, pos["volume"])
            if not self.leave_position_on_fill:
                new_vol = pos["volume"] - filled
                if new_vol <= 1e-9:
                    self._hedges.remove(pos)
                else:
                    pos["volume"] = new_vol
            identifier = pos["identifier"] if pos in self._hedges else pos_ticket
            identifier = int(identifier)
        else:
            identifier = order_ticket  # POSITION_IDENTIFIER == ticket de apertura
            self._hedges.append({
                "ticket": order_ticket, "identifier": order_ticket, "symbol": request["symbol"],
                "type": otype, "volume": filled, "price_open": price,
                "magic": request.get("magic", 0), "comment": request.get("comment", ""),
                "time": 1_700_000_000,
            })
        state = ORDER_STATE_FILLED if filled + 1e-9 >= volume else ORDER_STATE_PARTIAL
        self._orders[order_ticket] = self._mk_order(
            order_ticket, request, state, volume, volume - filled
        )
        if not self.fill_without_deal:
            self._deals[deal_ticket] = SimpleNamespace(
                ticket=deal_ticket, order=order_ticket, volume=filled, price=price,
                commission=0.0, time=1_700_000_000,
                position_id=identifier,
            )
        return SimpleNamespace(
            retcode=10009, order=order_ticket, deal=deal_ticket,
            volume=filled, price=price, position=identifier,
        )


class HedgingAccountingFakeMT5(HedgingFakeMT5):
    """HedgingFakeMT5 que ADEMAS refleja fills en balance/equity (reconciliable).

    Necesario para ejercitar la secuencia de arranque COMPLETA del runtime
    (reconciliacion incluida) tras un reinicio con posicion viva.
    """

    def __init__(self) -> None:
        super().__init__()
        self._net: dict[str, list] = {}
        self._mark = 1.1001
        self._next_pos = 7000

    def _cs(self, symbol: str) -> float:
        return float(getattr(self.symbols[symbol], "trade_contract_size", 1.0))

    def order_send(self, request):
        result = super().order_send(request)
        if request.get("action") == TRADE_ACTION_REMOVE:
            return result
        if self.send_mode != "ok" or result is None:
            return result
        if int(getattr(result, "retcode", -1)) != 10009:
            return result
        symbol = request["symbol"]
        volume = float(getattr(result, "volume", request["volume"]))
        price = float(request["price"])
        signed = volume if int(request["type"]) == ORDER_TYPE_BUY else -volume
        cs = self._cs(symbol)
        qty, avg = self._net.get(symbol, [0.0, price])
        new = qty + signed
        self.account.balance -= signed * price * cs
        if abs(new) <= 1e-12:
            self._net.pop(symbol, None)
        elif qty == 0.0:
            self._net[symbol] = [new, price]
        elif (qty > 0) == (signed > 0):
            self._net[symbol] = [new, (qty * avg + signed * price) / new]
        else:
            self._net[symbol] = [new, avg]
        self._mark = price
        self.account.equity = self.account.balance + sum(
            q * self._mark * self._cs(s) for s, (q, _) in self._net.items()
        )
        return result


def _fake() -> HedgingFakeMT5:
    fake = HedgingFakeMT5()
    fake.add_symbol(INSTRUMENT)
    fake.add_tick(INSTRUMENT, bid=1.0999, ask=1.1001)
    return fake


def _adapter(fake, *, allow=True) -> MT5BrokerAdapter:
    cfg = MT5Config(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={INSTRUMENT: INSTRUMENT}, allow_restricted_hedging=allow,
    )
    creds = MT5Credentials(login=42, password=PASSWORD_CANARY, server="demo")
    adapter = MT5BrokerAdapter(cfg, creds, mt5=fake, clock=lambda: NOW)
    adapter.connect()
    return adapter


def _coord(journal, adapter, *, allow=True, account_mode=2, kill_switch=None):
    return LiveExecutionCoordinator(
        broker=adapter, order_manager=OrderManager(), journal=journal,
        sessions=FakeSessions(),
        kill_switch=kill_switch or KillSwitchCoordinator(),
        instrument=INSTRUMENT, instrument_spec=_spec(), account_id="DEMO-1",
        account_currency="USD", max_open_positions=3,
        risk_state_provider=lambda: AccountRiskState(equity=10_000.0, open_positions=0),
        market_data_provider=lambda: MarketDataReference(
            INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME
        ),
        data_max_age_s=120.0, account_mode_provider=lambda: account_mode,
        clock=lambda: NOW, run_id="HEDGE-T", allow_restricted_hedging=allow,
    )


def _seed_owned(journal, adapter, cid, *, identifier, ticket, side=OrderSide.BUY,
                volume=0.5, price=1.1):
    journal.begin(OrderIntent(
        client_order_id=cid, order_id=f"ORD-{cid}", instrument=INSTRUMENT,
        side=side.value, order_type="MARKET", quantity=volume,
    ))
    journal.mark_state(cid, "FILLED")
    journal.link_position(cid, str(identifier))


def _hedge_runtime(journal, adapter, ledger_path, marks):
    """Runtime HEDGING restringido con ledger/store propios (reconciliable)."""
    return LiveExecutionRuntime(
        broker=adapter, order_manager=OrderManager(), journal=journal,
        ledger=AccountLedger(initial_cash=10_000.0, account_currency="USD"),
        ledger_store=LedgerStore(ledger_path), sessions=FakeSessions(),
        kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
        instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
        max_open_positions=3,
        risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
        market_data_provider=lambda: MarketDataReference(
            INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
        data_max_age_s=120.0, account_mode_provider=lambda: 2,
        marks_provider=marks, allow_restricted_hedging=True,
        clock=lambda: NOW, run_id="RT-HEDGE", mt5_accounting=passing_admission(),
    )


# --------------------------------------------------------------------------
# 2/3. Gate de modo de cuenta (NETTING vs HEDGING opt-in)
# --------------------------------------------------------------------------


class TestAccountModeGate:
    def test_netting_unchanged(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        fake.account.margin_mode = 0  # NETTING
        adapter = _adapter(fake)
        coord = _coord(journal, adapter, allow=False, account_mode=0)
        out = coord.submit(
            side=OrderSide.BUY, quantity=0.1, strategy_id="s",
            strategy_version="1.0.0", client_order_id="N-1",
        )
        assert out.allowed is True and fake.send_count == 1

    def test_hedging_without_optin_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=False)
        coord = _coord(journal, adapter, allow=False, account_mode=2)
        with pytest.raises(UnsupportedAccountModeError):
            coord.submit(
                side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                strategy_version="1.0.0", client_order_id="H-1",
            )
        assert fake.send_count == 0

    def test_hedging_with_optin_open_and_linkage(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        out = coord.submit(
            side=OrderSide.BUY, quantity=0.1, strategy_id="s",
            strategy_version="1.0.0", client_order_id="H-OPEN",
        )
        assert out.allowed is True and fake.send_count == 1
        # La posicion se creo con identidad; la vinculacion durable cid->id es
        # responsabilidad del runtime (`_link_open_position`); aqui probamos el
        # primitivo durable del journal.
        positions = adapter.get_positions()
        assert len(positions) == 1
        pid = positions[0].position_id
        assert pid is not None and positions[0].ticket is not None
        journal.link_position("H-OPEN", pid)
        assert journal.find_by_position_id(pid)[0].client_order_id == "H-OPEN"


# --------------------------------------------------------------------------
# 7/8. Cierre/reduccion DIRIGIDOS por POSITION_IDENTIFIER
# --------------------------------------------------------------------------


class TestDirectedCloseReduce:
    def test_close_uses_current_ticket_for_identifier(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        # Posicion con identifier 1000 pero ticket ACTUAL 2000 (p.ej. re-apertura
        # por servicio: el identifier NO cambia, el ticket SI).
        fake.add_hedge_position(identifier=1000, ticket=2000, type=ORDER_TYPE_BUY,
                                volume=0.5, price=1.1)
        _seed_owned(journal, adapter, "O-1", identifier=1000, ticket=2000)
        out = coord.close_position(target_position_id="1000")
        assert out.allowed is True
        req = fake.last_request
        assert req["action"] == TRADE_ACTION_DEAL
        assert req["position"] == 2000  # TICKET ACTUAL, no el identifier
        assert req["type"] == ORDER_TYPE_SELL
        assert req["volume"] == pytest.approx(0.5)
        assert adapter.get_positions() == []

    def test_partial_reduce_residual(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=1001, ticket=1001, type=ORDER_TYPE_BUY,
                                volume=0.5, price=1.1)
        _seed_owned(journal, adapter, "O-2", identifier=1001, ticket=1001)
        out = coord.reduce_position(quantity=0.2, target_position_id="1001")
        assert out.allowed is True
        assert fake.last_request["position"] == 1001
        pos = adapter.get_positions()
        assert len(pos) == 1 and pos[0].quantity == pytest.approx(0.3)

    def test_reduce_exceeding_volume_rejected(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=1002, ticket=1002, type=ORDER_TYPE_BUY,
                                volume=0.1, price=1.1)
        _seed_owned(journal, adapter, "O-3", identifier=1002, ticket=1002)
        with pytest.raises(ExecutionBlockedError):
            coord.reduce_position(quantity=0.5, target_position_id="1002")
        assert fake.send_count == 0
        assert adapter.get_positions()[0].quantity == pytest.approx(0.1)

    def test_close_requires_target_id(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=1003, ticket=1003, type=ORDER_TYPE_BUY,
                                volume=0.5, price=1.1)
        _seed_owned(journal, adapter, "O-4", identifier=1003, ticket=1003)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position()  # sin target_position_id
        assert ei.value.stage == "position_target"
        assert fake.send_count == 0

    def test_unknown_target_id_blocked(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=1004, ticket=1004, type=ORDER_TYPE_BUY,
                                volume=0.5, price=1.1)
        # sin vinculo en journal -> propiedad desconocida
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position(target_position_id="1004")
        assert ei.value.stage == "position_ownership"
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# 11/12/13. Ambiguedad, posiciones externas, confusion de tickets
# --------------------------------------------------------------------------


class TestAmbiguityExternal:
    def test_two_positions_same_symbol_block(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=2001, ticket=2001, type=ORDER_TYPE_BUY, volume=0.2)
        fake.add_hedge_position(identifier=2002, ticket=2002, type=ORDER_TYPE_SELL, volume=0.2)
        _seed_owned(journal, adapter, "O-A", identifier=2001, ticket=2001)
        with pytest.raises(ExecutionBlockedError):
            coord.close_position(target_position_id="2001")

    def test_external_position_ownership_unknown(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=2003, ticket=2003, type=ORDER_TYPE_BUY, volume=0.2)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position(target_position_id="2003")
        assert ei.value.stage == "position_ownership"

    def test_order_ticket_confused_with_position_ticket(self, tmp_path):
        # El identifier estable NO es el ticket de orden/deal: un target que solo
        # coincide con un ticket de orden no debe resolver la posicion.
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        adapter.submit_order  # referencia (no se envia)
        fake.add_hedge_position(identifier=5000, ticket=6000, type=ORDER_TYPE_BUY, volume=0.3)
        _seed_owned(journal, adapter, "O-5", identifier=5000, ticket=6000)
        # target = ticket de orden 9999 (inexistente como identifier) -> block
        with pytest.raises(ExecutionBlockedError):
            coord.close_position(target_position_id="9999")
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# Journal v4: vinculacion durable / migracion / conflicto
# --------------------------------------------------------------------------


class TestJournalPositionLinkage:
    def test_link_position_idempotent_and_conflict(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent(
            client_order_id="C-1", order_id="ORD-C-1", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        journal.link_position("C-1", "777")
        journal.link_position("C-1", "777")  # idempotente
        assert journal.get("C-1").position_id == "777"
        with pytest.raises(IntentJournalDuplicateError):
            journal.link_position("C-1", "888")
        reloaded = IntentJournal(journal.path)
        assert reloaded.get("C-1").position_id == "777"
        assert reloaded.find_by_position_id("777")[0].client_order_id == "C-1"

    def test_runtime_open_links_position_id(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        rt = LiveExecutionRuntime(
            broker=adapter, order_manager=OrderManager(), journal=journal,
            ledger=AccountLedger(initial_cash=10_000.0, account_currency="USD"),
            ledger_store=LedgerStore(tmp_path / "ledger.json"), sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
            instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
            data_max_age_s=120.0, account_mode_provider=lambda: 2,
            marks_provider=lambda: {INSTRUMENT: 1.1},
            allow_restricted_hedging=True, clock=lambda: NOW, run_id="RT-LINK",
            mt5_accounting=passing_admission(),
        )
        assert rt.initialize().ready is True
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="H-LINK")
        intent = journal.get("H-LINK")
        assert intent.position_id is not None
        assert journal.find_by_position_id(intent.position_id)[0].client_order_id == "H-LINK"

    def test_runtime_ownership_blocks_external(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)  # fresh adapter (gate single-assign)
        fake.add_hedge_position(identifier=9001, ticket=9001, type=ORDER_TYPE_BUY,
                                volume=0.2, price=1.1)
        rt = LiveExecutionRuntime(
            broker=adapter, order_manager=OrderManager(), journal=journal,
            ledger=AccountLedger(initial_cash=10_000.0, account_currency="USD"),
            ledger_store=LedgerStore(tmp_path / "ledger.json"), sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
            instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
            data_max_age_s=120.0, account_mode_provider=lambda: 2,
            marks_provider=lambda: {INSTRUMENT: 1.1},
            allow_restricted_hedging=True, clock=lambda: NOW, run_id="RT-H",
            mt5_accounting=passing_admission(),
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "ownership"
        assert fake.send_count == 0

    def test_runtime_hedging_without_optin_not_ready(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=False)
        rt = LiveExecutionRuntime(
            broker=adapter, order_manager=OrderManager(), journal=journal,
            ledger=AccountLedger(initial_cash=10_000.0, account_currency="USD"),
            ledger_store=LedgerStore(tmp_path / "ledger.json"), sessions=FakeSessions(),
            kill_switch=KillSwitchCoordinator(), instrument=INSTRUMENT,
            instrument_spec=_spec(), account_id="DEMO-1", account_currency="USD",
            max_open_positions=3,
            risk_state_provider=lambda: AccountRiskState(equity=10_000.0),
            market_data_provider=lambda: MarketDataReference(
                INSTRUMENT, 3600, OPEN_TIME, CLOSE_TIME),
            data_max_age_s=120.0, account_mode_provider=lambda: 2,
            marks_provider=lambda: {INSTRUMENT: 1.1},
            allow_restricted_hedging=False, clock=lambda: NOW, run_id="RT-H0",
        )
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "account_mode"

    def test_position_id_forbidden_in_older_schema(self, tmp_path):
        # Un doc v3 que declara position_id es inconsistente (fail-closed).
        import json as _json

        path = tmp_path / "j.json"
        journal = IntentJournal.initialize(path)
        journal.begin(OrderIntent(
            client_order_id="C-1", order_id="ORD-C-1", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        journal.link_position("C-1", "777")
        raw = _json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = 3  # declara v3 pero contiene position_id (v4)
        path.write_text(_json.dumps(raw), encoding="utf-8")
        with pytest.raises(IntentJournalSchemaError):
            IntentJournal(path)


# --------------------------------------------------------------------------
# 5/6. Reinicio: vinculacion durable vs. fail-closed; OPEN desconocida
# --------------------------------------------------------------------------


class TestOwnershipReconstruction:
    def test_restart_after_linked_open_is_ready(self, tmp_path):
        # Escenario 5 (positivo): la vinculacion cid->POSITION_IDENTIFIER se
        # persistio ANTES del reinicio => el runtime la RECONSTRUYE del journal
        # durable, supera la verificacion de propiedad y queda preparado, sin
        # reenviar la apertura.
        path = tmp_path / "j.json"
        ledger_path = tmp_path / "ledger.json"
        fake = HedgingAccountingFakeMT5()
        fake.add_symbol(INSTRUMENT)
        fake.add_tick(INSTRUMENT, bid=1.0999, ask=1.1001)
        journal = IntentJournal.initialize(path)
        adapter = _adapter(fake, allow=True)
        rt = _hedge_runtime(journal, adapter, ledger_path,
                            lambda: {INSTRUMENT: fake._mark})
        assert rt.initialize().ready is True
        rt.submit(side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                  strategy_version="1.0.0", client_order_id="H-R")
        linked = journal.get("H-R").position_id
        assert linked is not None
        assert fake.send_count == 1

        # "Reinicio": nuevo adapter (mismo terminal) + journal y ledger recargados.
        adapter2 = _adapter(fake, allow=True)
        journal2 = IntentJournal(path)
        assert journal2.get("H-R").position_id == linked  # vinculacion durable
        rt2 = _hedge_runtime(journal2, adapter2, ledger_path,
                             lambda: {INSTRUMENT: fake._mark})
        rep = rt2.initialize()
        assert rep.ready is True and rep.stage == "ready"
        assert fake.send_count == 1  # NUNCA se reenvia la apertura

    def test_restart_before_link_failclosed_no_adoption(self, tmp_path):
        # Escenario 5 (negativo): OPEN llenado en el broker pero la vinculacion
        # NO llego a persistirse (caida en la frontera). El runtime NO adopta la
        # posicion (magic/comment NO son prueba de propiedad) => block fail-closed.
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent(
            client_order_id="H-CRASH", order_id="ORD-H-CRASH",
            instrument=INSTRUMENT, side="BUY", order_type="MARKET", quantity=0.5,
        ))
        journal.mark_state("H-CRASH", "FILLED")
        # Posicion viva con magic/comment "de Atlas" pero SIN vinculo durable.
        fake.add_hedge_position(
            identifier=4001, ticket=4001, type=ORDER_TYPE_BUY, volume=0.5,
            price=1.1, magic=adapter._magic_for("H-CRASH"),
            comment=adapter._comment_for("H-CRASH"),
        )
        rt = _hedge_runtime(journal, adapter, tmp_path / "ledger.json",
                            lambda: {INSTRUMENT: 1.1})
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "ownership"
        assert fake.send_count == 0
        assert journal.get("H-CRASH").position_id is None  # sin adopcion

    def test_restart_link_to_absent_position_failclosed(self, tmp_path):
        # Divergencia journal/broker: la vinculacion existe (posicion_id) y el
        # intent NO es terminal, pero la posicion no esta viva en el broker =>
        # block fail-closed en el gate de PROPIEDAD.
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        journal = IntentJournal.initialize(tmp_path / "j.json")
        journal.begin(OrderIntent(
            client_order_id="H-GONE", order_id="ORD-H-GONE",
            instrument=INSTRUMENT, side="BUY", order_type="MARKET", quantity=0.5,
        ))
        # El broker SI conoce la orden (activa, no llenada) => recovery resuelve.
        fake.add_open_order(
            5001, symbol=INSTRUMENT, magic=adapter._magic_for("H-GONE"),
            comment=adapter._comment_for("H-GONE"), volume=0.5, price=1.1,
        )
        journal.mark_state("H-GONE", "SUBMITTED")
        journal.link_position("H-GONE", "9999")  # vinculo a posicion inexistente
        rt = _hedge_runtime(journal, adapter, tmp_path / "ledger.json",
                            lambda: {INSTRUMENT: 1.1})
        rep = rt.initialize()
        assert rep.ready is False and rep.stage == "ownership"
        assert fake.send_count == 0


class TestHedgingUnknownNoResubmit:
    def test_hedging_unknown_open_blocks_resubmit(self, tmp_path):
        # Escenario 6: OPEN con resultado UNKNOWN no se reenvía; bloquea la
        # siguiente apertura y el lookup autoritativo no repite la orden.
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.send_mode = "none"  # respuesta perdida => UNKNOWN
        out = coord.submit(
            side=OrderSide.BUY, quantity=0.1, strategy_id="s",
            strategy_version="1.0.0", client_order_id="H-UNK",
        )
        assert out.allowed is True
        assert out.submission.status is SubmitStatus.UNKNOWN
        assert journal.get("H-UNK").state == "UNKNOWN"
        assert fake.send_count == 1

        with pytest.raises(UnresolvedIntentsError):
            coord.submit(
                side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                strategy_version="1.0.0", client_order_id="H-NEXT",
            )
        assert fake.send_count == 1

        coord.resolve_pending()  # lookup-only: NUNCA reenvia
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# H-1: resultado de CLOSE/REDUCE por evidencia AUTORITATIVA
# --------------------------------------------------------------------------


def _lookup(cid, *, status, state=None, filled=0.0, volume=0.0, side=OrderSide.SELL):
    if status != HISTORY_COMPLETE:
        return OrderHistoryLookup(cid, status)
    bro = BrokerOrder(
        client_order_id=cid, broker_order_id="900", instrument=INSTRUMENT,
        side=side, order_type=OrderType.MARKET, quantity=volume, state=state,
        filled_quantity=filled, avg_fill_price=1.1,
        fills=(BrokerFill("900", filled, 1.1, NOW, 0.0, 1),) if filled > 0 else (),
    )
    return OrderHistoryLookup(cid, status, order=bro)


class TestAuthoritativeCloseReduce:
    # (1) Cierre completo: orden ejecutada + sin deal perdido + posicion ausente.
    def test_close_full_confirmed_when_evidence_coherent(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3006, ticket=3006, volume=0.5)
        _seed_owned(journal, adapter, "O-F1", identifier=3006, ticket=3006)
        out = coord.close_position(target_position_id="3006")
        assert out.allowed is True and out.stage == "closed"
        assert journal.get("CLOSE:EURUSD").state == "FILLED"
        assert journal.get("CLOSE:EURUSD").terminal is True
        assert adapter.get_positions() == []
        assert fake.send_count == 1

    # (2) Orden completa pero posicion AUN existente: NO cierre confirmado.
    def test_close_order_filled_but_position_present_not_confirmed(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3012, ticket=3012, volume=0.5)
        _seed_owned(journal, adapter, "O-X2", identifier=3012, ticket=3012)
        fake.leave_position_on_fill = True  # datos de posicion contradictorios
        out = coord.close_position(target_position_id="3012")
        assert out.stage == "close_unknown" and out.stage != "closed"
        assert journal.get("CLOSE:EURUSD").state == "UNKNOWN"
        assert len(adapter.get_positions()) == 1  # la posicion sigue viva
        assert fake.send_count == 1

    # (3a) CLOSE UNKNOWN: no se declara cierre.
    def test_close_unknown_not_reported_closed(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3001, ticket=3001, volume=0.5)
        _seed_owned(journal, adapter, "O-U1", identifier=3001, ticket=3001)
        fake.send_mode = "none"  # respuesta perdida => UNKNOWN
        out = coord.close_position(target_position_id="3001")
        assert out.allowed is True
        assert out.submission.status is SubmitStatus.UNKNOWN
        assert out.stage == "close_unknown" and out.stage != "closed"
        assert journal.get("CLOSE:EURUSD").state == "UNKNOWN"
        assert fake.send_count == 1

    # (3b) Posicion ausente pero SIN fills atribuibles: no se infiere exito.
    def test_close_position_gone_without_attributable_fills_not_success(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3010, ticket=3010, volume=0.5)
        _seed_owned(journal, adapter, "O-X3", identifier=3010, ticket=3010)
        # La posicion desaparece, pero la orden no tiene deals: historial
        # incompleto => la ausencia NO se interpreta como exito.
        fake.fill_without_deal = True
        out = coord.close_position(target_position_id="3010")
        assert out.stage == "close_unknown" and out.stage != "closed"
        assert journal.get("CLOSE:EURUSD").state == "UNKNOWN"
        assert journal.get("CLOSE:EURUSD").fills_applied == ()
        assert fake.send_count == 1

    # (4) REDUCE confirmada con residual esperado.
    def test_reduce_confirmed_with_expected_residual(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3016, ticket=3016, volume=0.5)
        _seed_owned(journal, adapter, "O-F4", identifier=3016, ticket=3016)
        out = coord.reduce_position(quantity=0.2, target_position_id="3016")
        assert out.allowed is True and out.stage == "reduced"
        assert journal.get("REDUCE:EURUSD:0.2").state == "FILLED"
        pos = adapter.get_positions()
        assert len(pos) == 1 and pos[0].quantity == pytest.approx(0.3)
        assert fake.send_count == 1

    # (5) REDUCE parcial con residual coherente con lo ejecutado.
    def test_reduce_partial_coherent_residual(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3004, ticket=3004, volume=0.5)
        _seed_owned(journal, adapter, "O-P2", identifier=3004, ticket=3004)
        fake.partial_fraction = 0.5  # 0.2 solicitado => 0.1 ejecutado
        out = coord.reduce_position(quantity=0.2, target_position_id="3004")
        assert out.stage == "reduce_partial" and out.stage != "reduced"
        assert journal.get("REDUCE:EURUSD:0.2").state == "PARTIALLY_FILLED"
        pos = adapter.get_positions()
        assert len(pos) == 1 and pos[0].quantity == pytest.approx(0.4)
        assert fake.send_count == 1

    # (2b/5b) CLOSE parcial: no se declara cierre total.
    def test_close_partial_not_full(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3003, ticket=3003, volume=0.5)
        _seed_owned(journal, adapter, "O-P1", identifier=3003, ticket=3003)
        fake.partial_fraction = 0.4  # 0.5 solicitado => 0.2 ejecutado
        out = coord.close_position(target_position_id="3003")
        assert out.allowed is True
        assert out.stage == "close_partial" and out.stage != "closed"
        assert journal.get("CLOSE:EURUSD").state == "PARTIALLY_FILLED"
        assert journal.get("CLOSE:EURUSD").terminal is False
        assert adapter.get_positions()[0].quantity == pytest.approx(0.3)

    # (6) REDUCE con residual inesperado (discrepancia): no resuelto y bloquea.
    def test_reduce_unexpected_residual_unresolved_and_blocks(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3013, ticket=3013, volume=0.5)
        _seed_owned(journal, adapter, "O-X6", identifier=3013, ticket=3013)
        # La orden declara 0.1 ejecutado, pero la posicion se redujo 0.2:
        # residual observado (0.3) != esperado (0.4) => discrepancia.
        monkeypatch.setattr(
            adapter, "lookup_order_history",
            lambda cid: _lookup(cid, status=HISTORY_COMPLETE,
                                state=BrokerOrderState.PARTIALLY_FILLED,
                                filled=0.1, volume=0.2),
        )
        out = coord.reduce_position(quantity=0.2, target_position_id="3013")
        assert out.stage == "reduce_unknown" and out.stage != "reduced"
        assert journal.get("REDUCE:EURUSD:0.2").state == "UNKNOWN"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.reduce_position(quantity=0.1, target_position_id="3013")
        assert ei.value.stage == "reduce_pending"
        assert fake.send_count == 1

    # (7) Posicion desaparecida + historial incompleto: sin reenvio.
    def test_position_vanished_history_incomplete_no_resend(self, tmp_path, monkeypatch):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3014, ticket=3014, volume=0.5)
        _seed_owned(journal, adapter, "O-X7", identifier=3014, ticket=3014)
        monkeypatch.setattr(
            adapter, "lookup_order_history",
            lambda cid: OrderHistoryLookup(cid, HISTORY_INCOMPLETE),
        )
        out = coord.close_position(target_position_id="3014")
        assert out.stage == "close_unknown"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position(target_position_id="3014")
        assert ei.value.stage == "reduce_pending"
        assert fake.send_count == 1

    # (8) Error/None en la consulta de posiciones posterior: no exito.
    def test_position_query_failure_not_success(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3011, ticket=3011, volume=0.5)
        _seed_owned(journal, adapter, "O-X8", identifier=3011, ticket=3011)
        fake.fail_positions_on_send = True  # la consulta falla tras el envio
        out = coord.close_position(target_position_id="3011")
        assert out.stage == "close_unknown" and out.stage != "closed"
        assert journal.get("CLOSE:EURUSD").state == "UNKNOWN"
        assert fake.send_count == 1

    # (9a) Recuperacion misma sesion sin duplicar.
    def test_unknown_then_resolve_no_duplicate(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3007, ticket=3007, volume=0.5)
        _seed_owned(journal, adapter, "O-R1", identifier=3007, ticket=3007)
        fake.send_mode = "none"
        out = coord.reduce_position(
            quantity=0.1, operation_id="R-DUP", target_position_id="3007"
        )
        assert out.submission.status is SubmitStatus.UNKNOWN
        send_after = fake.send_count
        fake.send_mode = "ok"
        ticket = 950
        fake._orders[ticket] = fake._mk_order(
            ticket,
            {"symbol": INSTRUMENT, "magic": adapter._magic_for("R-DUP"),
             "comment": adapter._comment_for("R-DUP"), "type": ORDER_TYPE_SELL,
             "volume": 0.1, "price": 1.1},
            ORDER_STATE_FILLED, 0.1, 0.0,
        )
        fake.add_deal(9501, ticket, volume=0.1, price=1.1)
        adapter._remember("R-DUP", ticket)
        coord.resolve_pending()
        assert journal.get("R-DUP").state == "FILLED"
        assert journal.get("R-DUP").fills_applied == (9501,)
        assert fake.send_count == send_after  # NUNCA se reenvia

    # (9b) Recuperacion tras REINICIO sin duplicar.
    def test_restart_pending_resolves_no_duplicate(self, tmp_path):
        path = tmp_path / "j.json"
        journal = IntentJournal.initialize(path)
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=3008, ticket=3008, volume=0.5)
        _seed_owned(journal, adapter, "O-RS", identifier=3008, ticket=3008)
        fake.send_mode = "none"
        out = coord.reduce_position(
            quantity=0.1, operation_id="R-RS", target_position_id="3008"
        )
        assert out.submission.status is SubmitStatus.UNKNOWN
        send_after = fake.send_count
        # "Reinicio": journal y coordinador nuevos.
        journal2 = IntentJournal(path)
        adapter2 = _adapter(fake, allow=True)
        coord2 = _coord(journal2, adapter2, allow=True, account_mode=2)
        assert journal2.get("R-RS").state == "UNKNOWN"
        fake.send_mode = "ok"
        ticket = 960
        fake._orders[ticket] = fake._mk_order(
            ticket,
            {"symbol": INSTRUMENT, "magic": adapter2._magic_for("R-RS"),
             "comment": adapter2._comment_for("R-RS"), "type": ORDER_TYPE_SELL,
             "volume": 0.1, "price": 1.1},
            ORDER_STATE_FILLED, 0.1, 0.0,
        )
        fake.add_deal(9601, ticket, volume=0.1, price=1.1)
        adapter2._remember("R-RS", ticket)
        coord2.resolve_pending()
        assert journal2.get("R-RS").state == "FILLED"
        assert fake.send_count == send_after

    # (12) Kill switch activo: cero envios.
    def test_close_kill_switch_halt_zero_sends(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(
            journal, adapter, allow=True, account_mode=2,
            kill_switch=FakeKillSwitch(can_trade=False, state="HALT"),
        )
        fake.add_hedge_position(identifier=3015, ticket=3015, volume=0.5)
        _seed_owned(journal, adapter, "O-X12", identifier=3015, ticket=3015)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position(target_position_id="3015")
        assert ei.value.stage == "kill_switch"
        assert fake.send_count == 0


# --------------------------------------------------------------------------
# H-2: identidad de operacion CLOSE/REDUCE sin colisiones
# --------------------------------------------------------------------------


class TestOperationIdentity:
    def test_close_reopen_close_new_identity(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=11, ticket=11, volume=0.5)
        _seed_owned(journal, adapter, "O-A", identifier=11, ticket=11)
        assert coord.close_position(target_position_id="11").stage == "closed"
        assert adapter.get_positions() == []
        # Nueva posicion en el MISMO simbolo.
        fake.add_hedge_position(identifier=22, ticket=22, volume=0.5)
        _seed_owned(journal, adapter, "O-BB", identifier=22, ticket=22)
        out = coord.close_position(target_position_id="22")
        assert out.stage == "closed" and out.stage != "reduce_idempotent"
        assert adapter.get_positions() == []
        assert fake.send_count == 2
        assert journal.get("CLOSE:EURUSD#2") is not None

    def test_two_reduces_same_quantity_distinct(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=33, ticket=33, volume=0.5)
        _seed_owned(journal, adapter, "O-R", identifier=33, ticket=33)
        assert coord.reduce_position(
            quantity=0.2, target_position_id="33"
        ).stage == "reduced"
        assert adapter.get_positions()[0].quantity == pytest.approx(0.3)
        assert coord.reduce_position(
            quantity=0.2, target_position_id="33"
        ).stage == "reduced"
        assert adapter.get_positions()[0].quantity == pytest.approx(0.1)
        assert fake.send_count == 2
        assert journal.get("REDUCE:EURUSD:0.2#2") is not None

    def test_retry_same_operation_id_idempotent(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=44, ticket=44, volume=0.5)
        _seed_owned(journal, adapter, "O-C", identifier=44, ticket=44)
        assert coord.close_position(
            operation_id="X-1", target_position_id="44"
        ).stage == "closed"
        out = coord.close_position(operation_id="X-1", target_position_id="44")
        assert out.stage == "reduce_idempotent"
        assert fake.send_count == 1

    def test_unresolved_blocks_new_close_other_id(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=55, ticket=55, volume=0.5)
        _seed_owned(journal, adapter, "O-K", identifier=55, ticket=55)
        fake.send_mode = "none"
        assert coord.close_position(
            operation_id="U-1", target_position_id="55"
        ).stage == "close_unknown"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.close_position(target_position_id="55")  # otro id
        assert ei.value.stage == "reduce_pending"
        assert fake.send_count == 1

    def test_restart_keeps_pending_and_blocks(self, tmp_path):
        path = tmp_path / "j.json"
        journal = IntentJournal.initialize(path)
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=66, ticket=66, volume=0.5)
        _seed_owned(journal, adapter, "O-PP", identifier=66, ticket=66)
        fake.send_mode = "none"
        assert coord.close_position(
            operation_id="P-1", target_position_id="66"
        ).stage == "close_unknown"
        # "Reinicio": journal recargado + nuevo coordinador.
        journal2 = IntentJournal(path)
        adapter2 = _adapter(fake, allow=True)
        coord2 = _coord(journal2, adapter2, allow=True, account_mode=2)
        assert journal2.get("P-1").state == "UNKNOWN"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord2.close_position(target_position_id="66")
        assert ei.value.stage == "reduce_pending"
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# H-3: la ruta DIRECTA del adaptador no elude la politica HEDGING
# --------------------------------------------------------------------------


def _direct_order(cid, *, side=OrderSide.BUY, quantity=0.1):
    return Order(
        strategy_id="s", strategy_version="1.0.0", account_id="A",
        instrument=INSTRUMENT, side=side, quantity=quantity,
        order_type=OrderType.MARKET, client_order_id=cid,
    )


class TestAdapterDirectHedgingGate:
    def test_direct_hedging_open_without_optin_rejected(self):
        fake = _fake()
        adapter = _adapter(fake, allow=False)
        assert adapter._execution_gate is None  # sin coordinador
        with pytest.raises(BrokerPermissionError):
            adapter.submit_order(_direct_order("D-H1"))
        assert fake.send_count == 0

    def test_direct_hedging_open_with_optin_allowed(self):
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        out = adapter.submit_order(_direct_order("D-H2"))
        assert out.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1

    def test_direct_netting_open_no_regression(self):
        fake = _fake()
        fake.account.margin_mode = 0  # NETTING
        adapter = _adapter(fake, allow=False)
        out = adapter.submit_order(_direct_order("D-N1"))
        assert out.status is SubmitStatus.CONFIRMED
        assert fake.send_count == 1


# --------------------------------------------------------------------------
# H-3 bis (02E): modo de cuenta no verificable => fail-closed en la ruta directa
# --------------------------------------------------------------------------


class TestAdapterAccountModeFailClosed:
    def _prepared(self):
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        return fake, adapter

    def _assert_rejected_no_send(self, adapter, fake, exc):
        with pytest.raises(exc):
            adapter.submit_order(_direct_order("AM-1"))
        assert fake.check_count == 0   # order_check NO invocado
        assert fake.send_count == 0    # order_send NO invocado
        assert fake.last_request is None
        assert "AM-1" not in adapter._cid_to_ticket  # sin identidad registrada

    def test_account_info_none_rejected(self):
        fake, adapter = self._prepared()
        fake.account_info = lambda: None
        self._assert_rejected_no_send(adapter, fake, BrokerConnectionError)

    def test_account_info_raises_rejected(self):
        fake, adapter = self._prepared()

        def boom():
            raise RuntimeError("boom")

        fake.account_info = boom
        self._assert_rejected_no_send(adapter, fake, BrokerConnectionError)

    def test_account_missing_margin_mode_rejected(self):
        fake, adapter = self._prepared()
        fake.account = SimpleNamespace(
            login=42, balance=1.0, equity=1.0, currency="USD",
            trade_allowed=True, trade_expert=True,
        )  # sin margin_mode
        self._assert_rejected_no_send(adapter, fake, BrokerPermissionError)

    def test_account_unknown_margin_mode_rejected(self):
        fake, adapter = self._prepared()
        fake.account.margin_mode = 7  # valor no reconocido
        self._assert_rejected_no_send(adapter, fake, BrokerPermissionError)

    def test_account_non_int_margin_mode_rejected(self):
        fake, adapter = self._prepared()
        fake.account.margin_mode = "NETTING"  # tipo invalido
        self._assert_rejected_no_send(adapter, fake, BrokerPermissionError)


# --------------------------------------------------------------------------
# H-5: validacion de position_id al cargar el journal
# --------------------------------------------------------------------------


class TestJournalPositionIdValidation:
    def _write(self, tmp_path, value):
        import json as _json

        path = tmp_path / "j.json"
        journal = IntentJournal.initialize(path)
        journal.begin(OrderIntent(
            client_order_id="C-1", order_id="ORD-C-1", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        raw = _json.loads(path.read_text(encoding="utf-8"))
        raw["intents"]["C-1"]["position_id"] = value
        path.write_text(_json.dumps(raw), encoding="utf-8")
        return path

    def test_none_is_valid(self, tmp_path):
        path = self._write(tmp_path, None)
        assert IntentJournal(path).get("C-1").position_id is None

    def test_string_roundtrip(self, tmp_path):
        path = self._write(tmp_path, "777")
        assert IntentJournal(path).get("C-1").position_id == "777"

    @pytest.mark.parametrize("bad", [123, 1.5, True, ["7"], {"a": 1}, "", "   "])
    def test_invalid_values_rejected(self, tmp_path, bad):
        path = self._write(tmp_path, bad)
        with pytest.raises(IntentJournalCorruptionError):
            IntentJournal(path)


# --------------------------------------------------------------------------
# Seccion 6: control de aperturas duplicadas y CANCEL bajo HEDGING
# --------------------------------------------------------------------------


class TestHedgingEntryAndCancel:
    def test_entry_blocked_when_position_exists(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        fake.add_hedge_position(identifier=71, ticket=71, volume=0.5)
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.submit(
                side=OrderSide.BUY, quantity=0.1, strategy_id="s",
                strategy_version="1.0.0", client_order_id="H-ENTRY",
            )
        assert ei.value.stage == "position_evidence"
        assert fake.send_count == 0

    def test_cancel_preserves_position_identity(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        # Posicion propiedad de Atlas (no debe alterarse por una cancelacion).
        fake.add_hedge_position(identifier=77, ticket=77, volume=0.5)
        _seed_owned(journal, adapter, "O-POS", identifier=77, ticket=77)
        # Orden de apertura PENDIENTE a cancelar.
        journal.begin(OrderIntent(
            client_order_id="O-CN", order_id="ORD-O-CN", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        fake.add_open_order(
            6000, symbol=INSTRUMENT, magic=adapter._magic_for("O-CN"),
            comment=adapter._comment_for("O-CN"), volume=0.1, price=1.1,
        )
        adapter._remember("O-CN", 6000)
        out = coord.cancel_pending(client_order_id="O-CN")
        assert out.allowed is True and out.stage == "cancel_confirmed"
        positions = adapter.get_positions()
        assert len(positions) == 1 and positions[0].position_id == "77"
        assert journal.get("O-POS").position_id == "77"  # identidad intacta
        assert journal.get("O-CN").state == "CANCELLED"

    def test_cancel_blocked_when_broker_unhealthy(self, tmp_path):
        journal = IntentJournal.initialize(tmp_path / "j.json")
        fake = _fake()
        adapter = _adapter(fake, allow=True)
        coord = _coord(journal, adapter, allow=True, account_mode=2)
        journal.begin(OrderIntent(
            client_order_id="O-CU", order_id="ORD-O-CU", instrument=INSTRUMENT,
            side="BUY", order_type="MARKET", quantity=0.1,
        ))
        fake.add_open_order(
            6001, symbol=INSTRUMENT, magic=adapter._magic_for("O-CU"),
            comment=adapter._comment_for("O-CU"), volume=0.1, price=1.1,
        )
        adapter._remember("O-CU", 6001)
        adapter._state = "DISCONNECTED"
        with pytest.raises(ExecutionBlockedError) as ei:
            coord.cancel_pending(client_order_id="O-CU")
        assert ei.value.stage == "broker_health"
        assert fake.cancel_count == 0
