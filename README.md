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
brokers/       Adaptadores de broker (MT5, futuros, IBKR, crypto)
monitoring/    Observable del sistema
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
No hay ejecución real de órdenes todavía (solo `development`/backtest).

* Última sesión: `93f22db` — ciclo **H002 (Long/Short Asymmetry)**.
* Suite de tests: **188 passed** (`python -m pytest`).
* Motor: `ENGINE_VERSION = 2` (reset diario de `max_daily_loss` + monetización
  por `point_value`; invariante para `point_value=1`, por eso el grafo H001
  conserva sus `experiment_id`).

### Infraestructura de datos

`data/sources.py` define `DataSource` + `DataManifest` (sha256 + rango + params):
el backtester no sabe de dónde vienen los datos.

| Proveedor | Fuente | Profundidad |
| --- | --- | --- |
| `KrakenSource` | API pública Kraken | ~720 barras (~30 días) |
| `BinanceVisionSource` | `data.binance.vision` (klines mensuales) | histórica completa |
| `CsvSource` | CSV local normalizado | — |
| `ParquetSource` | pyarrow (opcional, futuro) | — |

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

* **H001**: en sintético (GBM) breakout ≈ random; en BTC 1h real breakout > random
  pero sin edge neto con solo 30 días. No se optimizaron parámetros.
* **H002**: split temporal 75/25 (test untouched). Contraste LONG−SHORT por trade
  **persiste en OOS** (train +1170$ → test +252$). La señal bruta (gross) es
  positiva en el histórico profundo. **Caveat**: BTC 2019–2026 es alcista
  estructural → la asimetría puede ser beta direccional, no edge idiosincrático.

### Cómo retomar

```powershell
python -m pytest                                     # 188 tests (baseline de regresión)
python experiments/e7_h002_asymmetry.py              # H002 train/test + métricas
python experiments/post_mortem.py <report1.json> <report2.json>
python experiments/e6_real_market.py                 # breakout vs controles (real)
```

**Siguiente paso acordado — H003:** contrastar el sesgo LONG contra *buy & hold*
y por régimen de mercado (bull/bear/lateral) para separar beta direccional de
edge. **No** optimizar lookback/ATR/SMA/stops hasta entonces.

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

## Próximos hitos

Según `00_MASTER_SPECIFICATION.md`; el foco inmediato es profundidad de datos y
análisis de hipótesis (H003), no más complejidad estratégica ni optimización.