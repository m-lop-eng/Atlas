# Atlas — Modular Algorithmic Trading System

Plataforma de robo-trading algorítmico multi-mercado, modular y escalable.

## Principios

* La estrategia no sabe dónde se ejecutará (Broker Adapter / separación de capas).
* El Risk Engine tiene veto sobre cualquier señal.
* Validación por falsación: ningún backtest demuestra rentabilidad futura.
* Reproducibilidad, trazabilidad y control de riesgo por encima de rentabilidad histórica.

## Documentación de referencia

| Documento | Contenido |
| --- | --- |
| `00_MASTER_SPECIFICATION.md` | Visión, arquitectura, roadmap |
| `01_GENERAL_RULES.md` | Reglas generales obligatorias |
| `02_QUANT_RESEARCH_METHODOLOGY.md` | Metodología de investigación |
| `03_RISK_MANAGEMENT.md` | Framework de riesgo |
| `04_BACKTEST_VALIDATION.md` | Backtest y validación |
| `05_TRADING_KNOWLEDGE_BASE.md` | Base de conocimiento |
| `06_EXECUTION_OPERATION.md` | Ejecución y operación |
| `07_PROP_FIRM_RULES_FRAMEWORK.md` | Reglas de cuentas de fondeo |
| `08_DEVELOPMENT_WORKFLOW.md` | Estándares de desarrollo |
| `09_STRATEGY_LIFECYCLE.md` | Ciclo de vida de estrategias |

## Estructura

```text
config/        Configuración por entorno (development/paper/production)
data/          Adquisición, limpieza, validación y control de calidad de datos
research/      Hipótesis, features y experimentos
strategies/    Implementación de estrategias (base + mercados)
backtesting/   Motor de backtest (event-driven, costes realistas)
validation/    OOS, walk-forward, Monte Carlo, stress
portfolio/     Exposición agregada, correlación, asignación
risk/          Position sizing, límites y Risk Engine
execution/     Modelo de órdenes y Order Manager
brokers/       Adaptadores de broker (contrato B3): PaperBroker de sandbox
live/          Motor de datos en vivo: MarketDataAdapter + LiveDataEngine
streaming/     Track paralelo (S1–S6): WebSocket read-only + reconnect + integridad + puente a B1
sessions/      Gestión de sesiones de mercado (PRE_OPEN…UNKNOWN)
reconciliation/ Motor de reconciliación interno vs broker (B4)
killswitches/  Coordinador de kill switches globales (B5)
monitoring/    MonitoringEngine: salud por componente (B6)
papertrading/  Paper/forward: PaperTradingEngine (B7) + ForwardRunner (A4) + streaming forward (S7)
database/      PostgreSQL (modelos y sesión)
reporting/     Métricas y reportes
tests/         Unit / integration / regression
deployment/    Infraestructura y despliegue
docs/          Documentación técnica del repositorio
```

## Entornos

* `development` — sin ejecución real posible.
* `paper` — ejecución simulada con arquitectura de producción.
* `production` — solo cuando una estrategia supere todo el lifecycle.

## Estado actual

**Milestone 1 — Pipeline de investigación reproducible (en curso).**
Arquitectura, motor de backtest, riesgo, ejecución, diagnóstico por trade,
controles nulos, lineage de experimentos y datos reales funcionando.
Incluye la **capa en vivo** (B1–B7; ver «Capa en vivo»), el **forward continuo
A4** (ingesta batch; ver «Forward continuo (Fases A1–A4)»), el **track de
streaming S1–S6** (read-only; ver «Streaming (S1–S6)»), la **integración
streaming → paper/forward S7** (ver «Streaming Paper/Forward (S7)») y su
**operación con backfill REST S8** (ver «S8 — Streaming Operational Backfill»).
No hay ejecución real de órdenes todavía (solo `development`/backtest y
paper/forward simulado).

* Última sesión: **S8 — Streaming Operational Backfill (implementado y validado operacionalmente; CONGELADO)** — commits `8b27d35` (S8) y `28a778c` (bound del ciclo). Añade backfill **REST read-only** (`GET /api/v3/klines`, sin credenciales) para gaps recientes que el batch de binance.vision no cubre, inyectado en S7 sin modificar S1–S7/B1/S4/S6; driver `experiments/e19_streaming_operational.py` (namespace `experiments/streaming_operational/outputs/FORWARD-STREAM-OPS-B1/`). Verdictos: `S8_IMPLEMENTATION_PASS`, `S8_GAP_RECOVERY_OPERATIONAL_PASS` (**gap natural de 7 velas recuperado por S4/REST/B1**, `recovered=7`), `S8_CONTINUOUS_OPERATIONAL_PASS` (3 ciclos `--loop --cycles 3` con `status=completed`, `restart` idempotente, 0 duplicados) y `S8-C2-POLLDUR-001` **CLOSED** (`--cycle-timeout-seconds`, presupuesto soft: `start_seconds≈4 s` ≪ 120 s). Suite **773 passed**; A4/FINAL_OOS/H002 intactos. **C3 (scheduler/deployment) NO abierto**: queda una **decisión operativa humana pendiente** (S8 supervisado vs. desatendido). Ver «S8 — Streaming Operational Backfill» y «Cómo retomar».
* Sesión previa (`git log -1 75dba6d`/`56d7c0a`): **S7 — Streaming Paper/Forward Integration + hardening** — camino `Binance WebSocket → S2/S3/S4 → B1 → ClosedBarEvent → ForwardRunner` (paper; `PaperBrokerAdapter`); validado en real con 1 cierre (`CLOSED_BAR_OPERATIONAL_PASS`, `S7_RELEASE_AUDIT_PASS`). Ver «Streaming Paper/Forward (S7)». Suite **731→737 passed**.
* Sesión previa (`git log -1 df76310`): **cierre de la salud operacional S6** — dedup de `gaps_unrecoverable` por **firma estable** de `IntegrityReport`, `last_closed_bar` solo desde `StreamingPipeline.closed_bars` (B1) y `last_integrity_evidence` en el snapshot (reutiliza `evidence()` de S4, sin duplicar lógica). Ver «Streaming (S1–S6)». Suite **685 passed**.
* Sesión previa (retoma A4, 6 ciclos): **A4 sigue en automático y se recuperó del incidente de red** — la tarea `Atlas-Forward-A4` completó el ciclo diario programado el 29/09 00:30 local (28/09 22:30Z; `LastRunTime=29/09 0:30:01`, `LastTaskResult=0`, `NumberOfMissedRuns=0`, próxima 30/09 00:30) que **añadió 48 barras** descargando `BTCUSDT-1h-2026-09-27` sin error. Estado real de A4: **6 ciclos / 321 barras** (`2026-09-14`→`2026-09-28`), 321 `RECONCILIATION_OK`, kill switch `ALLOW` (0 episodios), monitoring `HEALTHY`, 0 UNKNOWN, equity `101334.72`, `mdd -0.007566`, integridad `ok`. El único incidente sigue siendo el `PROVIDER_ERROR` (DNS `getaddrinfo failed`) del ciclo previo (28/09 00:32 UTC), ya superado en el ciclo siguiente; el fail-safe no añadió barras ni duplicó exposición. Retoma de solo lectura (sin cambios de código). Suite **682 passed**; H002/FINAL_OOS intactos.
* Sesión previa (`git log -1 c8617d2`): **retoma operacional con A4 ejecutándose solo** — primer ciclo programado confirmado (`LastRunTime=28/09 0:44:13`); estado entonces: **5 ciclos / 273 barras** (`2026-09-14`→`2026-09-26`), 273 `RECONCILIATION_OK`, kill switch `ALLOW`, monitoring `HEALTHY`, 0 UNKNOWN. Ese ciclo registró **1 `PROVIDER_ERROR`** (DNS `getaddrinfo failed` al descargar `BTCUSDT-1h-2026-09-27.zip`; caída de red, no defecto de Atlas); documentó el track de streaming S1–S6. Suite **682 passed**.
* Sesión previa (`git log -1 1547593`): **track de streaming S1–S6 (aislado de A4)** — contrato (S1), adaptador Binance WebSocket read-only (S2), reconnect + heartbeat (S3), backfill/reconciliación de integridad sin atajo `reconnect→confirm` (S4), puente a `LiveDataEngine` con B1 como autoridad (S5) y salud operacional que solo observa (S6). Ver «Streaming (S1–S6)». Suite **682 passed**; H002/FINAL_OOS intactos.
* Sesión previa (`git log -1 f130015`): **endurecimiento del scheduler de A4 (Change 6)** — `deployment/schedule_a4.py`: `StartWhenAvailable=True`, `WakeToRun=False`, batería bloqueada, diaria 00:30, `IgnoreNew`, instalación idempotente (`-Force`), sin catch-up propio; principal `S4U` objetivo (cae a `Interactive` sin admin). Ver «Scheduler de A4 (Change 6)». Suite **595 passed**; H002/FINAL_OOS intactos.
* Sesión previa (`git log -1 72135a4`): **auditoría + hardening operacional (5 hallazgos)** — cierre de la auditoría por capas de A4/forward: latch B5 persistente (`0a528ab`), `reconcile()` B1 endurecido (`ce8ab09`), severidad multi-fuente (`e30286e`), hardening de datos (`589d3c9`) y single-instance lock + input fingerprint (`72135a4`). Ver «Auditoría operacional y hardening». Suite **583 passed**; H002/FINAL_OOS intactos.
* Sesión previa (`git log -1 e6c0c3e`): **forward continuo A4** — `ForwardRunner`
  (cadena B1–B7 por barra, persistencia idempotente, audit reproducible) + Fases
  A1–A4 sobre datos reales; driver operativo `experiments/e14_forward_continuous.py`
  (ingesta incremental programada, tarea `Atlas-Forward-A4`); fix `OP-DEFECT-A2-001`
  (precio medio FIFO del `PaperBrokerAdapter`, commit `e5c5fc4`). Ver «Forward
  continuo (Fases A1–A4)» y «Cómo retomar». Suite **535 passed**; FINAL_OOS intacto.
