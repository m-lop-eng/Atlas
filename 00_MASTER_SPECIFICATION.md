# MASTER SPECIFICATION v1.0

## Plataforma de Robo-Trading Algorítmico Multi-Mercado

**Versión:** 1.0
**Estado:** Diseño / Pre-desarrollo
**Objetivo:** Construir una infraestructura cuantitativa profesional, modular y escalable para desarrollar, validar y ejecutar estrategias algorítmicas mediante cuentas de fondeo y, posteriormente, capital propio.

---

# 1. VISIÓN DEL PROYECTO

Construir una plataforma de trading algorítmico que permita desarrollar múltiples estrategias independientes y ejecutarlas en diferentes mercados y brokers sin tener que reconstruir el sistema cada vez.

El sistema deberá comenzar con **1–2 mercados** y posteriormente poder ampliarse a:

* Forex
* Futuros
* Acciones
* Índices/CFDs
* Crypto

La plataforma estará diseñada alrededor de una separación estricta entre:

```text
DATA
RESEARCH
STRATEGY
BACKTEST
PORTFOLIO
RISK
EXECUTION
BROKER
MONITORING
```

La estrategia no debe depender de un broker concreto.

---

# 2. OBJETIVO FINANCIERO

El objetivo NO es maximizar el rendimiento de un backtest.

El objetivo es identificar estrategias que presenten:

* ventaja estadística
* robustez
* estabilidad
* capacidad de sobrevivir a costes reales
* drawdown controlado
* baja sensibilidad a parámetros
* comportamiento razonablemente consistente fuera de muestra
* capacidad de funcionar dentro de las restricciones de cuentas de fondeo

El sistema debe priorizar:

**Robustez > Control de riesgo > Reproducibilidad > Escalabilidad > Rentabilidad**

---

# 3. FILOSOFÍA DE TRADING

El proyecto estará inicialmente orientado a:

### Trading intradía sistemático

Con posibilidad de mantener posteriormente:

* estrategias swing
* estrategias multi-day
* estrategias de mayor frecuencia

No se buscará High Frequency Trading.

El primer objetivo será desarrollar estrategias de frecuencia baja/media, donde:

* los costes sean controlables
* la ejecución sea técnicamente viable
* exista suficiente número de operaciones para realizar análisis estadístico
* el sistema pueda funcionar mediante infraestructura razonablemente asequible

---

# 4. MERCADOS INICIALES

Se comenzará con dos familias:

## Mercado 1 — Futuros

Instrumentos iniciales candidatos:

* NQ
* ES

Se investigarán ambos, pero no se asumirá que ambos deben terminar utilizándose.

## Mercado 2 — Forex

Instrumento inicial:

* EUR/USD

Posteriormente se podrán incorporar:

* GBP/USD
* USD/JPY
* AUD/USD
* otros pares líquidos

La incorporación de nuevos instrumentos requerirá validación independiente.

---

# 5. POR QUÉ ESTOS MERCADOS

La selección inicial se basa en:

* liquidez
* disponibilidad de datos
* infraestructura de ejecución
* profundidad del mercado
* existencia de brokers/API
* posibilidad de operar sistemáticamente
* disponibilidad de cuentas de fondeo
* costes relativamente transparentes
* posibilidad de diversificación futura

La selección NO implica que estos mercados sean necesariamente más rentables.

---

# 6. ARQUITECTURA GENERAL

```text
                    ┌───────────────────┐
                    │   MARKET DATA     │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │    DATA ENGINE    │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ RESEARCH ENGINE   │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ BACKTEST ENGINE   │
                    └─────────┬─────────┘
                              │
                              ▼
                 ┌──────────────────────────┐
                 │ ROBUSTNESS / VALIDATION  │
                 └────────────┬─────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ STRATEGY ENGINE   │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ PORTFOLIO ENGINE  │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │    RISK ENGINE    │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ ORDER MANAGEMENT  │
                    └─────────┬─────────┘
                              │
                ┌─────────────┼─────────────┐
                ▼             ▼             ▼
              MT5         FUTURES API      IBKR
                │             │             │
                └─────────────┼─────────────┘
                              ▼
                       LIVE ACCOUNTS
```

---

# 7. STACK TECNOLÓGICO

## Lenguaje principal

