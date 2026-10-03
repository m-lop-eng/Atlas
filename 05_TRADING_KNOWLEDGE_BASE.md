# 05_TRADING_KNOWLEDGE_BASE.md

**Project:** Modular Multi-Market Algorithmic Trading System
**Document:** Trading Knowledge Base
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Date:** 2026-09-18
**Authority:** Master Specification

---

# 1. PURPOSE

This document defines the project's centralized trading and quantitative-finance knowledge base.

Its purpose is to provide a structured reference for:

* developers
* quantitative researchers
* strategy developers
* risk managers
* data engineers
* execution engineers
* AI agents
* future maintainers

The Knowledge Base provides concepts, definitions, mechanisms, known limitations, implementation considerations, and references relevant to systematic trading.

It is not, by itself, a collection of trading strategies.

A statement contained in this document must **not automatically become a trading rule**.

Any potential trading edge must be independently researched, implemented, backtested, validated, and challenged according to:

* `01_GENERAL_RULES.md`
* `02_QUANT_RESEARCH_METHODOLOGY.md`
* `03_RISK_MANAGEMENT.md`
* `04_BACKTEST_VALIDATION.md`

---

# 2. KNOWLEDGE CLASSIFICATION

Every important knowledge item should be classified.

## 2.1 Classification levels

### ESTABLISHED

A broadly accepted technical, mathematical, financial, or engineering concept supported by strong evidence.

Examples:

* bid/ask spread exists in a market with quoted bid and ask prices
* transaction costs reduce strategy returns
* look-ahead bias invalidates backtests
* correlation measures linear dependence between variables

---

### EMPIRICAL

A relationship observed repeatedly in data but whose strength, persistence, or applicability may depend on the market and period.

Examples:

* volatility clustering
* momentum effects
* mean-reversion behavior in certain instruments
* intraday seasonal patterns

Empirical observations must still be validated for the project's specific markets and timeframe.

---

### HYPOTHESIS

A proposed mechanism or relationship that has not yet been sufficiently validated.

Examples:

* a particular breakout condition creates exploitable excess returns
* a specific volatility regime improves strategy performance
* a particular feature predicts future returns

Hypotheses must enter the research process rather than being treated as facts.

---

### IMPLEMENTATION-SPECIFIC

Knowledge that depends on the project's architecture, broker, exchange, data provider, software, or configuration.

Examples:

* broker order semantics
* tick-size configuration
* contract specifications
* API behavior
* database schemas
* execution latency characteristics

---

### UNVERIFIED

Information that has been encountered but has not yet been sufficiently verified.

It must not be used as a production assumption.

---

# 3. KNOWLEDGE ENTRY STANDARD

Important knowledge entries should contain, where applicable:

```text
ID:
Topic:
Classification:
Definition:
Mechanism:
Practical relevance:
Known limitations:
Market applicability:
Project applicability:
Source/reference:
Confidence:
Requires empirical validation:
Last reviewed:
```

AI agents should prefer documented and verified knowledge over assumptions.

---

# 4. MARKET FOUNDATIONS

## 4.1 Financial markets

A financial market provides a mechanism through which financial instruments can be traded.

Important dimensions include:

* price
* liquidity
* volatility
* trading hours
* participants
* transaction costs
* market structure
* order types
* settlement
* leverage
* financing
* regulation

Different markets have materially different statistical and execution characteristics.

Therefore:

> A strategy should not be assumed to transfer from one asset class to another without validation.

---

# 5. ASSET CLASSES

## 5.1 Futures

Futures are standardized contracts representing an agreement to transact an underlying asset or financial exposure under defined contract specifications.

Relevant characteristics include:

* contract size
* tick size
* tick value
* expiry
* rollover
* margin
* trading sessions
* settlement
* exchange fees
* liquidity
* market depth

Futures require explicit handling of contract lifecycle and roll methodology.

---

## 5.2 Foreign Exchange

Forex represents trading between currencies.

Important characteristics include:

* currency pairs
* bid/ask spread
* liquidity
* session overlap
* rollover/swap
* leverage
* broker-specific execution
* decentralized market structure

Forex data and execution conditions can differ materially between brokers.