* Sesión previa (`git log -1 066a00d`): **construcción del dataset FINAL_OOS con adquisición real + builder** — `research/final_oos_acquisition.py` conecta `FinalOosDatasetBuilder` con binance.vision **monthly preferred / daily fallback** y es **offline-first** (`data/raw/` como caché de inputs inmutables con sha256 → el dataset se reconstruye sin volver a la red). Devuelve `BLOCKED_BY_DATA_AVAILABILITY` o `ELIGIBLE_PARA_DRY_RUN` **sin nunca autorizar ni consumir** (adquisición ≠ autorización; verificado por `tests/test_final_oos_acquisition.py`: monthly completo sin daily innecesario, monthly 404→daily, combinación monthly+daily, gap real→BLOCKED sin rellenado, reproducibilidad idempotente, no consumo, no mutación de registros). Suite **293 tests verdes** (`python -m pytest`); el perímetro congelado del FINAL_OOS sigue intacto.
* Sesión previa (`git log -1 c23cbd3`): **semántica del desbloqueo del FINAL_OOS** (min_span 365d: un zip mensual NO desbloquea; monthly/daily = solo adquisición) verificada por `tests/test_final_oos_semantics.py`.
* Sesión previa (`git log -1 21ae515`): **infraestructura del FINAL_OOS** (estado `BLOCKED_BY_DATA_AVAILABILITY`: requiere cobertura completa ≥ 365 días de datos nuevos, fin ≥ 2027-09-01 — ver «Semántica del FINAL_OOS»; el dump mensual 2026-09, aún 404, NO desbloquea el OOS): pre-registro congelado (`research/decisions/FINAL_OOS_PRE_REGISTRATION.json`, estado FROZEN, criterios de lectura NULL de falsación), config de ejecución congelada (`config/final_oos/config.yaml`, `config_hash=cb639bbf…`, strategy hash `491ed76d…` idéntico a H004/H004-B), pipeline mecánico (`research/final_oos_pipeline.py`) verificado por tests de integración con dataset sintético (`tests/test_final_oos_pipeline.py`) y validación mecánica del plan (`tests/test_final_oos_preregistration.py`), y runner `experiments/e12_final_oos.py` (modo status/preparación/autorización; el consumo es irreversible). Corregido además `data/quality.py` (`_ts_delta_seconds` ahora UTC-aware).
* Nuevo (cierre H004-B): `research/evidence.json` registra **H004-B → `RESEARCH_REQUIRED`** con nota `SYSTEMATIC_INSTABILITY` (6/12 folds positivas, mediana +0.3%, media +5.4%, concentración ALTA, 3 vecindades negativas); notas de H004 y H002 ampliadas; `research/decisions/H004_WALK_FORWARD.json` incorpora `h004b_closure` embebiendo `temporal_stability`; `research/decisions/H002_LONG_SHORT_ASYMMETRY.json` añade `temporal_stability_followup` (la inestabilidad refuerza el caveat de beta direccional; no autoriza BOTH/SHORT). Ver `git log -1 fa8b51a`.
* Nuevo: `experiments/e11_h004b_diagnosis.py`, `experiments/outputs/h004b_diagnosis.json` y
  `research/decisions/H004B_RESULT.json` (con el pre-registro embebido como prueba
  de que el criterio existía antes de ver los números). `research/decisions/H004B_PRE_REGISTRATION.json`
  queda en `estado: COMPLETE` con esquema y criterios intactos.
* Suite de tests: **293 passed** (`python -m pytest`).
* Nuevo: `research/evidence.py` + `research/evidence.json` (estado de evidencia por
  hipótesis `HYPOTHESIS → RESEARCH_REQUIRED → ROBUSTNESS_SUPPORTED → FINAL_OOS_PENDING
  → FINAL_OOS_PASSED/FAILED → PAPER`, ciclo de vida de `FINAL_OOS`
  `BLOCKED_BY_DATA_AVAILABILITY → DECLARED → CONSUMED` y `evidence.guard` combinado
  con `DATA_ROLE` para impedir reutilización de OOS/crear FINAL_OOS sin datos nuevos)
  y `tests/test_evidence.py`.
* Nuevo: `research/decisions/H004B_PRE_REGISTRATION.json` — esquema **congelado
  antes de ejecutar** de H004-B como DIAGNOSTICO (no ciclo de rendimiento):
  12 ventanas trimestrales re-ancladas 2021-04→2024-04, `fixed-parameter`
  (estrategia fija × ventana, NO walk-forward que recalibre), semántica de
  límites `[start,end)` igual a e9/e10, criterios de lectura pre-escritos y
  validación mecánica en `tests/test_h004b_scheme.py`. Estado `COMPLETE`
  (ejecutado; lifecycle `PRE_REGISTERED → AUTHORIZED/RUN_IN_PROGRESS → COMPLETE`,
  esquema intacto).
* Nuevo: `experiments/e10_h005_w2_diagnosis.py` y `research/decisions/H005_W2_DIAGNOSIS.json`
  (causa de W2: beta direccional adversa del filtro LONG en un bear estructural, ver más abajo).
* Nuevo: `research/data_role.py` (rol `DEVELOPMENT/VALIDATION/FINAL_OOS/OBSERVED`
  + guard anti-reutilización de OOS), `research/data_roles.json`,
  `experiments/e9_h004_walk_forward.py` y `tests/test_data_role.py`.
* `research/benchmarking.py` (de H003): B&H normalizado, B&H por exposición,
  control de exposición temporal neutra, régimen exógeno + `tests/test_benchmarking.py`.
* Motor: `ENGINE_VERSION = 2` (reset diario de `max_daily_loss` + monetización
  por `point_value`; invariante para `point_value=1`, por eso el grafo H001
  conserva sus `experiment_id`).

### Capa en vivo (B1–B7, sesión actual)

Arquitectura de producción (B1–B7). Sigue sin ejecución real de órdenes ni
broker/capital real; el entorno **paper/forward ya opera en continuo** (Fase A4,
ver «Forward continuo (Fases A1–A4)»):

* **B1 — Market Data (`live/`, commit `26842c6`)**: `MarketDataAdapter`
  (contrato), `BackfillProvider` y `LiveDataEngine` con `EngineState` y
  `DataQuality`. Consumo determinista de eventos de mercado → emisión de
  `ClosedBarEvent`s; reloj inyectable para staleness; nunca fabrica cierres
  falsos.
* **B2 — Sessions (`sessions/`, commit `18f811b`)**: `MarketSessionManager`
  con el autómata de sesión (`PRE_OPEN/OPEN/CLOSING/CLOSED/HALTED/UNKNOWN`) y
  transiciones temporalmente deterministas.
