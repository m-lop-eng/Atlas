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

* Última sesión: **preparada la infraestructura del FINAL_OOS** (esperando el dump mensual 2026-09 de binance.vision, que a fecha de hoy sigue 404): pre-registro congelado (`research/decisions/FINAL_OOS_PRE_REGISTRATION.json`, estado FROZEN, criterios de lectura NULL de falsación), config de ejecución congelada (`config/final_oos/config.yaml`, `config_hash=cb639bbf…`, strategy hash `491ed76d…` idéntico a H004/H004-B), pipeline mecánico (`research/final_oos_pipeline.py`) verificado por tests de integración con dataset sintético (`tests/test_final_oos_pipeline.py`) y validación mecánica del plan (`tests/test_final_oos_preregistration.py`), y runner `experiments/e12_final_oos.py` (modo status/preparación/autorización; el consumo es irreversible). Corregido además `data/quality.py` (`_ts_delta_seconds` ahora UTC-aware: los gaps de 1h ya no dependen del timezone/DST de la máquina).
* Nuevo (cierre H004-B): `research/evidence.json` registra **H004-B → `RESEARCH_REQUIRED`** con nota `SYSTEMATIC_INSTABILITY` (6/12 folds positivas, mediana +0.3%, media +5.4%, concentración ALTA, 3 vecindades negativas); notas de H004 y H002 ampliadas; `research/decisions/H004_WALK_FORWARD.json` incorpora `h004b_closure` embebiendo `temporal_stability`; `research/decisions/H002_LONG_SHORT_ASYMMETRY.json` añade `temporal_stability_followup` (la inestabilidad refuerza el caveat de beta direccional; no autoriza BOTH/SHORT). Ver `git log -1 fa8b51a`.
* Nuevo: `experiments/e11_h004b_diagnosis.py`, `experiments/outputs/h004b_diagnosis.json` y
  `research/decisions/H004B_RESULT.json` (con el pre-registro embebido como prueba
  de que el criterio existía antes de ver los números). `research/decisions/H004B_PRE_REGISTRATION.json`
  queda en `estado: COMPLETE` con esquema y criterios intactos.
* Suite de tests: **259 passed** (`python -m pytest`).
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

### Cómo retomar

Punto de partida de la próxima sesión: **infraestructura FINAL_OOS lista; falta el dato**.
Verificar que `research/evidence.json` mantiene H001–H005 y H004-B en `RESEARCH_REQUIRED`
y `final_oos: BLOCKED_BY_DATA_AVAILABILITY` con `minimum_start=1788220800` (2026-09-01T00:00Z).
Primer comando: `python experiments/e12_final_oos.py` (status). No abrir H006 ni ejecutar
experimentos nuevos antes de un FINAL_OOS limpio.

```powershell
python -m pytest                                     # 259 tests (baseline de regresión)
python experiments/e12_final_oos.py                  # FINAL_OOS: status (BLOCKED hasta dump 2026-09)
python experiments/e7_h002_asymmetry.py              # H002 train/test + métricas
python experiments/e8_h003_breakout_vs_buyhold.py    # H003 A-E: benchmarks/régimen/decisión
python experiments/e9_h004_walk_forward.py           # H004: walk-forward fijo + DATA_ROLE + decisión
python experiments/e10_h005_w2_diagnosis.py          # H005-W2: causa del fallo 2022 (diagnóstico)
python experiments/e11_h004b_diagnosis.py            # H004-B: localización temporal (SISTEMÁTICO)
python experiments/post_mortem.py <report1.json> <report2.json>
python experiments/e6_real_market.py                 # breakout vs controles (real)
```

**Siguiente paso acordado — infraestructura FINAL_OOS lista; falta el dato (calendario del proveedor):**
la causa de W2 está documentada (beta direccional adversa del filtro LONG en bears) y la
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

Cuando binance.vision publique el dump 2026-09: ampliar `experiments/data/` (nuevo
manifest + sha256), ejecutar `python experiments/e12_final_oos.py --status`, revisar
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

## Próximos hitos

Según `00_MASTER_SPECIFICATION.md`; el foco inmediato es ampliar datos para un
`FINAL_OOS` limpio (bloqueado por el calendario del proveedor, no por Atlas) y
decidir, tras el FINAL_OOS, si existe una señal generalizable (H006) con el
diagnóstico H004-B (inestabilidad temporal SISTEMÁTICA) y la causa de W2
(beta direccional adversa del filtro LONG) documentados. No más complejidad
estratégica ni optimización.