**Python 3.x**

Será el lenguaje principal para:

* investigación
* backtesting
* análisis
* portfolio
* riesgo
* APIs
* reporting
* monitorización

## MQL5

Utilizar MQL5 exclusivamente cuando la ejecución mediante MetaTrader 5 lo requiera.

## C#

Se utilizará cuando la infraestructura de futuros seleccionada lo requiera.

## Base de datos

**PostgreSQL**

## Contenedores

**Docker**

## Control de versiones

**Git**

## Entorno

Linux/VPS para producción.

Windows puede utilizarse para herramientas específicas como MT5 cuando sea necesario.

---

# 8. PRINCIPIO FUNDAMENTAL DE ARQUITECTURA

El siguiente principio es obligatorio:

> La estrategia no sabe dónde se ejecutará.

Ejemplo:

```python
signal = strategy.generate_signal(data)

approved = risk_engine.validate(signal)

if approved:
    execution_engine.execute(signal)
```

La estrategia no debe contener:

```python
mt5.order_send(...)
```

ni:

```python
ib.placeOrder(...)
```

La ejecución pertenece a otra capa.

---

# 9. ESTRUCTURA DEL REPOSITORIO

```text
trading_system/
│
├── README.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml
│
├── config/
│   ├── development/
│   ├── paper/
│   └── production/
│
├── data/
│   ├── ingestion/
│   ├── cleaning/
│   ├── validation/
│   └── storage/
│
├── research/
│   ├── hypotheses/
│   ├── features/
│   ├── experiments/
│   └── notebooks/
│
├── strategies/
│   ├── base/
│   ├── futures/
│   ├── forex/
│   └── common/
│
├── backtesting/
│   ├── engine/
│   ├── execution/
│   ├── costs/
│   └── reports/
│
├── validation/
│   ├── out_of_sample/
│   ├── walk_forward/
│   ├── monte_carlo/
│   └── stress/
│
├── portfolio/
│   ├── exposure/
│   ├── correlation/
│   └── allocation/
│
├── risk/
│   ├── position_sizing/
│   ├── limits/
│   └── prop_rules/
│
├── execution/
│   ├── order_manager/
│   └── adapters/
│
├── brokers/
│   ├── mt5/
│   ├── futures/
│   └── ibkr/
│
├── monitoring/
│
├── database/
│
├── reporting/
│
├── tests/
│
├── deployment/
│
└── docs/
```

---

# 10. DATA ENGINE

El Data Engine será responsable de:

* adquisición
* validación
* limpieza
* normalización
* almacenamiento

Debe conservarse la información de procedencia del dato.

Cada dataset debe tener:

```text
source
instrument
timeframe
timezone
start
end
version
download_date
```

Debe detectarse:

* duplicación
* gaps
* timestamps incorrectos
* valores imposibles
* cambios de contrato
* problemas de sesión

---

# 11. DATOS Y SESIONES

Todos los timestamps deben normalizarse internamente a UTC.

La lógica de estrategia deberá poder trabajar posteriormente con:

* sesión europea
* sesión americana
* sesión asiática
* apertura
* cierre
* premarket
* horarios específicos del instrumento

Nunca codificar horarios ambiguos.

---

# 12. BACKTEST ENGINE

El backtester deberá simular:

* entradas
* salidas
* stops
* take profits
* trailing stops
* comisiones
* spread
* slippage
* latencia
* tamaño de posición
* gaps
* sesiones
* rollover
* costes específicos del instrumento

El backtest deberá producir tanto resultados agregados como un registro completo de operaciones.

---

# 13. MÉTRICAS

Cada backtest deberá producir como mínimo:

* Net Profit
* CAGR
* Annual Return
* Volatility
* Sharpe
* Sortino
* Calmar
* Maximum Drawdown
* Drawdown Duration
* Profit Factor
* Expectancy
* Win Rate
* Average Win
* Average Loss
* Payoff Ratio
* Number of Trades
* Exposure
* Turnover
* MAE
* MFE

Y:

* P&L diario
* P&L mensual
* P&L anual
* distribución de operaciones
* distribución de pérdidas
* distribución de ganancias

---

# 14. DESARROLLO DE ESTRATEGIAS

No comenzar con una estrategia predeterminada.