* **B3 — Broker sandbox (`brokers/`, commit `e158863`)**: contrato
  `BrokerAdapter` (connect/subscribe/get_order/health/…) + `PaperBroker` de
  simulación con reloj inyectado; modelos de broker y errores
  (`BrokerConnectionError`, …).
* **B4 — Reconciliación (`reconciliation/`, commit `9f820a5`)**: motor que
  compara el estado interno (órdenes/fills/posiciones/cuenta) contra el
  broker — fuente de verdad — y emite `RECONCILIATION_OK/BLOCKED/UNKNOWN`
  (nunca un MATCH falso). `ReconciliationOrchestrator` hace solo lecturas;
  los fills autoritativos se adoptan vía `get_order` y el resolutor de
  órdenes UNKNOWN queda auditable. Cualquier discrepancia bloquea nuevas
  operaciones (06 §59/§67).
* **B5 — Kill Switches (`killswitches/`, commit `0face53`)**:
  `KillSwitchCoordinator` con `ALLOW/BLOCK_NEW_ORDERS/HALT/EMERGENCY`,
  fuentes auditadas y **latch**: una condición despejada NO auto-desbloquea;
  exige `recover(reason, health_checks_ok=True)` explícito. Sin liquidación
  automática y sin API de órdenes.
* **B6 — Monitoring (`monitoring/`)**: `MonitoringEngine` con salud
  independiente por componente (`DATA_FEED/BROKER/RECONCILIATION/SESSION/
  SYSTEM/EXECUTION`). Estados `HEALTHY/DEGRADED/FAILED/UNKNOWN` (UNKNOWN
  nunca es HEALTHY). Freshness real: `last_success` vs `now` vs umbrales
  **configurables** (nunca constantes enterradas); reloj UTC-aware
  obligatorio (DST). `apply_to_kill_switch`: **el monitor detecta, B5
  decide** (la precedencia y el latch siguen en B5, sin duplicarse). El
  monitor nunca envía/cancela órdenes.

* **B7 — Paper Trading (`papertrading/`)**: orquestador paper de extremo a
  extremo. `PaperTradingEngine` consume `ClosedBarEvent` y ejecuta la cadena
  completa por barra: sesión (guard de entrada) → señal sobre la barra cerrada
  (misma interfaz que el backtest) → RiskEngine (veto, tamaño por `point_value`)
  → Kill Switch → `OrderManager` → `PaperBrokerAdapter` → fills/posición/equity
  → reconciliación → monitoring ↺. Temporalidad anti-lookahead idéntica al
  backtest (decisión en `t`, orden al OPEN de `t+1`). Exige un broker con
  `price_source` (`TypeError` en `__init__` si falta): nunca envía a un broker
  real. Reiniciable sin duplicar exposición (`signed_signals` + `RESTART_SKIP`),
  audit trail reconstruible por ID (`trail`), fills parciales/VWAP, submission
  UNKNOWN resuelto sin reenvío y fail-safe de reconciliación. Sin liquidación
  automática por sesión CLOSED (solo stop/time). `ReplayMarketDataAdapter`
  para dry-runs sin red.

Suite actual: **731 tests verdes** (`python -m pytest`). El perímetro
congelado del FINAL_OOS sigue intacto.

### Forward continuo (Fases A1–A4)

Operación Paper/Forward sobre la estrategia **congelada H002** (FINAL_OOS
intacto; principio rector: `operational evidence ≠ strategy evidence`). Código:

* **`papertrading/forward_runner.py`** (`ForwardRunner` + `build_forward_runner`,
  commits `956934e`/`e5c5fc4`): runner por barra de la cadena B1–B7 con
  persistencia idempotente y audit reproducible. `start()` **reproduce
  `bars.jsonl` desde cero** (reconstruye el engine y deduplica → un restart no
  duplica barras ni exposición). Guardas congeladas: `strategy_hash=491ed76d…`,
  `config_hash=cb639bbf…`, riesgo paper ≤ 1%, `PaperBrokerAdapter` obligatorio.
  Persiste `bars.jsonl` (fuente de verdad log-first), `records.ndjson`,
  `audit.jsonl`, `snapshot.json` y `manifest.json`; `_atomic_replace()` reintenta
  ante locks transitorios de OneDrive/AV.
* **`experiments/e13_forward_paper.py`**: replay OHLC → runner (driver de fase).
* **`experiments/e14_forward_continuous.py`** (commit `e6c0c3e`): **driver
  operativo de A4**. Un ciclo reconstruye el runner desde su `bars.jsonl` y
  enruta solo las velas 1h nuevas del proveedor real por **B1**
  (`LiveDataEngine` + `ReplayMarketDataAdapter` + `BackfillProvider`) hacia B2–B7;
  emite `report_a4.json`, `ops_log.ndjson` e `incidents.ndjson`. `--once`
  (idempotente), `--status` (solo lectura), `--loop`. Tarea programada
  **`Atlas-Forward-A4`** (diaria 00:30) sobre `experiments/forward/outputs/`
  (artefactos gitignored).

Reglas vigentes de A4: H002 no se modifica; no se optimizan parámetros ni se
seleccionan estrategias; no se abre H006; no se toca FINAL_OOS; sin broker ni
capital real; los resultados forward se **registran**, nunca recalibran H002.

| Fase | Alcance | Resultado |
| --- | --- | --- |
| A1 Smoke | replay corto | OK |
| A2 24 h | 48 barras reales (ventana) | 168/168 `RECONCILIATION_OK`; descubrió `OP-DEFECT-A2-001` |
| A3 7 d | 168 barras reales (ventana) | 12/12 criterios operativos OK |
| A4 continuo | ingesta incremental real | **EN CURSO** (tarea diaria) |

**Incidente `OP-DEFECT-A2-001`** (A2, barra `2026-09-21T10:00Z`): `PaperBrokerAdapter._avg_entry`
promediaba *todos* los fills del lado de la posición, incluidos los de un trade
ya cerrado → `POSITION` MISMATCH interno (`83760.0`) vs broker (`82411.18`) →
`RECONCILIATION_BLOCKED` → latch de B5 (sin auto-recovery). El fail-safe B4/B5
actuó correctamente; el defecto era de representación de estado del broker. Fix
FIFO sobre el neto abierto (commit `e5c5fc4`) + 2 regresiones
(`test_broker_adapter::…::test_avg_entry_only_open_quantity_fifo`,
`test_forward_runner::test_multi_trade_session_keeps_reconciliation_ok`).
Tras el fix: A2 168/168 OK; A3 288/288 OK (0 UNKNOWN, 0 auto-recovery,
kill switch siempre `ALLOW`, replay byte-identical).

### Auditoría operacional y hardening (5 hallazgos)

Auditoría por capas (solo lectura) del forward/A4 previa a la fase continua. Cinco
hallazgos corregidos en **commits aislados**, sin tocar H002/FINAL_OOS ni las
reglas de trading:

| # | Hallazgo | Fix | Commit |
| --- | --- | --- | --- |
| 1 | El latch B5 no sobrevivía al restart del forward (un latch MANUAL/SYSTEM/BROKER se perdía) | estado B5 persistido/restaurado en `snapshot.json`; sin auto-recovery; solo `recover()` explícito | `0a528ab` |
| 2 | `LiveDataEngine.reconcile()` emitía backfill sin validar (duplicados, out-of-order, vela no cerrada, gap interno) y marcaba HEALTHY | validación símbolo→ts/intervalo→OHLC→cerrada→orden→duplicados→contigüidad; inválido ⇒ `DEGRADED` (nueva `INCOMPLETE_BAR`) | `ce8ab09` |
| 3 | `apply_to_kill_switch` usaba último-escritor dentro de la misma fuente (EMERGENCY degradado por HALT de EXECUTION) | agregación por **máxima severidad** por fuente; B5 sigue decidiendo | `e30286e` |
| 4 | Adquisición: `_existing_dataset` no verificaba sha/rango, raw no atómico, filas/volumen sin validar | integridad real (sha+conteo+extremos+intervalo), raw atómico (tmp+fsync+replace), validación de filas/volumen; causas `dataset_integrity`/`corrupt_file`/`coverage_incomplete`/`acquisition_error` | `589d3c9` |
| 5 | A4 sin lock de instancia ni fingerprint de input | `ForwardInstanceLock` (`.forward.lock`, lock del SO) + `input_fingerprint` en `manifest.json` (prefijo append-only; mismatch ⇒ fail-safe) | `72135a4` |