---

## 5.3 Equities

Equities represent ownership interests in companies.

Relevant considerations include:

* corporate actions
* dividends
* stock splits
* mergers
* delistings
* trading halts
* borrow availability for short positions
* liquidity
* exchange rules

Historical equity datasets must account appropriately for corporate actions and survivorship bias.

---

## 5.4 Indices and CFDs

Indices may be represented through futures, ETFs, CFDs, or other instruments.

The representation matters.

The same nominal index can have different:

* pricing
* trading hours
* spreads
* financing
* rollover
* liquidity
* execution

A strategy should therefore identify the actual traded instrument rather than merely the underlying reference index.

---

## 5.5 Cryptocurrencies

Crypto markets operate across multiple exchanges with potentially different:

* liquidity
* fees
* market structures
* trading hours
* order books
* funding mechanisms
* market participants

Cross-exchange strategies require explicit handling of:

* exchange-specific prices
* latency
* fees
* transfer constraints
* liquidity
* outages
* API limitations

---

# 6. MARKET MICROSTRUCTURE

Market microstructure studies how orders interact to produce transactions and prices.

Understanding microstructure is essential when modeling execution.

---

## 6.1 Bid and ask

The:

* **bid** is the highest currently quoted buying price
* **ask** is the lowest currently quoted selling price

The difference is the:

> bid-ask spread

---

## 6.2 Spread

The spread represents an immediate transaction cost under simplified execution assumptions.

For a market order:

```text
Buy → approximately ask
Sell → approximately bid
```

Actual execution can differ because of:

* latency
* price movement
* liquidity
* market impact
* partial fills

---

## 6.3 Liquidity

Liquidity describes the market's ability to absorb transactions without excessive price movement.

Relevant dimensions include:

* trading volume
* market depth
* spread
* order-book depth
* execution speed
* resilience after large transactions

Volume alone is not a complete measure of liquidity.

---

## 6.4 Slippage

Slippage is the difference between an expected/reference execution price and the actual execution price.

It can result from:

* market movement
* latency
* insufficient liquidity
* order size
* volatility
* queue position
* market impact

Backtests must model slippage where materially relevant.

---

## 6.5 Market impact

Large orders can influence market prices.

Market impact may depend on:

* order size
* available liquidity
* volatility
* time of day
* market regime

The assumption of unlimited liquidity is generally inappropriate for realistic execution modeling.

---

# 7. ORDER TYPES

The system should understand at minimum:

* market orders
* limit orders
* stop orders
* stop-limit orders
* bracket orders
* reduce-only orders where supported
* time-in-force rules

Order semantics must be implemented according to the actual broker/exchange.

---

## 7.1 Market order

Designed to execute as quickly as possible at available prices.

Primary advantages:

* execution priority

Primary risk:

* uncertain execution price

---

## 7.2 Limit order

Specifies a maximum purchase price or minimum selling price.

Primary advantage:

* price control

Primary risk:

* non-execution

---

## 7.3 Stop order

Becomes executable after a defined trigger condition.

Important considerations:

* trigger price
* execution price
* gaps
* slippage
* broker implementation

---

# 8. LATENCY

Latency is the time between an event and the corresponding system response.

Potential sources include:

```text
Market
↓
Data provider
↓
Network
↓
Application
↓
Strategy
↓
Risk engine
↓
Order manager
↓
Broker API
↓
Exchange
```

Latency can materially affect:

* high-frequency strategies
* breakout strategies
* news strategies
* short holding periods
* execution-sensitive systems

The system should not assume zero latency unless the strategy explicitly does not depend on execution timing.

---

# 9. STATISTICS

## 9.1 Probability

Probability describes uncertainty associated with possible outcomes.

Trading systems should treat returns as distributions rather than deterministic sequences.

---

## 9.2 Expected value

Expected value:

$$
E[X] = \sum_i p_i x_i
$$

For a simplified trading system:

$$
E = P(W)\cdot AvgWin - P(L)\cdot AvgLoss
$$

A positive expected value does not guarantee profitability over a finite sample.

---

## 9.3 Variance and volatility

Variance measures dispersion around the mean.

