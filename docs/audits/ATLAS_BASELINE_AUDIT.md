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

### F-03 — exposure/leverage/margin/correlation/concentration — **IMPLEMENTED (paper phase 1) / PARTIAL**
- ID: F-03
- severidad: **HIGH** (gross/net/leverage/concentration de paper resueltos; margin/correlation siguen gap)
- archivo: `risk/engine.py`, `risk/types.py` (`RiskConfig`/`AccountRiskState`), `papertrading/engine.py`
- **estado: IMPLEMENTED (paper phase 1)** para **gross**, **net**, **leverage** e
  **instrument concentration**. **Pendiente**: margin/correlation
  (`INFRASTRUCTURE GAP`) y **backtest fuera de alcance**.
- **Implementación (sin commit todavía)**:
  - `RiskConfig` añade `max_gross_exposure`/`max_net_exposure`/`max_leverage`/
    `max_instrument_concentration` (`float | None = None`; `None` = gate
    deshabilitado; validación `> 0` cuando no es `None`; sin defaults numéricos).
  - `AccountRiskState`: `gross_exposure`/`net_exposure` pasan a `float | None`
    (default `None`, nunca `0.0` como "sin dato") + `exposure_by_instrument:
    dict[str, float] | None`.
  - `RiskEngine.validate` evalúa 4 gates (solo si el límite está configurado;
    métrica `None` con gate activo = fail-closed; `equity<=0` = fail-safe ya
    existente). No usa `portfolio.compute_portfolio_risk()`.
  - `PaperTradingEngine._risk_state()` puebla las métricas desde
    `AccountLedger.valuation(self._marks())` (fuente autoritativa única); nunca
    del broker; valuation incompleta ⇒ `None`.
  - Tests: `tests/test_risk_exposure_gates.py` (gates unit + integración paper).
- **Hallazgo original**: el veto de riesgo no incluye exposición (gross/net),
  leverage, margin, correlación ni concentración; `AccountRiskState` expone
  `gross_exposure/net_exposure/margin_usage` sin que `validate` los compruebe.
- **Fuente autoritativa única (F-06B)**: `AccountLedger.valuation(marks) ->
  ValuationSnapshot` (commit `eeac54d`): `gross_exposure = Σ|q·mark|`,
  `net_exposure = Σ q·mark` (**signed**), `equity`, `unrealized_pnl`,
  `exposure_by_instrument` (notional firmado). `point_value=1`, USD, spot.
- **Contrato congelado (decisiones cerradas; sin ambigüedad residual):**

#### 1. Gross exposure
- Definición: `gross_exposure = Σ |quantity × mark|` (USD).
- Fuente: `ValuationSnapshot.gross_exposure`.
- Gate fase 1: `gross_exposure <= max_gross_exposure` (**absoluto**, USD).
- `max_gross_exposure = None` ⇒ gate deshabilitado. **No** existe `max_gross_ratio`
  (el control relativo a equity lo cubre leverage).

#### 2. Net exposure
- Definición: `net_exposure = Σ quantity × mark` (**signed**: LONG `+`, SHORT `−`).
- Fuente: `ValuationSnapshot.net_exposure`.
- Gate fase 1: `abs(net_exposure) <= max_net_exposure` (**absoluto**, USD).
- `max_net_exposure = None` ⇒ gate deshabilitado. **No** hay límites net-long/net-short
  separados en fase 1.
- **Prohibido** usar `abs(long_exposure − short_exposure)` ni la semántica de
  `portfolio.compute_portfolio_risk()`.

#### 3. Leverage
- Definición: `leverage = gross_exposure / equity`.
- Gate: `leverage <= max_leverage` (`max_leverage` configurable; alcance inicial
  = **account**). `None` ⇒ deshabilitado. Sin lógica broker-specific.
- `equity <= 0` con gate activo ⇒ leverage **no evaluable → fail-closed**.

#### 4. Concentration
- Fase 1 **solo instrumento**:
  `instrument_concentration_i = abs(exposure_by_instrument[i]) / equity`.
- Gate: `max_i(instrument_concentration_i) <= max_instrument_concentration`
  (fracción de equity). `None` ⇒ deshabilitado. Numerador en **magnitud absoluta**.
- `equity <= 0` con gate activo ⇒ **fail-closed**.
- **Deferred**: concentración por strategy/market/currency/economic factor (sin
  fuente autoritativa en F-06).