El Research Engine deberá permitir investigar diferentes familias.

Inicialmente:

### A. Trend Following

### B. Momentum

### C. Breakout

### D. Mean Reversion

Posteriormente:

### E. Volatility

### F. Statistical Arbitrage

### G. Pairs Trading

### H. Multi-factor

### I. Machine Learning

ML será opcional y posterior.

---

# 15. PROTOCOLO DE INVESTIGACIÓN

Cada estrategia debe comenzar por una hipótesis.

Ejemplo:

```text
HYPOTHESIS:
Existe una relación estadística entre X y Y
bajo determinadas condiciones de mercado.
```

Después:

```text
Hypothesis
↓
Data
↓
Feature
↓
Signal
↓
Backtest
↓
Validation
```

No desarrollar una estrategia únicamente porque una combinación de indicadores produzca un gráfico de equity atractivo.

---

# 16. CONTROL DE OVERFITTING

Cada experimento debe registrar:

* parámetros probados
* número de pruebas
* dataset utilizado
* período
* resultado
* versión del código

El sistema debe identificar:

* parámetros excesivamente específicos
* estrategias extremadamente sensibles
* resultados concentrados en pocos trades
* dependencia excesiva de determinados períodos
* dependencia excesiva de determinados instrumentos

---

# 17. OUT-OF-SAMPLE

Los datos deben dividirse temporalmente.

Ejemplo conceptual:

```text
|--------- IN SAMPLE ---------|------ OOS ------|
        desarrollo                validación
```

Nunca utilizar el OOS para optimizar la estrategia.

Si el OOS se utiliza para modificar parámetros, deja de considerarse verdaderamente OOS.

---

# 18. WALK-FORWARD

Implementar:

```text
Train
  ↓
Test
  ↓
Move Window
  ↓
Train
  ↓
Test
  ↓
...
```

Registrar el rendimiento de cada período independiente.

Una estrategia que solamente funciona en una ventana concreta debe ser considerada frágil.

---

# 19. MONTE CARLO

Aplicar simulaciones sobre:

* orden de operaciones
* retornos
* slippage
* costes
* parámetros
* condiciones de ejecución

Calcular:

* distribución de drawdown
* drawdown esperado
* peor drawdown simulado
* probabilidad de pérdida
* probabilidad de alcanzar objetivos
* riesgo de ruina
* duración de períodos negativos

---

# 20. STRESS TESTING

Cada estrategia deberá someterse a escenarios como:

* aumento de spread
* aumento de slippage
* menor liquidez
* reducción de frecuencia
* ejecución peor de la esperada
* pérdida de operaciones
* incremento de costes
* cambios de volatilidad

La estrategia no deberá depender de condiciones de ejecución excesivamente optimistas.

---

# 21. RISK ENGINE

El Risk Engine será independiente de las estrategias.

Deberá controlar:

```text
Risk per trade
Strategy risk
Instrument risk
Market risk
Portfolio risk
Daily loss
Weekly loss
Maximum drawdown
Exposure
Correlation
Leverage
```

---

# 22. RIESGO INICIAL

El sistema será diseñado para permitir inicialmente:

**0,25 % de riesgo teórico por operación**

como parámetro configurable.

No debe asumirse que este porcentaje será el óptimo.

El objetivo inicial es operar de forma conservadora y posteriormente escalar únicamente cuando los datos lo justifiquen.

---

# 23. ESCALADO

Nunca incrementar el riesgo simplemente porque una estrategia haya tenido un buen mes.

El escalado deberá depender de:

* forward performance
* drawdown
* estabilidad
* cumplimiento de reglas
* calidad de ejecución
* número de operaciones
* desviación respecto al backtest

---

# 24. PORTFOLIO ENGINE

El Portfolio Engine calculará:

```text
Total Exposure
Total Risk
Risk by Strategy
Risk by Instrument
Risk by Market
Long Exposure
Short Exposure
Correlation
Concentration
```

El sistema debe detectar exposiciones indirectas.

Ejemplo:

```text
NQ Long
ES Long
NASDAQ Stocks Long
BTC Long
```

no deben tratarse como cuatro riesgos completamente independientes.

---

# 25. POSITION SIZING

Implementar inicialmente:

**Fixed Fractional / Risk Based Position Sizing**