Aparcado (no bloqueante con adquisición batch): `reconnect()` desde `DEGRADED`.
Suite tras el cierre: **583 passed**. **A4 demuestra operación forward/paper, no
valida H002 para capital real** (el gate sigue siendo el FINAL_OOS preregistrado).

### Scheduler de A4 (Change 6)

`deployment/schedule_a4.py` instala/actualiza la tarea Windows
**`Atlas-Forward-A4`** que ejecuta `e14_forward_continuous.py --once`. Cambio
aislado (commit `f130015`): no toca e14/ForwardRunner/H002/FINAL_OOS.

| Parámetro | Objetivo |
| --- | --- |
| `StartWhenAvailable` | True (Windows reejecuta la tarea perdida cuando el equipo vuelve) |
| `WakeToRun` | False |
| batería | bloqueada (`DisallowStartIfOnBatteries`/`StopIfGoingOnBatteries` = True) |
| frecuencia | diaria 00:30, un único trigger |
| `MultipleInstances` | `IgnoreNew` |
| reinstalación | idempotente (mismo nombre + `Register-ScheduledTask -Force`, no duplica) |
| principal | objetivo `S4U` (sin sesión interactiva); **en este equipo queda `Interactive`** porque S4U requiere elevación |

**No hay catch-up propio**: A4 no implementa recuperación de ciclos perdidos;
Windows recupera la ejecución y A4 procesa exactamente un ciclo (sin duplicar
barras: el runner deduplica por `open_time`). Con `Interactive`, un ciclo perdido
se recupera al volver el equipo **con sesión iniciada**; para ejecutar sin sesión
hay que reinstalar como admin (`python -m deployment.schedule_a4 install`).

Las **321 barras** actuales se reparten en **6 ciclos**: 3 smoke manuales el
26/09 14:05 y, desde el despliegue, ciclos lanzados por la tarea (los dos
últimos automáticos: 28/09 00:32 UTC y 28/09 22:30 UTC = 29/09 00:30 local). Son
validación operativa inicial, no evidencia acumulada; esta empieza con ciclos
reales separados en el tiempo (que añadan barras, que no las añadan, que
reinicien, y que recuperen alguno). El ciclo automático ya demostró tres cosas:
A4 corre sin intervención, su fail-safe tolera una caída de red del proveedor
(`PROVIDER_ERROR` → 0 barras nuevas, sin duplicar exposición) y **se recupera**
en el ciclo siguiente (48 barras nuevas, sin incidente).

### Streaming (S1–S6, track paralelo a A4)

Track de datos en vivo por **WebSocket, completamente aislado de A4**: no
modifica `live/` (B1), no cambia H002 y no toca FINAL_OOS. Objetivo: construir y
probar, sin arriesgar A4, el camino que permitirá a Atlas trabajar con datos
actuales. Principio rector: **`S6 observa; no autoriza trading`**. Pasos en
commits aislados:

| Paso | Alcance | Commit |
| --- | --- | --- |
| **S1** | Contrato `StreamingMarketDataAdapter` + eventos (`StreamData`/`Heartbeat`/`Connected`/`Disconnected`) + errores tipados + reloj UTC-aware + sandbox determinista | `1abb4c9` |
| **S2** | Adaptador **Binance WebSocket read-only** (`BinanceStreamingAdapter`): protocolo público de klines, normalización a `MarketDataEvent`, `is_closed`, transporte desacoplado por `Protocol` | `30a96a6` |
| **S3** | `ReconnectManager`: backoff determinista, heartbeat ping/pong, límite de intentos, resubscribe, sin conexiones duplicadas, dedup de cierres y **`data_integrity_unknown`** | `8c61dfe` |
| **S4** | Backfill + reconciliación de integridad (`IntegrityReconciler`/`IntegrityCoordinator`): rango perdido, validación/orden/dedup/conflictos/continuidad, **sin atajo `reconnect→confirm`** | `2b4128a` |
| **S5** | Integración con B1 (`B1StreamingAdapter`, `IntegrityBackfillProvider`, `StreamingPipeline`): todos los datos pasan por `LiveDataEngine` | `92add33` |
| **S6** | Salud operacional (`StreamingHealthMonitor`/`StreamingHealthSnapshot`): diagnóstico estructurado, **solo observa** | `1547593` |

Pipeline:

```text
Binance WebSocket → S2 adapter → S3 ReconnectManager → S4 integridad
    → B1StreamingAdapter → LiveDataEngine (AUTORIDAD) → ClosedBarEvent
```

Tras una interrupción:

```text
gap → B1 DEGRADED → integridad UNKNOWN → backfill (S4)
      ├── no HEALTHY → [] → B1 sigue DEGRADED (no emite)
      └── HEALTHY → B1.reconcile() → velas contiguas → ClosedBarEvent
```

Reglas preservadas (verificadas por tests):

* **B1 sigue siendo la autoridad**: ningún dato llega a estrategia sin pasar por
  `LiveDataEngine`; no hay un segundo camino paralelo que lo sortee.
* **Transporte ≠ integridad**: reconectar no autoriza; `data_integrity_unknown`
  solo lo limpia un reporte S4 HEALTHY. **No existe** el atajo
  `reconnect() → confirm_data_integrity()`. Si el backfill falla, B1 permanece
  `DEGRADED` y no emite barras recuperadas como si fueran normales.
* **Read-only por diseño**: sin claves API ni ninguna vía de órdenes/escritura
  (verificado por test de fuentes).
* **S6 observa, no decide**: `StreamingHealthSnapshot.level` es informativo; no
  hay campos de autorización (`can_trade`/`authorized`) ni un segundo kill
  switch. La autorización seguirá en B1/B5/monitoring/risk.

Código: `streaming/` (`adapter.py`, `events.py`, `errors.py`, `clock.py`,
`sandbox.py`, `transport.py`, `binance.py`, `backoff.py`, `reconnect.py`,
`integrity.py`, `bridge.py`, `health.py`); drivers smoke read-only
`experiments/e15_binance_ws_smoke.py` (S2),
`experiments/e16_binance_reconnect_smoke.py` (S3) y
`experiments/e17_streaming_pipeline_smoke.py` (S5). **87 tests deterministas**
del track (S1–S6). La integración con `ForwardRunner`/`PaperBroker` se cerró en
**S7** (ver «Streaming Paper/Forward (S7)»).

### Streaming Paper/Forward (S7, commit `75dba6d`)

Integra el pipeline de streaming con la ejecución paper/forward, **sin segundo
camino** y sin tocar A4/H002/FINAL_OOS. Código:

* **`papertrading/streaming_forward.py`**: `StreamingForwardRunner` (orquesta
  `StreamingPipeline` → `ForwardRunner`), `StreamingBackfillProvider` (traduce
  `DataFetchError` → `ConnectionError` para que S4 marque `BLOCKED`, fail-safe) y
  `load_seed_bar`/`SeedError` (reconstruyen la última `ClosedBarEvent` desde
  `bars.jsonl`, incluido `emission_sequence`).
* **`experiments/e18_streaming_forward.py`**: CLI `--once`/`--loop`/`--status`
  (sin scheduler), lock `.streaming.lock`, namespace propio
  `experiments/streaming/outputs/FORWARD-STREAM-B1/` con `streaming_manifest.json`
  (provenance separada), `streaming_health.ndjson` (S6), `ops_log.ndjson`,
  `incidents.ndjson`, `report_stream.json` + artefactos del `ForwardRunner`
  (`bars.jsonl`, `records.ndjson`, `audit.jsonl`, `snapshot.json`, `manifest.json`).
* **Warm-start** (commit `5a0760b`): `runner.start()` → seed desde `bars.jsonl` →
  `pipeline.connect()` → `pipeline.warm_start(seed)` (silencioso: no emite, no
  genera callback, no entra en `closed_bars` ni en S6). El seed detecta huecos
  **entre reinicios**, que B1 no vería en un arranque limpio.
* **Invariantes**: único camino
  `WebSocket → S2/S3/S4 → B1 LiveDataEngine → ClosedBarEvent → ForwardRunner`;
  B1 es la única autoridad de `ClosedBarEvent`; solo `PaperBrokerAdapter`; S6
  observa y **nunca** toca B5 ni autoriza; A4 batch y FINAL_OOS intactos.

Validación operacional (paper-only, real, 2 sesiones `--once`):