#### 5. `None` y cero (ratificado)
- Límite `None`/ausente = gate deshabilitado.
- Métrica `0.0` = cero real.
- Métrica no disponible = `None`, **nunca** `0.0`.
- **Los defaults `0.0` actuales de `AccountRiskState.gross_exposure/net_exposure`
  son ambiguos y deberán eliminarse** en la implementación de F-03.

#### 6. Valuation incompleta
- `valuation() = None` (posición abierta sin mark válido): si el gate está
  deshabilitado (`limit=None`) no se evalúa; si el gate está activo ⇒ **no
  evaluable → fail-closed**. Nunca sustituir por `0.0`; nunca usar el broker como
  fallback del expected risk state.

#### 7. `AccountRiskState` (requisito de implementación futura)
- `gross_exposure: float | None`, `net_exposure: float | None` (y análogo para
  cualquier métrica realmente no disponible). **No** cambiar ahora.

#### 8. Alcance
- F-03 fase 1 = **paper trading** (`PaperTradingEngine` obtiene las métricas de
  `AccountLedger.valuation()`). **Backtest fuera** hasta disponer de
  `AccountLedger` equivalente o fuente semánticamente equivalente. **No** crear
  una segunda implementación de gross/net para simetría.

#### 9. Margin — **INFRASTRUCTURE GAP**
- No implementar. Requiere: initial/maintenance margin por instrumento, available
  margin, projected margin after order y política contractual de rechazo (§20).

#### 10. Correlation — **INFRASTRUCTURE GAP**
- No implementar. Requiere: series de precios, ventanas, matriz y definición de
  exposición efectiva correlacionada (§11-12).

#### 11. `portfolio.compute_portfolio_risk()`
- **No** es fuente de verdad para F-03; **no** conectarlo a `RiskEngine`.
- Su `total_net_exposure = abs(long − short)` **no** sustituye el signed net de
  `AccountLedger`. **Legacy/no-wired** (marcado para limpieza posterior).

#### 12. Ubicación de configuración (futuro)
- Los límites pertenecen a `RiskConfig` cuando se implemente F-03:
  `max_gross_exposure`, `max_net_exposure`, `max_leverage`,
  `max_instrument_concentration`. **No** añadirlos ahora al modelo ni a YAML.
- Coherencia con F-11: ausencia / `None` = gate disabled; **no** inventar defaults.

| Gate | Fórmula | Fuente | Límite/config | `equity <= 0` | valuation incompleta | Estado |
|---|---|---|---|---|---|---|
| Gross | `Σ\|q·mark\|` | `ValuationSnapshot.gross_exposure` | `max_gross_exposure` (USD, abs) | n/a (absoluto) | fail-closed si activo | IMPLEMENTED (paper) |
| Net | `Σ q·mark` (signed) | `ValuationSnapshot.net_exposure` | `max_net_exposure` (USD, abs) | n/a (absoluto) | fail-closed si activo | IMPLEMENTED (paper) |
| Leverage | `gross / equity` | `ValuationSnapshot` | `max_leverage` (account) | fail-safe (raise) | fail-closed | IMPLEMENTED (paper) |
| Concentration | `max_i(\|exposure_i\|/equity)` | `ValuationSnapshot.exposure_by_instrument` | `max_instrument_concentration` | fail-safe (raise) | fail-closed | IMPLEMENTED (paper, solo instrumento) |
| Margin | §20 | ninguna | ninguna | — | — | INFRASTRUCTURE GAP |
| Correlation | §11-12 | ninguna | ninguna | — | — | INFRASTRUCTURE GAP |

- **Limitación documentada (sin inventar semántica)**: el gate evalúa la
  exposición **autoritativa actual** del estado de cuenta (pre-trade). El
  contrato congelado no define "exposición proyectada post-trade", por lo que
  **no** se modela. Con el motor actual (máx. 1 posición, sin señal si ya hay
  posición) la exposición en el momento de la señal es la vigente.
- **Pendiente para cerrar F-03**: margin/correlation (infraestructura) y
  extender a **backtest** (requiere `AccountLedger`/fuente equivalente).

### F-04 — `PaperTradingEngine._risk_state()` fija kill_switch/account_active/strategy_active — **REFUTED (separación contractual)**
- ID: F-04
- severidad: **LOW** (reclasificada de MEDIUM)
- archivo: `papertrading/engine.py` (`_risk_state`)
- **estado: REFUTED** — no es un bug; es separación de responsabilidades.
- **Investigación**: `RiskEngine.validate` comprueba `strategy_active`,
  `account_active` y `kill_switch`. `_risk_state()` pasa `kill_switch=False`,
  `account_active=True`, `strategy_active=True`, y el kill switch se aplica en
  su **propio gate** en `_execute_pending` (`self.kill_switch.status().can_trade`).