Volatility is commonly represented by the standard deviation of returns.

Volatility is important for:

* position sizing
* risk management
* regime classification
* portfolio construction
* stress testing

---

## 9.4 Covariance

Covariance measures how two variables move together.

It is an important building block for:

* portfolio risk
* correlation
* factor analysis
* portfolio optimization

---

## 9.5 Correlation

Correlation standardizes covariance.

$$
\rho_{XY} =
\frac{Cov(X,Y)}
{\sigma_X\sigma_Y}
$$

Correlation can change through time.

Therefore:

> Historical correlation should not be assumed to remain constant.

---

# 10. TIME SERIES

Trading data is fundamentally time-series data.

Important properties include:

* autocorrelation
* non-stationarity
* seasonality
* trends
* volatility clustering
* structural breaks
* regime changes

Standard machine-learning assumptions often need adaptation when applied to financial time series.

---

# 11. STATIONARITY

A stationary process has statistical properties that remain sufficiently stable through time under the relevant definition.

Financial prices are often non-stationary.

Transformations such as:

* returns
* log returns
* spreads
* normalized variables

may sometimes produce more stable representations.

However:

> Stationarity should be tested rather than assumed.

---

# 12. AUTOCORRELATION

Autocorrelation measures dependence between observations separated by a time lag.

It may be useful for identifying:

* persistence
* mean reversion
* momentum
* seasonality
* residual structure

Observed autocorrelation must be evaluated against statistical significance and economic relevance.

---

# 13. REGRESSION

Regression models relationships between variables.

Potential uses include:

* forecasting
* factor modeling
* residual analysis
* pairs trading
* feature analysis
* risk modeling

Regression relationships can break down under regime changes.

---

# 14. HYPOTHESIS TESTING

Statistical hypothesis testing can be used to evaluate whether observed relationships are compatible with a null hypothesis.

Relevant concepts include:

* null hypothesis
* alternative hypothesis
* test statistic
* p-value
* confidence interval
* statistical power
* Type I error
* Type II error

Statistical significance does not automatically imply trading profitability.

A statistically significant relationship may be:

* too small
* too unstable
* too expensive to trade
* economically irrelevant

---

# 15. MULTIPLE TESTING

Testing many hypotheses increases the probability of obtaining apparently significant results by chance.

Examples:

* many indicators
* many parameter combinations
* many markets
* many timeframes
* many entry rules
* many exit rules

The system should therefore track experiments.

Research results should never be evaluated without considering the number of hypotheses tested.

---

# 16. CONFIDENCE INTERVALS

Confidence intervals can provide information about uncertainty around estimated quantities.

They are useful for:

* average returns
* Sharpe estimates
* win rate
* expected value
* model coefficients

Point estimates alone can create false precision.

---

# 17. QUANTITATIVE PERFORMANCE METRICS

## 17.1 Sharpe ratio

A simplified Sharpe ratio:

$$
Sharpe =
\frac{E[R_p-R_f]}{\sigma_p}
$$

It measures excess return relative to volatility.

Limitations include sensitivity to:

* non-normal distributions
* autocorrelation
* outliers
* return frequency
* estimation period

---

## 17.2 Sortino ratio

The Sortino ratio focuses on downside deviation rather than total volatility.

It can be useful when downside risk is more relevant than symmetric volatility.

---

## 17.3 Calmar ratio

Commonly related to return relative to maximum drawdown.

It is useful for evaluating return relative to observed drawdown risk.

---

## 17.4 Maximum drawdown

Maximum drawdown measures the largest observed decline from a previous equity peak to a subsequent trough.

It is one of the most important risk metrics in the system.

---

## 17.5 Value at Risk

VaR estimates a loss threshold at a given confidence level under a defined methodology.

Example:

```text
1-day 95% VaR
```

means that, under the model assumptions, losses should exceed the estimated threshold approximately 5% of the time.

VaR does not describe the magnitude of losses beyond that threshold.

---

## 17.6 Conditional Value at Risk

CVaR, also called Expected Shortfall under common definitions, focuses on expected losses beyond the VaR threshold.

It is useful for tail-risk analysis.

---

## 17.7 Kelly criterion