| Sesión | `processed` | intrabar | cierre | `ClosedBarEvent` | `bars.jsonl` | `records` | dup | incidentes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 (16:49Z) | 100 | 100 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 (16:58Z→17:00Z) | 200 | 199 | **1** | **1** | **1** | **1** | 0 | 0 |

Cierre real capturado: `BTCUSDT` `open_time=16:00:00Z` → `close_time=17:00:00Z`,
`interval_seconds=3600`, `emission_sequence=1`. Resultado:
**`CLOSED_BAR_OPERATIONAL_PASS`** y **`S7_RELEASE_AUDIT_PASS`**. Requiere la
dependencia opcional `websocket-client` (`pip install "atlas[streaming]"`).

La captura de cierre y el hardening de arranque/cierre de S7 se cerraron en
`75dba6d`/`56d7c0a`. El **backfill oportuno** para gaps recientes se añadió en
**S8** (ver «S8 — Streaming Operational Backfill»).

### S8 — Streaming Operational Backfill (commits `8b27d35` + `28a778c`, CONGELADO)

Resuelve la limitación operacional de S7: el backfill basado en
`BinanceVisionDailySource` (~1 día de retraso) no puede cubrir un gap reciente,
dejando B1 `DEGRADED`. S8 añade una fuente **REST read-only** y la inyecta como
`BackfillProvider` en S7 **sin modificar S1–S7/B1/S4/S6**.

* **`data/binance_rest.py`** — `BinanceRestKlinesSource`: `GET /api/v3/klines`
  (`https://api.binance.com`), sin auth; normaliza a barras Atlas, filtra rango,
  alineación y **vela cerrada** (`open_time + interval <= now`), página acotada
  (`limit<=1000`, `max_pages`), timeout por request, retry acotado, 404 sin
  reintento, errores tipificados `DataFetchError`. Reutiliza `_http_get_bytes`.
* **`papertrading/streaming_backfill_rest.py`** — `RestStreamingBackfillProvider`
  (`BackfillProvider`): mapea a `MarketDataEvent(is_closed=True)` y traduce
  `DataFetchError → ConnectionError` (S4 `BLOCKED`, fail-safe). Nunca emite
  `ClosedBarEvent`; solo alimenta `IntegrityReconciler.reconcile_open_ended`.
* **`experiments/e19_streaming_operational.py`** — driver independiente (no toca
  e18/S7): namespace `experiments/streaming_operational/outputs/FORWARD-STREAM-OPS-B1/`,
  `--once/--loop/--status`, lock propio, provenance (`binance-rest-klines`),
  `incidents`/`ops_log`/`report_stream`; shutdown `disconnect()`+`checkpoint()`.
  Commit `28a778c` añade **`--cycle-timeout-seconds`** (presupuesto **soft**,
  comprobado entre `poll_once()`), timeouts de conexión/recepción por CLI y
  `start_seconds`; el agotamiento registra `status="budget_exhausted"` +
  `completed=false` + incidente `CYCLE_BUDGET_EXHAUSTED` (nunca como ciclo
  completado). Sin scheduler.

**Validación operacional real** (paper-only, namespace S8):

| Verdicto | Evidencia |
| --- | --- |
| `S8_IMPLEMENTATION_PASS` | implementación + 773 tests verdes |
| `S8_GAP_RECOVERY_OPERATIONAL_PASS` | gap **natural** de 7 velas recuperado por S4/REST/B1 (`recovered=7`, `status=HEALTHY`) |
| `S8_CONTINUOUS_OPERATIONAL_PASS` | 3 ciclos `--loop --cycles 3`, `status=completed`, `restart` idempotente, 0 duplicados, 0 incidentes |
| `S8-C2-POLLDUR-001` **CLOSED** | ciclo real `completed`, `start_seconds≈4 s` ≪ 120 s |

**Deuda registrada (no bloquea el cierre del milestone)**:

* **Soft timeout**: `--cycle-timeout-seconds` no interrumpe un `poll_once()` en
  curso; no es hard deadline. Solo se reabriría si aparece evidencia de
  insuficiencia.
* **429/`Retry-After`** real: no caracterizado (no se fabrica un 429).
* **`ts == end`**: desviación non-blocking conocida (decisión futura explícita).
* **Crash/kill real**: no ejercitado.
* **Scheduler**: **no implementado** (C3 no abierto).

### Infraestructura de datos

`data/sources.py` define `DataSource` + `DataManifest` (sha256 + rango + params):
el backtester no sabe de dónde vienen los datos.

| Proveedor | Fuente | Profundidad |
| --- | --- | --- |
| `KrakenSource` | API pública Kraken | ~720 barras (~30 días) |
| `BinanceVisionSource` | `data.binance.vision` (klines mensuales) | histórica completa |
| `BinanceVisionDailySource` | `data.binance.vision` (klines diarias 1h) | mecanismo diario de adquisición |
| `CsvSource` | CSV local normalizado | — |
| `ParquetSource` | pyarrow (opcional, futuro) | — |

El orquestador `research/final_oos_acquisition.py` (commit 3) automatiza la
adquisición del dataset congelado del FINAL_OOS:

```text
Binance Vision → monthly (preferred) / daily (fallback)
              → data/raw/ (inputs inmutables con sha256; offline-first)
              → FinalOosDatasetBuilder (lee SOLO de data/raw/)
              → quality + coverage + lineage → dataset congelado (CSV + manifest)
              → BLOCKED_BY_DATA_AVAILABILITY | ELIGIBLE_PARA_DRY_RUN
```

`python research/final_oos_acquisition.py` reporta el estado (nunca autoriza ni
consume: adquisición ≠ autorización; dataset disponible ≠ FINAL_OOS consumido;
verificado por `tests/test_final_oos_acquisition.py`).

Datasets (CSV gitignored; manifest JSON versionado):

* `experiments/data/market_BTCUSDT_60min.csv` — 67.141 barras, 2019-01 → 2026-08.
* `experiments/data/market_XXBTZUSD_60min.csv` — 721 barras (Kraken, 30 días).

```powershell
python data/market.py binance BTCUSDT 2019-01-01   # histórico profundo
python data/market.py kraken  XXBTZUSD 60 2019-01-01
```

### Ciclos de investigación

| Ciclo | Pregunta | Estado | Evidencia |
| --- | --- | --- | --- |
| **H001** | ¿El breakout supera controles nulos? | `RESEARCH_REQUIRED` | `research/decisions/ATLAS_FIRST_BREAKOUT.json` |
| **H002** | ¿Existe asimetría LONG vs SHORT? | `RESEARCH_REQUIRED` (observación persistente) | `research/decisions/H002_LONG_SHORT_ASYMMETRY.json` |
| **H003** | ¿Breakout LONG o beta de BTC? | `RESEARCH_REQUIRED` (Caso A: timing preliminar, no beta pura) | `research/decisions/H003_BREAKOUT_VS_BUYHOLD.json` |
| **H004** | ¿Es estable temporalmente el timing LONG? | `RESEARCH_REQUIRED` (ventaja en 4/4 validaciones pero inestable) | `research/decisions/H004_WALK_FORWARD.json` |
| **H005-W2** | ¿Qué característica del entorno de 2022 explica la pérdida? | `RESEARCH_REQUIRED` (causa documentada: beta direccional adversa) | `research/decisions/H005_W2_DIAGNOSIS.json` |
| **H004-B** | ¿Es la inestabilidad LOCALIZADA (2022-2023) o SISTEMÁTICA? | `RESEARCH_REQUIRED` — cierre: **SISTEMÁTICO** (inestabilidad temporal; 6/12 folds positivas) | `research/decisions/H004B_RESULT.json` |

* **H001**: en sintético (GBM) breakout ≈ random; en BTC 1h real breakout > random
  pero sin edge neto con solo 30 días. No se optimizaron parámetros.
* **H002**: split temporal 75/25 (test untouched). Contraste LONG−SHORT por trade
  **persiste en OOS** (train +1170$ → test +252$). La señal bruta (gross) es
  positiva en el histórico profundo. **Caveat**: BTC 2019–2026 es alcista
  estructural → la asimetría puede ser beta direccional, no edge idiosincrático.
* **H003**: tratamiento `direction=long` **idéntico** a H002-A (cero parámetros
  tocados). Contra benchmark B&H (100% expuesto), B&H escalado a la misma
  exposición de capital, y control de exposición temporal **neutra** a señales.
  Breakout LONG > B&H-scaled y > control neutro en train **y** test, con ~mitad
  de drawdown que B&H puro. Por régimen: neto positivo en bull/bear/lateral en
  train; en test BULL es el único régimen neto negativo. **Conclusión**
  `Caso A` (evidencia preliminar de timing) pero el test 2024–2026 ya fue
  observado por H002 → no es OOS desconocido.
