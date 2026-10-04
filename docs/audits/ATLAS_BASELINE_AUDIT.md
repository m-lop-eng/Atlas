# Atlas Baseline Audit

Auditoría técnica reproducible del estado del repositorio en
`HEAD = 53625fb`, previa a cualquier modificación de la lógica de trading.
Solo lectura + ejecución de tests; **no** se corrigió ningún hallazgo.

Comando de reproducción del baseline:

```powershell
python --version
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
python -m compileall -q <paquetes>
```

---

## Repository State

- Repositorio git; rama `main`; `HEAD = 53625fb`
  (`feat: M4 hypothesis lifecycle, selection and validation gates`).
- `git status --short` = ` M README.md` (cambio preexistente, fuera de alcance).
- Sin CI: no existe `.github/`, `.gitlab-ci.yml` ni `azure-pipelines.yml`.
- `__pycache__` / `*.pyc` / `*.pyo` / `egg-info`: **no versionados**
  (gitignored). `atlas.egg-info` y `.pytest_cache` existen en disco, no en git.
- Datasets: CSVs **no** versionados (`*.csv` ignorado); solo se versionan
  manifests (`experiments/data/*.manifest.json`). `experiments/*/outputs/`
  ignorado.
- `docs/` no existía antes de esta auditoría.

## Runtime and Dependencies

- **Python detectado: 3.14.0** (el `pyproject` declara `requires-python = ">=3.12"`).
- Dependencias declaradas: `PyYAML>=6.0`, `SQLAlchemy>=2.0`, `psycopg[binary]>=3.1`.
- Opcionales: `dev` (`pytest>=8`, `pytest-cov>=5`), `streaming` (`websocket-client>=1.6`).
- Entorno real: `coverage 7.16.1`, `pytest` presentes; `websocket-client`
  instalado (usado por S2). El sistema tiene muchas más librerías globales
  (Flask, Celery, Dash…) ajenas al proyecto.
- Paquetes principales: `config, data, research, strategies, backtesting,
  validation, portfolio, risk, execution, brokers, live, streaming, sessions,
  reconciliation, killswitches, monitoring, papertrading, database, reporting,
  deployment, experiments`.
- Configuración: `pyproject.toml`, `config/{development,paper,production}/settings.yaml`,
  `config/final_oos/config.yaml`, `docker-compose.yml`, `.env.example`.
- Tests: **53 archivos `test_*.py`**.

## Test Baseline

Comandos exactos y resultado:

| Comando | Resultado |
| --- | --- |
| `python -m pytest` | **798 passed** in ~11.4s, 0 failed, 0 skipped |
| `python -m compileall -q <paquetes>` | exit 0 (sin errores de compilación) |
| `python -m pytest --cov=. --cov-report=term-missing` | **TOTAL 85%** (16205 stmts, 2436 miss) |

Warnings: no se observaron warnings en la salida estándar.

Cobertura por módulo crítico (term-missing):

| Módulo | Cobertura | Sin cubrir (relevante) |
| --- | --- | --- |
| `backtesting/engine.py` | 95% | 45,49-53,412-415 |
| `risk/engine.py` | 98% | 172 |
| `risk/position_sizing.py` | 97% | 73 |
| `risk/types.py` | 97% | 72,112 |
| `execution/order.py` | 99% | 105 |
| `execution/order_manager.py` | 98% | 170 |
| `brokers/paper.py` | 93% | 93,163,184,191,211,251,255,262,291,348 |
| `papertrading/engine.py` | 90% | 262-266,289-292,310-311,344-345,404,417,421,430,434,508-520,529,547-551,577-578,581,637-638,648-649,651,659-660 |
| `reconciliation/engine.py` | 96% | 36,38,193-203,502 |
| `reconciliation/orchestrator.py` | 90% | 75,104-105,161-164 |
| `portfolio/engine.py` | 100% | — |
| `data/quality.py` | 82% | 36,79-81,99-101,123-135,157-158,196,198-205 |
| `data/loader.py` | 92% | 77-78,83 |
| `live/engine.py` | 91% | 73,77,103,105,122-123,261,281-282,307-311,357,370-371,403-405,433-437,452-456,507,509 |
| `monitoring/engine.py` | 92% | 67,150,154-157 |
| `monitoring/checks.py` | 82% | múltiples |
| `killswitches/coordinator.py` | 100% | — |
| `config/loader.py` | 86% | 58,61,65-66,69 |

## Architecture Inventory