Kelly sizing provides a theoretical framework for maximizing long-term logarithmic growth under specific assumptions.

It can produce aggressive sizing.

Therefore:

> Kelly should be treated primarily as an analytical concept, not as a default production position-sizing rule.

The project's initial risk framework is defined separately in `03_RISK_MANAGEMENT.md`.

---

## 17.8 Risk of ruin

Risk of ruin estimates the probability that losses will reduce capital beyond a defined unacceptable threshold.

It depends on:

* edge
* variance
* position sizing
* win/loss distribution
* dependency between trades
* account constraints

---

# 18. STRATEGY FAMILIES

Strategy families describe broad mechanisms rather than guaranteed sources of alpha.

---

## 18.1 Trend following

Attempts to capture persistent directional movement.

Typical concepts:

* moving-average relationships
* breakouts
* directional filters
* trailing exits

Potential weaknesses:

* sideways markets
* whipsaws
* delayed entries

---

## 18.2 Momentum

Attempts to exploit persistence in price movement.

Momentum may be:

* cross-sectional
* time-series
* short-term
* medium-term
* long-term

Momentum definitions must be explicit.

---

## 18.3 Breakout

Attempts to trade price movement beyond a defined range or reference level.

Potential inputs include:

* previous highs/lows
* volatility bands
* session ranges
* consolidation ranges

Important risks include false breakouts and execution slippage.

---

## 18.4 Mean reversion

Attempts to exploit temporary deviations from a reference value.

Potential references include:

* moving averages
* statistical spreads
* VWAP
* relative-value relationships

Mean reversion can fail during strong directional regimes.

---

## 18.5 Statistical arbitrage

Attempts to exploit statistical relationships between instruments or variables.

Potential methods include:

* pairs trading
* cointegration
* spread trading
* factor-neutral portfolios

Relationships must be continuously monitored because statistical relationships can change.

---

## 18.6 Volatility strategies

Strategies can target:

* volatility expansion
* volatility contraction
* volatility regimes
* realized volatility
* implied volatility where applicable

Volatility itself can be both a signal and a risk variable.

---

## 18.7 Carry

Carry strategies attempt to capture returns associated with holding an asset or position over time.

Examples may include:

* interest-rate differentials
* futures basis
* funding mechanisms

Carry can have substantial regime and tail-risk characteristics.

---

## 18.8 Machine-learning strategies

Machine learning may be used for:

* classification
* regression
* regime detection
* feature selection
* signal ranking
* portfolio allocation

ML should not be introduced merely because it is technically sophisticated.

A simpler baseline should normally be established first.

---

# 19. TECHNICAL ANALYSIS

Technical-analysis concepts may provide candidate features.

Examples:

* moving averages
* RSI
* MACD
* ATR
* Bollinger Bands
* volatility measures
* support/resistance
* market structure
* volume-derived features

These concepts should be treated as **candidate transformations or hypotheses**, not inherently profitable signals.

The system must not assume:

```text
Indicator exists
        ↓
Therefore
        ↓
Trading edge exists
```

The edge must be demonstrated empirically.

---

# 20. FUNDAMENTAL AND EVENT FEATURES

Where relevant, future strategies may incorporate:

* macroeconomic data
* interest rates
* inflation
* employment data
* earnings
* economic calendars
* corporate events
* central-bank decisions

Critical considerations include:

* publication timestamp
* availability timestamp
* revisions
* delayed data
* market reaction
* timezone

A strategy must never use information before it was actually available to the market.

---

# 21. EXECUTION KNOWLEDGE

Execution is part of the strategy's realized performance.

Important variables include:

* spread
* commission
* slippage
* latency
* order type
* fill probability
* partial fills
* queue position
* market impact
* liquidity

A strategy with positive gross expectancy may become unprofitable after execution costs.

Therefore:

$$
Net\ Return =
Gross\ Return
-
Trading\ Costs
-
Slippage
-
Other\ Costs
$$

---

# 22. PORTFOLIO CONSTRUCTION

A portfolio is not simply a collection of profitable strategies.

Portfolio construction must consider:

* correlation
* volatility
* drawdown
* exposure
* concentration
* liquidity
* leverage
* capital requirements
* risk contribution