* **H004**: **walk-forward de parámetros fijos** (sin recalibrar, sin seleccionar
  por ventana). Config congelada = H002-A/H003-A (`params_hash` idéntico). Cuatro
  validaciones ~1 año dentro de `DEVELOPMENT` (2021, 2022, 2023, 2024→oct).
  Resultado: el breakout LONG supera al control neutro y al B&H escalado en
  **4/4** ventanas (delta vs neutro +10 a +33 pp), `+ + - ++`, media +21.7%,
  mediana +17.9%, 3/4 positivas. **Caveat**: W2 (2022) es netamente negativa en
  bull+bear+lateral y W3 aporta el 63.7% del P&L positivo → robustez temporal
  **no** demostrada pero tampoco rechazada. El fallo BULL de H003 no recurre de
  forma sistemática (solo W2). `previous_oos_consumed=true`, `final_oos_consumed=false`.
* **DATA_ROLE** (nuevo en H004): `research/data_role.py` etiqueta cada tramo del
  dataset (`DEVELOPMENT/VALIDATION/FINAL_OOS/OBSERVED`) y el `guard_final_oos`
  impide presentar el tramo OBSERVED (2024-10→2026-08) como OOS desconocido.
  No hay FINAL_OOS limpio: requiere datos nuevos antes de declararlo.
* **H004-B** (ejecutado): **estrategia fija × 12 ventanas trimestrales re-ancladas**
  (2021-04 → 2024-04), cold-start por ventana, sin recalibración ni selección.
  Resultado trimestral: `-4.6 +5.4 -9.5 -4.3 -14.4 -3.6 +11.1 +56.4 +4.3 -24.5 +25.9 +23.0`
  (2021Q2→2024Q1), media +5.4%, mediana +0.3%, **6/12 positivas** y **3 vecindades
  negativas** (2021Q2 · 2021Q4→2022Q3 · 2023Q3). A la granularidad trimestral la
  inestabilidad **no se localiza** en 2022-2023: es un patrón recurrente
  intercalado con trimestres fuertes (`+56.4%` en 2023Q1). VEREDICTO pre-registrado:
  **SISTEMÁTICO**. Diagnóstico de ubicación: no modifica la estrategia, no abre
  H006, no desbloquea el FINAL_OOS.
* **H005-W2**: **diagnóstico de la causa de W2 (2022)** con config congelada.
  Conclusión: W2 no invalida el edge, lo expone. La pérdida (-12.0%, PF 0.84)
  procede de **beta direccional adversa** (mercado -64.5% en 2022), no de:
  trades extremos (bottom-5 = 6.6% del gross-, stop medio *menor* que el resto),
  costes (0.09% del equity) ni un evento concentrado (7/12 meses negativos).
  Evidencia: win rate 23% (vs 26-32%), avg win $2.280 (vs hasta $5.819), MFE/MAE
  1.42 (el más bajo) con mediana MFE < MAE (sin follow-through alcista), TIME-exits
  ganan 78% (vs 87-91% en W1/W3/W4). Operando **ambas** direcciones 2022 ≈ -2.7%
  (PF 0.98): la lógica breakout no desaparece; el filtro LONG concentra el riesgo
  en un bear. Incluso perdiendo, LONG supera a B&H puro (-64%), B&H escalado
  (-13%) y control neutral (-30%) dentro de W2. **Implicación**: limitación
  estructural es la restricción LONG (H002). Sin cambio de estrategia (prohibido
  hasta FINAL_OOS limpio).

### Semántica del desbloqueo del FINAL_OOS

**Qué desbloquea la evaluación** (congelado en `FINAL_OOS_PRE_REGISTRATION.json` y
verificado por `tests/test_final_oos_preregistration.py`):

* `FINAL_OOS_START = 2026-09-01T00:00 UTC` (`start_epoch=1788220800`): primer epoch de
  datos limpios tras el tramo OBSERVED.
* `MIN_SPAN = 365 días` (`min_span_seconds=31536000`).
* Se exige `end_epoch - start_epoch >= MIN_SPAN` (`regla_de_fin` en el preregistro y
  `preflight` en `research/final_oos_pipeline.py`), siendo `end_epoch` la última hora
  completa del manifest (`derive_end`).
* Por tanto: `FINAL_OOS_END >= 2027-09-01T00:00 UTC` (i.e. la última barra del manifest
  debe ser `>= 2027-08-31T23:00 UTC`).

Consecuencias operacionales:

* **El dump mensual de 2026-09 NO desbloquea el FINAL_OOS.** Aunque existiera, daría
  `[2026-09-01, 2026-10-01)` ≈ 30 días < 365 días → sigue `BLOCKED_BY_DATA_AVAILABILITY`.
* Los dumps **monthly/daily de binance.vision son únicamente mecanismos de adquisición**:
  Atlas no debe saber (ni le importa) si una vela viene de un zip mensual o de zips
  diarios. La unidad que importa es el dataset congelado (CSV + manifest sha256).
* El **primer momento ejecutable** depende de disponer de cobertura completa hasta
  `2027-09-01T00:00 UTC` (el dump mensual de 2027-08, ~publicado en 2027-09).

Matriz documental (misma aritmética que `preflight`, `matriz = [inicio, fin)`):

| rango derivado | span | resultado |
| --- | --- | --- |
| `[2026-09-01, 2026-10-01)` | ~30 días | `BLOCKED` |
| `[2026-09-01, 2027-01-01)` | ~122 días | `BLOCKED` |
| `[2026-09-01, 2027-08-31)` | ~364 días | `BLOCKED` |
| `[2026-09-01, 2027-09-01)` | 365 días | `eligible` |

La matriz queda verificada mecánicamente por `tests/test_final_oos_semantics.py`.

### Cómo retomar

Punto de partida de la próxima sesión: **S8 (Streaming Operational Backfill)
CONGELADO en `28a778c`** — implementado, validado operacionalmente y auditado
(`S8_IMPLEMENTATION_PASS`, `S8_GAP_RECOVERY_OPERATIONAL_PASS`,
`S8_CONTINUOUS_OPERATIONAL_PASS`, `S8-C2-POLLDUR-001` CLOSED), **S7 cerrado**,
**A4 (forward continuo) EN MARCHA y ejecutándose solo**, **track de streaming
S1–S6 cerrado** y **auditoría de los 5 hallazgos + scheduler A4 endurecido**;
todo commiteado (último commit `28a778c`) y working tree limpio salvo `README.md`
(documentación) y artefactos gitignored. **No hay milestone nuevo abierto.**

**Punto de decisión pendiente (NO técnico, operativo)**: definir si la operación
de S8 será **supervisada** (usar `--loop --cycles N` + runbook, sin scheduler;
probablemente no requiere C3) o **desatendida** (abrir **C3-SPEC** de
deployment/scheduler **antes** de tocar `deployment/`). **No** implementar
scheduler por inercia. Si es supervisada, cerrar S8 y pasar al siguiente
milestone funcional/arquitectónico; si es desatendida, redactar y aprobar
C3-SPEC primero (scheduler/frecuencia, duración/presupuesto, arranque/parada,
reboot, lock huérfano, logs/retención, `BLOCKED`/`UNRECOVERABLE`, intervención
humana, aislamiento de A4, recuperación tras proceso muerto).

Contexto fijado:
* B1–B7 + `ForwardRunner` cerrados; Fases A1/A2/A3 aprobadas; **A4 desplegado y
  confirmado en automático** (tarea `Atlas-Forward-A4`, ciclos del 28/09 00:32 UTC
  y del 29/09 00:30 local = 28/09 22:30 UTC, este último ya en 6 ciclos / 321
  barras) como ingesta incremental programada (batch: el `MarketDataAdapter` de A4
  es `ReplayMarketDataAdapter`; la fuente real es batch con ~1 día de retraso).
* **Streaming S1–S6 cerrado** (commits `1abb4c9`→`1547593`), **S7 integrado**
  (`5a0760b`+`75dba6d`) y **S8 con backfill REST** (`8b27d35`+`28a778c`):
  pipeline `WebSocket → LiveDataEngine (B1) → ClosedBarEvent → ForwardRunner`
  (paper), read-only, con reconnect, integridad (S4), salud operacional (S6),
  warm-start entre reinicios y backfill REST oportuno para gaps recientes
  (S8, `--cycle-timeout-seconds` soft). Validado en real con 1 cierre y con un
  gap de 7 velas recuperado. Ver «Streaming Paper/Forward (S7)» y «S8 —
  Streaming Operational Backfill».
