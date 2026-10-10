"""MT5 read-only probe (MT5-DEMO-READONLY-01).

Probe AISLADO para verificar conectividad y metadatos de una cuenta MT5 DEMO
sin habilitar ninguna operacion de trading. NO es un `BrokerAdapter`: no expone
`submit_order`, `cancel_order`, `order_check`, `order_send` ni metodos de
modificacion. Solo: inicializar conexion, leer `terminal_info`/`account_info`,
leer metadatos de simbolos configurados, validar `InstrumentSpec` y desconectar.

Reutiliza la configuracion/credenciales/validacion existentes de `brokers.mt5`
(`MT5Config`, `MT5Credentials`, `read_mt5_symbol_spec`, `instrument_spec_from_mt5`,
`_load_mt5`). Importar este modulo NO inicializa MT5 (carga perezosa).

Verificacion DEMO real: se exige `account_info().trade_mode ==
ACCOUNT_TRADE_MODE_DEMO` (0). Cuenta real (2), concurso (1) o desconocida (-1)
=> fail-closed. Una cuenta autenticada puede tener `trade_allowed`/`trade_expert`
deshabilitados y aun asi ser examinada en solo lectura: esos flags se INFORMAN,
nunca habilitan operaciones ni se confunden con el estado READY del adapter.

Seguridad: credenciales SOLO por entorno (`MT5Credentials.from_env`); nunca en
CLI/YAML/codigo/logs; nunca se muestran contrasenas; el login se enmascara.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Mapping

from execution.instrument import AssetClass, InstrumentSpec, InstrumentSpecError

from brokers.models import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    BrokerInvalidOrderError,
    BrokerPermissionError,
)
from brokers.mt5 import (
    MT5Config,
    MT5Credentials,
    MT5Environment,
    MT5SymbolSpec,
    SYMBOL_TRADE_MODE_FULL,
    _load_mt5,
    instrument_spec_from_mt5,
    read_mt5_symbol_spec,
)

_UTC = timezone.utc

# Constantes oficiales de `account_info().trade_mode`.
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2
_TRADE_MODE_LABELS = {
    ACCOUNT_TRADE_MODE_DEMO: "DEMO",
    ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
    ACCOUNT_TRADE_MODE_REAL: "REAL",
}


class ReadOnlyProbeError(ValueError):
    """El probe de solo lectura no puede completarse (fail-closed)."""


def _mask_login(login: object) -> str:
    """Enmascara el numero de cuenta (nunca completo)."""
    s = str(login)
    if len(s) <= 2:
        return "***"
    return f"***{s[-2:]}"


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _utc_now() -> datetime:
    return datetime.now(_UTC)


@dataclass(frozen=True, slots=True)
class ReadOnlyTerminal:
    connected: bool
    company: str
    name: str
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class ReadOnlyAccount:
    login_masked: str
    server: str
    currency: str
    trade_mode: int
    trade_mode_label: str
    demo_verified: bool
    trade_allowed: bool
    trade_expert: bool
    balance: float | None
    equity: float | None
    margin_free: float | None
    leverage: int | None


@dataclass(frozen=True, slots=True)
class SymbolReport:
    atlas_id: str
    mt5_name: str
    operable: bool
    trade_mode: int
    volume_min: float | None
    volume_step: float | None
    volume_max: float | None
    contract_size: float | None
    tick_size: float | None
    tick_value: float | None
    currency_base: str
    currency_profit: str
    currency_margin: str
    instrument_spec: InstrumentSpec | None
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadOnlyProbeReport:
    demo_verified: bool
    terminal: ReadOnlyTerminal
    account: ReadOnlyAccount
    symbols: tuple[SymbolReport, ...]
    ok: bool

    def to_dict(self) -> dict:
        payload = asdict(self)
        for sym, src in zip(payload["symbols"], self.symbols):
            sym["instrument_spec"] = (
                None if src.instrument_spec is None else src.instrument_spec.to_dict()
            )
        return payload


class MT5ReadOnlyProbe:
    """Probe de solo lectura sobre una sesion MT5 DEMO.

    No hereda de `BrokerAdapter` y NO define ningun metodo de trading.
    """

    def __init__(
        self,
        config: MT5Config,
        credentials: MT5Credentials,
        *,
        mt5=None,
        clock=_utc_now,
    ) -> None:
        if not isinstance(config, MT5Config):
            raise ReadOnlyProbeError("config debe ser MT5Config")
        if not isinstance(credentials, MT5Credentials):
            raise BrokerAuthError("credentials debe ser MT5Credentials")
        if config.environment is not MT5Environment.DEMO:
            raise BrokerPermissionError(
                "MT5-DEMO-READONLY-01: el probe solo admite entorno DEMO; "
                "produccion/live PROHIBIDA"
            )
        if not config.symbol_map:
            raise ReadOnlyProbeError(
                "symbol_map vacio: se requiere mapping explicito Atlas->MT5"
            )
        names = list(config.symbol_map.values())
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ReadOnlyProbeError(f"mapping MT5 ambiguo (duplicado): {dupes}")
        self.config = config
        self._credentials = credentials
        self._clock = clock
        self._mt5 = mt5
        self._state = "DISCONNECTED"
        self._terminal: ReadOnlyTerminal | None = None
        self._account: ReadOnlyAccount | None = None

    # -------------------------------------------------------------- estado

    @property
    def state(self) -> str:
        return self._state

    def _module(self):
        if self._mt5 is None:
            self._mt5 = _load_mt5()
        return self._mt5

    def _require_connected(self) -> None:
        if self._state != "CONNECTED_READONLY":
            raise BrokerConnectionError(
                f"probe no conectado (estado={self._state!r})"
            )

    # -------------------------------------------------------------- ciclo

    def connect(self) -> "MT5ReadOnlyProbe":
        """Inicializa, autentica y VERIFICA que la cuenta es DEMO (solo lectura)."""
        if self.config.environment is not MT5Environment.DEMO:
            raise BrokerPermissionError(
                "MT5-DEMO-READONLY-01: solo entorno DEMO"
            )
        self._state = "CONNECTING"
        mt5 = self._module()
        kwargs: dict = {
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
        except Exception as exc:  # noqa: BLE001
            self._state = "FAILED"
            raise BrokerConnectionError(
                f"MT5 initialize fallo ({type(exc).__name__})"
            ) from exc
        if not ok:
            self._state = "FAILED"
            raise self._classify_connect_error(mt5)
        try:
            self._terminal = self._read_terminal(mt5)
            self._account = self._read_account(mt5)
        except BaseException:
            self.disconnect()
            raise
        if not self._account.demo_verified:
            self.disconnect()
            raise BrokerPermissionError(
                "cuenta MT5 no DEMO o indeterminate "
                f"(trade_mode={self._account.trade_mode}); fail-closed"
            )
        self._state = "CONNECTED_READONLY"
        return self

    def _classify_connect_error(self, mt5) -> BrokerError:
        try:
            error = mt5.last_error()
        except Exception:  # noqa: BLE001
            error = None
        code = error[0] if isinstance(error, (tuple, list)) and error else None
        if code == -6:
            return BrokerAuthError("autenticacion MT5 fallida")
        return BrokerConnectionError(
            f"MT5 initialize devolvio False (code={code})"
        )

    def disconnect(self) -> None:
        """Desconecta y libera recursos (idempotente, seguro ante fallos)."""
        if self._state in ("DISCONNECTED", "FAILED"):
            self._state = "DISCONNECTED"
            return
        try:
            self._module().shutdown()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        self._state = "DISCONNECTED"

    def __enter__(self) -> "MT5ReadOnlyProbe":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # -------------------------------------------------------------- lectura

    def _read_terminal(self, mt5) -> ReadOnlyTerminal:
        info = mt5.terminal_info()
        if info is None:
            raise BrokerConnectionError("terminal_info() no disponible")
        return ReadOnlyTerminal(
            connected=bool(getattr(info, "connected", False)),
            company=str(getattr(info, "company", "")),
            name=str(getattr(info, "name", "")),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
        )

    def _read_account(self, mt5) -> ReadOnlyAccount:
        info = mt5.account_info()
        if info is None:
            raise BrokerAuthError("account_info() no disponible")
        currency = str(getattr(info, "currency", "") or "")
        if not currency:
            raise ReadOnlyProbeError("account_info sin moneda de cuenta")
        trade_mode = int(getattr(info, "trade_mode", -1))
        login = getattr(info, "login", None)
        expected_login = self._credentials.login
        if login is not None and int(login) != int(expected_login):
            raise BrokerAuthError(
                "login observado no coincide con el esperado (identidad no verificada)"
            )
        server_obs = str(getattr(info, "server", "") or "")
        if server_obs and server_obs.strip().lower() != self._credentials.server.strip().lower():
            raise BrokerAuthError(
                "servidor observado no coincide con el esperado (identidad no verificada)"
            )
        leverage = getattr(info, "leverage", None)
        return ReadOnlyAccount(
            login_masked=_mask_login(login),
            server=server_obs,
            currency=currency,
            trade_mode=trade_mode,
            trade_mode_label=_TRADE_MODE_LABELS.get(trade_mode, "UNKNOWN"),
            demo_verified=(trade_mode == ACCOUNT_TRADE_MODE_DEMO),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            trade_expert=bool(getattr(info, "trade_expert", False)),
            balance=_finite_or_none(getattr(info, "balance", None)),
            equity=_finite_or_none(getattr(info, "equity", None)),
            margin_free=_finite_or_none(getattr(info, "margin_free", None)),
            leverage=int(leverage) if isinstance(leverage, int) else None,
        )

    def _read_symbol_spec(self, atlas_id: str, mt5_name: str) -> MT5SymbolSpec:
        mt5 = self._module()
        try:
            return read_mt5_symbol_spec(
                mt5, mt5_name, atlas_id,
                filling_mode_default=self.config.filling_mode,
                require_operable=False,
                # SOLO LECTURA: NO se modifica el Market Watch (sin symbol_select).
                select_if_invisible=False,
            )
        except BrokerError as exc:
            raise ReadOnlyProbeError(
                f"simbolo {atlas_id!r} ({mt5_name!r}) no disponible/valido: {exc}"
            ) from exc

    def _report_from_spec(
        self,
        atlas_id: str,
        spec: MT5SymbolSpec,
        expected: InstrumentSpec | None,
    ) -> SymbolReport:
        errors: list[str] = []
        instrument_spec: InstrumentSpec | None = None
        try:
            instrument_spec = instrument_spec_from_mt5(
                spec,
                atlas_instrument_id=atlas_id,
                asset_class=expected.asset_class if expected else AssetClass.OTHER,
                contract_size=expected.contract_size if expected else None,
                quote_currency=expected.quote_currency if expected else None,
                base_currency=expected.base_currency if expected else None,
            )
        except InstrumentSpecError as exc:
            errors.append(
                f"InstrumentSpec incompatible: {exc}" if expected
                else f"contrato economico no derivable: {exc}"
            )
        if not spec.visible:
            errors.append(
                "simbolo no visible en Market Watch (el probe no lo selecciona)"
            )
        if spec.trade_mode != SYMBOL_TRADE_MODE_FULL:
            errors.append(f"simbolo no operable (trade_mode={spec.trade_mode})")
        return SymbolReport(
            atlas_id=atlas_id,
            mt5_name=spec.name,
            operable=(spec.trade_mode == SYMBOL_TRADE_MODE_FULL),
            trade_mode=spec.trade_mode,
            volume_min=spec.volume_min,
            volume_step=spec.volume_step,
            volume_max=spec.volume_max,
            contract_size=instrument_spec.contract_size if instrument_spec else None,
            tick_size=spec.trade_tick_size,
            tick_value=spec.trade_tick_value,
            currency_base=spec.currency_base,
            currency_profit=spec.currency_profit,
            currency_margin=spec.currency_margin,
            instrument_spec=instrument_spec,
            ok=not errors,
            errors=tuple(errors),
        )

    def read_expected_specs(
        self, expected: Mapping[str, InstrumentSpec]
    ) -> tuple[SymbolReport, ...]:
        """Valida los simbolos contra `InstrumentSpec` esperados (cross-check)."""
        self._require_connected()
        reports: list[SymbolReport] = []
        for atlas_id, mt5_name in self.config.symbol_map.items():
            spec = self._read_symbol_spec(atlas_id, mt5_name)
            reports.append(
                self._report_from_spec(atlas_id, spec, expected.get(atlas_id))
            )
        return tuple(reports)

    def read_symbols(self, *, strict: bool = True) -> tuple[SymbolReport, ...]:
        """Lee/valida los simbolos configurados. `strict` => fail-closed."""
        self._require_connected()
        reports = tuple(
            self._report_from_spec(atlas_id, self._read_symbol_spec(atlas_id, mt5_name), None)
            for atlas_id, mt5_name in self.config.symbol_map.items()
        )
        if strict and any(not r.ok for r in reports):
            bad = [r.atlas_id for r in reports if not r.ok]
            raise ReadOnlyProbeError(f"simbolos incompatibles/no operables: {bad}")
        return reports

    def report(self, *, strict: bool = True) -> ReadOnlyProbeReport:
        self._require_connected()
        assert self._terminal is not None and self._account is not None
        symbols = self.read_symbols(strict=strict)
        ok = self._account.demo_verified and all(s.ok for s in symbols)
        return ReadOnlyProbeReport(
            demo_verified=self._account.demo_verified,
            terminal=self._terminal,
            account=self._account,
            symbols=symbols,
            ok=ok,
        )


def run_readonly_probe(
    config: MT5Config,
    credentials: MT5Credentials,
    *,
    mt5=None,
    strict: bool = True,
    clock=_utc_now,
) -> ReadOnlyProbeReport:
    """Conecta, lee y desconecta SIEMPRE (finally), sin enviar ordenes."""
    probe = MT5ReadOnlyProbe(config, credentials, mt5=mt5, clock=clock)
    try:
        probe.connect()
        return probe.report(strict=strict)
    finally:
        probe.disconnect()


# --------------------------------------------------------------------- CLI

def _load_symbol_map_from_env() -> dict:
    import os

    raw = os.getenv("MT5_SYMBOL_MAP", "")
    if not raw.strip():
        raise ReadOnlyProbeError(
            "MT5_SYMBOL_MAP vacio: se requiere mapping explicito Atlas->MT5 (JSON)"
        )
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReadOnlyProbeError(f"MT5_SYMBOL_MAP JSON invalido: {exc}") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise ReadOnlyProbeError("MT5_SYMBOL_MAP debe ser un objeto JSON no vacio")
    return {str(k): str(v) for k, v in mapping.items()}


def main(argv: list[str] | None = None) -> int:
    """Invocacion explicita (NO se ejecuta aqui contra un terminal real)."""
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="atlas-mt5-readonly",
        description="Probe MT5 DEMO de SOLO LECTURA (no envia ordenes).",
    )
    parser.add_argument("--terminal-path", default=os.getenv("MT5_TERMINAL_PATH"))
    args = parser.parse_args(argv)

    credentials = MT5Credentials.from_env()
    config = MT5Config(
        provider="mt5",
        environment=MT5Environment.DEMO,
        terminal_path=args.terminal_path,
        symbol_map=_load_symbol_map_from_env(),
    )
    report = run_readonly_probe(config, credentials, strict=True)
    print(json.dumps(report.to_dict(), indent=2, default=str))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - invocacion explicita
    raise SystemExit(main())


__all__ = [
    "ACCOUNT_TRADE_MODE_CONTEST",
    "ACCOUNT_TRADE_MODE_DEMO",
    "ACCOUNT_TRADE_MODE_REAL",
    "MT5ReadOnlyProbe",
    "ReadOnlyAccount",
    "ReadOnlyProbeError",
    "ReadOnlyProbeReport",
    "ReadOnlyTerminal",
    "SymbolReport",
    "main",
    "run_readonly_probe",
]