Two apparently different strategies may have highly correlated risk.

---

# 23. FACTOR EXPOSURE

Strategies may have implicit exposure to factors such as:

* market direction
* volatility
* momentum
* value
* carry
* liquidity
* interest rates

Understanding factor exposure helps identify hidden concentration.

---

# 24. MACHINE LEARNING FOR TRADING

Machine learning requires particular caution in financial applications.

## 24.1 Feature engineering

Features should be derived only from information available at the prediction timestamp.

---

## 24.2 Leakage

Data leakage occurs when information unavailable at decision time enters model training or prediction.

Examples:

* future prices
* future returns
* revised economic data
* future normalization statistics
* incorrectly aligned labels

Leakage can create apparently exceptional but invalid backtests.

---

## 24.3 Training and testing

Time-series ML should respect temporal ordering.

Typical structure:

```text
TRAIN
↓
VALIDATION
↓
TEST / OOS
```

Random shuffling is generally inappropriate when temporal dependency matters.

---

## 24.4 Walk-forward ML

For production-oriented systems, models may be:

```text
Train
↓
Predict future period
↓
Move window
↓
Retrain
↓
Predict next period
```

This better reflects the information available through time.

---

## 24.5 Concept drift

The statistical relationship between features and outcomes can change.

The system should monitor:

* feature distributions
* prediction distributions
* performance
* calibration
* regime behavior

---

## 24.6 ML governance

ML models must have:

* versioning
* training datasets
* feature definitions
* model parameters
* evaluation reports
* validation periods
* deployment records
* rollback capability

---

# 25. DATA ENGINEERING KNOWLEDGE

Trading systems depend heavily on data quality.

Important concepts include:

* raw data
* normalized data
* cleaned data
* derived data
* metadata
* timestamps
* timezone
* missing values
* duplicates
* outliers
* schema versioning
* provenance
* dataset versioning

Raw data should generally be preserved.

Derived datasets should be reproducible from documented transformations.

---

# 26. TIME AND TIMEZONES

Time must be handled explicitly.

The system should distinguish:

* UTC
* exchange time
* broker server time
* local time
* daylight-saving transitions

Internally, UTC should normally be the canonical reference.

Session logic must explicitly account for daylight-saving changes where relevant.

---

# 27. DATA QUALITY

The Data Engine should detect:

* missing candles
* duplicate timestamps
* impossible OHLC relationships
* invalid prices
* abnormal gaps
* inconsistent volume
* timezone errors
* contract discontinuities

Example OHLC constraint:

$$
High \geq Max(Open,Close)
$$

$$
Low \leq Min(Open,Close)
$$

Invalid observations should be flagged before research.

---

# 28. FUTURES ROLLOVERS

Futures contracts expire.

Historical analysis may therefore require:

* individual contracts
* continuous contracts
* rollover rules
* back-adjustment methodology
* volume-based rolls
* open-interest-based rolls

The selected methodology can materially affect backtest results.

It must therefore be documented.

---

# 29. BROKER AND EXCHANGE ARCHITECTURE

The system should distinguish:

```text
Strategy
    ↓
Risk
    ↓
Order Manager
    ↓
Broker Adapter
    ↓
Broker / Exchange
```

The strategy should not depend directly on broker-specific APIs.

This enables:

* broker replacement
* multi-broker deployment
* paper/live separation
* testing
* standardized execution

---

# 30. OPERATIONAL KNOWLEDGE

Production trading systems require operational engineering.

Relevant concepts include:

* VPS
* containers
* Docker
* process supervision
* health checks
* monitoring
* logging
* alerting
* backups
* database recovery
* secrets management
* deployment automation
* incident response

A profitable strategy that cannot operate reliably is not production-ready.

---

# 31. OBSERVABILITY

The production system should expose:

* system status
* broker connectivity
* data freshness
* strategy state
* open positions
* pending orders
* risk utilization
* P&L
* drawdown
* errors
* latency
* execution quality

Logs should contain enough information to reconstruct important events.

---

# 32. COMMON FAILURE MODES

The following failure modes must be actively investigated.

## 32.1 Overfitting