La cantidad se determinará en función de:

```text
Account Equity
Risk %
Entry
Stop
Instrument Value
Contract Size
Current Exposure
Portfolio Risk
```

Posteriormente estudiar:

* volatility targeting
* ATR sizing
* dynamic risk
* drawdown-based scaling

---

# 26. CUENTAS DE FONDEO

El sistema debe permitir representar cada cuenta mediante configuración.

Ejemplo:

```yaml
account:
  provider:
  account_id:
  currency:
  nominal_balance:
  daily_loss_limit:
  max_drawdown:
  drawdown_type:
  trailing_drawdown:
  overnight_allowed:
  news_allowed:
  max_position_size:
```

Las reglas deben ser configurables.

Nunca deben estar codificadas dentro de las estrategias.

---

# 27. PROP FIRM RULE ENGINE

Antes de ejecutar una operación:

```text
Signal
↓
Portfolio Risk
↓
Prop Rule Engine
↓
Execution
```

El Rule Engine comprobará:

* daily loss
* max drawdown
* trailing drawdown
* tamaño máximo
* horario
* noticias
* overnight
* weekend
* instrumentos permitidos
* otras restricciones aplicables

Las reglas deberán verificarse contra la documentación vigente del proveedor antes de desplegar una cuenta real.

---

# 28. CHALLENGE SIMULATOR

Crear un simulador que reproduzca las reglas de una cuenta de evaluación.

Input:

```text
Strategy
Account Size
Target
Daily Loss Limit
Max Drawdown
Drawdown Type
Trading Rules
```

Output:

```text
Probability of Passing
Probability of Breach
Expected Days to Target
Maximum Drawdown
Worst Daily Loss
Distribution of Outcomes
```

Se utilizarán simulaciones Monte Carlo.

---

# 29. EJECUCIÓN

Crear interfaz:

```python
class BrokerAdapter:

    def connect()
    def disconnect()

    def get_account()

    def get_positions()
    def get_orders()

    def get_market_data()

    def submit_order()
    def cancel_order()
    def modify_order()
```

Adapters independientes:

```text
MT5Adapter
FuturesAdapter
IBKRAdapter
CryptoAdapter
```

La estrategia nunca llamará directamente a estos adapters.

---

# 30. IDEMPOTENCIA DE ÓRDENES

El Order Manager debe impedir:

* órdenes duplicadas
* reenvío accidental
* doble ejecución después de una reconexión

Cada orden debe disponer de un identificador único.

El sistema debe poder recuperar su estado después de un reinicio.

---

# 31. FAIL-SAFE

Ante:

* pérdida de conexión
* pérdida de datos
* API caída
* error de broker
* servidor reiniciado
* datos incoherentes

el sistema deberá pasar a un estado seguro.

Nunca debe continuar enviando órdenes si no puede determinar correctamente:

* posición actual
* órdenes pendientes
* estado de cuenta
* datos de mercado

---

# 32. KILL SWITCH

Debe existir:

### Strategy Kill Switch

Detiene una estrategia.

### Market Kill Switch

Detiene estrategias de un mercado.

### Account Kill Switch

Detiene una cuenta.

### Global Kill Switch

Detiene todo el sistema.

El Global Kill Switch debe:

1. detener nuevas órdenes
2. cancelar órdenes pendientes cuando proceda
3. alertar al operador
4. registrar el evento
5. opcionalmente cerrar posiciones según política configurada

---

# 33. PAPER TRADING

Antes de dinero real:

```text
Backtest
↓
OOS
↓
Walk Forward
↓
Monte Carlo
↓
Stress
↓
Paper
```

El Paper Trading deberá utilizar datos de mercado reales cuando sea posible.

Debe registrar las operaciones simuladas exactamente igual que las reales.

---

# 34. FORWARD TESTING

Comparar:

```text
Backtest
vs
Paper
vs
Live
```

Medir diferencias en:

* entradas
* salidas
* slippage
* frecuencia
* P&L
* drawdown
* latencia

Si el comportamiento live se aleja significativamente del esperado, detener el escalado.

---

# 35. MONITORING

Dashboard mínimo:

