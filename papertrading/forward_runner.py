"""ForwardRunner (FORWARD): orquestador paper/forward continuo sobre H002 congelada.

Cadena (consumiendo EXCLUSIVAMENTE `ClosedBarEvent`, nunca intrabar):

    LiveDataEngine -> [ClosedBarEvent] -> ForwardRunner.on_closed_bar
        -> PaperTradingEngine (B7): sesion -> H002 señal -> RiskEngine -> Kill
           Switch -> OrderManager -> PaperBrokerAdapter -> fills/posicion/equity
           -> Reconciliacion -> Monitoring -> Kill Switch
        -> proyeccion por barra (ForwardBarRecord) -> persistencia

El runner es un ORQUESTADOR: no contiene logica de estrategia ni de riesgo;
toda decision vive en las capas B1-B7. Sus responsabilidades son:

  1. Congelar la config: guard mecanico del strategy_hash (491ed76d...). Si no
     coincide, ForwardConfigError (la estrategia NO se recalibra).
  2. Riesgo OPERATIVO de paper (config/paper/settings.yaml) autoritativo:
     puede ser inferior al 1% de la estrategia porque solo modifica el capital
     simulado y el riesgo de ejecucion del entorno, nunca los params congelados.
  3. Persistir por barra: bars.jsonl (input, fuente de verdad), records.ndjson
     (ForwardBarRecord por barra), audit.jsonl (log bruto del engine),
     snapshot.json (estado atomico) y manifest.json (lineage/guardas).
  4. Idempotencia y reproducibilidad: al arrancar, el runner REPRODUCE el
     bars.jsonl sobre un engine recien construido y regenera los outputs desde
     cero; mismo input + misma config -> mismo resultado. Las barras duplicadas
     o out-of-order no se procesan (ni engine ni runner las reprocesan).
  5. PaperBrokerAdapter OBLIGATORIO: ningun broker real entra por aqui.
  6. FINAL_OOS INTOCABLE: el preregistro solo se lee para el guard mecanico;
     nunca se muta evidence.json / data_roles.json / config / preregistro.

Reloj: el runner avanza el reloj del stack al cierre de cada barra (tiempo de
mercado), lo que hace la reproduccion determinista y consistente con B7. La
deteccion de staleness/gaps contra el reloj de pared vive en B1 (LiveDataEngine),
que detiene la emision; aqui cada barra que llega ya paso el filtro B1.

Costes: el PaperBrokerAdapter B3 no modela comisiones (fill al precio de barra
abierta/stop/cierre). El forward paper es un piloto de comportamiento/fiabilidad;
el modelo de costes del FINAL_OOS (0.15/lado) es exclusivo de la evaluacion OOS.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from brokers.paper import PaperBrokerAdapter
from execution.order import reset_order_sequence
from execution.order_manager import OrderManager
from killswitches.coordinator import KillSwitchCoordinator
from live.events import ClosedBarEvent
from monitoring.engine import MonitoringEngine
from monitoring.models import HealthStatus
from papertrading.engine import PaperTradingEngine
from papertrading.models import PaperEngineConfig, PaperEvent
from reconciliation.engine import ReconciliationEngine
from research.experiment import make_config_hash
from risk.engine import RiskEngine
from risk.types import RiskConfig
from sessions.manager import MarketSessionManager, SessionDecision
from sessions.markets import btc_24_7_calendar
from strategies.breakout import BreakoutParams, BreakoutStrategy
from strategies.base.strategy import StrategyMetadata

ROOT = Path(__file__).resolve().parents[1]

FROZEN_STRATEGY_HASH = (
    "491ed76d79f1034452e98f72453141a4c69110cc8d487f166a89072df332687f"
)
FROZEN_CONFIG_HASH = (
    "cb639bbf55f0b1f25aee21841b7291e0de7d8e360580090e9dd987fba6d9653c"
)
FROZEN_MAX_STRATEGY_RISK = 0.01

_UTC = timezone.utc


def _atomic_replace(tmp: Path, dest: Path, retries: int = 3, wait: float = 0.2) -> None:
    """Reemplazo atómico con reintento ante locks transitorios (OneDrive/AV).

    `os.replace` puede fallar con PermissionError si el destino está siendo
    sincronizado (OneDrive) o escaneado por AV justo en ese instante. Se reintenta
    con espera corta varias veces; si persiste se relanza (fallo real).
    """
    for attempt in range(retries):
        try:
            os.replace(tmp, dest)
            return
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(wait)


class ForwardConfigError(ValueError):
    """La configuracion no respeta el perimetro congelado (guard mecanico)."""


def _now_utc() -> datetime:
    return datetime.now(_UTC)


def _iso_dt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=_UTC).isoformat()


@dataclass(slots=True)
class ForwardClock:
    """Reloj mutable del stack forward.

    El runner lo avanza al cierre de cada barra (tiempo de mercado) para que la
    reproduccion sea determinista; si nunca se setea, devuelve reloj de pared.
    """

    _value: datetime | None = None

    def __call__(self) -> datetime:
        return self._value if self._value is not None else _now_utc()

    def set(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self.__call__()


@dataclass(frozen=True, slots=True)
class ForwardBarRecord:
    """Audit trail de UNICA barra cerrada (el contrato de datos del forward).

    Campos exigidos por el pedido: timestamp, data quality, session state,
    strategy version/hash, señal, RiskDecision, KillSwitchStatus, order intent,
    submission status, fills, posicion, equity, reconciliation, monitoring.
    """

    run_id: str
    timestamp: str
    bar_open_time: int
    bar_close_time: int
    interval_seconds: int
    emission_sequence: int
    data_quality: str
    session_state: str | None
    session_can_open: bool | None
    strategy_id: str
    strategy_version: str
    strategy_hash: str
    signal: str
    risk_decision: str
    approved_quantity: float
    risk_reason: str
    kill_switch: str
    can_trade: bool
    order_intent: str | None
    submission_status: str | None
    fills_quantity: float
    fills_vwap: float | None
    position: float
    position_stop: float | None
    equity: float
    reconciliation_status: str | None
    reconciliation_diffs: int
    monitoring_overall: str | None
    monitoring_components: dict = field(default_factory=dict)
    restarted_skip: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "bar_open_time": self.bar_open_time,
            "bar_close_time": self.bar_close_time,
            "interval_seconds": self.interval_seconds,
            "emission_sequence": self.emission_sequence,
            "data_quality": self.data_quality,
            "session_state": self.session_state,
            "session_can_open": self.session_can_open,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_hash": self.strategy_hash,
            "signal": self.signal,
            "risk_decision": self.risk_decision,
            "approved_quantity": self.approved_quantity,
            "risk_reason": self.risk_reason,
            "kill_switch": self.kill_switch,
            "can_trade": self.can_trade,
            "order_intent": self.order_intent,
            "submission_status": self.submission_status,
            "fills_quantity": self.fills_quantity,
            "fills_vwap": self.fills_vwap,
            "position": self.position,
            "position_stop": self.position_stop,
            "equity": self.equity,
            "reconciliation_status": self.reconciliation_status,
            "reconciliation_diffs": self.reconciliation_diffs,
            "monitoring_overall": self.monitoring_overall,
            "monitoring_components": dict(self.monitoring_components),
            "restarted_skip": self.restarted_skip,
        }


def _monitoring_overall(refs: dict) -> tuple[str | None, dict]:
    statuses = ["FAILED", "UNKNOWN", "DEGRADED", "HEALTHY"]
    for name in statuses:
        if refs.get(name):
            return name, {s: refs.get(s, []) for s in statuses if refs.get(s)}
    return None, {}


class ForwardRunner:
    """Orquestador paper/forward continuo, idempotente y reproducible.

    Args:
        engine: PaperTradingEngine con stack B1-B7 completo (recien construido).
        output_dir: directorio raiz de persistencia; los archivos se escriben
            en `output_dir/run_id/`.
        strategy_hash: hash congelado de H002; debe ser 491ed76d... o se lanza
            ForwardConfigError.
        clock: reloj del runner (ForwardClock con timepo de mercado).
        lineage: metadatos para el manifest (los llena build_forward_runner).

    Uso tipico:
        runner = build_forward_runner(run_id="FORWARD-PAPER")
        runner.start()                     # reproduce bars.jsonl si existe
        runner.on_closed_bar(closed_bar)   # por cada ClosedBarEvent de B1
    """

    def __init__(
        self,
        engine: PaperTradingEngine,
        *,
        run_id: str,
        output_dir: str | Path,
        instrument: str,
        strategy_hash: str,
        clock: ForwardClock | None = None,
        lineage: dict | None = None,
    ) -> None:
        if not isinstance(engine.broker, PaperBrokerAdapter):
            raise TypeError(
                "ForwardRunner exige PaperBrokerAdapter (nunca un broker real)."
            )
        if strategy_hash != FROZEN_STRATEGY_HASH:
            raise ForwardConfigError(
                f"strategy_hash {strategy_hash} != congelado {FROZEN_STRATEGY_HASH}. "
                "H002 esta congelada: no se puede abrir forward con otra config."
            )
        self.engine = engine
        self.run_id = run_id
        self.instrument = instrument
        self.strategy_hash = strategy_hash
        self._clock = clock or ForwardClock()
        self._lineage = dict(lineage or {})
        self.output_dir = Path(output_dir) / run_id
        self._bars_path = self.output_dir / "bars.jsonl"
        self._records_path = self.output_dir / "records.ndjson"
        self._audit_path = self.output_dir / "audit.jsonl"
        self._snapshot_path = self.output_dir / "snapshot.json"
        self._manifest_path = self.output_dir / "manifest.json"
        self._last_open_time: int | None = None
        self._records: list[ForwardBarRecord] = []
        self._started = False

    @property
    def records(self) -> tuple[ForwardBarRecord, ...]:
        return tuple(self._records)

    def start(self) -> "ForwardRunner":
        """Arranca el runner: reproduce bars.jsonl (si existe) desde cero.

        La reproduccion regenera records.ndjson / audit.jsonl / snapshot.json
        sobre el engine recien construido, garantizando que un restart no
        duplica exposicion ni reprocesa barras viejas.

        El estado del Kill Switch (B5) se persiste en snapshot.json y se
        restaura EXACTAMENTE al arrancar: el latch y las condiciones activas
        sobreviven al restart y NUNCA se degradan a ALLOW por el reinicio
        (solo un `recover(reason, health_checks_ok=True)` explicito rehabilita
        el trading). Un snapshot legacy sin bloque `kill_switch` arranca sin
        latch (ese formato no persistia B5).

        Input fingerprint: el manifest registra sha256+conteo+extremos de
        bars.jsonl (`checkpoint()` lo refresca al cerrar cada ciclo). Al
        arrancar se verifica que bars.jsonl EMPIECE con el prefijo registrado;
        una extension por append (crash a mitad de ciclo) es valida, pero una
        alteracion/truncamiento del estado lanza ForwardConfigError (fail-safe,
        sin reconstruir en silencio).
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        recorded = self._load_manifest_fingerprint()
        if recorded is not None:
            current = self._input_fingerprint(limit=int(recorded["bars"]))
            if current != recorded:
                raise ForwardConfigError(
                    "input fingerprint mismatch: bars.jsonl ya no coincide con el "
                    f"manifest (manifest bars={recorded.get('bars')}, "
                    f"first_ts={recorded.get('first_ts')}, "
                    f"last_ts={recorded.get('last_ts')}); estado alterado o input "
                    "inesperado: no se procesa (fail-safe)."
                )
        self._write_manifest()
        kill_switch_snapshot = self._load_kill_switch_snapshot()
        persisted = self._load_bars()
        if persisted:
            self._reset_derived()
            for row in persisted:
                closed = self._closed_from_row(row)
                self._last_open_time = closed.open_time
                self._clock.set(_closed_close_dt(closed))
                self._process(closed, str(row.get("quality", "HEALTHY")))
        if kill_switch_snapshot is not None:
            self.engine.kill_switch.load_snapshot(kill_switch_snapshot)
            self._write_snapshot()
        self._started = True
        return self

    def on_closed_bar(
        self,
        closed: ClosedBarEvent,
        *,
        data_quality: str = "HEALTHY",
        at: datetime | None = None,
    ) -> ForwardBarRecord | None:
        """Una barra cerrada (emitida por B1). Idempotente ante duplicados."""
        if not self._started:
            raise RuntimeError("ForwardRunner.start() antes de procesar barras.")
        if (
            self._last_open_time is not None
            and closed.open_time <= self._last_open_time
        ):
            return None
        self._clock.set(at or _closed_close_dt(closed))
        self._last_open_time = closed.open_time
        self._append_bar(closed, data_quality)
        return self._process(closed, data_quality)

    # ---------------------------------------------------------------- persist

    def _write_manifest(self) -> None:
        created_at = _now_utc().isoformat()
        if self._manifest_path.exists():
            try:
                existing = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                created_at = str(existing.get("created_at", created_at))
            except (OSError, json.JSONDecodeError):
                pass
        manifest = {
            "run_id": self.run_id,
            "instrument": self.instrument,
            "broker": "PaperBrokerAdapter",
            "strategy_hash": self.strategy_hash,
            "config_hash": FROZEN_CONFIG_HASH,
            "created_at": created_at,
            "input_fingerprint": self._input_fingerprint(),
            "output_files": {
                "bars": self._bars_path.name,
                "records": self._records_path.name,
                "audit": self._audit_path.name,
                "snapshot": self._snapshot_path.name,
            },
        }
        manifest.update(self._lineage)
        self._write_json(self._manifest_path, manifest)

    def checkpoint(self) -> None:
        """Refresca el manifest con el fingerprint del input/estado actual.

        Se llama al cerrar cada ciclo forward: el manifest identifica de forma
        exacta las barras que produjeron el estado en reposo.
        """
        self._write_manifest()

    def _input_fingerprint(self, limit: int | None = None) -> dict:
        """Identidad del input persistido (bars.jsonl), opcionalmente truncado.

        Devuelve sha256 + conteo + extremos. Con `limit`, hashea SOLO las
        primeras `limit` barras: permite comprobar que el estado arranca con el
        prefijo ya registrado (extension por append es valida; alteracion no).
        """
        data = self._bars_path.read_bytes() if self._bars_path.exists() else b""
        if limit is not None and limit >= 0:
            pos = 0
            count = 0
            while count < limit:
                nl = data.find(b"\n", pos)
                if nl == -1:
                    break
                pos = nl + 1
                count += 1
            data = data[:pos]
        bars = 0
        first_ts: int | None = None
        last_ts: int | None = None
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            ts = int(json.loads(line)["bar"]["open_time"])
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            bars += 1
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bars": bars,
            "first_ts": first_ts,
            "last_ts": last_ts,
        }

    def _load_manifest_fingerprint(self) -> dict | None:
        """Fingerprint de input registrado en el manifest previo (o None)."""
        if not self._manifest_path.exists():
            return None
        try:
            manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        fp = manifest.get("input_fingerprint")
        if not isinstance(fp, dict) or "bars" not in fp or "sha256" not in fp:
            return None  # manifest legacy: sin fingerprint -> sin verificacion
        return fp

    def _load_bars(self) -> list[dict]:
        if not self._bars_path.exists():
            return []
        rows: list[dict] = []
        with self._bars_path.open("r", encoding="utf-8", newline="\n") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _load_kill_switch_snapshot(self) -> dict | None:
        """Estado B5 persistido (latch) del snapshot previo, o None.

        Compatibilidad: snapshot ausente o sin bloque `kill_switch` (formato
        legacy) equivalen a un arranque sin latch. Un bloque presente pero
        malformado falla en voz alta: NUNCA se degrada a ALLOW por un snapshot
        ilegible.
        """
        if not self._snapshot_path.exists():
            return None
        try:
            snap = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ForwardConfigError(
                f"snapshot ilegible en {self._snapshot_path}: {exc}"
            ) from exc
        block = snap.get("kill_switch")
        if block is None:
            return None
        if not isinstance(block, dict) or "recovery_required" not in block:
            raise ForwardConfigError(
                "bloque kill_switch invalido en snapshot (falta recovery_required)."
            )
        return block

    @staticmethod
    def _closed_from_row(row: dict) -> ClosedBarEvent:
        bar = row["bar"]
        return ClosedBarEvent(
            symbol=bar["symbol"],
            open_time=int(bar["open_time"]),
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            volume=bar["volume"],
            interval_seconds=int(bar["interval_seconds"]),
            emission_sequence=int(bar["emission_sequence"]),
        )

    def _append_bar(self, closed: ClosedBarEvent, quality: str) -> None:
        row = {
            "bar": {
                "symbol": closed.symbol,
                "open_time": closed.open_time,
                "open": closed.open,
                "high": closed.high,
                "low": closed.low,
                "close": closed.close,
                "volume": closed.volume,
                "interval_seconds": closed.interval_seconds,
                "emission_sequence": closed.emission_sequence,
            },
            "quality": quality,
        }
        with self._bars_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            fh.flush()

    def _reset_derived(self) -> None:
        for path in (self._records_path, self._audit_path):
            if path.exists():
                path.unlink()

    def _append_record(self, record: ForwardBarRecord) -> None:
        with self._records_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps(
                    record.to_dict(), sort_keys=True, separators=(",", ":"), default=str
                )
                + "\n"
            )
            fh.flush()

    def _append_audit(self, entry) -> None:
        with self._audit_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": entry.id,
                        "at": entry.at.isoformat(),
                        "event": entry.event,
                        "refs": entry.refs,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )
            fh.flush()

    def _write_snapshot(self) -> None:
        forward = {
            "run_id": self.run_id,
            "strategy_hash": self.strategy_hash,
            "bars_persisted": len(self._load_bars()),
            "records": len(self._records),
            "last_open_time": self._last_open_time,
        }
        snap = {
            "engine": self.engine.snapshot(),
            "forward": forward,
            "kill_switch": self.engine.kill_switch.snapshot(),
        }
        tmp = self._snapshot_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(snap, indent=2, default=str), encoding="utf-8"
        )
        _atomic_replace(tmp, self._snapshot_path)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        _atomic_replace(tmp, path)

    # --------------------------------------------------------------- procesado

    def _process(self, closed: ClosedBarEvent, quality: str) -> ForwardBarRecord:
        before = len(self.engine.audit_log)
        self.engine.on_closed_bar(closed)
        entries = tuple(self.engine.audit_log[before:])
        record = self._project(closed, entries, quality)
        self._records.append(record)
        self._append_record(record)
        for entry in entries:
            self._append_audit(entry)
        self._write_snapshot()
        return record

    def _project(
        self,
        closed: ClosedBarEvent,
        entries: tuple,
        quality: str,
    ) -> ForwardBarRecord:
        signal = "NO_TRADE"
        risk_approved: bool | None = None
        risk_qty = 0.0
        risk_reason = ""
        order_intent: str | None = None
        submission: str | None = None
        fills_qty = 0.0
        vwap: float | None = None
        rec_status: str | None = None
        rec_diffs = 0
        monitoring_refs: dict = {}
        restarted = False

        for entry in entries:
            ev = entry.event
            refs = entry.refs
            if ev == PaperEvent.SIGNAL.value:
                signal = str(refs.get("action", "SIGNAL"))
            elif ev == PaperEvent.SIGNAL_WHILE_IN_POSITION.value:
                signal = ev
            elif ev == PaperEvent.RISK_DECISION.value:
                risk_approved = bool(refs.get("approved"))
                risk_qty = float(refs.get("quantity", 0.0))
                risk_reason = str(refs.get("reason", ""))
            elif ev in (PaperEvent.ENTRY_STAGED.value, PaperEvent.ENTRY_SUBMITTED.value):
                order_intent = "ENTRY:" + str(refs.get("signal_id", ""))
            elif ev in (PaperEvent.ENTRY_FILLED.value, PaperEvent.EXIT_FILLED.value):
                quantity = refs.get("quantity")
                price = refs.get("price")
                if quantity is not None:
                    fills_qty = float(quantity)
                if price is not None:
                    vwap = float(price)
                submission = "FILLED"
                if ev == PaperEvent.EXIT_FILLED.value:
                    order_intent = "EXIT:" + str(refs.get("exit_cid", ""))
            elif ev in (PaperEvent.EXIT_STOP.value, PaperEvent.EXIT_TIME.value):
                order_intent = "EXIT:" + str(refs.get("position_id", ""))
            elif ev == PaperEvent.EXIT_SUBMITTED.value:
                order_intent = "EXIT:" + str(refs.get("exit_cid", ""))
            elif ev == PaperEvent.SUBMISSION_UNKNOWN.value:
                submission = "UNKNOWN"
            elif ev == PaperEvent.SUBMISSION_RESOLVED.value:
                submission = "RESOLVED"
            elif ev == PaperEvent.ENTRY_REJECTED_BROKER.value:
                submission = "REJECTED"
            elif ev == PaperEvent.ENTRY_SKIPPED_SESSION.value:
                submission = "SKIPPED(SESSION)"
            elif ev == PaperEvent.ENTRY_SKIPPED_KILL_SWITCH.value:
                submission = "SKIPPED(KILL_SWITCH)"
            elif ev == PaperEvent.ENTRY_SKIPPED_BROKER.value:
                submission = "SKIPPED(BROKER)"
            elif ev == PaperEvent.RECONCILIATION.value:
                rec_status = refs.get("status")
                rec_diffs = int(refs.get("diffs", 0))
            elif ev == PaperEvent.MONITORING.value:
                monitoring_refs = dict(refs)
            elif ev == PaperEvent.RESTART_SKIP.value:
                restarted = True

        if risk_approved is True:
            risk_decision = "APPROVED"
        elif risk_approved is False:
            risk_decision = "REJECTED"
        else:
            risk_decision = "NOT_EVALUATED"

        ks = self.engine.kill_switch.status()
        sess: SessionDecision = self.engine.sessions.get_session(
            closed.symbol, closed.close_time
        )
        pos = self.engine.position
        curve = self.engine.equity_curve
        overall, components = _monitoring_overall(monitoring_refs)

        return ForwardBarRecord(
            run_id=self.run_id,
            timestamp=_iso_dt(closed.close_time),
            bar_open_time=closed.open_time,
            bar_close_time=closed.close_time,
            interval_seconds=closed.interval_seconds,
            emission_sequence=closed.emission_sequence,
            data_quality=quality,
            session_state=sess.state.value,
            session_can_open=sess.can_open_positions,
            strategy_id=self.engine.strategy.metadata.strategy_id,
            strategy_version=self.engine.strategy.metadata.version,
            strategy_hash=self.strategy_hash,
            signal=signal,
            risk_decision=risk_decision,
            approved_quantity=risk_qty,
            risk_reason=risk_reason,
            kill_switch=ks.state.value,
            can_trade=ks.can_trade,
            order_intent=order_intent,
            submission_status=submission,
            fills_quantity=fills_qty,
            fills_vwap=vwap,
            position=pos.quantity if pos is not None else 0.0,
            position_stop=pos.stop_price if pos is not None else None,
            equity=curve[-1][1] if curve else 0.0,
            reconciliation_status=rec_status,
            reconciliation_diffs=rec_diffs,
            monitoring_overall=overall,
            monitoring_components=components,
            restarted_skip=restarted,
        )


