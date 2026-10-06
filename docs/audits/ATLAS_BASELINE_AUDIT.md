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
- Sin CI en el baseline: no existía `.github/`, `.gitlab-ci.yml` ni
  `azure-pipelines.yml`. **Resuelto**: existe `.github/workflows/ci.yml`
  (commit `ffb5942`).
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

## Current Status (post-remediation) — `HEAD = 3fb3203`

Estado verificable actual (los conteos de arriba son el **baseline histórico**
en `HEAD = 53625fb`; se conservan como referencia de auditoría):

- Suite: **976 passed**; `compileall` exit 0; coverage global **86%**.
- Findings **CLOSED**: F-01, F-02, F-03 (paper projected pre-fill), F-05, F-06
  (A/B/C-RESTART/C ACCOUNT), F-07, F-09/F-10. **REFUTED**: F-04. **CI**: cerrado
  (`.github/workflows/ci.yml`).
- Siguen **abiertos/deferred**:
  - **F-08** — `CONTRACT GAP — NOT APPLICABLE TO CURRENT INSTRUMENT CONTRACT`
  (preventivo para instrumentos integer-only; sin bug en el alcance actual).
  Subfindings: **F-08-a** (quantity NaN/Inf, **`CLOSED — REFUTED`**: sin ruta
  productiva) y **F-08-b** (instrument quantity metadata, `INFRASTRUCTURE GAP`).
  - **F-11** — **`CLOSED — REFUTED`** (claim de riesgo operativo: `config/
  production/settings.yaml` es **template/placeholder/no-runtime**; el arranque
  inseguro por `null` queda **refutado**). El rol productivo futuro queda
  **deliberadamente diferido** (bloque F-11 separado). Subfindings: **F-11-a**
  (superficie de config F-03, **`IMPLEMENTED`**: los 4 límites configurables vía
  `config/paper/settings.yaml`), **F-11-b** (validación estructural del bloque
  `risk`, **`CONTRACT GAP — NO CURRENT OPERATIONAL IMPACT`**; subfindings
  **F-11-b-1/-2/-3**), **F-11-c** (cotas superiores F-03, **`CLOSED — REFUTED`**).
  - **margin / correlation** — `INFRASTRUCTURE GAP` (F-03 fase 1 no los modela).
  - **backtest** — fuera de alcance de F-03 (requiere `AccountLedger`/fuente
    equivalente).
  - **F-06-RECOVERY** — **`CLOSED — REFUTED`**: `snapshot()/load_snapshot()` es
    **API de soporte/test**; el recovery productivo es **replay** de `bars.jsonl`
    (`ForwardRunner.start()`); `load_snapshot()` no es mecanismo de recovery
    operativo. Ver F-06-RECOVERY.
- Nota: la sección «Test Baseline» y las líneas de evidencia por-finding
  (`798/807/817/833/845 passed`) son **históricas** y documentan cuándo se cerró
  cada hallazgo; no describen el estado actual.

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
| Portfolio | `portfolio/engine.py` (exposición descriptiva; **no integrado por diseño** — no es la fuente de exposición; ver S-05-portfolio). |
| Database | `database/models.py`, `database/session.py` (schema-first; **sin rol de runtime definido**; ver S-05-db). |
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

### F-03 — exposure/leverage/margin/correlation/concentration — **IMPLEMENTED (projected, paper) / PARTIAL**
- ID: F-03
- severidad: **HIGH** (gross/net/leverage/concentration projected de paper resueltos; margin/correlation siguen gap)
- archivo: `risk/engine.py`, `risk/types.py` (`RiskConfig`/`ProjectedExposure`), `papertrading/engine.py`
- **estado: IMPLEMENTED (projected enforcement pre-fill, paper)** para **gross**,
  **net**, **leverage** e **instrument concentration**. **Pendiente**:
  margin/correlation (`INFRASTRUCTURE GAP`) y **backtest fuera de alcance**.
- **Implementación (sin commit todavía)**:
  - `RiskConfig` añade `max_gross_exposure`/`max_net_exposure`/`max_leverage`/
    `max_instrument_concentration` (`float | None = None`; `None` = gate
    deshabilitado; validación `> 0` cuando no es `None`; sin defaults numéricos).
  - `ProjectedExposure` (value object **frozen/efímero**): `equity`,
    `gross_exposure`, `net_exposure`, `exposure_by_instrument`. Derivado de
    ledger + orden + precio de ejecución; **no** es autoridad ni guarda
    posiciones.
  - `RiskEngine.validate_projected_exposure(projected)` evalúa los 4 gates
    (solo si el límite está configurado; métrica `None` con gate activo =
    fail-closed). `RiskEngine.validate` **ya no** aplica los gates F-03 sobre el
    estado actual (eliminado el doble veto). No usa
    `portfolio.compute_portfolio_risk()`.
  - `PaperTradingEngine._execute_pending` ejecuta el gate **pre-fill** (último
    punto antes del fill) con el **precio de ejecución** (`open(t+1)` en paper
    MARKET); si se viola o la métrica no está disponible ⇒ `RISK_REJECTED` y la
    orden **no** llega al broker ni al ledger. `_projected_exposure()` construye
    el value object desde `AccountLedger` (autoridad) + signed quantity.
  - Tests: `tests/test_risk_exposure_gates.py` (gates projected unit +
    integración pre-fill, incluido el ejemplo obligatorio de la auditoría).
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
| Gross | `Σ\|q·mark\|` | projected desde `AccountLedger` | `max_gross_exposure` (USD, abs) | n/a (absoluto) | fail-closed si activo | IMPLEMENTED (projected, paper) |
| Net | `Σ q·mark` (signed) | projected desde `AccountLedger` | `max_net_exposure` (USD, abs) | n/a (absoluto) | fail-closed si activo | IMPLEMENTED (projected, paper) |
| Leverage | `gross / equity` | projected desde `AccountLedger` | `max_leverage` (account) | fail-closed | fail-closed | IMPLEMENTED (projected, paper) |
| Concentration | `max_i(\|exposure_i\|/equity)` | projected desde `AccountLedger` | `max_instrument_concentration` | fail-closed | fail-closed | IMPLEMENTED (projected, paper, solo instrumento) |
| Margin | §20 | ninguna | ninguna | — | — | INFRASTRUCTURE GAP |
| Correlation | §11-12 | ninguna | ninguna | — | — | INFRASTRUCTURE GAP |

- **Evolución**: `a185cf7` implementó el gate sobre la exposición **actual**
  (pre-trade), que en el motor mono-instrumento (flat al decidir) **no vinculaba**
  la orden. Se sustituyó por el **gate proyectado pre-fill** (ver subsección
  «F-03 projected exposure (post-trade)»), que evalúa la exposición resultante de
  aceptar la orden al precio de ejecución. `RiskEngine.validate` ya **no** veta
  por exposición actual (se eliminó el doble control).
- **Pendiente para cerrar F-03**: margin/correlation (infraestructura) y extender
  a **backtest** (requiere `AccountLedger`/fuente equivalente).

---

#### F-03 projected exposure (post-trade) — IMPLEMENTED (paper)

**A. Current-state gap.** Auditoría posterior a `a185cf7`:
**`CURRENT-STATE GATE IS CORRECT BUT POST-TRADE GAP EXISTS`**. El gate evalúa la
exposición **actual**; en el motor mono-instrumento (sin pyramiding, `validate`
solo en flat) esa exposición es 0, así que los 4 gates no vinculan la orden que
los dispara. Ejemplo: `equity=1_000_000`, `max_gross_exposure=100_000`, orden
nueva notional `125_375`: `validate` ve `gross=0` (aprueba) y el fill crea
`gross=125_375` (límite superado).