| Capa | Estado real |
| --- | --- |
| Data | `data/sources.py` (Kraken, BinanceVision monthly/daily, CSV, Parquet opcional), `data/loader.py`, `data/quality.py`, `data/synthetic.py`. Datasets profundos BTC 1h (67k barras) presentes en disco. |
| Backtesting | `backtesting/engine.py` (`BacktestEngine` v2, event-driven bar-a-bar, single-position). Determinista; cubierto por BT2. |
| Research | `research/pipeline.py` (lineage + `experiment_id`), `research/experiment.py`, `research/evidence.py`, `research/data_role.py`, `research/benchmarking.py`, `research/hypothesis.py` (M4). |
| Strategies | `strategies/breakout.py`, `strategies/random_entry.py`. |
| Risk | `risk/engine.py`, `risk/position_sizing.py`, `risk/types.py`. |
| Execution | `execution/order.py`, `execution/order_manager.py` (en memoria). |
| Brokers | `brokers/base.py` (ABC), `brokers/paper.py` (`PaperBrokerAdapter`) — **único adapter concreto**. |
| Live (B1–B7) | `live/`, `sessions/`, `reconciliation/`, `killswitches/`, `monitoring/`, `papertrading/`. |
| Streaming (S1–S8) | `streaming/`, `papertrading/streaming_forward.py`, `data/binance_rest.py`, `experiments/e19_*`. |
| Portfolio | `portfolio/engine.py` (solo exposición descriptiva; **no integrado**). |
| Database | `database/models.py`, `database/session.py` (definidos; **no usados** fuera del paquete). |
| Deployment | `deployment/schedule_a4.py` (tarea Windows A4). |
| Production/live execution | **NO existe** ejecución real: no hay broker real, credenciales ni endpoints de órdenes. Solo `development`/backtest y paper/forward. |

## Confirmed Findings

### F-01 — `_day_key()` depende del timezone local (reset diario incorrecto) — **RESOLVED — pending commit**
- ID: F-01
- severidad: **HIGH**
- archivo: `backtesting/engine.py:38-56`
- función: `_day_key`
- **estado: RESOLVED — pending commit**
- **causa raíz**: `datetime.fromtimestamp(float(ts))` sin `tz=timezone.utc`
  convertía el epoch (UTC por definición) al **TZ local** del host antes de
  `.date()`. Una barra cerca de medianoche UTC cambiaba de "día de trading"
  según el offset local, alterando el reset de `max_daily_loss` y el resultado
  del backtest entre máquinas.
- **decisión**: UTC explícito en el motor (epoch son UTC; el "día de trading" no
  es configurable).
- **corrección**: `datetime.fromtimestamp(float(ts), tz=timezone.utc).date()`
  (y `from datetime import datetime, timezone`). Mínima; sin deps nuevas.
- **tests añadidos** (`tests/test_daily_reset.py`):
  - `TestDayKeyTimezoneInvariance`: valor UTC en medianoche; iteración de TZ
    (`UTC`, `Pacific/Kiritimati` +14, `Etc/GMT+12` −12) con `_tz_guard`
    (`TZ`+`time.tzset()` cuando el SO lo permite, restaurado siempre); con −12
    la representación ingenua da `2024-01-01` mientras `_day_key` da `2024-01-02`
    (demuestra el bug); independencia del TZ real del host; ISO con offset.
  - `TestDailyResetTimezoneInvariance`: el backtest completo (2 días alrededor
    de medianoche) produce resultado idéntico bajo 3 TZ.
- **nota de entorno**: `time.tzset` no existe en Windows; `_tz_guard` lo maneja
  (cambia `TZ` solo si hay `tzset`, restaura siempre) y el caso −12 se verifica
  explícitamente cuando el SO lo permite.
- **evidencia pruebas**: `pytest tests/test_daily_reset.py` → 11 passed;
  suite → **817 passed**.
- **cobertura**: `backtesting/engine.py` 95% → **96%** (global 85%).
- **no cerrado del todo** hasta el commit.
- **hallazgo relacionado**: `.date()` en `engine.py:50` (ISO con offset) — el
  offset del string se respeta (correcto); sin otro uso sospechoso en
  `backtesting/`.

### F-02 — `max_strategy_risk` no se aplica con `point_value` — **RESOLVED — pending commit**
- ID: F-02
- severidad: **MEDIUM**
- archivo: `risk/engine.py` (`RiskEngine.validate`)
- **estado: RESOLVED — pending commit**
- **Decisión humana ratificada (contrato canónico, opción A)**: `max_strategy_risk`
  = **máximo riesgo por operación como fracción de equity** →
  `risk_per_trade <= max_strategy_risk`.
- **Fórmula por rama (después)**:
  - Sizing (`point_value`): se rechaza si `risk_per_trade > max_strategy_risk`
    (check `max_strategy_risk`); el algoritmo de sizing no cambia
    (`qty = risk_cap/(distance·point_value)`).
  - Estimator (`trade_risk_estimator`): `theoretical_risk <= max_strategy_risk × equity`
    (antes `≤ max_strategy_risk × risk_cap`).
- **Fórmula por rama (antes)**:
  - Sizing: sin comprobación de `max_strategy_risk` (inerte).
  - Estimator: `theoretical_risk <= max_strategy_risk × risk_cap`.