A strategy is excessively adapted to historical data.

---

## 32.2 Look-ahead bias

Future information influences a historical decision.

---

## 32.3 Data leakage

Information unavailable at decision time enters the research process.

---

## 32.4 Survivorship bias

Historical analysis excludes assets that disappeared, creating unrealistic historical universes.

---

## 32.5 Selection bias

The researcher selects favorable periods, markets, or strategies after observing results.

---

## 32.6 Data snooping

Repeated experimentation increases the probability of finding accidental relationships.

---

## 32.7 Execution mismatch

Backtest assumptions differ materially from live execution.

---

## 32.8 Regime change

The market mechanism underlying a strategy changes.

---

## 32.9 Parameter fragility

Small parameter changes cause large performance deterioration.

---

## 32.10 Excessive complexity

The strategy contains unnecessary rules that increase overfitting risk and maintenance cost.

---

## 32.11 Hidden correlation

Different strategies lose simultaneously because they share underlying exposures.

---

## 32.12 Operational failure

The trading logic may be valid while the production system fails because of:

* connectivity
* software bugs
* API failures
* incorrect state
* duplicate orders
* infrastructure failures
* incorrect configuration

---

# 33. RESEARCH PRINCIPLE: SIMPLE BEFORE COMPLEX

The project should generally follow:

```text
Simple hypothesis
      ↓
Simple baseline
      ↓
Robust validation
      ↓
Identify limitations
      ↓
Add complexity only when justified
```

Complexity must solve a demonstrated problem.

It should not be added simply because it improves historical performance.

---

# 34. STRATEGY TRANSFERABILITY

A strategy developed for one market should not automatically be assumed valid for another.

Transferability must be evaluated independently.

Questions include:

* Does the mechanism exist in both markets?
* Are transaction costs comparable?
* Is liquidity comparable?
* Are trading hours comparable?
* Is volatility comparable?
* Does the signal survive normalization?
* Does the strategy depend on market-specific microstructure?

---

# 35. KNOWLEDGE VS TRADING RULE

The following distinction is mandatory.

### Knowledge

> High volatility can increase execution risk.

### Hypothesis

> A volatility filter may improve strategy performance.

### Trading rule

> Do not enter when ATR exceeds X.

The first is knowledge.

The second is a research hypothesis.

The third is an implementation that requires empirical validation.

AI agents must preserve this distinction.

---

# 36. KNOWLEDGE BASE GOVERNANCE

The Knowledge Base should be version controlled.

Each important change should record:

```text
Date
Author / Agent
Topic
Previous understanding
New understanding
Reason for change
Source
Confidence
Impact on system
Required revalidation
```

If a knowledge change affects an existing strategy or risk rule, the affected components should be identified.

---

# 37. SOURCE PRIORITY

When resolving conflicting information, prefer:

1. Official exchange documentation
2. Official broker/API documentation
3. Official regulatory documentation
4. Peer-reviewed academic research
5. High-quality quantitative research
6. Reputable industry documentation
7. Secondary educational material
8. Forums and informal sources

Lower-quality sources may generate hypotheses but should not automatically establish production assumptions.

---

# 38. CURRENT INFORMATION

This document should avoid embedding rapidly changing information such as:

* current broker fees
* current prop-firm rules
* current API limitations
* current margin requirements
* current exchange schedules

Such information belongs in the appropriate dedicated configuration or rule framework.

The source and review date must be recorded when dynamic information is required.

---

# 39. AI AGENT USAGE RULES

AI agents working on the project should follow these rules.

### Rule 1

Consult the Knowledge Base before introducing domain assumptions.

### Rule 2

Do not convert knowledge directly into a strategy.

### Rule 3

Identify whether an assertion is:

* established
* empirical
* hypothetical
* implementation-specific
* unverified

### Rule 4

Do not invent missing financial or market information.

### Rule 5

When uncertainty exists, state the uncertainty explicitly.

### Rule 6

Do not use a source without considering its reliability and date.

### Rule 7

Do not silently modify the Knowledge Base.

### Rule 8

When new evidence contradicts an existing entry, create a documented review rather than silently replacing the information.

### Rule 9