**B. Projected exposure contract.**
`projected_state = current_state + proposed_order_effect`, determinista y sin
segunda fuente de posiciones:
- `order_signed_qty = +qty` (BUY) / `-qty` (SELL); `qty =
  decision.approved_quantity` (sizing ya aplicado).
- `projected_qty[i] = current_qty[i] + order_signed_qty` (solo el instrumento de
  la orden; el resto sin cambio). `current_qty` proviene de
  `AccountLedger.valuation(marks)` (estado actual); **no** del broker.
- El instrumento propuesto se **crea** en `projected_exposure_by_instrument`
  aunque hoy no tenga posición.

**C. Execution-time enforcement (decisión clave).**
- Una evaluación **anticipada** (en la señal) puede usar el reference mark
  actual y es **orientativa**, no el enforcement.
- El **gate bloqueante** se ejecuta en el **último punto pre-fill**, con el
  **precio de ejecución disponible en ese momento**. En paper MARKET ese precio
  es el **`open` de `t+1`**, inmediatamente antes de aplicar el fill
  (`_execute_pending` → `_set_price(open)`).
- Objetivo: garantizar que la orden aceptada **no** produzca exposición superior
  al límite **al precio efectivo de ejecución conocido**.
- **Un gap respecto al `close` puede invalidar una aprobación anticipada**: el
  check final pre-fill es el **enforcement efectivo**. No se modela slippage
  adicional ni mercado adverso más allá del precio de ejecución realmente
  disponible.
- `None` (valuation incompleta) + gate activo ⇒ fail-closed también aquí.

**D. Fórmulas.** Con `P_i` = precio de ejecución (ver C):
`projected_notional[i] = projected_qty[i] × P_i`;
`projected_exposure_by_instrument = {i: projected_notional[i]}`;
`projected_gross = Σ|projected_notional[i]|`;
`projected_net = Σ projected_notional[i]` (signed);
`projected_leverage = projected_gross / current_equity` (ver F);
`projected_concentration = max_i(|projected_notional[i]| / current_equity)`.
Comparación: `projected_gross <= max_gross_exposure`,
`abs(projected_net) <= max_net_exposure`,
`projected_leverage <= max_leverage`,
`projected_concentration <= max_instrument_concentration`.

**E. Operaciones.** Mismo mecanismo de signed quantity para todas.

| Operación | Estado proyectado | ¿Evalúa gross/net/leverage/concentration? | Regla |
|---|---|---|---|
| Apertura desde flat | `q_proj = order_signed_qty` | Sí (los 4) | Crear instrumento y comparar proyectado; rechazar si algún gate activo se viola |
| Aumento | `q_proj = q_cur + order_signed_qty` (mismo signo) | Sí (los 4) | Sumar al notional existente (no bypass) |
| Reducción | `q_proj = q_cur - \|q\|` (mismo signo, menor magnitud) | Sí (los 4) | Proyectado ≤ actual ⇒ no puede empeorar; sin regla especial |
| Cierre | `q_proj = 0` | Sí (los 4) | Proyectado = 0 ⇒ pasa; mismo camino que las demás |
| Reversal | `q_proj = q_cur + order_signed_qty` (p. ej. `+10` y `-15` ⇒ `-5`) | Sí (los 4) | Usar `q_proj` resultante; **nunca** sumar magnitudes LONG/SHORT |

- **Caso contractual de apertura**: `equity=1_000_000`, `gross actual=0`, orden
  `125_375` notional, `max_gross=100_000` ⇒ **REJECT antes del fill**.

**F. Equity de referencia.** Denominador de los límites relativos (leverage,
concentración) = **`current_equity`** (equity de referencia **pre-trade**, de
`AccountLedger.valuation(marks)`). No implica que el equity post-fill real sea
idéntico; **no** incorpora todavía fees/commission ni P&L no modelado. En el
modelo spot/`point_value=1` ejecutar al mark es equity-neutral (`equity = cash +
Σ q·mark`; `cash − q·P` y `+q·P` se cancelan), pero el fill real puede diferir
del mark. `equity <= 0` con gate relativo activo ⇒ fail-closed.

**G. Pending/partial/fees (deferred).**
- **Órdenes pendientes**: fase actual = **una** pendiente, **sin** reserva de
  exposición de múltiples órdenes; el check final pre-fill reevalúa con el estado
  actual y el precio efectivo. Múltiples pendientes/pyramiding = **extensión
  futura**.
- **Parciales/multi-fill**: fuera de alcance (entrada MARKET de paper atómica).
  Enforcement por fill parcial = **extensión futura**.
- **Fees/commission**: `AccountLedger.apply_fill` **no** modela coste
  (`BrokerFill.commission` default `0.0`; I4). La exposición proyectada **no**
  incorpora costes y **no** se afirma que el límite sea neto de costes.
  Incorporarlos requiere **ampliación contractual posterior**.

**H. Current-state vs projected gate.** Para una **nueva orden**, el control
material de F-03 es el **estado proyectado**. **No** se mantiene un segundo veto
independiente sobre la exposición actual si solo duplica el mismo control sin
protección adicional. Pueden conservarse funciones auxiliares actuales, pero
semánticamente F-03 tiene **una sola decisión de riesgo por orden**, basada en el
estado proyectado. El projected **subsume** al current-state (sin orden,
proyectado = actual; reducción/cierre no empeoran).

**I. Decisiones futuras/deferred.**
1. Reserva de exposición para múltiples órdenes pendientes / pyramiding.
2. Enforcement durante fills parciales / revalidación por fill.
3. Modelado de fees/commission/slippage (hoy fuera; I4).
4. Extensión a **backtest** (requiere `AccountLedger`/fuente equivalente).
5. margin/correlation (`INFRASTRUCTURE GAP`, sin cambios).
6. Semánticas ratificadas que **no** cambian: gross abs, net signed, leverage,
   concentración por instrumento, `None=disabled`, fail-closed, paper phase 1.

**Estado del contrato projected: `IMPLEMENTED (paper, sin commit)`.** Enforcement
pre-fill con precio de ejecución (`open(t+1)`); projected state desde
`AccountLedger`; F-03 no modela fees/commission/slippage; margin/correlation
`INFRASTRUCTURE GAP`; backtest deferred.

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

### F-06-RECOVERY — gap de recovery `load_snapshot` (ledger↔broker tras restart) — **CLOSED — REFUTED**
- ID: F-06-RECOVERY (subfinding posterior a F-06C; **no** reabre F-06C).
- **estado: `CLOSED — REFUTED`** (HEAD `e4d6f99`). No existe production role; el
  recovery productivo actual es **replay**.
- **Decisión contractual ratificada**:
  > `PaperTradingEngine.snapshot()/load_snapshot()` es una **API de soporte/test**.
  > `snapshot()` también se escribe como **artefacto de lineage** en A4/streaming,
  > pero `load_snapshot()` **no** es un mecanismo de recovery operativo. El recovery
  > productivo actual es **replay de `bars.jsonl`** mediante
  > `ForwardRunner.start()`, que reconstruye broker y ledger juntos.
  > `load_snapshot()` **no garantiza** recuperación de órdenes operativas, adopción
  > de fills, ni persistencia de `OrderManager`, `_pending` o `_pending_resolution`;
  > **no sustituye al replay**. Las invariantes de **F-06C** siguen vigentes para la
  > **consistencia del snapshot**. Convertir `load_snapshot()` en recovery operativo
  > requeriría un **contrato separado**.