- **Configs `max_strategy_risk: 0.9`** (research/experiment; `h002`,
  `first_experiment`, `real_market_first`, `trend_filter`, `config/final_oos`):
  - **NO se modifican.** Bajo el contrato A, `0.9` = 90% de equity de riesgo por
    operación → requiere revisión.
  - Inspección: no hay documentación ni commit que justifique `0.9`; §13 usa
    `1.00%`. Los manifests de paper/forward registran `0.01` (1%). **No se
    determina inequívocamente** el valor pretendido → **pendiente de decisión**
    documentada (NO se inventa). Con `risk_per_trade=0.01`, `0.9` sigue siendo
    coherente (`0.01 ≤ 0.9`) y no altera backtests.
  - **Nota**: cambiar el contrato a A no cambia resultados de experimentos
    existentes, porque en sizing solo añade un check que `0.9` satisface; el
    sizing permanece idéntico.
- **Tests**: `TestMaxStrategyRiskContract` (sizing: below/at/above ceiling,
  reproducción del bug, unidades numéricas) y `TestEstimatorMaxStrategyRiskContract`
  (below/at/above, límite `×equity`). Ajustados dos tests legacy que codificaban
  la fórmula B.
- **Documentación**: `03_RISK_MANAGEMENT.md §13`, docstring de `RiskEngine`,
  comentario en `risk/types.py` — definición única y equivalente.
- **Evidencia**: `pytest` → 833 passed; `compileall` OK; `risk/engine.py` 98%.
- **no cerrado del todo** hasta el commit.

### F-03 — exposure/leverage/margin/correlation/concentration — **REQUIRES MINI-SPEC / INFRASTRUCTURE GAP**
- ID: F-03
- severidad: **HIGH**
- archivo: `risk/engine.py`, `risk/types.py`, `portfolio/engine.py`
- **estado: REQUIRES MINI-SPEC / INFRASTRUCTURE GAP** — NO corregido.
- Los campos existentes **no** constituyen un contrato operacional completo:
  faltan límites configurables y/o datos runtime/modelos necesarios. **No** se
  implementan gates para no introducir política de riesgo no especificada.

| Gate | Definición | Límite config | Dato runtime | Modelo | Tests | Clasificación |
|---|---|---|---|---|---|---|
| Gross exposure | `portfolio/engine.py`; campo muerto `AccountRiskState.gross_exposure` | No | No poblado | Parcial | Solo cálculo | Requiere mini-spec |
| Net exposure | idem | No | No | Parcial | Solo cálculo | Requiere mini-spec |
| Margin | §20 (texto) | No | No | No | No | Gap de infraestructura |
| Leverage | §19 fórmula `gross/equity` | No (`max_leverage` inexistente) | No | Doc sin impl | No | Gap de infraestructura |
| Correlation | §11-12 | No | No | No | No | Gap de infraestructura |
| Concentration | §18 | No | No | No | No | Gap de infraestructura |

- Gross/net requieren además **integrar** `portfolio.compute_portfolio_risk`
  (hoy no wired) con el risk path y poblar `AccountRiskState`.
- Margin/leverage/correlation/concentration requieren **definición/modelado**
  adicional (límites, ventanas, datos de margen por instrumento).
- **NO implementar gates** sin contrato.

### F-03 — El veto de riesgo NO incluye exposición/leverage/margin/correlación/concentración
- ID: F-03
- severidad: **HIGH**
- archivo: `risk/engine.py` + `risk/types.py` (`gross_exposure`, `net_exposure`, `margin_usage` definidos pero no usados en `validate`)
- función: `RiskEngine.validate`
- descripción: `AccountRiskState` expone `gross_exposure/net_exposure/margin_usage`
  pero `validate` no los comprueba; `portfolio.compute_portfolio_risk` es
  descriptivo. No hay veto por concentración/correlación/apalancamiento.
- impacto: el sistema puede aprobar órdenes que, agregadas, excedan límites de
  portafolio; incoherente con `03_RISK_MANAGEMENT.md §10/§12`.
- evidencia: `risk/engine.py` no referencia esos campos (búsqueda 0 hits).
- test existente: `test_portfolio.py` (solo cálculo).
- test necesario: veto por exposición agregada.
- recomendación: integrar `portfolio`/exposición en el veto o marcar explícito
  como no implementado (MVP).

### F-04 — `PaperTradingEngine._risk_state()` fija kill_switch/account_active/strategy_active
- ID: F-04
- severidad: **MEDIUM**
- archivo: `papertrading/engine.py` (`_risk_state`)
- descripción: siempre pasa `kill_switch=False`, `account_active=True`,
  `strategy_active=True`. El kill switch se consulta **aparte** en
  `_execute_pending` (`self.kill_switch.status().can_trade`), por lo que B5 sí
  bloquea la entrada; pero el RiskEngine **nunca** puede vetar por kill switch ni
  por cuenta/estrategia inactiva en este camino.