def _closed_close_dt(closed: ClosedBarEvent) -> datetime:
    return datetime.fromtimestamp(closed.close_time, tz=_UTC)


def build_forward_runner(
    *,
    run_id: str = "FORWARD-PAPER",
    output_dir: str | Path = "experiments/forward",
    frozen_config_path: str | Path = ROOT / "config" / "final_oos" / "config.yaml",
    paper_config_path: str | Path = ROOT / "config" / "paper" / "settings.yaml",
    preregistration_path: str | Path = (
        ROOT / "research" / "decisions" / "FINAL_OOS_PRE_REGISTRATION.json"
    ),
    clock: ForwardClock | None = None,
    initial_cash: float | None = None,
) -> ForwardRunner:
    """Fabrica el runner forward con la configuracion CONGELADA.

    Guardas mecanicas (ForwardConfigError si algo no encaja con el perimetro
    congelado de H002 / FINAL_OOS):
      - strategy params hash == 491ed76d... (== preregistro).
      - config_hash == cb639bbf... (== preregistro).
      - riesgo operativo paper (config/paper/settings.yaml) autoritativo para
        el entorno; no puede superar el max_strategy_risk congelado (1%).
      - Broker PaperBrokerAdapter (unico permitido).

    Returns:
        ForwardRunner listo para `.start()` y `on_closed_bar(...)`.
    """
    frozen = yaml.safe_load(Path(frozen_config_path).read_text(encoding="utf-8"))
    paper = yaml.safe_load(Path(paper_config_path).read_text(encoding="utf-8"))
    plan = json.loads(Path(preregistration_path).read_text(encoding="utf-8"))
    frozen_strategy = plan["estrategia_congelada"]
    params = frozen["strategy"]["params"]

    params_hash = make_config_hash(params)
    config_hash = make_config_hash(frozen)
    if params_hash != FROZEN_STRATEGY_HASH:
        raise ForwardConfigError(
            f"params hash {params_hash} != congelado {FROZEN_STRATEGY_HASH}."
        )
    if config_hash != FROZEN_CONFIG_HASH:
        raise ForwardConfigError(
            f"config_hash {config_hash} != congelado {FROZEN_CONFIG_HASH}."
        )
    if params_hash != frozen_strategy["strategy_config_hash"]:
        raise ForwardConfigError("params hash no coincide con el preregistro FINAL_OOS.")
    if params != frozen_strategy["params"]:
        raise ForwardConfigError("params no coinciden con el preregistro FINAL_OOS.")

    paper_risk = paper["risk"]
    engine_cfg = frozen["engine"]
    account_id = "FORWARD-PAPER-01"
    instrument = str(frozen["strategy"]["instrument"])
    cash = float(
        initial_cash if initial_cash is not None else paper["paper_account"]["initial_equity"]
    )
    risk_cfg = RiskConfig(
        risk_per_trade=float(paper_risk["risk_per_trade"]),
        max_open_positions=int(paper_risk["max_open_positions"]),
        max_daily_loss=float(paper_risk["max_daily_loss"]),
        max_drawdown=float(paper_risk["max_drawdown"]),
    )
    if risk_cfg.risk_per_trade > FROZEN_MAX_STRATEGY_RISK:
        raise ForwardConfigError(
            "riesgo operativo paper supera el max_strategy_risk congelado (1%)."
        )

    metadata = StrategyMetadata(
        strategy_id=str(frozen["strategy"]["strategy_id"]),
        strategy_name=str(frozen["strategy"]["strategy_name"]),
        strategy_family=str(frozen["strategy"]["strategy_family"]),
        market=str(frozen["strategy"]["market"]),
        instrument=instrument,
        timeframe=str(frozen["strategy"]["timeframe"]),
        version=str(frozen["strategy"]["version"]),
        parameter_set_version=str(frozen["strategy"]["parameter_set_version"]),
        data_version=str(frozen["strategy"]["data_version"]),
        status="FROZEN",
    )
    strategy = BreakoutStrategy(
        metadata, BreakoutParams(**{k: params[k] for k in params})
    )

    runner_clock = clock or ForwardClock()
    reset_order_sequence()  # determinismo byte-a-byte del audit (order_id)
    broker = PaperBrokerAdapter(
        account_id=account_id, initial_cash=cash, clock=runner_clock
    )
    sessions = MarketSessionManager()
    sessions.register(instrument, btc_24_7_calendar())
    kill_switch = KillSwitchCoordinator(clock=runner_clock)
    engine = PaperTradingEngine(
        strategy,
        OrderManager(),
        RiskEngine(risk_cfg),
        broker,
        sessions,
        ReconciliationEngine(),
        kill_switch,
        MonitoringEngine(clock=runner_clock),
        clock=runner_clock,
        run_id=run_id,
        config=PaperEngineConfig(
            account_id=account_id,
            instrument=instrument,
            point_value=float(engine_cfg["point_value"]),
            min_size=float(engine_cfg["min_size"]),
            max_bars_in_trade=int(engine_cfg["max_bars_in_trade"]),
        ),
    )

    lineage = {
        "strategy": {
            "id": metadata.strategy_id,
            "version": metadata.version,
            "hash": FROZEN_STRATEGY_HASH,
            "params": dict(params),
        },
        "risk_operational": {
            "risk_per_trade": risk_cfg.risk_per_trade,
            "max_open_positions": risk_cfg.max_open_positions,
            "max_daily_loss": risk_cfg.max_daily_loss,
            "max_drawdown": risk_cfg.max_drawdown,
            "max_strategy_risk": FROZEN_MAX_STRATEGY_RISK,
        },
        "costs": {
            "model": "paper sin comisiones (PaperBrokerAdapter B3)",
            "frozen_final_oos_per_side": 0.15,
        },
        "source_configs": {
            "frozen": str(frozen_config_path),
            "paper": str(paper_config_path),
            "preregistration": str(preregistration_path),
        },
        "preregistration": plan["id"],
    }

    return ForwardRunner(
        engine,
        run_id=run_id,
        output_dir=output_dir,
        instrument=instrument,
        strategy_hash=FROZEN_STRATEGY_HASH,
        clock=runner_clock,
        lineage=lineage,
    )


__all__ = [
    "FROZEN_CONFIG_HASH",
    "FROZEN_STRATEGY_HASH",
    "ForwardBarRecord",
    "ForwardClock",
    "ForwardConfigError",
    "ForwardRunner",
    "build_forward_runner",
]