- **Distinción producción**: `snapshot()` producción = **artefacto** (lineage/estado);
  `load_snapshot()` producción = **no usado**; **replay = recovery productivo**. Las
  divergencias de `OrderManager`/pending tras `load_snapshot()` **no** son
  production bug (capacidad no contratada).
- **Evidencia**: `load_snapshot()` del engine tiene **cero callers productivos** (solo
  `test_paper_trading`, `test_broker_restart`, `test_account_reconciliation`);
  `snapshot()` es invocado por `forward_runner._write_snapshot` (`snapshot.json`)
  como artefacto; el restart de A4/streaming reproduce `bars.jsonl`. **No es un
  `IMPLEMENTATION BUG` productivo.**
- **Separación respecto a F-06C**: **F-06C permanece `CLOSED`.** Sus invariantes
  siguen intactas y verificadas en `engine.snapshot()`/`load_snapshot()`:
  - snapshot **envelope**;
  - `checkpoint_id` consistente (raíz == ledger == broker);
  - **ledger y broker independientes**;
  - **restore atómico** (prevalidar → swap);
  - **no reconstrucción cruzada**.
  Este gap posterior **no** invalida esas invariantes.
- **Estado actual del recovery (dos rutas)**:
  1. **A4/streaming (producción)**: recovery por **replay** de `bars.jsonl`
     (`ForwardRunner.start()`); broker y ledger se reconstruyen **juntos**; **no**
     depende de persistir `OrderManager`/`_pending`; **no** existe el gap de
     adopción broker→ledger.
  2. **`PaperTradingEngine.load_snapshot()`** (test/support): restaura **ledger** y
     **broker** (atómicos), pero **NO** restaura `OrderManager`, `_pending` ni
     `_pending_resolution`; **no adopta** fills broker→ledger. `reconcile()` puede
     **detectar** la divergencia y **bloquear**, pero **no adopta**.
- **Failure modes (solo `load_snapshot`; NO son paths productivos actuales)**:
  - **pending no enviada** → puede perderse en `load_snapshot` (A4 la re-evalúa por
    replay);
  - **SUBMITTED/SUBMITTED** → `OrderManager` vacío ⇒ `MISSING_INTERNAL` → BLOCKED;
  - **SUBMITTED/FILLED** → `MISSING_INTERNAL` + mismatch POSITION/ACCOUNT → BLOCKED;
  - **SUBMITTED/CANCELLED** → `MISSING_INTERNAL` → BLOCKED;
  - **`SubmitStatus.UNKNOWN`** → no se persiste ni se resuelve;
  - **broker FILLED** → **no** se adopta en el ledger.
- **Relación con S-04-a**: `OrderStatus.UNKNOWN` ya es **transient** (S-04-a,
  `1d26304`). El gap actual **no** consiste en que UNKNOWN sea terminal; consiste en
  que `load_snapshot()` **no persiste/restaura el estado operativo** necesario para
  resolver órdenes tras restart. S-04-a **habilitó la semántica** (el grafo permite
  `UNKNOWN → SUBMITTED/PARTIALLY_FILLED/FILLED/REJECTED/CANCELLED`), pero **no**
  cablea el persistir/restaurar ni el disparador de lookup.
- **No confundir** `reconciliation detects discrepancy` con
  `recovery adopts broker state`: en la ruta `load_snapshot` reconciliation
  **detecta y bloquea**, pero **no** adopta; en producción el replay evita el
  problema.
- **Decisión RESUELTA**: `load_snapshot()` = **test/support only** (no recovery
  operativo). **NO** se persigue la Opción B; si algún día se quisiera
  `load_snapshot` como mecanismo operativo, sería un **contrato separado**
  (persistencia de `OrderManager`/`_pending`/`_pending_resolution`, recuperación de
  `UNKNOWN`, adopción de fills, idempotencia `(client_order_id, fill_sequence)`,
  política de divergencia, autoridad durante restart). **No** es production blocker.