- impacto: doble fuente de decisión; el `checks` del RiskEngine es incompleto en
  paper. No es un bypass (B5 sigue aplicando), es una divergencia de contrato.
- evidencia: código `_risk_state`.
- test existente: `test_paper_trading.py`, `test_kill_switches.py`.
- test necesario: garantizar que el veto efectivo (B5 en `_execute_pending`) no
  puede saltarse y documentar la separación.
- recomendación: pasar el estado real de B5/cuenta al `AccountRiskState` o
  documentar que el veto de kill switch vive exclusivamente en B5.

### F-05 — `PaperBrokerAdapter.get_account_state()` equity de SHORT — **RESOLVED — pending full regression validation**
- ID: F-05
- severidad: **HIGH**
- archivo: `brokers/paper.py:225-243`
- función: `get_account_state`
- **estado: RESOLVED — pending full regression validation**
- **causa raíz**: `equity = cash + Σ abs(quantity)·price`. Para LONG, `cash`
  pagó el coste y `qty·price` revalúa (activo). Para SHORT, `cash` ya ingresó la
  venta (pasivo) y `abs(qty)·price` **duplicaba** la posición en lugar de restar
  la obligación económica.
- **fórmula corregida** (derivada del modelo; sin fees porque
  `BrokerFill.commission=0` y el broker no aplica comisiones):
  ```
  unrealized = quantity * (mark_price - avg_entry_price)   # quantity dirigida
  equity     = cash + Σ quantity · mark_price
  ```
- **cambio**: `cash + Σ abs(p.quantity)*price` → `cash + Σ p.quantity*price`
  (una única fórmula con signo, sin ramas LONG/SHORT).
- **tests añadidos** (`tests/test_broker_adapter.py::TestAccountEquitySign`):
  LONG sin movimiento/ganancia/pérdida; SHORT sin movimiento (no 10.100/10.200),
  ganador (10.010), perdedor (9.990); invariante `unrealized` con signo;
  posición cerrada `unrealized==0` (realizado reflejado en cash); multi-fill
  SHORT (avg_entry 105, qty −2, equity 99.990).
- **regresión**: `pytest` → **807 passed** (798 + 9). `compileall` → exit 0.
- **cobertura**: `brokers/paper.py` 93% antes → **93%** después (sin reducción).
- **no cerrado del todo** hasta la validación de regresión completa (suite
  entera verde + revisión).

### F-06 — `_internal_snapshot()` toma la cuenta del propio broker — **REQUIRES MINI-SPEC / INFRASTRUCTURE GAP**
- ID: F-06
- severidad: **MEDIUM**
- archivo: `papertrading/engine.py` (`_internal_snapshot`), `reconciliation/orchestrator.py`
- **estado: REQUIRES MINI-SPEC / INFRASTRUCTURE GAP** — NO corregido.
- **Hallazgo: CONFIRMADO (ACCOUNT tautológico).**
- **Flujo actual**:
  - `_internal_snapshot()` → `account = AccountSnapshot(acct...)` desde
    `self.broker.get_account_state()` (`papertrading/engine.py:701-702`).
  - `broker_state_from_adapter()` → `account` desde el **mismo**
    `adapter.get_account_state()` (`orchestrator.py:91-96`).
  - `_reconcile_account` compara `internal.account` vs `broker.account` → ambas
    derivan del broker → **siempre MATCH** (salvo mutación síncrona inexistente).
- **Qué NO puede detectarse**: cualquier divergencia real entre el cash/equity
  que Atlas *debería* tener (por sus propios fills/P&L) y el que el broker
  declara. La reconciliación ACCOUNT no aporta contraste.
- **Independencia de fuentes**:

| Fuente | Representa | Independiente del broker | Integrada runtime | Adecuada expected |
|---|---|---:|---:|---:|
| Portfolio (`portfolio/`) | exposición agregada (descriptiva) | Sí | **No** (0 importadores) | No |
| OrderManager (`execution/`) | ciclo de órdenes | Sí | Sí | Solo ORDER/FILL |
| Database (`database/`) | ORM | Sí | **No** (0 importadores) | No |
| Backtest state | realized P&L interno | Sí | Sí | No (no reconcilia) |
| `_last_cash/_last_equity` | caché del broker | **No** | Sí | No |
| Broker account | cash/equity del broker | **No** | Sí | No (para expected) |

- **No existe** hoy un ledger interno de cash/equity independiente. `PaperBrokerAdapter`
  es fuente observada + ejecutora + única poseedora de cash/positions.
- **ORDER/FILL/POSITION** sí comparan `OrderManager`/`_position` (interno) vs broker
  → independientes; **no afectados** por F-06.
