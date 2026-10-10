"""Tests MT5-DEMO-READONLY-01: probe de conexion MT5 DEMO de solo lectura.

Usa un FakeMT5 que FALLA si se invoca cualquier metodo de trading. Nunca se
importa ni inicializa una sesion real de MetaTrader5.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from execution.instrument import InstrumentSpec
from brokers.mt5 import MT5Config, MT5ConfigError, MT5Credentials, MT5Environment
from brokers.mt5_readonly import (
    ACCOUNT_TRADE_MODE_DEMO,
    MT5ReadOnlyProbe,
    ReadOnlyProbeError,
    run_readonly_probe,
)
from brokers.models import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerPermissionError,
)

PASSWORD_CANARY = "not-a-real-password"


class ReadOnlyFakeMT5:
    """Fake minimo de SOLO LECTURA; los metodos de trading fallan si se llaman."""

    def __init__(self) -> None:
        self.initialize_called = False
        self.initialize_ok = True
        self.initialize_kwargs = None
        self.shutdown_called = 0
        self.last_error_value = (0, "ok")
        self.terminal = SimpleNamespace(
            connected=True, company="DemoBroker", name="Tester", trade_allowed=True
        )
        self.account = SimpleNamespace(
            login=123456, server="Demo-Server", currency="USD",
            trade_mode=ACCOUNT_TRADE_MODE_DEMO, trade_allowed=True,
            trade_expert=True, balance=10_000.0, equity=10_000.0,
            margin_free=9_000.0, leverage=100,
        )
        self.symbols: dict = {}
        self.trading_calls: list[str] = []
        self.symbol_select_calls: list[tuple] = []

    # --- lectura
    def initialize(self, **kwargs):
        self.initialize_called = True
        self.initialize_kwargs = kwargs
        return self.initialize_ok

    def shutdown(self):
        self.shutdown_called += 1
        return True

    def last_error(self):
        return self.last_error_value

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable):
        self.symbol_select_calls.append((name, bool(enable)))
        s = self.symbols.get(name)
        if s is None:
            return False
        s.visible = bool(enable)
        return True

    # --- sentinelas de trading (NO deben llamarse)
    def order_check(self, request):
        self.trading_calls.append("order_check")
        raise AssertionError("order_check invocado (prohibido en solo lectura)")

    def order_send(self, request):
        self.trading_calls.append("order_send")
        raise AssertionError("order_send invocado (prohibido en solo lectura)")

    def orders_get(self, *a, **k):
        self.trading_calls.append("orders_get")
        raise AssertionError("orders_get invocado (prohibido)")

    def positions_get(self, *a, **k):
        self.trading_calls.append("positions_get")
        raise AssertionError("positions_get invocado (prohibido)")

    # --- helper
    def add_symbol(self, name, **over):
        fields = dict(
            name=name, visible=True, volume_min=0.01, volume_step=0.01,
            volume_max=100.0, point=0.01, trade_tick_size=0.01,
            trade_tick_value=1.0, trade_contract_size=100.0,
            currency_base="XAU", currency_profit="USD", currency_margin="USD",
            trade_mode=4, filling_mode=1, digits=2,
        )
        fields.update(over)
        self.symbols[name] = SimpleNamespace(**fields)


def _config(**over) -> MT5Config:
    params = dict(
        provider="mt5", environment=MT5Environment.DEMO,
        symbol_map={"XAUUSD": "XAUUSD.a"},
    )
    params.update(over)
    return MT5Config(**params)


def _creds() -> MT5Credentials:
    return MT5Credentials(login=123456, password=PASSWORD_CANARY, server="Demo-Server")


def _probe(fake=None, config=None, creds=None):
    fake = fake or ReadOnlyFakeMT5()
    if "XAUUSD.a" not in fake.symbols:
        fake.add_symbol("XAUUSD.a")
    return MT5ReadOnlyProbe(config or _config(), creds or _creds(), mt5=fake), fake


# 1 --------------------------------------------------------------------------
class TestReadOnlyHappyPath:
    def test_valid_demo_config_initializes_and_reads(self):
        probe, fake = _probe()
        with probe:
            report = probe.report()
        assert fake.initialize_called is True
        assert report.demo_verified is True
        assert report.account.trade_mode_label == "DEMO"
        assert report.account.currency == "USD"
        assert report.terminal.connected is True
        assert len(report.symbols) == 1
        sym = report.symbols[0]
        assert sym.atlas_id == "XAUUSD" and sym.mt5_name == "XAUUSD.a"
        assert sym.operable is True and sym.ok is True
        assert sym.contract_size == pytest.approx(100.0)
        assert fake.shutdown_called >= 1
        assert fake.trading_calls == []


# 2 --------------------------------------------------------------------------
class TestEnvironment:
    def test_unknown_environment_fails_closed(self):
        with pytest.raises(MT5ConfigError):
            MT5Config(provider="mt5", environment="prod", symbol_map={"A": "A"})

    def test_non_demo_environment_rejected_without_init(self):
        fake = ReadOnlyFakeMT5()
        fake.add_symbol("XAUUSD.a")
        with pytest.raises(BrokerPermissionError):
            _probe(fake=fake, config=_config(environment=MT5Environment.LIVE))
        assert fake.initialize_called is False


# 3 --------------------------------------------------------------------------
class TestAccountVerification:
    def test_real_account_rejected(self):
        probe, fake = _probe()
        fake.account.trade_mode = 2  # REAL
        with pytest.raises(BrokerPermissionError):
            probe.connect()
        assert fake.shutdown_called >= 1  # se libera la sesion
        assert fake.trading_calls == []

    def test_unknown_account_mode_rejected(self):
        probe, fake = _probe()
        fake.account.trade_mode = -1  # indeterminado
        with pytest.raises(BrokerPermissionError):
            probe.connect()
        assert fake.shutdown_called >= 1

    def test_demo_without_trading_permissions_is_readonly_ok(self):
        probe, fake = _probe()
        fake.account.trade_allowed = False
        fake.account.trade_expert = False
        with probe:
            report = probe.report()
        assert report.ok is True
        assert report.account.trade_allowed is False
        assert report.account.trade_expert is False
        assert report.demo_verified is True
        assert fake.trading_calls == []


# 4 --------------------------------------------------------------------------
class TestCredentials:
    def test_missing_credentials_fail_closed(self):
        with pytest.raises(BrokerAuthError):
            MT5Credentials.from_env({})

    def test_credentials_repr_hides_password(self):
        assert PASSWORD_CANARY not in repr(_creds())

    def test_login_mismatch_rejected_without_leaking(self):
        probe, fake = _probe()
        fake.account.login = 999999  # observado != esperado
        with pytest.raises(BrokerAuthError) as exc:
            probe.connect()
        assert PASSWORD_CANARY not in str(exc.value)
        assert fake.shutdown_called >= 1


# 5 --------------------------------------------------------------------------
class TestSymbolMapping:
    def test_empty_mapping_rejected(self):
        with pytest.raises(ReadOnlyProbeError):
            MT5ReadOnlyProbe(_config(symbol_map={}), _creds())

    def test_ambiguous_mapping_rejected(self):
        with pytest.raises(ReadOnlyProbeError):
            MT5ReadOnlyProbe(
                _config(symbol_map={"A": "SAME", "B": "SAME"}), _creds()
            )

    def test_unavailable_symbol_fails_closed(self):
        probe, fake = _probe(config=_config(symbol_map={"XAUUSD": "NOPE"}))
        with probe:
            with pytest.raises(ReadOnlyProbeError):
                probe.read_symbols()
        assert fake.trading_calls == []

    def test_incompatible_instrument_spec_rejected(self):
        probe, fake = _probe()
        with probe:
            reports = probe.read_expected_specs(
                {"XAUUSD": InstrumentSpec("XAUUSD", contract_size=1.0, quote_currency="USD")}
            )
        assert len(reports) == 1
        assert reports[0].ok is False
        assert any("incompatible" in e.lower() for e in reports[0].errors)

    def test_probe_does_not_modify_market_watch(self):
        probe, fake = _probe()
        with probe:
            probe.report()
        # SOLO LECTURA: nunca se selecciona/modifica el Market Watch.
        assert fake.symbol_select_calls == []

    def test_invisible_symbol_reported_without_selection(self):
        fake = ReadOnlyFakeMT5()
        fake.add_symbol("XAUUSD.a", visible=False)
        probe = MT5ReadOnlyProbe(_config(), _creds(), mt5=fake)
        with probe:
            reports = probe.read_symbols(strict=False)
        assert reports[0].ok is False
        assert any("visible" in e.lower() for e in reports[0].errors)
        assert fake.symbol_select_calls == []  # no se intenta seleccionar


# 6 --------------------------------------------------------------------------
class TestLifecycleAndIsolation:
    def test_disconnect_on_exception(self):
        probe, fake = _probe(config=_config(symbol_map={"XAUUSD": "NOPE"}))
        with pytest.raises(ReadOnlyProbeError):
            with probe:
                probe.read_symbols()
        assert fake.shutdown_called == 1  # __exit__ libera recursos

    def test_run_helper_always_disconnects(self):
        fake = ReadOnlyFakeMT5()
        fake.add_symbol("XAUUSD.a")
        report = run_readonly_probe(_config(), _creds(), mt5=fake)
        assert report.ok is True
        assert fake.shutdown_called >= 1

    def test_probe_never_invokes_trading_methods(self):
        probe, fake = _probe()
        with probe:
            probe.report()
        assert fake.trading_calls == []
        for name in ("submit_order", "cancel_order", "order_check", "order_send"):
            assert not hasattr(MT5ReadOnlyProbe, name)

    def test_import_does_not_initialize(self):
        # Construir el probe NO inicializa; solo `connect()` lo hace.
        probe, fake = _probe()
        assert fake.initialize_called is False
        probe.connect()
        assert fake.initialize_called is True
        probe.disconnect()

    def test_query_before_connect_raises(self):
        probe, _ = _probe()
        with pytest.raises(BrokerConnectionError):
            probe.read_symbols()