* **Auditoría operacional cerrada** (commits `0a528ab`→`72135a4`): latch B5
  persistente, `reconcile()` B1 endurecido, severidad multi-fuente, hardening de
  datos y single-instance lock + input fingerprint (ver «Auditoría operacional y
  hardening»). `reconnect()` desde `DEGRADED` queda **aparcado**.
* **Scheduler endurecido** (commit `f130015`): `StartWhenAvailable=True`, sin
  `WakeToRun`, batería bloqueada, diaria, idempotente; principal `S4U` objetivo
  (en este equipo `Interactive` hasta reinstalar como admin). Ver «Scheduler de
  A4 (Change 6)».
* Estrategia H002 **FROZEN**; FINAL_OOS **🔒 intacto** (`BLOCKED_BY_DATA_AVAILABILITY`;
  ver «Semántica del desbloqueo del FINAL_OOS»).
* `research/evidence.json`: H001–H005 y H004-B siguen en `RESEARCH_REQUIRED`.

Secuencia de retoma (en orden):
1. `python -m pytest` → baseline de regresión (hoy **773 passed**).
2. `python experiments/e14_forward_continuous.py --status` → informe operativo
   actual de A4 (**solo lectura**: no modifica artefactos).
3. Revisar `experiments/forward/outputs/FORWARD-PAPER-A4/`: `report_a4.json`,
   `ops_log.ndjson`, `incidents.ndjson`.
   * El primer ciclo nuevo materializa la transición **manifest legacy →
     `input_fingerprint`** (y crea `.forward.lock`); a partir de ahí, un cambio
     inesperado de `bars.jsonl` da `INPUT_FINGERPRINT_MISMATCH` (fail-safe).
   * Si `incidents.ndjson` tiene entradas → tratarlas como **incidente
     operacional** (investigar → fix aislado → test determinista → commit), no
     como motivo para cambiar la estrategia. (El `PROVIDER_ERROR` del 28/09 ya se
     clasificó como caída de red/DNS del proveedor, **sin** fix pendiente; el
     ciclo del 29/09 se recuperó solo, descargando los días que faltaban.)
   * Si todo verde → no hay nada que hacer: A4 sigue corriendo por la tarea
     programada `Atlas-Forward-A4` (diaria 00:30; `StartWhenAvailable` recupera
     una ejecución perdida).
4. FINAL_OOS: sigue bloqueado por calendario; comprobar con
   `python experiments/e12_final_oos.py` (status). **No** abrir H006 ni
   reescribir datasets como "prueba".
5. Streaming S1–S6 + S7 (solo lectura): `python -m pytest tests/test_streaming_*.py`
   o los smokes `experiments/e15_*`/`e16_*`/`e17_*`. **S7**: informe con
   `python -m experiments.e18_streaming_forward --status` (**no** adquiere lock ni
   escribe artefactos); evidencia en
   `experiments/streaming/outputs/FORWARD-STREAM-B1/`. **No** lanzar `--loop` ni
   sesiones WebSocket sin autorización explícita; S6 solo observa.
6. **S8 (solo lectura)**: informe con
   `python -m experiments.e19_streaming_operational --status` (**no** adquiere lock
   ni escribe artefactos); evidencia en
   `experiments/streaming_operational/outputs/FORWARD-STREAM-OPS-B1/`
   (`ops_log.ndjson`, `streaming_health.ndjson`, `report_stream.json`,
   `incidents.ndjson`). Operación supervisada (si se autoriza):
   `--loop --cycles N --cycle-timeout-seconds 120 --recv-timeout 1 --max-events 30
   --max-polls 2 --poll-seconds 1`. **No** borrar ni regenerar la evidencia; tras
   cualquier terminación anómala, auditar lock/checkpoint/estado antes de
   reiniciar (`budget_exhausted` **≠** `completed`; timeout externo de la
   herramienta **≠** fallo de S8; `BLOCKED`/`UNRECOVERABLE` son paradas). **No**
   abrir C3 ni tocar `deployment/` sin decisión operativa explícita.

Reglas que NO cambian durante A4: H002 no se modifica; sin optimización ni
selección de estrategias; no se abre H006; no se toca FINAL_OOS; sin broker ni
capital real; forward **no** recalibra la estrategia. El streaming S1–S8 no toca
A4/H002/FINAL_OOS y no autoriza trading (S6 solo observa). S8 **congelado** en
`28a778c`; su deuda (soft timeout, 429/`Retry-After` no ejercitado, `ts == end`,
crash/kill real, scheduler) queda registrada y no se "limpia" por completitud.

```powershell
python -m pytest                                       # suite completa (baseline de regresión)
python experiments/e14_forward_continuous.py --status  # A4: informe operativo (solo lectura)
python experiments/e14_forward_continuous.py --once    # A4: forzar un ciclo de ingesta
python experiments/e12_final_oos.py                    # FINAL_OOS: status (BLOCKED hasta cobertura ≥ 2027-09-01)
python experiments/e7_h002_asymmetry.py                # H002 train/test + métricas
python experiments/e9_h004_walk_forward.py             # H004: walk-forward fijo + DATA_ROLE + decisión
Get-ScheduledTask -TaskName "Atlas-Forward-A4"         # estado de la tarea programada
python -m deployment.schedule_a4 status                # scheduler A4: config real (solo lectura)
python -m deployment.schedule_a4 install               # (re)instalar tarea endurecida (admin para S4U)
python experiments/e15_binance_ws_smoke.py             # S2: smoke read-only Binance WebSocket
python experiments/e16_binance_reconnect_smoke.py      # S3: smoke read-only reconexión (fuerza caída)
python experiments/e17_streaming_pipeline_smoke.py     # S5: smoke read-only stream -> B1
python -m experiments.e18_streaming_forward --status   # S7: informe (solo lectura; no lock)
python -m experiments.e18_streaming_forward --once --max-events 200 --max-polls 4  # S7: una sesion acotada (requiere red)
python -m experiments.e19_streaming_operational --status  # S8: informe (solo lectura; no lock)
python -m experiments.e19_streaming_operational --once --max-events 30 --max-polls 2 --cycle-timeout-seconds 120 --recv-timeout 1  # S8: un ciclo acotado (requiere red)
python -m experiments.e19_streaming_operational --loop --cycles 3 --cycle-timeout-seconds 120 --recv-timeout 1 --max-events 30 --max-polls 2 --poll-seconds 1  # S8: 3 ciclos supervisados
```

El streaming requiere la dependencia opcional `websocket-client`
(`pip install "atlas[streaming]"`).

**Siguiente paso acordado — sin milestone nuevo: mantener A4 (forward continuo)
observando y S8 congelado en operación paper supervisada.** S8 está implementado
y validado (`S8_IMPLEMENTATION_PASS`, `S8_GAP_RECOVERY_OPERATIONAL_PASS`,
`S8_CONTINUOUS_OPERATIONAL_PASS`, `S8-C2-POLLDUR-001` CLOSED) y **congelado en
`28a778c`**. Su deuda (soft timeout `--cycle-timeout-seconds`, 429/`Retry-After`
no ejercitado, `ts == end`, crash/kill real, scheduler) queda **registrada**, no
se "limpia" por completitud. Queda una **única decisión operativa humana**:
operación **supervisada** (usar el runbook + `--loop --cycles`; sin scheduler,
probablemente sin C3) o **desatendida** (abrir **C3-SPEC** y no tocar
`deployment/` hasta aprobarla). **No** implementar scheduler por inercia. El
desbloqueo del FINAL_OOS depende de ≥ 365 días completos de datos nuevos (hasta
`2027-09-01T00:00Z`), NO de un zip mensual (ver «Semántica del desbloqueo del
FINAL_OOS»); el perímetro congelado no se modifica mientras se espera. La causa de W2
está documentada (beta direccional adversa del filtro LONG en bears) y la
inestabilidad temporal, a granularidad trimestral, es **SISTEMÁTICA** (no se localiza en
2022-2023; 6/12 folds positivas, 3 vecindades negativas: 2021Q2 · 2021Q4→2022Q3 · 2023Q3).
Lección clave de H004-B: **los positivos se concentran en pocas ventanas (~83% en 3 folds) y
la ventaja frente al control neutro no es consistente por ventana** (en 2023Q4 y 2024Q1 el
control neutro superó al breakout). La hipótesis LONG-only **no** presenta ventaja temporal
estable → la pregunta correcta ya no es "cómo hacer que LONG funcione mejor", sino
"¿existe una señal generalizable detrás del rendimiento observado?". H004-B fue diagnóstico:
**no** justifica modificar la estrategia, no abre H006 ni desbloquea el FINAL_OOS.