| Campo | Valor actual | Fuente real disponible | ¿Hardcode correcto? | ¿Debe cambiar? |
|---|---|---|---|---|
| kill_switch | `False` | `self.kill_switch.status().can_trade` | **Sí (separación B5)** | **No** |
| account_active | `True` | ninguna (no hay lifecycle de cuenta) | Sí (contractual) | No |
| strategy_active | `True` | ninguna (1 estrategia siempre activa) | Sí (contractual) | No |

- **kill_switch**: B5 es una **capa de autorización separada** (`06 §36-37`,
  `03 §36-37`: "el monitor detecta, B5 decide"), con un único punto de
  enforcement (`_execute_pending`). Duplicarla dentro de `RiskEngine` sería un
  **segundo punto de decisión** sobre la misma condición (el propio `06` describe
  kill switches como capa aparte del risk sizing). Los tests existentes
  (`test_active_kill_switch_never_reaches_broker`,
  `test_fail_then_recover_then_trade_again`) **codifican** que el bloqueo se
  registra como `ENTRY_SKIPPED_KILL_SWITCH` (gate de ejecución), **no** como
  `RISK_REJECTED`.
- **Verificación empírica**: al cablear `kill_switch` en `_risk_state()` (probado
  y revertido), el veto se movía al RiskEngine (`RISK_REJECTED`) y **rompía 2
  tests** que documentan el diseño de un solo gate. Eso confirma que el `False`
  es **deliberado**, no una desincronización.
- **account_active**: no existe estado de cuenta/suspensión (ni en
  `PaperBrokerAdapter`, ni config, ni método). En paper la cuenta está siempre
  abierta → constante contractual.
- **strategy_active**: no existe enable/disable/halt por estrategia; el engine
  opera una única estrategia siempre activa → constante contractual.
- **Conclusión**: no hay divergencia de contrato. No se modifica.
- **No se tocó** F-02/F-03/F-06/F-07.

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

### F-06 — ledger interno autoritativo + ACCOUNT reconciliation — **RESOLVED**
- ID: F-06
- severidad: **MEDIUM**
- archivo: `papertrading/ledger.py`, `papertrading/engine.py`, `brokers/paper.py`, `reconciliation/orchestrator.py`
- **estado: RESOLVED — F-06A ✅ / F-06B ✅ / F-06C-RESTART ✅ / F-06C ACCOUNT ✅.**
- **Hallazgo original: CONFIRMADO y CORREGIDO (ACCOUNT tautológico).**
  `_internal_snapshot().account` obtenía la cuenta del broker
  (`self.broker.get_account_state()`), igual que `broker_state_from_adapter()`
  (`orchestrator.py`); la reconciliación ACCOUNT era **tautológica** (siempre
  MATCH). Ahora `expected.account` procede del **ledger** y `actual.account` del
  **broker** (fuentes independientes): una divergencia real produce ACCOUNT
  MISMATCH → RECONCILIATION_BLOCKED.
- **F-06A — IMPLEMENTED, cerrado en commit `4bb66c2`**:
  - `AccountLedger` (`papertrading/ledger.py`) = **autoridad interna única** de
    `cash`, `positions` (quantity dirigida + avg_entry) y `realized_pnl`.
  - Bootstrap **por configuración** (`initial_cash`; nunca del broker).
  - `PaperTradingEngine._position` es **proyección** (metadata operativa), no
    autoridad contable; `realized_pnl` delegado al ledger.
  - Fill identity estable `(client_order_id, fill_sequence)`; idempotencia
    individual **y** de lote multi-fill.
  - Accounting spot/`point_value=1`/USD: BUY `cash-=q·p`, SELL `cash+=q·p`;
    LONG/SHORT/reducción/cierre preservados.
- **F-06B — IMPLEMENTED, cerrado en commit `eeac54d`**:
  - Derivaciones internas (estado **derivado**, no primario) en
    `AccountLedger.valuation(marks) -> ValuationSnapshot`:
    `equity = cash + Σ quantity·mark` (sin `abs`; conserva F-05),
    `unrealized = Σ quantity·(mark − avg_entry)`,
    `gross = Σ |quantity·mark|`, `net = Σ quantity·mark`, `by_instrument` (signed
    notional).
  - Marks del runtime de mercado (`PaperTradingEngine._marks()`, mismo
    `price_source` que el PaperBroker); mark inválido/ausente con posición
    abierta → `valuation()=None` (fail-closed); sin posiciones → `equity=cash`.
  - `PaperTradingEngine._mark()` deriva de `ledger.cash` + `ledger.valuation()`
    en lugar de `broker.get_account_state()`; salud de broker/monitoring intactos.
