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

### F-06 — `_internal_snapshot()` toma la cuenta del propio broker
- ID: F-06
- severidad: **MEDIUM**
- archivo: `papertrading/engine.py` (`_internal_snapshot`)
- descripción: `account = self.broker.get_account_state()`; el "interno" usa al
  broker como fuente para la cuenta, por lo que la reconciliación contable
  (`ACCOUNT`) compara broker contra broker → siempre MATCH, no detecta
  divergencias contables internas.
- impacto: la reconciliación de cuenta es tautológica; solo órdenes/fills/posiciones
  aportan contraste real.
- evidencia: código; `reconciliation/engine.py` compara `internal.account` vs
  broker.
- test existente: `test_reconciliation.py`, `test_paper_trading.py`.
- test necesario: inyectar un estado interno contable independiente y detectar
  mismatch.
- recomendación: mantener un ledger interno (cash/equity propios) y reconciliarlo.

### F-07 — `OrderManager` admite transiciones de estado inválidas
- ID: F-07
- severidad: **HIGH**
- archivo: `execution/order_manager.py:156-166`
- función: `_transition`
- descripción: solo rechaza transiciones **desde** estado terminal. No valida el
  grafo: p. ej. `CREATED → SUBMITTED` (sin `APPROVED`), `ACKNOWLEDGED → APPROVED`,
  `APPROVED → CREATED`… todas permitidas. Las transiciones legales no están
  codificadas.
- impacto: ciclo de vida de orden laxo; un adapter/estado inconsistente podría
  producir secuencias imposibles sin error.
- evidencia: `_transition` no consulta estado actual.
- test existente: `test_orders.py`.
- test necesario: tabla de transiciones permitidas + rechazo de las inválidas.
- recomendación: definir tabla explícita (ver abajo) y aplicarla en `_transition`.

Tabla de transiciones propuesta (permitidas):

```
CREATED        -> RISK_CHECKED, APPROVED, REJECTED, CANCELLED
RISK_CHECKED   -> APPROVED, REJECTED, CANCELLED
APPROVED       -> SUBMITTING, SUBMITTED, REJECTED, CANCELLED
SUBMITTING     -> SUBMITTED, ACKNOWLEDGED, UNKNOWN, FAILED, REJECTED
SUBMITTED      -> ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED, UNKNOWN, FAILED
ACKNOWLEDGED   -> PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED, UNKNOWN, FAILED
PARTIALLY_FILLED -> PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, UNKNOWN, FAILED
FILLED/CANCELLED/REJECTED/EXPIRED/FAILED/UNKNOWN -> (terminal: ninguna)
```
(UNKNOWN se modela como terminal en `OrderStatus.terminal`; su resolución
correcta requeriría un estado no terminal — hallazgo adicional, ver F-15.)

### F-08 — Position sizing produce cantidades fraccionarias sin soporte de enteros
- ID: F-08
- severidad: **MEDIUM**
- archivo: `risk/position_sizing.py` (`position_size_from_stop`), `risk/engine.py:154-173`
- descripción: la cantidad es `risk_cap / loss_per_unit`, continua
  (`float`). No hay redondeo a contratos enteros ni `quantity_step`, pese a que
  instrumentos como NQ/ES exigen enteros. Los defaults (`min_size=0.001` en
  `config/final_oos`) asumen fraccionario (cripto).
- impacto: para mercados de contrato entero, el sizing no es operable tal cual.
- evidencia: `position_sizing.py:70`.
- test existente: `test_position_sizing.py`, `test_units.py`.
- test necesario: caso entero (round/step) o documentar que el MVP es fraccional.
- recomendación: añadir `quantity_step`/`integer_contracts` como configuración.

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

### F-11 — `config/production/settings.yaml`: `null` en métricas de riesgo
- ID: F-11
- severidad: **HIGH**
- archivo: `config/production/settings.yaml` (`risk.*: null`)
- descripción: `max_open_positions/risk_per_trade/max_daily_loss/max_drawdown = null`.
  `config.load_config` **no** valida el bloque `risk`; `RiskConfig` solo se
  construye desde el `config.yaml` de experimento, no desde `settings.yaml`. No
  hay camino que hoy consuma `settings.yaml.risk`, pero tampoco hay guard: si se
  consume, `null` no es `float`/`int` y `RiskConfig.__post_init__` compararía
  `null` → `TypeError` o valores inválidos.
- impacto: configuración de producción aparentemente sin límites de riesgo; sin
  validación mecánica.
- evidencia: `config/production/settings.yaml`; `config/loader.py` no toca `risk`.
- test existente: `test_config.py` (no valida `risk`).
- test necesario: validar que producción exige métricas de riesgo no nulas.
- recomendación: validación explícita del bloque `risk` por entorno.

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