- **Por qué NO se hace un pseudo-fix**: copiar el estado del broker a otro objeto
  sería una segunda referencia, no independencia semántica; no detectaría nada.
- **Mini-spec necesaria (fuera de una corrección puntual)**:
  - componente que **posea** el ledger interno (candidato: un `AccountLedger`);
  - **eventos** que lo actualicen (fills adoptados, P&L realizado/no realizado,
    costes) — el `PaperTradingEngine` ya tiene `_realized_pnl`/fills, pero no un
    cash/equity interno **autoritativo e independiente**;
  - **qué compara ACCOUNT**: cash y equity internos vs `broker.get_account_state()`;
  - integrar el ledger en el lifecycle de paper (hoy el cash/equity se leen del
    broker en `_mark`);
  - decidir el alcance (paper primero; backtest no reconcilia).
- **Evidencia**: inspección de `reconciliation/`, `papertrading/engine.py`,
  `portfolio/`, `database/` (0 integración).
- **no se implementa gate ni ledger ad hoc.**
- **no cerrado** hasta su mini-spec.

### F-07 — `OrderManager` admite transiciones de estado inválidas — **RESOLVED — pending commit**
- ID: F-07
- severidad: **HIGH**
- archivo: `execution/order_manager.py` (`_transition`)
- **estado: RESOLVED — pending commit**
- **Causa raíz**: `_transition` solo rechazaba transiciones **desde** estado
  terminal; no validaba que la arista perteneciera al grafo. Permitía
  `CREATED → ACKNOWLEDGED`, `CREATED → SUBMITTED`, `APPROVED → ACKNOWLEDGED`,
  etc. (reproducido).
- **Grafo contractual reconstruido** (de `06_EXECUTION_OPERATION.md §7` y de los
  callers reales `create→approve→submit→acknowledge/fill/reject/cancel`),
  ahora en `ALLOWED_TRANSITIONS` (fuente única):
  ```
  CREATED          -> RISK_CHECKED, APPROVED, REJECTED, CANCELLED
  RISK_CHECKED     -> APPROVED, REJECTED, CANCELLED
  APPROVED         -> SUBMITTING, SUBMITTED, REJECTED, CANCELLED
  SUBMITTING       -> SUBMITTED, ACKNOWLEDGED, REJECTED, UNKNOWN, FAILED
  SUBMITTED        -> ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED, EXPIRED, UNKNOWN, FAILED
  ACKNOWLEDGED     -> PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED, EXPIRED, UNKNOWN, FAILED
  PARTIALLY_FILLED -> PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED, EXPIRED, UNKNOWN, FAILED
  FILLED/CANCELLED/REJECTED/EXPIRED/FAILED/UNKNOWN -> (terminal: sin salidas)
  ```
- **Estados terminales**: `FILLED, CANCELLED, REJECTED, EXPIRED, FAILED, UNKNOWN`
  (`OrderStatus.terminal`), sin aristas de salida.
- **Implementación**: `_transition` valida el grafo **antes** de mutar; mantiene
  el tipo de error (`OrderStateError`), el retorno (`Order`) y la semántica
  terminal. `from == to` solo si el grafo lo permite (`PARTIALLY_FILLED → PARTIALLY_FILLED`);
  `CREATED → CREATED` se rechaza.
- **Sin mutación parcial**: la comprobación precede al `replace`; una transición
  inválida deja `status/fill_quantity/average_fill_price/commission/reject_reason`
  intactos (verificado por test).
- **`apply_fill` no se toca**: usa `replace()` directo (no `_transition`), por lo
  que `SUBMITTED/ACKNOWLEDGED → PARTIALLY_FILLED/FILLED` sigue funcionando.
- **Callers reales**: `backtesting/engine.py` y `papertrading/engine.py` usan
  `create→approve→submit` (legal) y `reject`/`apply_fill` (legales); sin cambios.
- **Tests**: `TestTransitionGraph` (12): lifecycle legal, `CREATED→ACK`,
  `CREATED→SUB`, `APPROVED→ACK`, `SUBMITTED→APPROVED` rechazadas; sin mutación
  parcial; terminal sin salida; `CREATED→REJECTED/CANCELLED` permitidas;
  `reject_reason` preservado; self-transition `CREATED` inválida y
  `PARTIALLY_FILLED` válida; todos los terminales sin salidas.
- **Evidencia**: `pytest` → 845 passed; `execution/order_manager.py` 99%.
- **no cerrado del todo** hasta el commit.
- **Hallazgo incidental (NO corregido)**: `UNKNOWN` está modelado como terminal,
  pero §8 lo describe como estado a **resolver** con `get_order`; al ser terminal,
  no hay arista para salir de él. Fuera del alcance de F-07.