- **F-06C-RESTART — IMPLEMENTED, cerrado en commit `c722896`**: al derivar
  `expected_account` del ledger, el restart revela que el `PaperBrokerAdapter`
  **no rehidrataba** su estado observed (broker fresco → ACCOUNT MISMATCH →
  kill switch). Se implementó persistencia **independiente** del broker:
  - `PaperBrokerAdapter.snapshot(checkpoint_id)` / `load_snapshot(snap,
    checkpoint_id)`: persiste/restaura `cash`, `_broker_seq` (contador de IDs),
    `_orders` (con `fills`, estados y `fill_sequence` estable) y `_lossy`.
    **NO** persiste marks/`price_source` (datos de mercado). **NO** usa el ledger.
  - `PaperTradingEngine.snapshot()` añade `checkpoint_id` común y bloque
    `broker` (además de `ledger`); `load_snapshot()` restaura **primero** el
    ledger y **después** el broker, verificando `ledger.checkpoint ==
    broker.checkpoint`. Restart con `ledger` sin bloque `broker` (legacy),
    checkpoint mismatch o bloque corrupto → `BrokerSnapshotError` (**fail-closed**;
    nunca broker fresco en silencio).
  - New run (sin snapshot) sigue bootstrapping desde configuración; A4/
    `ForwardRunner` intacto (reconstruye ambos por replay de `bars.jsonl`).
  - Tests: `tests/test_broker_restart.py` (25) + `TestRestart` adaptado.
  - **Restore ATOMICO**: `PaperBrokerAdapter.load_snapshot` parsea todo el
    estado temporal y solo hace swap al final (nunca mutacion parcial);
    `validate_snapshot`/`AccountLedger.probe_snapshot` permiten al engine
    prevalidar ledger+broker+metadata ANTES de aplicar, de modo que un snapshot
    invalido deja el engine intacto (ISS-1/ISS-2 de la review, corregidos).
- **F-06C ACCOUNT esperado — IMPLEMENTED (sin commit)**: `_internal_snapshot()`
  ahora construye `expected_account` desde el **ledger**:
  `cash = ledger.cash`, `equity = ledger.valuation(current_marks).equity` (mark
  del runtime `_marks()`, mismo que el broker, fijado por `_mark()`).
  `actual_account` sigue procediendo del broker (`broker_state_from_adapter`).
  Si `valuation()` es `None` (posición abierta sin mark válido) →
  `account=None` (UNKNOWN), sin fabricar equity ni consultar al broker.
  No hay ruta `broker → expected`. `ReconciliationEngine`/tolerancia 1e-9 y
  `broker_state_from_adapter` **no** tocados; POSITION/ORDER/FILL intactos.
  Tests: `tests/test_account_reconciliation.py` (17) + `TestRestart` adaptado.
- **ORDER/FILL/POSITION** ya eran independientes (OrderManager/`_position` vs
  broker); no afectados.
- **Fuera de F-06 (deuda F-06A/B)**: **I2** reversión LONG↔SHORT (semántica
  pre-existente, sin re-anclaje de avg_entry), **I3** snapshots legacy restauran
  `applied_fills=[]`, **I4** comisiones no aplicadas (fase posterior).
- **Issues no bloqueantes conocidos (F-06B)**: **I5** `BROKER_ERROR` usado como
  etiqueta de audit para mark inválido (semánticamente sería market-data/
  degraded; no existe taxonomía equivalente, no se inventa). **I6** `_valid_mark`
  exige `finite && >0` (supuesto razonable para el modelo spot de Fase 1).
- **Evidencia**: `papertrading/ledger.py`, `papertrading/engine.py`,
  `brokers/paper.py`, `tests/test_broker_restart.py`,
  `tests/test_account_reconciliation.py`, `reconciliation/orchestrator.py`.
  Suite **938 passed**; `ledger.py` 100%. ACCOUNT match/mismatch, tolerancia,
  valuation-unavailable, independencia (broker→expected prohibido) y restart
  (consistente→MATCH / divergente→MISMATCH) cubiertos por tests.
- **RESOLVED.** Deuda I2/I3/I4/I5/I6 sigue **fuera** de F-06 (no bloqueante).

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