```text
EQUITY
P&L
DRAWDOWN
OPEN RISK
EXPOSURE
POSITIONS
ORDERS
STRATEGIES
BROKERS
CONNECTIONS
SLIPPAGE
LATENCY
PROP LIMITS
```

---

# 36. ALERTAS

Alertar ante:

* pérdida de conexión
* órdenes rechazadas
* slippage anormal
* drawdown
* pérdida diaria
* API caída
* datos incorrectos
* estrategia detenida
* exceso de exposición
* violación potencial de reglas
* kill switch

---

# 37. LOGGING

Registrar cada evento importante.

Cada operación deberá poder reconstruirse mediante:

```text
Timestamp
Strategy ID
Strategy Version
Account ID
Instrument
Signal
Entry
Stop
Target
Order
Fill
Quantity
Slippage
Commission
PnL
Risk
Portfolio State
```

---

# 38. VERSIONADO DE ESTRATEGIAS

Cada estrategia debe tener:

```text
Strategy ID
Strategy Version
Parameter Version
Code Commit
Dataset Version
```

Ejemplo:

```text
NQ_MOMENTUM
v1.3.2
params_07
commit_a83f92
dataset_2026_08
```

Esto permitirá saber exactamente qué versión generó cada operación.

---

# 39. SEGURIDAD

Nunca almacenar:

* contraseñas
* API keys
* tokens

en el repositorio.

Utilizar:

```text
.env
environment variables
secrets
```

Separar estrictamente:

```text
DEVELOPMENT
PAPER
LIVE
```

El entorno de desarrollo no debe poder ejecutar operaciones reales accidentalmente.

---

# 40. TESTING

Cada módulo deberá disponer de tests.

### Unit Tests

Para:

* señales
* indicadores
* position sizing
* risk
* portfolio

### Integration Tests

Para:

* brokers
* APIs
* database

### Regression Tests

Para backtests.

### Failure Tests

Simular:

* desconexión
* API caída
* duplicación de órdenes
* datos corruptos
* reinicio
* latencia
* slippage

---

# 41. BASE DE DATOS

PostgreSQL almacenará:

```text
market_data_metadata
instruments
strategies
strategy_versions
parameters
experiments
backtests
trades
orders
fills
positions
accounts
risk_events
system_events
```

Los datos históricos pesados podrán mantenerse en almacenamiento especializado si PostgreSQL deja de ser apropiado.

---

# 42. RESEARCH DATABASE

Cada experimento debe ser reproducible.

Registrar:

```text
Experiment ID
Strategy
Dataset
Period
Parameters
Code Version
Metrics
Validation Results
Decision
```

Posibles estados:

```text
RESEARCH
PROMISING
VALIDATION
REJECTED
PAPER
LIVE
RETIRED
```

---

# 43. CRITERIOS DE PROMOCIÓN DE UNA ESTRATEGIA

Una estrategia no puede pasar directamente:

```text
Backtest → Live
```

Debe pasar:

```text
Research
↓
Backtest
↓
Validation
↓
OOS
↓
Walk Forward
↓
Monte Carlo
↓
Stress
↓
Paper
↓
Forward
↓
Live
```

Cada transición tendrá criterios documentados.

---

# 44. CRITERIOS DE RECHAZO

Una estrategia deberá ser rechazada o devuelta a investigación si presenta:

* dependencia excesiva de pocos trades
* parámetros extremadamente sensibles
* deterioro severo OOS
* Monte Carlo incompatible con el riesgo
* drawdown excesivo
* dependencia excesiva de un período
* costes que destruyen el edge
* slippage excesivo
* comportamiento inconsistente
* problemas metodológicos

No se modificará una estrategia simplemente para evitar su rechazo.

---

# 45. MACHINE LEARNING

ML será un módulo opcional.

Antes de utilizar ML deberá existir una estrategia baseline sencilla.

El modelo ML tendrá que demostrar una mejora fuera de muestra.

Se controlará:

* leakage
* overfitting
* feature drift
* temporal validation
* complejidad
* estabilidad

Si un modelo complejo no mejora significativamente al baseline, se mantendrá el baseline.

---

# 46. INFRAESTRUCTURA

Primera etapa:

* ordenador local para desarrollo
* Git
* Python
* PostgreSQL
* Docker
* VPS cuando comience paper trading continuo