### F-08 — Position sizing sin `quantity_step`/contratos enteros — **REQUIRES DECISION**
- ID: F-08
- severidad: **MEDIUM**
- archivo: `risk/position_sizing.py` (`position_size_from_stop`), `risk/engine.py` (`_size_fixed_fractional`)
- **estado: REQUIRES DECISION** — NO implementado.
- **Hallazgo: AMBIGUO/REFUTADO en el alcance actual.** La premisa "NQ/ES exigen
  enteros" **no está respaldada por ningún instrumento configurado**.
- **Evidencia de contrato**:
  - No existe `quantity_step`, `lot_size`, `contract_size` ni `min_quantity` en
    código, configs ni docs (solo `min_size`, que es un **umbral mínimo**, no un
    step).
  - Instrumentos realmente configurados/operados: `BTCUSDT`, `XXBTZUSD` (crypto,
    **fraccionables**) y `SAMPLE` (sintético). Ningún futuro de contrato entero.
  - `Order.quantity: int | float`, broker `quantity: float` → el contrato
    vigente **admite fracciones**.
  - `point_value` (p.ej. NQ=50) existe para mercados futuros, pero **ningún**
    config lo usa.
  - `03 §6` menciona "Minimum contract size / Lot size" como cosas que el cálculo
    *debería* considerar (aspiracional), sin definir el contrato operativo.
- **Opción respaldada por el repo**: **C — fraccionalidad permitida** en el
  alcance actual (cripto). No hay base para A (enteros) ni B (step por instrumento)
  sin inventar un contrato (instrumento/step/moneda).