- **Nota de docstring (hardening/documentation cleanup separado, NO finding
  funcional)**: el docstring de `snapshot()` ("Estado serializable JSON para
  **restart** sin duplicar exposición") es **impreciso** respecto al recovery
  productivo actual (que es **replay**). Corregir el docstring, si se desea, es un
  cleanup separado; **no** abre un finding funcional.

### F-07 — `OrderManager` admite transiciones de estado inválidas — **CLOSED** (excepción `UNKNOWN` resuelta en S-04-a)
- ID: F-07
- severidad: **HIGH**
- archivo: `execution/order_manager.py` (`_transition`)
- **estado: CLOSED** (cerrado en `84108d8`; la excepción de `UNKNOWN` se resolvió
  en S-04-a, reabriendo contractualmente **solo** `UNKNOWN`).
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
- **Hallazgo incidental — RESUELTO (S-04-a)**: `UNKNOWN` estaba modelado como
  terminal, pero §8 lo describe como estado a **resolver** con `get_order`.
  Corregido: `UNKNOWN` es **transitorio** (no terminal) con salidas congeladas
  `{SUBMITTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED}`; ver **S-04-a**.
  El resto del grafo de F-07 queda **intacto**.

### F-08 — Position sizing sin `quantity_step`/contratos enteros — **CONTRACT GAP — NOT APPLICABLE TO CURRENT INSTRUMENT CONTRACT**
- ID: F-08
- severidad: **MEDIUM** (histórica; sin bug observable en el alcance actual)
- archivo: `risk/position_sizing.py` (`position_size_from_stop`), `risk/engine.py` (`_size_fixed_fractional`)
- **estado: `CONTRACT GAP — NOT APPLICABLE TO CURRENT INSTRUMENT CONTRACT`**
  (NO implementado; preventivo para instrumentos futuros).
- **Hallazgo original**: position sizing sin `quantity_step`/contratos enteros;
  premisa "NQ/ES exigen enteros" **no respaldada** por ningún instrumento
  configurado.
- **Re-auditoría (HEAD `e38a624`)**:
  - El contrato vigente **permite cantidades fraccionarias**: `Order.quantity:
    int | float`, `BrokerOrder.quantity: float`, `BrokerFill.quantity: float`,
    `AccountLedger` opera con floats.
  - Instrumentos realmente configurados/operados: **`BTCUSDT`**, **`XXBTZUSD`**
    (crypto, fraccionables) y **`SAMPLE`** (sintético); `point_value=1.0`,
    `min_size=0.001`. **No hay evidencia de instrumentos integer-only.**
  - **No existen** `quantity_step`, `lot_size`, `contract_size` ni `min_quantity`
    en producción (código/config/docs).
  - **`min_size` es un umbral mínimo, NO un múltiplo/step** (`approved_quantity <
    min_size` → rechazo; engine.py:685, backtesting/engine.py:417).
  - La cantidad se **preserva sin transformación** a lo largo de la ruta:
    `approved_quantity` → `ProjectedExposure` (signed delta) → `Order.quantity` →
    `BrokerFill.quantity` → `AccountLedger.apply_fill`. `OrderManager()` en
    producción sin `_max_quantity`/clamp.
  - **No existe `round`/`floor`/`ceil`/`quantize` posterior al projected gate.**
- **Conclusión**: **no hay bug observable en el alcance actual** (fracciones).
  La premisa original queda **resuelta/no aplicable** al contrato de instrumentos
  vigente; F-08 permanece como `CONTRACT GAP` **preventivo** para el día en que se
  incorpore un instrumento de contrato entero/step.

#### F-08-a — quantity NaN/Inf validation — **CLOSED — REFUTED**
- ID: F-08-a (subfinding separado de F-08).
- archivo: `execution/order.py` (`Order.__post_init__`), `brokers/paper.py`
  (`submit_order`), `risk/position_sizing.py`, `risk/engine.py`.
- **estado: `CLOSED — REFUTED`** (origin audit completado en `HEAD 3b473a7`).
- **Hallazgo original**: `Order.__post_init__` (`quantity <= 0`) y
  `PaperBrokerAdapter.submit_order` (`not quantity or quantity <= 0`) **no**
  rechazan `NaN`/`Inf` (`NaN <= 0`/`Inf <= 0` son `False`).
- **Origin graph de `approved_quantity`**:
  `ClosedBarEvent` → `on_closed_bar` → `strategy.generate_signal` →
  `Signal(reference_price, stop_price)` → `RiskEngine.validate` →
  `risk_cap = maximum_monetary_risk(equity, risk_per_trade)`;
  `distance = |reference_price − stop_price|`;
  `unit_risk = distance × point_value`; `quantity = risk_cap / unit_risk`.
  Orígenes capaces de inyectar `NaN/Inf`: `equity`, `reference_price`/`stop_price`,
  `point_value`, `risk_per_trade`.
- **Tabla de orígenes / alcanzabilidad**:

  | Origen | ¿Produce NaN/Inf? | Barrera upstream | ¿Llega a `approved_quantity`? | Evidencia |
  |---|---|---|---|---|
  | `risk_per_trade` | Sí (matemáticamente) | `RiskConfig.__post_init__` exige `0 < v < 1` → NaN/Inf `ValueError` | **No** (`UNREACHABLE BY INVARIANT`) | `risk/types.py:65-74` |
  | `equity` | Sí | `_valid_mark` (NaN/Inf/≤0) + cash finito; `_last_equity` no se actualiza con `valuation()=None` | **No** (`UNREACHABLE BY INVARIANT`) | `papertrading/ledger.py:37-42` |
  | `reference_price`/`stop_price` | Sí | `LiveDataEngine._is_valid_ohlc` (`v != v`, `abs(v)==inf`) antes de emitir `ClosedBarEvent` | **No** (`BLOCKED UPSTREAM`) | `live/engine.py:504-512, 240-244, 177-180` |
  | `point_value` | Sí | Sin validación de finitud; proviene de config congelada, sin input runtime externo | **No** (`BLOCKED UPSTREAM`, contrato de config) | `papertrading/models.py:110`; `forward_runner.py:791` |
  | API directa (`RiskEngine.validate`/`Order`/`PaperBrokerAdapter`) con NaN | Sí | Ninguna | **No productivo** (`TEST/LOWER-LEVEL ONLY`) | `brokers/paper.py:97`; `execution/order.py:106` |

- **Comportamiento IEEE**: `distance <= 0`, `point_value <= 0`, `quantity <= 0`,
  `not q or q <= 0` y `approved_quantity < min_size` son **todas `False`** para
  `NaN`/`Inf` (no rechazan). `AccountLedger.apply_fill` **sí** rechaza `NaN`
  (`q != q`) — barrera defensiva **final** (post-broker).
- **Interacción F-03**: `_projected_exposure` propaga `signed_qty` a `notional`;
  con un gate **activo**, `NaN <= límite` es `False` → fail-closed; con gates
  **deshabilitados** (default) no hay comparación. F-03 **no** crea una vía nueva
  de aceptación, pero tampoco cierra F-08-a por sí solo.
- **Conclusión (origin audit)**: **no existe ruta productiva de paper trading que
  permita que `NaN`/`Inf` llegue a `approved_quantity`**:
  - OHLC de producción bloqueado por `LiveDataEngine._is_valid_ohlc` (streaming
    S1–S8/B1 y A4); el replay de A4 confía en `bars.jsonl` ya validado +
    `input_fingerprint` (sha256);
  - equity/marks protegidos por las invariantes del ledger;
  - `risk_per_trade` validado por `RiskConfig`;
  - `point_value` proviene de config congelada (sin ruta runtime externa);
  - inyecciones directas a `RiskEngine`/`Order`/`PaperBrokerAdapter` son
    lower-level/test-only;
  - `AccountLedger.apply_fill` mantiene una barrera defensiva final.
- **Residual de hardening (LOW / optional; NO implementado ahora)**:
  - validación defensiva de finitud en `Signal`;
  - validación defensiva de finitud en `Order`;
  - validación defensiva en `PaperBrokerAdapter`;
  - validación de finitud de `PaperEngineConfig.point_value`;
  - tests directos para `NaN`/`Inf` en `_is_valid_ohlc` (hoy
    `test_invalid_ohlc_rejected` solo cubre inconsistencia OHLC).
  No existe evidencia de bug productivo actual. **Reevaluar** si en el futuro se
  incorpora una ruta de ingesta que pueda bypassar `LiveDataEngine`, o si se
  relaja el contrato de configuración.

#### F-08-b — instrument quantity metadata
- ID: F-08-b (subfinding separado de F-08).
- archivo: `database/models.py::Instrument`.
- **estado: `INFRASTRUCTURE GAP`** — NO implementado.
- `Instrument` define `tick_size`/`point_value` pero **no** `quantity_step`/
  `lot_size`. La parte de "**DB no cableada**" de este subfinding **depende de
  S-05-db**; la ausencia de `quantity_step`/`lot_size` es un **gap independiente**.
- Si en el futuro se incorporan instrumentos integer-only, habrá que definir la
  **fuente de metadata** de cantidad y la **política de cuantización**.

- **Decisiones futuras (deferred; no inventar ahora)**:
  `quantity_step` por instrumento; fuente de metadata; punto de cuantización
  (pre-gate); `floor`/`round`/`ceil`/`reject`; y la relación entre cantidad
  cuantizada y el projected risk gate (`approved == projected delta`).

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

### F-11 — `config/production/settings.yaml`: `null` en métricas de riesgo — **CLOSED — REFUTED** (claim de riesgo operativo; rol productivo deliberadamente diferido)
- ID: F-11
- severidad: **HIGH** (histórica; reclasificada: el vector real es plantilla, no runtime)
- archivo: `config/production/settings.yaml`, `config/loader.py`
- **estado: `CLOSED — REFUTED`** — el claim original de **riesgo operativo**
  ("arranque inseguro por `null`") queda **refutado**: `config/production/
  settings.yaml` es **template/placeholder/no-runtime** y no tiene caller. El rol
  productivo queda **deliberadamente diferido** (nota abajo), no como finding
  funcional actual.
- **Decisión ratificada (F-11 principal)**: `config/production/settings.yaml` =
  **TEMPLATE / PLACEHOLDER / NON-RUNTIME**. No es configuración ejecutable, no es
  fuente de verdad de producción, no participa en paper/forward, `risk.*: null` no
  tiene efecto runtime, no se rellena con valores reales y no se conecta vía
  `config.loader.py` en este ciclo. **Cualquier futura conversión a configuración
  productiva es un bloque F-11 separado** y requerirá su propio contrato
  (endurecimiento del loader + fail-closed + valores reales), referido en
  **F-11-b-3**.
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
- **Decisión humana (RESUELTA)**: se adopta la opción **1 — plantilla
  documental/no-ejecutable**. No se rellena con valores reales ni se conecta al
  loader. (La opción 2 "config ejecutable" queda como **bloque F-11 separado**
  si algún día se decide, ver F-11-b-3.)
- **Hallazgo incidental**: `load_config`/`load_environment` sin integración en el
  arranque real → registrado como **F-11-b-1** (loader genérico, `CONTRACT GAP —
  FUTURE RUNTIME`).
- **CERRADO (claim de riesgo operativo refutado).** El rol productivo futuro queda
  como **nota/deferred**, no como finding funcional actual.
- **No se modificó F-04** ni ningún otro hallazgo.

#### Re-auditoría F-11 (HEAD `72a3544`)
- **Claim original refutado en el estado actual**: el riesgo de "arranque
  productivo inseguro por `null`" **no existe** porque
  `config/production/settings.yaml` es **template/placeholder** y **no tiene
  caller runtime**. `load_config`/`load_environment` existen pero **no están
  cableados** a un arranque real (0 callers fuera de `config/` y tests).
- **Runtime real de paper/forward**: `papertrading/forward_runner.py::
  build_forward_runner` lee directamente `config/paper/settings.yaml` +
  `config/final_oos/config.yaml` (vía `yaml.safe_load`, **no** vía
  `config.loader`) y construye `RiskConfig` con los campos legacy. **Nunca**
  consume `production/settings.yaml`.
- **Gap contractual restante** (histórico; ver decisión final): la
  documentación/docstring del loader **promete** un comportamiento de producción
  ("fail-closed") que **no** corresponde al wiring real (loader sin validación
  semántica de `risk` y sin consumidor). **Resolución**: el claim de riesgo
  operativo queda **`CLOSED — REFUTED`**; la falta de validación del loader se
  registra separada como **F-11-b-1** (`CONTRACT GAP — FUTURE RUNTIME`).
- **`config/production/settings.yaml`**: template/placeholder; **no runtime**;
  `risk.*: null` **no tiene efecto** actualmente. **No** convertirlo en ejecutable
  ni renombrarlo.
- **Paper/forward** (histórico): el archivo operativo de paper existe; tras
  **F-11-a** (`IMPLEMENTED`) **sí** transporta los cuatro límites F-03 vía
  `build_forward_runner`.

#### F-11-a — F-03 risk-limit configuration surface — **IMPLEMENTED**
- ID: F-11-a (subfinding separado de F-11).
- **estado: `IMPLEMENTED`** (contrato congelado en `932d8d6`). Los cuatro límites
  ya son configurables en paper/forward vía `config/paper/settings.yaml` →
  `build_forward_runner` → `RiskConfig` → `RiskEngine`. **No implementado**:
  backtest, production YAML.
- **Alcance**: superficie de configuración de los cuatro límites F-03 para el
  runtime real de paper/forward: `max_gross_exposure`, `max_net_exposure`,
  `max_leverage`, `max_instrument_concentration`.
- **D1 — Ubicación**: los cuatro campos viven bajo `risk:` en
  `config/paper/settings.yaml` (`risk.max_gross_exposure`,
  `risk.max_net_exposure`, `risk.max_leverage`,
  `risk.max_instrument_concentration`). **No** crear secciones paralelas
  (`exposure:`, `limits:`, `portfolio_risk:`).
- **D2 — Ausencia / null**: campo **ausente → `None`**; YAML **`null` → `None`**;
  `None` = **gate disabled**; **`0` NO** significa disabled; negativos, `NaN`,
  `Inf` y tipos inválidos son **configuración inválida**. No inventar defaults
  numéricos.
- **D3 — Paper / forward**: `paper` y `forward/A4` **comparten**
  `config/paper/settings.yaml`, `build_forward_runner` y `RiskConfig`. Cadena:
  `config/paper/settings.yaml → build_forward_runner → RiskConfig → RiskEngine →
  projected pre-fill gates`. **No** existe una segunda configuración de riesgo
  para forward.
- **D4 — final_oos**: `config/final_oos/config.yaml` permanece como configuración
  **frozen OOS/research**; **no** participa en el riesgo operativo de
  paper/forward; **no** se añaden allí los cuatro límites F-03. Mantener su
  `config_hash` y responsabilidades sin cambios.
- **D5 — Backtest**: F-03 projected sigue siendo **paper-only**. Los campos pueden
  aparecer en un config consumido por `RiskConfig` **sin efecto projected** en
  backtest; **no** se crea wiring nuevo en `backtesting/`.
- **D6 — Production YAML**: `config/production/settings.yaml` permanece
  **template/placeholder**, **fuera** del runtime paper/forward; no se rellena; no
  se conecta vía `config.loader`. Convertirlo en runtime será un bloque **F-11
  separado**.
- **D7 — Validación / ownership**: **`RiskConfig` es la autoridad semántica**. El
  builder de paper/forward **no inventa defaults**, convierte ausencia/null a
  `None`, pasa los valores explícitamente cuando existen, y deja que `RiskConfig`
  rechace valores inválidos. **No** modificar el loader genérico en este paso.
- **Configuración inválida**:
  - `risk` ausente → **fail-loud**;
  - YAML malformado → **fail-loud**;
  - campo desconocido → **ignorado** (decisión contractual actual);
  - `null`/ausente → **disabled legítimo**;
  - `0` → **inválido**;
  - negativo → **inválido**;
  - `NaN`/`Inf` → **inválido**;
  - tipo incorrecto → **inválido**.
- **Tabla contractual**:

  | Campo | YAML path | Tipo | Unidad | None/ausente | Validación | Runtime |
  |---|---|---|---|---|---|---|
  | `max_gross_exposure` | `risk.max_gross_exposure` | float | USD | disabled | >0 y finito | paper/forward |
  | `max_net_exposure` | `risk.max_net_exposure` | float | USD | disabled | >0 y finito | paper/forward |
  | `max_leverage` | `risk.max_leverage` | float | ratio | disabled | >0 y finito | paper/forward |
  | `max_instrument_concentration` | `risk.max_instrument_concentration` | float | fracción de equity | disabled | >0 y finito | paper/forward |

- **Cotas superiores**: **fuera** de este contrato; **F-11-c** = `CLOSED — REFUTED`
  (no existe upper bound universal exigible).

#### F-11-b — risk block structural validation — **CONTRACT GAP — NO CURRENT OPERATIONAL IMPACT**
- ID: F-11-b (subfinding separado de F-11).
- **estado: `CONTRACT GAP — NO CURRENT OPERATIONAL IMPACT`** — NO implementado.
- `config.loader.py` **no valida semánticamente** el bloque `risk`, pero **no tiene
  caller productivo**. El runtime real paper/forward usa `build_forward_runner()` y
  **falla explícitamente** ante estructuras inválidas. **No** existe fallback
  silencioso a gates disabled. Por tanto **no hay `IMPLEMENTATION BUG` actual**.
- El gap consiste en que la **validación estructural** y la **semántica de errores**
  no están formalmente contratadas. Subfindings: **F-11-b-1**, **F-11-b-2**,
  **F-11-b-3**.

##### F-11-b-1 — generic config loader validation
- ID: F-11-b-1 (subfinding separado).
- **estado: `CONTRACT GAP — FUTURE RUNTIME`**.
- `load_config`/`load_environment` validan YAML/`dict`/`environment`; **no** validan
  `risk`; **no** tienen caller productivo. Cualquier endurecimiento pertenece al
  momento en que el loader se convierta en runtime real.

##### F-11-b-2 — paper/forward risk block error semantics
- ID: F-11-b-2 (subfinding separado).
- **estado: `CONTRACT GAP`** (semántica de error; sin impacto operativo actual).
- El builder es actualmente **fail-loud**: `risk` ausente → `KeyError`;
  `risk: null` / no-dict → `TypeError`; campos inválidos → errores de `RiskConfig`
  (`ValueError`). Estas excepciones son **incidentales**, no una taxonomía
  contractual explícita.
- **Observación (no implementation bug)**: `max_open_positions` actualmente **no**
  tiene validación de rango/tipo equivalente en `RiskConfig` (`__post_init__` solo
  valida `risk_per_trade`/`max_strategy_risk`/`max_daily_loss`/`max_drawdown` y los
  4 límites F-03). Queda como **decisión contractual pendiente**.

##### F-11-b-3 — future production config runtime
- ID: F-11-b-3 (subfinding separado).
- **estado: `CONTRACT GAP — FUTURE`**.
- **No** mezclar con F-11-b. Convertir `config/production/settings.yaml` en runtime
  es un bloque **F-11 separado**.

##### Tabla de casos (runtime paper/forward)

| Entrada | Builder | RiskConfig | Resultado actual |
|---|---|---|---|
| `risk` ausente | `KeyError` | no llega | fail-loud |
| `risk: null` | `TypeError` | no llega | fail-loud |
| `risk` no-dict | `TypeError` | no llega | fail-loud |
| campo legacy ausente | `KeyError` | no llega | fail-loud |
| F-03 ausente | `.get()` → `None` | disabled | válido |
| F-03 `null` | `None` | disabled | válido |
| F-03 `0` | pasa | `ValueError` | fail-loud |
| F-03 negativo | pasa | `ValueError` | fail-loud |
| F-03 `NaN`/`Inf` | pasa | `ValueError` | fail-loud |
| unknown key | ignorado | no llega | según contrato actual |

##### Tests (F-11-b)
- **Runtime wiring**: `tests/test_forward_runner.py::TestRiskLimitConfigSurface`
  (F-03 config surface: ausente/null/explícitos/inválidos; `risk` ausente →
  `KeyError`; unknown key ignorado; integración REJECT/APPROVE pre-fill).
- **Objeto `RiskConfig`**: `tests/test_risk_engine.py`, `tests/test_risk_sizing.py`,
  `tests/test_risk_exposure_gates.py::TestConfigLimits`.
- **Loader genérico**: `tests/test_config.py` (entorno/archivo/dict; **no** valida
  `risk`).

##### Decisiones futuras (F-11-b; no tomadas ahora)
1. contrato formal de **validación estructural** de `risk`;
2. **semántica contractual de errores**;
3. si `max_open_positions` debe tener validación explícita de **tipo/rango**;
4. endurecimiento del **loader** si algún día se convierte en runtime;
5. relación con **F-11 production runtime** (F-11-b-3).

#### F-11-c — upper bounds for F-03 risk limits — **CLOSED — REFUTED**
- ID: F-11-c (subfinding separado de F-11).
- **estado: `CLOSED — REFUTED`** (HEAD `9da7067`).
- **Conclusión**: los cuatro límites (`max_gross_exposure`, `max_net_exposure`,
  `max_leverage`, `max_instrument_concentration`) admiten actualmente `None` **o**
  un **valor finito positivo**; **no existe un upper bound universal exigible por
  el contrato actual**.
  - Gross/net son límites **absolutos en USD** y pueden **superar equity**.
  - Leverage es **context-specific** según `03_RISK_MANAGEMENT.md §19`
    (account/instrument/strategy/broker/prop firm), no universal.
  - Concentration está definida como `abs(exposure_i) / equity` y **puede ser
    `> 1`** cuando existe leverage.
  - **No** existe ninguna relación contractual obligatoria entre los cuatro
    límites (no se asume `net <= gross`, `gross/equity <= leverage`, ni
    `concentration <= 1`).
- **Seguridad / operabilidad**:
  - `None` = gate **disabled**; un **valor grande** = gate **activo pero
    permisivo** (no es lo mismo que disabled).
  - La ausencia de upper bound **no** implica por sí misma bug, overflow, ni
    aceptación de `NaN`/`Inf`: `RiskConfig` ya rechaza valores **no finitos o no
    positivos** (`None` o `> 0` finito).
  - Un límite permisivo es **política operativa** (qué valores configurar), no un
    defecto de validación.
- **F-11-c-concentration (nota conceptual, no finding abierto)**: la pregunta
  "(0,1]" **no aplica** a la métrica actual. Cambiar a `abs(exposure_i) / gross`
  sería una **métrica distinta** (`share-of-gross`) y requeriría **otro contrato**;
  **no** se decide ni implementa esa alternativa ahora.

#### Decisiones humanas pendientes (F-11)
1. `config/production/settings.yaml`: **template no ejecutable** vs convertirlo en
   **runtime real**.
2. Si se decide runtime real: **endurecer el loader** y definir **fail-closed**.
3. **Wiring de los límites F-03** a paper/forward (F-11-a): **`IMPLEMENTED`**
   (configurable vía `config/paper/settings.yaml`).
4. **Contrato de límites superiores** (F-11-c): **`CLOSED — REFUTED`** (no existe
   upper bound universal exigible; la métrica actual de `concentration` no está
   acotada por 1). Nota conceptual `share-of-gross` fuera de alcance.
5. **Política exacta de validación estructural** del bloque `risk` (F-11-b;
   subfindings **F-11-b-1/-2/-3**).

## Suspected Findings

### S-01 — `live/engine.py:442-444` duplicado inalcanzable
- severidad: LOW — en `_record_issue` hay una línea de truncado; revisar posible
  duplicado de lógica. (Cobertura parcial.)

### S-02 — `papertrading/engine.py:508-520` ramas de posición poco cubiertas
- severidad histórica: **MEDIUM** (Suspected Finding, orientado a cobertura).
- **Hallazgo original**: las ramas de **entrada adicional / reducción parcial
  (pyramiding)** en `PaperTradingEngine` tenían baja cobertura (508-520 sin
  cubrir); comportamiento no ejercido.
- **Re-auditoría (HEAD `e126e58`)**:
  - El engine es **mono-instrumento, sin pyramiding** (`_on_signal` retorna si ya
    hay posición) y las salidas son **cierres totales `reduce_only`**
    (`quantity = abs(pos.quantity)`); por tanto las ramas de entrada adicional /
    reducción parcial del engine son **inalcanzables** bajo el contrato vigente.
  - La **contabilidad** de reducción parcial / realización de P&L reside en
    `AccountLedger.apply_fill()` (`papertrading/ledger.py`), **cubierta por
    tests** (`tests/test_account_ledger.py::test_partial_reduction_realizes_pnl`);
    el engine solo mantiene la **proyección** `PaperPosition` (metadata).
  - **No es `IMPLEMENTATION BUG`**: no hay defecto funcional; el código es
    inalcanzable por contrato.
- **Clasificación: `CONTRACT GAP — NOT APPLICABLE TO CURRENT ENGINE CONTRACT`.**
  No requiere implementación ahora; **solo debe reabrirse si se contrata
  pyramiding / reducción parcial**, extensión futura ya indicada en
  «F-03 projected exposure (post-trade) — I. Decisiones futuras/deferred».

### S-03 — `Redis`/`Celery`/`Flask` en el entorno global
- severidad: LOW — dependencias del entorno ajenas al proyecto; no declaradas en
  `pyproject`. Riesgo de confusión, no de código.

### S-04 — `reconciliation/engine.py` UNKNOWN terminal (ver F-07/F-15)
- severidad histórica: **MEDIUM**.
- **Hallazgo original**: el estado observado (UNKNOWN) parecía depender del
  `OrderStatus` (referencia colgante a "F-15", inexistente).
- **Re-auditoría (HEAD `e126e58`)**: **`CLOSED — REFUTED`.**
  - La reconciliación maneja `UNKNOWN` **explícitamente** y **antes** de la
    lógica terminal: si el broker no la tiene (engine.py:120-134) o si cualquiera
    de los dos lados es `UNKNOWN` (161-173) → `DiscrepancyClass.UNKNOWN`
    (bloquea, "resolver con `get_order`").
  - `_order_terminal()` (487-494) es una **definición local** que **excluye
    `UNKNOWN`** (`FILLED/CANCELLED/REJECTED/EXPIRED/FAILED`); **no** usa
    `OrderStatus.terminal`.
  - Por tanto la reconciliación **no depende de `OrderStatus.terminal`**; el
    comportamiento problemático implicado no existe.
  - **Tests explícitos**: `test_unknown_order_blocks_and_is_unknown`,
    `test_unknown_resolved_to_filled_then_match`,
    `test_unknown_internal_order_missing_fill_still_unknown`,
    `test_terminal_order_absent_from_broker_is_match` y
    `test_resolve_unknown_live_order_to_filled` (orquestador).
- **Subfinding separado (no es S-04)**: la tensión `OrderStatus.terminal` incluye
  `UNKNOWN` (execution/order.py:58-67) mientras §8 lo describe como estado a
  resolver — incidental de **F-07**, fuera del claim de S-04.

#### S-04-a — `OrderStatus.UNKNOWN` semantics (MODEL B) — **IMPLEMENTED**
- ID: S-04-a (subfinding separado de S-04).
- **estado: `IMPLEMENTED`** (contrato congelado en `6a6d575`; **MODEL B — UNKNOWN
  IS TRANSIENT**). Implementado en `execution/order.py` + `execution/order_manager.py`
  (solo afecta a `UNKNOWN`; el resto del grafo intacto).
- **Definición**: `UNKNOWN` = orden cuyo resultado **no pudo determinarse** después
  del envío (timeout / pérdida de respuesta). **No** significa fallo, cancelación,
  expiración ni éxito. Por tanto **NO es terminal**; `OrderStatus.terminal` debe
  **excluir `UNKNOWN`**.
- **Producers permitidos** (entradas ya presentes en `ALLOWED_TRANSITIONS`):
  `SUBMITTING → UNKNOWN`, `SUBMITTED → UNKNOWN`, `ACKNOWLEDGED → UNKNOWN`,
  `PARTIALLY_FILLED → UNKNOWN`. Solo cuando la capa de submit/broker determina
  disposición desconocida. **No** crear un producer artificial en paper actual.
- **Resolver**: broker `get_order` / resolución equivalente. **No** resubmit
  mientras el estado siga desconocido.
- **`ALLOWED_TRANSITIONS[UNKNOWN]` (congelado para el adapter concreto actual)**:
  `{SUBMITTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED}`.
  - **Evidencia**: `PaperBrokerAdapter` (único adapter concreto) produce como
    resultado de `get_order` esos 5 estados (`brokers/paper.py`: MARKET `FILLED`,
    caja insuficiente `REJECTED`, LIMIT/STOP `SUBMITTED`, `cancel_order`
    `CANCELLED`, `simulate_fill` `PARTIALLY_FILLED`/`FILLED`).
  - **Excluidos** `ACKNOWLEDGED`, `EXPIRED`, `FAILED`: **sin evidencia** de que el
    adapter actual los produzca (extensión futura para brokers reales, no
    necesaria hoy). No se inventan.
- **Self-loop**: `UNKNOWN → UNKNOWN` **NO permitido**. Un lookup que sigue sin
  resolver (broker devuelve `UNKNOWN`/`None`/error) **no muta el estado**: la orden
  permanece `UNKNOWN` para la siguiente reconciliación.
- **`apply_fill` semantics**:
  - `UNKNOWN → FILLED/PARTIALLY_FILLED` **con fill** se resuelve vía
    **`apply_fill`**; el estado final deriva del `fill_quantity` real.
  - `SUBMITTED`/`REJECTED`/`CANCELLED` **sin fills** se resuelven vía
    **`_transition`** (REJECTED por `reject(reason)` para preservar `reject_reason`;
    CANCELLED por `cancel`).
  - `UNKNOWN` aún no resuelto = **ninguna mutación**.
- **Reconciliation** (sin cambios en este contrato): reconciliation trata `UNKNOWN`
  **independientemente** de `OrderStatus.terminal`; `DiscrepancyClass.UNKNOWN` **no
  depende** del enum terminal (`_order_terminal` local ya excluye `UNKNOWN`). No se
  cambia reconciliation.
- **Restart / idempotencia**:
  - `UNKNOWN` persistido puede resolverse mediante broker lookup;
  - **no** se reenvía por estar `UNKNOWN`;
  - `client_order_id` impide recrear/reenviar la misma orden activa (con `UNKNOWN`
    no terminal, una orden `UNKNOWN` cuenta como activa);
  - la identidad de fill `(client_order_id, fill_sequence)` conserva la
    idempotencia del ledger;
  - el gap separado de **adopción ledger/broker tras restart** (F-06/F-03)
    permanece **fuera** de este contrato.
- **F-07**: **`F-07 REOPENED CONTRACTUALLY — UNKNOWN ONLY`** → **aplicado**
  (F-07 vuelve a `CLOSED`). Delta implementado (mínimo):
  1. `OrderStatus.terminal` excluye `UNKNOWN` (terminales: FILLED/CANCELLED/
     REJECTED/EXPIRED/FAILED).
  2. `ALLOWED_TRANSITIONS[UNKNOWN] = {SUBMITTED, PARTIALLY_FILLED, FILLED,
     REJECTED, CANCELLED}`.
  3. tests de terminalidad actualizados (`test_all_terminal_states_have_no_outgoing`
     sin UNKNOWN; `test_unknown_is_not_terminal`).
  4. tests de resolución y ausencia de self-loop añadidos (`TestUnknownTransient`).
  5. **no** se modificó el resto del grafo.
- **Documentación `06_EXECUTION_OPERATION.md`** (a alinear en la implementación
  futura; **no** editar ahora): §7 y §8 deben expresar una única semántica —
  `UNKNOWN` **no terminal**, **requiere reconciliation/broker lookup**, **no
  resubmit** antes de resolver.

### S-05 — `db`/`portfolio` definidos pero no integrados — **CONTRACT GAP — NO CURRENT FUNCTIONAL IMPACT**
- severidad histórica: **LOW**.
- **Claim original**: infraestructura definida pero no integrada (`database/` sin
  importadores externos; `portfolio/` sin conexión al veto, consecuencia de F-03).
- **Re-auditoría (HEAD `b2b1597`)**:
  - **No existe contrato** que obligue al runtime a consumir `database/`.
  - **No existe impacto funcional actual** (runtime usa config explícita y
    `AccountLedger`; nada se rompe).
  - **No es `IMPLEMENTATION BUG`** (no hay contrato que exija usar la DB).
  - **No es `INFRASTRUCTURE GAP`**: no hay una **capacidad de runtime contratada**
    que requiera la DB.
- **Clasificación: `CONTRACT GAP — NO CURRENT FUNCTIONAL IMPACT`.** No es
  production blocker. Subfindings: **S-05-db** y **S-05-portfolio**.

#### S-05-db — operational role of `database/`
- ID: S-05-db (subfinding separado de S-05).
- **estado: `CONTRACT GAP`** — NO implementado.
- `database/models.py::Instrument` existe; **no** hay migraciones, repositorios ni
  callers de runtime. `database/session.py` solo proporciona infraestructura de
  conexión **potencial** (sin consumidor).
- `Instrument.tick_size` / `point_value` **no** son fuente de verdad del runtime;
  paper/backtest/research usan **configuración explícita**
  (`PaperEngineConfig.point_value`, `BacktestConfig.point_value`,
  `ExecutionCosts.tick_size`, `config/final_oos/config.yaml`).
- **Pendiente decidir** si `database/` será: (1) fuente de verdad de metadata;
  (2) schema-first / infraestructura planificada; o (3) otra función contractual
  explícita. **No** se elige la arquitectura ahora.

#### S-05-portfolio — role of `portfolio/engine.py`
- ID: S-05-portfolio (subfinding separado de S-05).
- **estado: `CLOSED — REFUTED`.**
- **Motivo**: F-03 congeló explícitamente `AccountLedger.valuation()` como fuente
  autoritativa; `portfolio.compute_portfolio_risk()` **no** está cableado **por
  diseño**. **No** debe volver a presentarse como defecto de integración.

## Production Blockers (estado actual)

> Los ítems marcados **CLOSED** fueron blockers en el baseline inicial; se
> conservan aquí con su resolución para trazabilidad. La lista vigente de
> blockers es la de los ítems sin resolver.

1. **No existe ejecución real / broker real** (solo `PaperBrokerAdapter`), por
   diseño. No hay credenciales ni endpoints de órdenes (verificado: sin `ccxt`,
   `ib_insync`, MT5 al cual no). Sin embargo, esto **no es un blocker de
   *ejecución live* todavía** (fase no autorizada), sí lo es de *producción*.
   — **ABIERTO (por diseño)**.
2. **F-05 equity SHORT incorrecta** — **CLOSED** (formula `cash + Σ q·mark` con
   signo; ver F-05).
3. **F-02 `max_strategy_risk` inerte** — **CLOSED** (contrato `risk_per_trade <=
   max_strategy_risk`; ver F-02).
4. **F-03 sin veto de exposición/leverage/correlación** — **CLOSED (paper
   projected pre-fill)** para gross/net/leverage/concentration; **margin y
   correlation siguen `INFRASTRUCTURE GAP`** y backtest fuera de alcance.
5. **F-09/F-10 data quality incompleta + reordenamiento silencioso** — **CLOSED**
   (ver F-09/F-10).
6. **F-11 production config con `null`** — **`CLOSED — REFUTED`**:
   `config/production/settings.yaml` es **template/placeholder/no-runtime**; el
   arranque inseguro por `null` queda **refutado**. Rol productivo futuro =
   bloque separado. Subfindings: **F-11-a `IMPLEMENTED`**, **F-11-b** (contrato
   futuro), **F-11-c `CLOSED — REFUTED`**.
7. **F-08 sizing entero/`quantity_step`** — **`CONTRACT GAP — NOT APPLICABLE TO
   CURRENT INSTRUMENT CONTRACT`** (preventivo; sin bug actual; ver F-08).
8. **margin / correlation (F-03)** — **ABIERTO** (`INFRASTRUCTURE GAP`).
9. **F-06-RECOVERY** (`load_snapshot` recovery gap) — **`CLOSED — REFUTED`**
   (`snapshot()/load_snapshot()` = API de soporte/test; recovery productivo =
   **replay**; no production blocker). Ver F-06-RECOVERY.

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
- `data/quality.py` / loader: en el baseline, resumen incompleto (F-09) y
  reordenamiento silencioso (F-10). **Resuelto** (F-09/F-10 `CLOSED`; ver F-09 y
  F-10).
- `data/raw/` gitignored (caché de adquisición FINAL_OOS).

## Recommended Remediation Order

> Orden histórico del baseline, **anotado con el estado actual**. Lo ya
> completado se conserva para trazabilidad; el orden vigente es el de los ítems
> pendientes.

1. **F-05** (equity SHORT) — **DONE**.
2. **F-01** (`_day_key` UTC) — **DONE**.
3. **F-09 + F-10** (data quality + orden obligatorio) — **DONE**.
4. **F-02 + F-03** (veto de riesgo real + exposición) — **F-02 DONE**; **F-03
   DONE (paper projected pre-fill)**; margin/correlation y backtest pendientes.
5. **F-07** (tabla de transiciones de `OrderManager`) — **DONE**.
6. **F-06** (ledger interno real para reconciliación contable) — **DONE**
   (`4bb66c2`/`eeac54d`/`c722896`/`fbb0236`).
7. **F-08** (sizing entero/step para contratos) — **`CONTRACT GAP — NOT
   APPLICABLE TO CURRENT INSTRUMENT CONTRACT`** (preventivo; sin bug actual);
   subfindings **F-08-a** (**`CLOSED — REFUTED`**) y **F-08-b**
   (`INFRASTRUCTURE GAP`).
8. **F-11** (config de producción / validación del bloque `risk`) — **`CLOSED —
   REFUTED`** (claim de riesgo operativo; plantilla sin runtime). Subfindings:
   **F-11-a `IMPLEMENTED`**, **F-11-b** (contrato futuro, `CONTRACT GAP — NO
   CURRENT OPERATIONAL IMPACT`), **F-11-c `CLOSED — REFUTED`**.
9. **F-04** (contrato de estado de riesgo en paper) — **REFUTED** (no es bug).
10. S-02 (ramas de posición/pyramiding) — **`CONTRACT GAP` (no aplicable al
    contrato actual; reabrir solo si se contrata pyramiding/reducción parcial)**;
    S-04 (UNKNOWN en reconciliación) — **`CLOSED — REFUTED`**; **S-04-a**
    (`OrderStatus.UNKNOWN` transitorio, MODEL B) — **`CONTRACT FROZEN — READY FOR
    IMPLEMENTATION`** (`F-07 REOPENED CONTRACTUALLY — UNKNOWN ONLY`).
11. Añadir CI (pytest + coverage) — **DONE** (`.github/workflows/ci.yml`).

Pendientes vigentes: **F-08** (`CONTRACT GAP` preventivo, no aplicable al
contrato de instrumentos actual) y **F-08-b** (`INFRASTRUCTURE GAP`); **F-08-a**
queda **`CLOSED — REFUTED`** (sin ruta productiva); **F-11** = **`CLOSED —
REFUTED`** (su subfinding **F-11-b** permanece como `CONTRACT GAP — NO CURRENT
OPERATIONAL IMPACT` / futuro); **margin/correlation (F-03)**, **backtest fuera de
alcance (F-03)**; **F-06-RECOVERY** = **`CLOSED — REFUTED`** (recovery productivo
= replay; `load_snapshot` = test/support), y deuda no bloqueante
**I2/I3/I4/I5/I6** (ver F-06).

---

## Distinción explícita de estados (estado actual)

> Valores actuales en `HEAD = 3fb3203`. Los valores del baseline inicial
> (`798 passed`, F-05/F-01 afectando correctness, F-02/F-03/F-06 abiertos) se
> conservan en las secciones por-finding como evidencia histórica.

- **Tests passed**: sí — **976 passed**, `compileall` limpio, coverage global
  **86%**.
- **Correctness**: **sí** para los hallazgos de correctness del baseline — F-05
  (equity SHORT) y F-01 (TZ) **cerrados**.
- **Operational readiness**: paper/forward operativo con **F-02/F-03/F-06
  cerrados**; **F-11 = `CLOSED — REFUTED`** (config de producción
  template/sin runtime, no un bug de runtime; su **F-11-b** permanece como
  contrato futuro); permanecen **F-08** (`CONTRACT GAP` preventivo, no aplicable
  al contrato de instrumentos actual) y **margin/correlation
  (`INFRASTRUCTURE GAP`)**; **F-06-RECOVERY** = **`CLOSED — REFUTED`** (recovery
  productivo = replay; `load_snapshot` = test/support; no production blocker).
- **Production readiness**: **NO** — sin broker real (por diseño), sin
  margin/correlation, sin config de producción ejecutable (por diseño;
  `config/production/settings.yaml` es template/no-runtime; F-11 = `CLOSED —
  REFUTED`). **Ningún estado de "production ready"** se declara.