No contratar infraestructura costosa hasta que exista una estrategia candidata.

---

# 47. PRESUPUESTO

Presupuesto de desarrollo inicial recomendado:

**aproximadamente €1.000–€2.500 como reserva total**, no como gasto inmediato.

Objetivo:

* infraestructura
* datos
* VPS
* pequeñas pruebas
* posibles plataformas
* primera evaluación cuando el sistema esté validado

No utilizar capital significativo para operar live durante las primeras etapas.

El coste mensual debe mantenerse inicialmente aproximadamente en:

**€50–€150/mes**, salvo que la calidad de datos o las plataformas elegidas exijan más.

Antes de contratar cualquier servicio de pago deberá justificarse su necesidad.

---

# 48. CAPITAL DE FONDEO

No considerar el tamaño nominal de la cuenta como capital disponible.

La métrica relevante será:

**capital de riesgo permitido por las reglas de la cuenta.**

Una cuenta nominal de $100.000 con $10.000 de drawdown no debe modelarse como si el sistema dispusiera de $100.000 de capital de riesgo.

---

# 49. ESCALADO DEL CAPITAL

El sistema deberá permitir:

```text
1 cuenta
↓
validación
↓
2 cuentas
↓
validación
↓
más cuentas
```

No escalar únicamente porque una cuenta haya alcanzado un objetivo.

Debe existir evidencia suficiente de que:

* la estrategia funciona
* la ejecución funciona
* el riesgo es controlable
* la correlación está controlada
* las reglas se cumplen

---

# 50. PRIMER MVP

El MVP NO incluirá inicialmente:

* acciones
* crypto
* machine learning
* múltiples brokers
* múltiples prop firms
* estrategias complejas

El MVP incluirá:

```text
Python
PostgreSQL
Data Engine
Backtest Engine
Research Framework
Risk Engine básico
1 mercado
1 estrategia experimental
Paper Trading
Reporting
Tests
```

Después se incorporará el segundo mercado.

---

# 51. PRIMERA ESTRATEGIA

No está predeterminada.

El Research Engine deberá estudiar inicialmente:

### Futuros

* momentum
* trend
* breakout
* mean reversion

### Forex

* momentum
* trend
* breakout
* mean reversion

El objetivo es encontrar **familias de comportamiento**, no una combinación mágica de indicadores.

---

# 52. REGLAS PARA LA INVESTIGACIÓN

Cada investigación deberá responder:

1. ¿Cuál es la hipótesis?
2. ¿Por qué debería existir una ventaja?
3. ¿Qué fenómeno de mercado intenta capturar?
4. ¿Qué datos necesita?
5. ¿Cuál es el mecanismo de entrada?
6. ¿Cuál es el mecanismo de salida?
7. ¿Cómo se controla el riesgo?
8. ¿Qué costes existen?
9. ¿Cuándo deja de funcionar?
10. ¿Cómo se validará?

---

# 53. PRINCIPIO DE FALSACIÓN

No intentar demostrar que una estrategia funciona.

Intentar encontrar razones por las que podría NO funcionar.

Cada estrategia deberá someterse a:

```text
¿Qué pasa si...?
```

* spreads mayores
* slippage mayor
* volatilidad menor
* volatilidad mayor
* menos operaciones
* parámetros distintos
* período diferente
* instrumento diferente
* régimen de mercado diferente

Una estrategia robusta debe sobrevivir razonablemente a estas perturbaciones.

---

# 54. DOCUMENTACIÓN

Cada módulo debe disponer de documentación.

Crear:

```text
docs/
├── architecture.md
├── data.md
├── backtesting.md
├── research.md
├── risk.md
├── execution.md
├── deployment.md
└── strategies/
```

---

# 55. ROADMAP

## MILESTONE 0 — Arquitectura

Entregables:

* arquitectura
* stack
* repository
* interfaces
* database schema
* roadmap

---

## MILESTONE 1 — Data Engine

Entregables:

* ingestion
* validation
* normalization
* storage
* data quality reports

---

## MILESTONE 2 — Backtesting

Entregables:

* event-driven backtester
* transaction costs
* slippage
* metrics
* reports

---

## MILESTONE 3 — Research Framework

Entregables:

* strategy interface
* experiment tracking
* parameter management
* reproducibility

---