- **Por qué NO implementar**: introducir `quantity_step` ahora sería inventar
  contrato no respaldado (step por instrumento, política de redondeo, fuente de
  metadata). El usuario lo prohíbe explícitamente ("no introduzcas `quantity_step`
  solo porque el hallazgo lo menciona").
- **Ambigüedad a resolver (decisión humana)**: política de cuantización si en el
  futuro se añade un instrumento entero: `floor` (preserva el límite de riesgo),
  `round`, `ceil` (puede superarlo) o `reject`; y **dónde** vive la metadata
  (step/lot) por instrumento. Para sizing risk-based, `floor` es la única que
  garantiza `riesgo_final ≤ riesgo_objetivo`, pero debe ratificarse.
- **Alcance actual**: el sizing es correcto para los instrumentos fraccionables
  operados; no hay bug observable hoy.
- **No cerrar F-08 como RESOLVED**; queda `REQUIRES DECISION` para el día en que
  se incorpore un instrumento de contrato entero/step (p.ej. NQ/ES).

### F-09 — `summarize_quality()` NO ejecuta todos los checks — **RESOLVED — pending commit**
- ID: F-09
- severidad: **HIGH**
- archivo: `data/quality.py:163-187`
- función: `summarize_quality`
- **estado: RESOLVED — pending commit**
- **causa raíz**: el resumen solo agregaba `check_required_columns`,
  `check_ohlc_integrity`, `detect_duplicates` y (si `expected_interval_seconds`)
  `detect_timestamp_gaps`. `check_sorted_unique`, `check_timestamp_alignment` y
  `check_price_volume_integrity` estaban definidos pero **fuera** del resumen →
  un dataset desordenado, desalineado o con NaN/volumen inválido pasaba `ok`.
- **comportamiento anterior**: `summarize_quality(<bars desordenadas>)` → `ok`.
- **corrección**: agregar `check_sorted_unique` y
  `check_price_volume_integrity` **siempre**; `check_timestamp_alignment` dentro
  del bloque `expected_interval_seconds` (requiere el intervalo). No se
  modificó la semántica interna de ningún check ni la estructura
  (`QualitySummary.rows/issues/ok`).
- **tests añadidos** (`tests/test_data_quality.py::TestSummaryIntegratesAllChecks`):
  desorden (`ts_no_creciente`), desalineación (`ts_desalineado`), precio no
  finito (`precio_no_finito`), volumen inválido (`volumen_invalido`), y dataset
  válido (alineado, con volumen) → `ok` y `issues == []`.
- **API**: sin cambios; `_clean_bars` pasa a base `6000` (positiva y múltiplo de
  60) porque `ts=0` no es un timestamp válido.
- **evidencia**: `pytest tests/test_data_quality.py` → verde; suite → 824.
- **no cerrado del todo** hasta el commit.

### F-10 — `read_ohlc_csv()` reordena silenciosamente — **RESOLVED — pending commit**
- ID: F-10
- severidad: **MEDIUM**
- archivo: `data/loader.py:?`
- función: `read_ohlc_csv`
- **estado: RESOLVED — pending commit**
- **causa raíz**: `read_ohlc_csv` terminaba con `bars.sort(key=lambda b: b["ts"])`,
  ocultando un CSV desordenado. El contrato documentado (`04 §7 "Timestamp
  order"`; docstring de `loader.py` "el orden cronológico es obligatorio")
  exige que el orden de entrada sea correcto y **auditable**, no auto-corregido.
- **contrato confirmado**: el orden es responsabilidad de la entrada; la
  detección pertenece a `data.quality.check_sorted_unique`/`summarize_quality`.
  No se introduce excepción automática (no era el contrato).
- **corrección**: eliminar el `sort`; conservar el orden del archivo; docstring
  actualizado.
- **tests modificados/añadidos** (`tests/test_loader.py`):
  `test_preserva_orden_del_archivo` (antes `test_sorts_by_timestamp`: ahora un
  archivo `2,1` permanece `["2","1"]`), `test_desorden_detectado_por_quality`
  (desorden + `check_sorted_unique` + `summarize_quality` no `ok`),
  `test_orden_correcto_permanece`.
- **evidencia**: el loader ya no oculta el desorden; la etapa de calidad lo
  detecta.
- **no cerrado del todo** hasta el commit.

### F-10 — `read_ohlc_csv()` reordena silenciosamente
- ID: F-10
- severidad: **MEDIUM**
- archivo: `data/loader.py` (`bars.sort(key=lambda b: b["ts"])`)
- descripción: el loader ordena siempre por `ts`, ocultando un CSV mal ordenado.
  El enunciado exige que el orden sea obligatorio, no auto-corregido.
- impacto: un dataset con orden incorrecto se "arregla" sin que nadie lo sepa;
  no se audita el orden original.
- evidencia: `data/loader.py: last line`.
- test existente: `test_loader.py`.
- test necesario: CSV desordenado → error o registro del hallazgo.
- recomendación: validar orden y fallar (o registrar issue) en vez de reordenar
  silenciosamente.

### F-11 — `config/production/settings.yaml`: `null` en métricas de riesgo — **REQUIRES DECISION (config no ejecutable)**
- ID: F-11
- severidad: **HIGH** (reclasificada: el vector real es plantilla, no runtime)
- archivo: `config/production/settings.yaml`, `config/loader.py`
- **estado: REQUIRES DECISION** — NO implementado.
- **Reclasificación (Opción B)**: `config/production/settings.yaml` **NO es
  configuración ejecutable** en ningún runtime. Es una **plantilla/placeholder**.
- **Qué significa "production"**: `EnvName.PRODUCTION` existe solo en el loader;
  `load_config`/`load_environment` **no tienen ningún consumidor de runtime**
  (0 callers fuera de `config/` y `tests/test_config.py`).
- **Path exacto**:
  - `config/<env>/settings.yaml → config.loader.load_config → dict` (sin modelo
    ni validación de `risk`) → **ningún consumidor de ejecución**.
  - La config real de ejecución proviene del **`config.yaml` de experimento**
    (`research/pipeline.build_engine: RiskConfig(**cfg["risk"])`) y de
    `config/paper/settings.yaml` + frozen `config/final_oos/config.yaml`
    (`forward_runner`). **Nunca** de `production/settings.yaml`.
  - `docker-compose` fija `ATLAS_ENV=development` y el CMD es `pytest`.
- **Inventario `risk.*` de production**: `max_open_positions`, `risk_per_trade`,
  `max_daily_loss`, `max_drawdown` = `null` (placeholders). `portfolio`,
  `execution`, `database`, `logging` con valores.
- **Semántica de los `null`**: **placeholders intencionales** de plantilla, no
  "desactivado". No hay sustitución documentada ni default aplicado.
- **Validación existente antes del fix**: `load_config` valida solo que el YAML
  sea un `dict` y exista; **no** toca `risk`. `RiskConfig.__post_init__` validaría
  rango, pero **nunca recibe** estos `null` (ningún camino los conecta).
- **¿Ya existe fail-closed?**: **No aplicable al runtime** — no hay camino que
  cargue production/settings en ejecución, así que los `null` **no pueden**
  producir un arranque inseguro hoy.
- **Causa raíz**: discrepancia entre la **promesa del loader** ("ningún valor por
  defecto silencioso para métricas de riesgo en producción... detiene el
  arranque") y la realidad (el loader no valida `risk`, y no tiene caller de
  runtime). Es un **gap de contrato de configuración**, no un bug explotable.
- **Por qué NO implementar ahora**:
  - El archivo no participa en ningún startup path; añadir un fail-closed "de
    producción" sin caller inventaría contrato.
  - Rellenar los `null` con valores sería inventar parámetros de riesgo
    (prohibido).
  - Hacer que `load_config("production")` valide el bloque `risk` rompería
    `test_config::test_load_string_env` (que hoy espera éxito) y cambiaría la
    semántica del loader sin decisión.
- **Decisión humana necesaria**: elegir política de configuración de producción:
  1. **plantilla documental** → marcar el archivo como no-ejecutable (p. ej.
     cabecera/renombrado) y no validar; o
  2. **config ejecutable** → completar los `risk.*` con valores reales (humano) y
     endurecer `load_config` a fail-closed en `production` (reutilizando la
     validación de `RiskConfig`, sin defaults).
- **Hallazgo incidental (NO corregido)**: `load_config`/`load_environment` sin
  integración en el arranque real (posible dead code o pendiente de wiring).
- **no cerrado** hasta la decisión.
- **No se modificó F-04** ni ningún otro hallazgo.

## Suspected Findings

### S-01 — `live/engine.py:442-444` duplicado inalcanzable
- severidad: LOW — en `_record_issue` hay una línea de truncado; revisar posible
  duplicado de lógica. (Cobertura parcial.)

### S-02 — `papertrading/engine.py:508-520` ramas de posición poco cubiertas
- severidad: MEDIUM — las ramas de entrada adicional/reducción parcial
  (pyramiding) tienen baja cobertura (508-520 sin cubrir); comportamiento no
  ejercido.

### S-03 — `Redis`/`Celery`/`Flask` en el entorno global
- severidad: LOW — dependencias del entorno ajenas al proyecto; no declaradas en
  `pyproject`. Riesgo de confusión, no de código.

### S-04 — `reconciliation/engine.py` UNKNOWN terminal (ver F-07/F-15)
- severidad: MEDIUM — el estado observado (UNKNOWN) depende del `OrderStatus`.

### S-05 — `db`/`portfolio` definidos pero no integrados
- severidad: LOW — `database/` sin importadores externos; `portfolio/` sin
  conexión al veto (consecuencia de F-03). Documentarlo como MVP.

## Production Blockers

1. **No existe ejecución real / broker real** (solo `PaperBrokerAdapter`), por
   diseño. No hay credenciales ni endpoints de órdenes (verificado: sin `ccxt`,
   `ib_insync`, MT5 al cual no). Sin embargo, esto **no es un blocker de
   *ejecución live* todavía** (fase no autorizada), sí lo es de *producción*.
2. **F-05 equity SHORT incorrecta** — un backtest/paper con shorts produce P&L y
   drawdown erróneos. Blockea cualquier validación de estrategia corta.
3. **F-02 `max_strategy_risk` inerte** — un límite de riesgo declarado no protege
   en el pipeline con `point_value`.
4. **F-03 sin veto de exposición/leverage/correlación** — no apto para operar
   múltiples posiciones/instrumentos con control de portafolio.
5. **F-09/F-10 data quality incompleta + reordenamiento silencioso** — la puerta
   de datos no garantiza integridad antes de backtest/producción.
6. **F-11 production config con `null`** — configuración de producción sin
   validación.

## Research Status

- H001–H005 y H004-B: `RESEARCH_REQUIRED` en `research/evidence.json`.
- H004-B y H005: diagnósticos (no estrategias); ver BT3-E.
- Ninguna hipótesis seleccionada; `research/hypotheses/` y
  `research/selections/` **no** existen (M4 implementado, no usado).
- FINAL_OOS: `BLOCKED_BY_DATA_AVAILABILITY` (calendario); preregistro intacto.

## Data Status

- Datasets: `experiments/data/market_BTCUSDT_60min.csv` (67.141 barras,
  2019-01→2026-08), `market_XXBTZUSD_60min.csv` (Kraken ~30d), `sample_1h.csv`.
  CSVs gitignored; manifests versionados.
- `data/quality.py` presente pero incompleto en su resumen (F-09); loader
  reordena (F-10).
- `data/raw/` gitignored (caché de adquisición FINAL_OOS).

## Recommended Remediation Order

1. **F-05** (equity SHORT) — correctness de P&L; base de todo lo demás.
2. **F-01** (`_day_key` UTC) — reproducibilidad del reset diario.
3. **F-09 + F-10** (data quality completa + orden obligatorio) — puerta de datos.
4. **F-02 + F-03** (veto de riesgo real: `max_strategy_risk` + exposición).
5. **F-07** (tabla de transiciones de `OrderManager`).
6. **F-06** (ledger interno real para reconciliación contable).
7. **F-08** (sizing entero/step para contratos).
8. **F-11** (validación del bloque `risk` de producción).
9. **F-04** (contrato de estado de riesgo en paper).
10. S-02/S-04 (cobertura de ramas de posición y estados UNKNOWN).
11. Añadir CI (pytest + coverage) — no existe pipeline.

---

## Distinción explícita de estados

- **Tests passed**: sí — 798 passed, `compileall` limpio.
- **Correctness**: **NO** — F-05 (equity SHORT), F-01 (TZ) afectan resultados.
- **Operational readiness**: parcial — paper/forward operativo, pero con F-02/F-03
  (veto incompleto) y F-06 (reconciliación contable tautológica).
- **Production readiness**: **NO** — sin broker real (por diseño), sin veto de
  portafolio, sin validación de config de producción, sin CI. **Ningún estado de
  "production ready"** se declara.