Trading decisions must remain subordinate to the Risk Engine.

### Rule 10

No AI-generated strategy becomes production eligible without the complete validation lifecycle.

---

# 40. MINIMUM KNOWLEDGE DOMAINS FOR THE PROJECT

The project should maintain knowledge in at least these domains:

```text
Markets
Market Microstructure
Statistics
Probability
Time Series
Quantitative Finance
Risk Management
Portfolio Construction
Strategy Research
Execution
Data Engineering
Machine Learning
Software Engineering
Broker Architecture
Exchange Architecture
Infrastructure
Monitoring
Operational Risk
```

---

# 41. GLOSSARY

## Alpha

Excess return attributable to a strategy or exposure after the relevant benchmark and assumptions.

## Backtest

Historical simulation of a strategy using historical data.

## Bid

Highest quoted buying price.

## Ask

Lowest quoted selling price.

## Drawdown

Decline from an equity peak to a subsequent trough.

## Edge

A persistent statistical or economic advantage that may generate positive expected value after relevant costs.

## Expectancy

Expected average outcome per trade or unit of exposure under defined assumptions.

## Fill

Execution of an order.

## Leverage

Exposure relative to capital.

## Liquidity

Ability to transact with limited price impact.

## Look-ahead bias

Use of information that would not have been available at the historical decision time.

## Market impact

Price movement caused or contributed to by trading activity.

## OOS

Out-of-sample period not used for strategy development.

## Slippage

Difference between reference/expected and actual execution price.

## Spread

Difference between bid and ask.

## Walk-forward

Sequential process of training/researching on past data and evaluating on subsequent unseen data.

## Sharpe Ratio

Return relative to volatility, commonly adjusted for a risk-free rate.

## Sortino Ratio

Return relative to downside deviation.

## Maximum Drawdown

Largest historical peak-to-trough equity decline.

## Monte Carlo

Simulation framework used to explore possible outcomes under randomized or stressed assumptions.

## Regime

A period characterized by relatively different market behavior or statistical properties.

## Overfitting

Excessive adaptation to historical data that reduces generalization to unseen data.

---

# 42. KNOWLEDGE BASE MAINTENANCE

The Knowledge Base should be reviewed periodically and whenever:

* a new market is added
* a new broker is integrated
* a new asset class is introduced
* a major strategy family is introduced
* a material execution issue is discovered
* a validation methodology changes
* a significant research finding is established
* a production incident reveals a knowledge gap

Each review should determine whether existing assumptions remain valid.

---

# 43. RELATIONSHIP WITH OTHER PROJECT DOCUMENTS

This document provides conceptual and technical knowledge.

It does not override:

* `00_MASTER_SPECIFICATION.md`
* `01_GENERAL_RULES.md`
* `02_QUANT_RESEARCH_METHODOLOGY.md`
* `03_RISK_MANAGEMENT.md`
* `04_BACKTEST_VALIDATION.md`

Operational execution requirements will be defined in:

* `06_EXECUTION_OPERATION.md`

Prop-firm constraints will be defined in:

* `07_PROP_FIRM_RULES_FRAMEWORK.md`

Development procedures will be defined in:

* `08_DEVELOPMENT_WORKFLOW.md`

Strategy lifecycle requirements will be defined in:

* `09_STRATEGY_LIFECYCLE.md`

---

# 44. GOLDEN KNOWLEDGE RULE

> Knowledge informs research.
> Research produces hypotheses.
> Validation determines whether hypotheses survive evidence.
> Risk determines whether validated strategies may be deployed.

No knowledge entry, academic concept, indicator, market belief, AI recommendation, or historical observation is sufficient by itself to authorize live trading.

---

# 45. FINAL PRINCIPLE

The objective of the Knowledge Base is not to make the system believe that a particular trading method works.

Its objective is to make the system understand:

* what is known
* what is uncertain
* what must be tested
* what assumptions are being made
* what can invalidate a conclusion
* how market behavior interacts with execution and risk

The system must therefore maintain a strict separation between:

**knowledge → hypothesis → evidence → validation → deployment.**

This separation is mandatory for maintaining scientific integrity and operational reliability.