## MILESTONE 4 — Primera estrategia

Entregables:

* hypothesis
* implementation
* backtest
* validation
* documentation

---

## MILESTONE 5 — Robustness

Entregables:

* OOS
* Walk Forward
* Monte Carlo
* stress testing
* sensitivity analysis

---

## MILESTONE 6 — Paper Trading

Entregables:

* real-time data
* simulated execution
* monitoring
* alerts

---

## MILESTONE 7 — Primera cuenta

Solo si la estrategia supera los criterios anteriores.

---

## MILESTONE 8 — Segundo mercado

Añadir el segundo mercado utilizando las mismas interfaces.

---

## MILESTONE 9 — Portfolio

Introducir:

* múltiples estrategias
* correlación
* exposición agregada
* risk budgeting

---

## MILESTONE 10 — Escalado

Añadir:

* acciones
* crypto
* más futuros
* más Forex
* múltiples cuentas
* nuevos brokers

---

# 56. REGLAS DE DESARROLLO PARA EL AGENTE DE IA

El agente de desarrollo deberá:

* no inventar APIs
* verificar documentación actual cuando sea necesario
* no asumir que una prop firm permite determinado tipo de automatización
* no inventar reglas de cuentas
* no inventar resultados de backtests
* no presentar resultados sin ejecutar las pruebas
* explicar errores
* escribir tests
* mantener modularidad
* mantener reproducibilidad
* documentar decisiones

Cuando falte información crítica:

**preguntar antes de implementar.**

Cuando exista una decisión reversible:

**proponer una opción razonable y continuar.**

Cuando exista una decisión que pueda afectar significativamente al capital:

**detenerse y solicitar aprobación.**

---

# 57. REGLA DE NO-SOBREINGENIERÍA

No construir componentes antes de necesitarlos.

El sistema debe crecer:

```text
MVP
↓
Validación
↓
Necesidad real
↓
Nueva funcionalidad
```

No construir inicialmente una infraestructura para 50 robots si todavía no existe uno validado.

---

# 58. DEFINICIÓN DE "PRODUCCIÓN"

Una estrategia solo se considera producción cuando:

* código versionado
* backtest reproducible
* OOS completado
* Walk Forward completado
* Monte Carlo completado
* stress testing completado
* paper trading completado
* forward testing satisfactorio
* riesgo aprobado
* broker aprobado
* reglas de cuenta verificadas
* monitorización activa
* kill switch probado

---

# 59. PRIMERA FASE DE IMPLEMENTACIÓN

El proyecto comienza exclusivamente con:

## DATA ENGINE + BACKTEST ENGINE + RESEARCH FRAMEWORK

No desarrollar todavía:

* integración live
* cuenta de fondeo
* múltiples brokers
* ML
* portfolio multi-cuenta

Primero debemos demostrar que la infraestructura de investigación es correcta.

---

# 60. PRIMERA TAREA DEL AGENTE

Antes de escribir código, producir:

### Documento 1

Arquitectura técnica detallada.

### Documento 2

Estructura del repositorio.

### Documento 3

Stack tecnológico con justificación.

### Documento 4

Modelo inicial de base de datos.

### Documento 5

Interfaces Python principales.

### Documento 6

Roadmap de implementación.

### Documento 7

Plan de testing.

### Documento 8

Riesgos cuantitativos y técnicos.

Después de presentar estos documentos:

**NO comenzar automáticamente la implementación.**

Esperar aprobación.

---

# 61. PRINCIPIO FINAL

Este proyecto no tiene como objetivo fabricar un robot que "parezca rentable".

Tiene como objetivo construir un proceso reproducible para responder:

> **¿Existe evidencia suficiente de que una estrategia posee una ventaja estadística explotable después de costes, slippage, restricciones operativas y riesgo real?**

Si la respuesta es sí:

```text
RESEARCH
→ VALIDATION
→ PAPER
→ LIVE
```

Si la respuesta es no:

```text
REJECT
```

El rechazo de una estrategia es un resultado válido del sistema.

El objetivo final es construir una **cartera de estrategias algorítmicas independientes, robustas y controladas por un sistema central de riesgo**, capaz de operar progresivamente múltiples mercados y cuentas sin sacrificar trazabilidad ni control del riesgo.