El protocolo del FINAL_OOS ya está congelado y verificado (nada dependió de números):
1. `research/decisions/FINAL_OOS_PRE_REGISTRATION.json` (FROZEN) fija rango
   `start=1788220800` (2026-09-01T00:00Z), `min_span=365 días`, hashes, 29 métricas,
   benchmarks (B&H / B&H escalado / exposición temporal neutra) y criterios **NULL**
   de falsación (net_return>0, PF≥1, expectancy>0, beats-neutral; single-shot).
2. `research/final_oos_pipeline.py` + `tests/test_final_oos_pipeline.py` ejecutan toda la
   cadena sobre dataset sintético: calidad → manifest sha256 → guards (OBSERVED, un solo
   FINAL_OOS, sin doble ejecución, sin recalibración) → evaluación → reporte → consumo
   irreversible. `tests/test_final_oos_preregistration.py` verifica los hashes del plan.
3. `experiments/e12_final_oos.py` es el runner: sin dataset imprime status
   (BLOCKED_BY_DATA_AVAILABILITY); con `--csv/--manifest` hace dry-run sobre copias
   (nunca muta `evidence.json`/`data_roles.json`); con `--authorize --yes-consume`
   ejecuta el protocolo real (requiere plan en `AUTHORIZED`) y escribe
   `experiments/outputs/final_oos_report.json` + `research/decisions/FINAL_OOS_RESULT.json`.

Cuando exista cobertura completa de datos nuevos ≥ 365 días (última barra ≥
`2027-08-31T23:00 UTC`): ampliar `experiments/data/` (nuevo manifest + sha256,
con los mecanismos de adquisición vigentes: monthly y/o daily), ejecutar
`python experiments/e12_final_oos.py --status`, revisar
`--dry-run` y, tras autorización humana explícita (`estado: AUTHORIZED` en el plan),
`--authorize --yes-consume`.
**Nunca** reutilizar 2024–2026 como OOS, seleccionar trimestres, filtrar por régimen ni
optimizar lookback/ATR/stops/SMA.

### Métricas y análisis

`reporting/metrics.py` expone: `net_return`, `max_drawdown`, win/`expectancy`,
`expectancy_e` (E = P(w)·AvgW − P(l)·AvgL), `profit_factor`, `payoff_ratio`,
`avg_win/avg_loss`, `median_trade`, `trade_std`, `gross_profit/loss`, costes,
`avg_mae/avg_mfe` + ratio y rachas máximas. `research/analysis.py` construye las
secciones de reporte (señales, trades, performance, distribución, ejecución, lados).

### Auditoría de unidades

P&L, costes, MAE y MFE se monetizan por `point_value` (dólares por unidad de
precio); con `point_value=1` los resultados históricos no cambian. Verificado
que `risk in $ ≡ P&L in $` con `point_value=50` (`tests/test_units.py`),
requisito para pasar de BTC a EURUSD/NQ/ES.

### Lección operacional (B7)

Durante la construcción de `tests/test_paper_trading.py`, el fixture de
integración dimensionó **25 qty** en vez de 2.5 (equity 1 M ×
`risk_per_trade=0.0025` / 100 pts) y el `PaperBrokerAdapter` lo rechazó por
caja insuficiente, dejando la posición sin abrir y enmascarando el resto de la
cadena. Se corrigió en el sitio correcto: configuración explícita del fixture
(`risk_per_trade=0.00025`), no el broker. Regla para producción: **los defaults
no deben determinar silenciosamente el comportamiento de fixtures críticos** —
los tests de integración deben usar configuración explícita (equity,
`risk_per_trade`, `point_value`, stop y restricciones de cantidad).

## Próximos hitos

Según `00_MASTER_SPECIFICATION.md`. B1–B7 cerrados (capa en vivo + paper
probada), **forward continuo A4 en marcha**, **streaming S1–S6 cerrado**
(track paralelo aislado, solo observabilidad), **S7 (streaming → paper/forward)
implementado, validado y auditado** y **S8 (backfill REST + operación paper)
congelado en `28a778c`**. Hoja ordenada:
**A4 forward continuo** (observación operacional; EN CURSO) →
**decisión operativa S8** (supervisado vs. desatendido; **única decisión humana
pendiente**; C3-SPEC solo si se elige desatendido) →
**FINAL_OOS** (dataset congelado con ventana completa; bloqueado por calendario) →
**decisión de evidencia** →
**paper/forward evidence** (sin prisa: prueba infraestructura, no edge fuera de
muestra) →
**Live Pilot** (⏸️ no autorizado todavía).

El foco inmediato ya **no** es construir más infraestructura: es **dejar A4
operando** (ingesta incremental diaria sobre `experiments/forward/outputs/`) y
traer incidentes o el primer informe operativo periódico significativo. La
distinción se mantiene estricta: `operational evidence ≠ strategy evidence`. Los
5 hallazgos de la auditoría quedan cerrados (`0a528ab`→`72135a4`) y el scheduler
de A4 endurecido (`f130015`); `reconnect()` desde `DEGRADED` sigue aparcado
(no es requisito con adquisición batch).

El **track de streaming S1–S6** se construyó en paralelo, aislado de A4 (no toca
`live/`/H002/FINAL_OOS y no autoriza trading): contrato (S1), Binance WebSocket
read-only (S2), reconnect + heartbeat (S3), integridad sin atajo (S4), puente a
`LiveDataEngine` con B1 como autoridad (S5) y salud operacional que solo observa
(S6). Su integración con `ForwardRunner`/`PaperBroker` (**S7**, commits
`5a0760b`+`75dba6d`) está cerrada y validada en real con un cierre de 1h
(`CLOSED_BAR_OPERATIONAL_PASS`, `S7_RELEASE_AUDIT_PASS`). El backfill oportuno y
la operación paper acotada se cerraron en **S8** (`8b27d35`+`28a778c`,
`S8_GAP_RECOVERY_OPERATIONAL_PASS`, `S8_CONTINUOUS_OPERATIONAL_PASS`,
`S8-C2-POLLDUR-001` CLOSED), **congelado** y **sin scheduler**. Ver «S8 —
Streaming Operational Backfill» y «Cómo retomar»; `websocket-client` es
dependencia opcional (`pip install "atlas[streaming]"`).

La **decisión operativa pendiente** de S8 (supervisado vs. desatendido) es el
próximo paso: si es **supervisado**, S8 queda cerrado y se pasa al siguiente
milestone funcional/arquitectónico con la matriz de fallos y la deuda
conservadas; si es **desatendido**, se abre **C3-SPEC** (scheduler/frecuencia,
duración/presupuesto, arranque/parada, reboot, lock huérfano, logs/retención,
`BLOCKED`/`UNRECOVERABLE`, intervención humana, aislamiento de A4, recuperación
tras proceso muerto) y **no se toca `deployment/`** hasta aprobarla. **No**
convertir los riesgos "no ejercitados en real" en motivos para mantener S8
abierto: un milestone puede cerrarse con capacidades documentadas pendientes de
escenarios de estrés concretos.

En paralelo, el dataset congelado de datos nuevos con cobertura ≥ 365 días
(`[2026-09-01, 2027-09-01)`, última barra ≥ `2027-08-31T23:00 UTC`) sigue
bloqueado por calendario del proveedor, no por Atlas (ver «Semántica del
desbloqueo del FINAL_OOS»). El dump diario `data/raw/daily/` hoy cubre solo
2026-09-01→25: la ventana se completará a medida que el proveedor publique los
días restantes (no antes de ~2027-09-01), sin forzar adquisición. No se abre
ningún bloque de infraestructura nuevo mientras se espera; el sistema se mantiene
estable y el FINAL_OOS congelado (sin más complejidad estratégica ni
optimización). Cuando haya cobertura completa se seguirá la disciplina
status → dry-run → revisión → autorización → consumo irreversible.