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

* Última sesión: ciclo **H004 (walk-forward de parámetros fijos)** + **diagnóstico H005-W2** (causa del fallo 2022).
* Suite de tests: **210 passed** (`python -m pytest`).
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
| **H003** | ¿Breakout LONG o beta de BTC? | `RESEARCH_REQUIRED` (Caso A: timing preliminar, no beta pura) | `research/decisions/H003_BREAKOUT_VS_BUYHOLD.json` |
| **H004** | ¿Es estable temporalmente el timing LONG? | `RESEARCH_REQUIRED` (ventaja en 4/4 validaciones pero inestable) | `research/decisions/H004_WALK_FORWARD.json` |
| **H005-W2** | ¿Qué característica del entorno de 2022 explica la pérdida? | `RESEARCH_REQUIRED` (causa documentada: beta direccional adversa) | `research/decisions/H005_W2_DIAGNOSIS.json` |

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

### Cómo retomar

```powershell
python -m pytest                                     # 210 tests (baseline de regresión)
python experiments/e7_h002_asymmetry.py              # H002 train/test + métricas
python experiments/e8_h003_breakout_vs_buyhold.py    # H003 A-E: benchmarks/régimen/decisión
python experiments/e9_h004_walk_forward.py           # H004: walk-forward fijo + DATA_ROLE + decisión
python experiments/e10_h005_w2_diagnosis.py          # H005-W2: causa del fallo 2022 (diagnóstico)
python experiments/post_mortem.py <report1.json> <report2.json>
python experiments/e6_real_market.py                 # breakout vs controles (real)
```

**Siguiente paso acordado — H004-B o nueva hipótesis (según decisión):**
causa de W2 documento (beta direccional adversa del filtro LONG en bears).
Antes de otro ciclo:
(1) ampliar datos para declarar y **proteger** un `FINAL_OOS` limpio
    (`FINAL_OOS` hoy en estado `BLOCKED_BY_DATA_AVAILABILITY`: binance.vision aún
    no publica el dump mensual de 2026-09; el feed corta en 2026-08-31);
(2) decidir entre H004-B (más folds/re-anclaje de ventanas, siempre
    `fixed-parameter`) o una hipótesis formal sobre dirección por régimen,
    pre-registrada y evaluada SOLO contra el FINAL_OOS limpio.
**Nunca** reutilizar 2024–2026 como OOS ni optimizar lookback/ATR/stops/SMA.

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

Según `00_MASTER_SPECIFICATION.md`; el foco inmediato es ampliar datos para un
`FINAL_OOS` limpio (bloqueado por el calendario del proveedor, no por Atlas) y
decidir entre H004-B o una hipótesis de dirección por régimen con la causa de W2
documentada. No más complejidad estratégica ni optimización.