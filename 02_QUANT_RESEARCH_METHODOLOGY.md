# 02 — QUANTITATIVE RESEARCH METHODOLOGY

**Project:** Modular Algorithmic Trading System
**Document:** Quantitative Research Methodology
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Last Updated:** 2026-09-18

---

# 1. Purpose

This document defines the methodology used to research, develop, test, validate, compare, and reject quantitative trading strategies.

Its purpose is to establish a disciplined research process that minimizes:

* Overfitting.
* Curve fitting.
* Data mining.
* Look-ahead bias.
* Data leakage.
* Selection bias.
* Survivorship bias.
* Multiple-testing problems.
* Unrealistic execution assumptions.
* Excessive model complexity.

The objective is not to maximize the number of profitable strategies discovered.

The objective is to identify strategies with a plausible and reproducible statistical edge that demonstrate sufficient robustness to justify further validation.

---

# 2. Research Philosophy

## 2.1 Hypothesis-driven research

Every strategy should originate from an explicit hypothesis whenever reasonably possible.

The minimum structure is:

```text
Hypothesis
    ↓
Market mechanism
    ↓
Expected edge
    ↓
Observable variables
    ↓
Signal
    ↓
Trade construction
    ↓
Risk management
    ↓
Validation
```

The research process must not begin with:

> "Which indicator combination produces the highest return?"

It should begin with:

> "Is there a measurable market behavior that could plausibly generate a repeatable edge?"

---

# 3. Research Hierarchy

Research should progress through the following hierarchy:

```text
LEVEL 0 — Market understanding
        ↓
LEVEL 1 — Hypothesis
        ↓
LEVEL 2 — Data analysis
        ↓
LEVEL 3 — Simple prototype
        ↓
LEVEL 4 — Baseline backtest
        ↓
LEVEL 5 — Robustness
        ↓
LEVEL 6 — Out-of-sample
        ↓
LEVEL 7 — Walk-forward
        ↓
LEVEL 8 — Monte Carlo
        ↓
LEVEL 9 — Stress testing
        ↓
LEVEL 10 — Paper trading
        ↓
LEVEL 11 — Live validation
```

No optimization should occur before a meaningful baseline exists.

---

# 4. Strategy Research Record

Every strategy must have a unique identifier.

Example:

```text
STRAT-NQ-BREAKOUT-001
```

The strategy record should contain:

```text
Strategy ID
Strategy Name
Version
Asset Class
Instrument
Market
Timeframe
Researcher
Creation Date
Current Status
Hypothesis
Expected Edge
Entry Rules
Exit Rules
Risk Model
Execution Assumptions
Known Limitations
Validation Status
```

---

# 5. Strategy Status

Every strategy must have a defined lifecycle state.

Allowed states:

```text
IDEA
HYPOTHESIS
RESEARCH
PROTOTYPE
BACKTEST
ROBUSTNESS
OOS
WALK_FORWARD
MONTE_CARLO
STRESS_TEST
PAPER
FORWARD_VALIDATION
APPROVED
LIVE
MONITORING
DEGRADED
SUSPENDED
RETIRED
REJECTED
```

A strategy must not be represented as "validated" simply because its backtest is profitable.

---

# 6. Hypothesis Definition

Before implementation, the researcher should document:

## 6.1 Market

What market is being studied?

Examples:

* NQ futures.
* ES futures.
* EUR/USD.
* Individual equities.
* Crypto.

---

## 6.2 Time horizon

Specify:

* Intraday.
* Swing.
* Position.
* High frequency, if applicable.

The initial project should prioritize systematic intraday and medium/low-frequency approaches rather than HFT.

---

## 6.3 Market phenomenon

What behavior is being exploited?

Examples:

* Momentum.
* Trend persistence.
* Mean reversion.
* Volatility expansion.
* Volatility contraction.
* Breakout behavior.
* Statistical relationship.
* Liquidity imbalance.
* Seasonality.
* Cross-sectional behavior.

---

## 6.4 Expected mechanism

The researcher must explain why the phenomenon could exist.

Potential explanations include:

* Behavioral biases.
* Market microstructure.
* Institutional flows.
* Risk premia.
* Liquidity dynamics.
* Information diffusion.
* Structural market constraints.

The explanation does not need to be proven conclusively, but it should be plausible and testable.

---

# 7. Strategy Construction

A strategy should be decomposed into independent components.

```text
Market Universe
        ↓
Data
        ↓
Features
        ↓
Regime Filter
        ↓
Signal
        ↓
Entry
        ↓
Position Sizing
        ↓
Stop / Risk
        ↓
Exit
        ↓
Execution
```

Each component should be independently testable.

---

# 8. Baseline Strategy

The first implementation must be deliberately simple.

The baseline should establish whether the underlying hypothesis has any measurable value before sophisticated optimization is introduced.

Example:

```text
Hypothesis:
Breakouts may exhibit short-term continuation in NQ.

Baseline:
20-bar breakout
+
fixed stop
+
fixed risk
+
simple exit
+
realistic transaction costs
```

Only after establishing the baseline should additional filters be considered.

---

# 9. Baseline Before Optimization

The research sequence must be:

```text
Simple Strategy
        ↓
Baseline Backtest
        ↓
Understand Failure Modes
        ↓
Controlled Improvements
        ↓
Robustness Testing
```

Not:

```text
Thousands of parameters
        ↓
Massive optimization
        ↓
Best equity curve
```

---

# 10. Feature Engineering

Features may include:

### Price

* Returns.
* Log returns.
* High/low ranges.
* Distance from moving averages.
* Breakout distances.
* Price acceleration.

### Volatility

* Realized volatility.
* ATR.
* Range expansion.
* Volatility percentile.

### Volume

Where reliable data exists:

* Volume.
* Relative volume.
* Volume change.
* Volume imbalance.

### Market structure

* Trend.
* Range.
* Breakout.
* Compression.
* Expansion.
* Support/resistance proxies.

### Time

* Session.
* Hour.
* Day of week.
* Market open.
* Market close.

### Cross-market

Potentially:

* Correlations.
* Relative strength.
* Intermarket relationships.
* Volatility indexes.
* Rates.
* Currency relationships.

Every feature must have a documented definition and timestamp availability.

---

# 11. Feature Selection Rules

Features must not be selected solely because they improve historical performance.

For every meaningful feature, evaluate:

```text
Economic/statistical rationale
Stability
Data quality
Availability
Sensitivity
Redundancy
Correlation
Potential leakage
```

Features with extremely unstable relationships require additional scrutiny.

---

# 12. Entry Logic

Entry rules must be explicit.

Examples:

```text
IF condition A
AND condition B
AND condition C
THEN generate LONG signal
```

The system must define:

* Signal timestamp.
* Order timestamp.
* Price used.
* Order type.
* Validity period.
* Cancellation conditions.

---

# 13. Exit Logic

Exit rules must be explicit and independently testable.

Possible exits:

* Stop loss.
* Take profit.
* Trailing stop.
* Time stop.
* Signal reversal.
* Volatility-based exit.
* Session close.
* Risk-based exit.

A strategy must not rely on ambiguous concepts such as:

> "Exit when momentum weakens."

The implementation must translate this into measurable rules.

---

# 14. Risk Model

Risk management must be separated from the strategy's alpha logic.

A strategy may generate:

```text
LONG
SHORT
NO TRADE
```

The Risk Engine determines:

```text
Position size
Maximum exposure
Allowed leverage
Risk per trade
Portfolio constraints
```

This separation allows the same strategy to be tested under different risk configurations.

---

# 15. Transaction Costs

Every meaningful backtest must include realistic:

```text
Commission
Spread
Slippage
Financing
Exchange fees
Market impact where relevant
```

The researcher must document the assumptions.

A strategy whose profitability disappears under modestly worse execution assumptions must be considered fragile.

---

# 16. First Backtest

The first backtest should answer:

> "Does the basic hypothesis appear to contain any measurable signal after realistic costs?"

It should not attempt to answer:

> "What is the maximum possible return?"

Required outputs include:

```text
Total return
Annualized return
Maximum drawdown
Sharpe
Sortino
Profit factor
Expectancy
Trade count
Win rate
Average win
Average loss
Exposure
Turnover
Drawdown duration
Equity curve
Monthly returns
```

---

# 17. Parameterization

Parameters should be classified.

### Category A — Structural

Examples:

* Market.
* Timeframe.
* Session.

### Category B — Strategy

Examples:

* Lookback.
* Breakout period.
* Volatility threshold.

### Category C — Risk

Examples:

* Risk per trade.
* Stop distance.
* Maximum exposure.

### Category D — Execution

Examples:

* Slippage.
* Order timeout.
* Execution delay.

This classification must be retained throughout research.

---

# 18. Parameter Optimization

Optimization is permitted only after a valid baseline exists.

Optimization should focus on identifying **stable regions**, not a single optimal combination.

Example:

```text
Parameter:
Breakout lookback

Results:

20 → positive
25 → positive
30 → positive
35 → positive
40 → positive
45 → positive
50 → positive
```

This provides more confidence than:

```text
37 → exceptionally profitable
36 → poor
38 → poor
```

---

# 19. Sensitivity Analysis

Every optimized strategy should undergo parameter sensitivity analysis.

For important parameters, evaluate:

```text
Lower value
Baseline value
Higher value
```

and preferably a broader grid around the selected region.

The objective is to identify:

* Stable regions.
* Sharp cliffs.
* Multiple local maxima.
* Parameter interactions.
* Fragile configurations.

---

# 20. Complexity Penalty

Increasing strategy complexity increases the probability of overfitting.

Complexity may result from:

* More indicators.
* More parameters.
* More filters.
* More conditional branches.
* More instruments.
* More timeframes.
* More model features.
* More optimization trials.

Complexity must therefore require justification.

---

# 21. Multiple Testing

The research system must track the number of:

* Strategies tested.
* Parameter combinations.
* Features tested.
* Instruments tested.
* Timeframes tested.
* Model variants tested.

This information should be stored with experiments.

A strategy discovered after thousands of unrecorded experiments must not be treated as if it were the first hypothesis tested.

---

# 22. Experiment Tracking

Every relevant experiment should have:

```text
Experiment ID
Date
Researcher/Agent
Strategy ID
Strategy version
Dataset
Instrument
Timeframe
Parameters
Costs
Execution assumptions
Train period
Test period
Results
Observations
Decision
```

Possible decisions:

```text
ACCEPT
REJECT
MODIFY
RESEARCH_MORE
```

---

# 23. Out-of-Sample Methodology

Data must be divided temporally.

Example:

```text
Historical Data
│
├── Training
│
├── Validation
│
└── Out-of-Sample
```

The exact proportions should depend on:

* Strategy type.
* Data frequency.
* Number of observations.
* Market regime coverage.

No universal percentage should be assumed.

---

# 24. Out-of-Sample Rules

OOS data must remain isolated.

If the researcher changes:

* Parameters.
* Features.
* Entry rules.
* Exit rules.
* Risk model.

after observing OOS results, the affected OOS period becomes part of the development process.

A new OOS period should then be established.

---

# 25. Walk-Forward Analysis

For strategies requiring parameter estimation, walk-forward testing should be considered.

Generic structure:

```text
TRAIN  → TEST
         ↓
TRAIN  → TEST
         ↓
TRAIN  → TEST
         ↓
TRAIN  → TEST
```

Each test period must remain unseen during its corresponding training period.

The objective is to simulate repeated real-world strategy deployment.

---

# 26. Monte Carlo Analysis

Monte Carlo testing should evaluate whether the observed historical performance is consistent with a range of plausible future outcomes.

Potential analyses:

### Trade-order randomization

Reorder historical trades.

### Return perturbation

Modify returns within statistically reasonable ranges.

### Cost perturbation

Increase transaction costs.

### Slippage perturbation

Increase execution slippage.

### Parameter perturbation

Test nearby parameter configurations.

---

# 27. Monte Carlo Outputs

Where applicable, calculate:

```text
Median return
5th percentile return
95th percentile return
Median drawdown
95th percentile drawdown
Worst simulated drawdown
Probability of exceeding drawdown threshold
Probability of loss
Probability of reaching target
Expected recovery duration
Risk of ruin
```

The system should emphasize distributions rather than a single simulated result.

---

# 28. Stress Testing

Stress tests should include adverse scenarios.

Examples:

```text
2× normal slippage
2× normal spread
Higher commissions
Reduced liquidity
Delayed execution
Missed trades
Volatility shock
Volatility collapse
Trend regime change
Range-bound regime
High-gap environment
```

The exact stress assumptions should be documented.

---

# 29. Market Regimes

Strategies should be evaluated across different market regimes where sufficient data exists.

Potential regimes:

```text
High volatility
Low volatility
Trending
Sideways
Bull
Bear
Crisis
Normal
```

The purpose is not necessarily to make a strategy profitable in every regime.

The purpose is to understand:

* Where it works.
* Where it fails.
* How severe failures are.
* Whether failures are predictable.
* Whether risk controls are adequate.

---

# 30. Strategy Families

The initial research program should investigate several strategy families.

### 30.1 Trend Following

Potential mechanisms:

* Persistence.
* Breakouts.
* Directional continuation.

### 30.2 Momentum

Potential mechanisms:

* Short-term or medium-term continuation.
* Relative strength.
* Cross-sectional momentum.

### 30.3 Breakout

Potential mechanisms:

* Range expansion.
* Volatility expansion.
* Liquidity transitions.

### 30.4 Mean Reversion

Potential mechanisms:

* Temporary price dislocations.
* Overextension.
* Range-bound behavior.

### 30.5 Statistical Arbitrage

Potential mechanisms:

* Stable statistical relationships.
* Relative-value deviations.

### 30.6 Volatility Strategies

Potential mechanisms:

* Volatility clustering.
* Regime transitions.
* Volatility mean reversion.

### 30.7 Machine Learning

Machine learning is optional.

It should only be introduced when:

1. A simpler baseline exists.
2. The problem is clearly defined.
3. There is sufficient data.
4. Leakage controls exist.
5. The model can be validated appropriately.
6. Complexity is justified.

Machine learning must not be used merely because it is technologically attractive.

---

# 31. Strategy Comparison

Strategies must not be ranked solely by:

```text
Net Profit
```

Comparison should consider:

```text
Return
Risk
Drawdown
Stability
Trade count
Execution sensitivity
Parameter sensitivity
OOS performance
Walk-forward performance
Monte Carlo distribution
Stress-test performance
Operational complexity
Correlation with existing strategies
```

The appropriate strategy may depend on the portfolio context.

---

# 32. Strategy Rejection Criteria

A strategy should normally be rejected when:

* Performance disappears after realistic costs.
* Performance exists only in-sample.
* Parameters are extremely fragile.
* Minor slippage destroys profitability.
* Performance depends on very few trades.
* Performance depends on one short historical period.
* Results cannot be reproduced.
* There is evidence of look-ahead bias.
* There is evidence of data leakage.
* The strategy requires unrealistic execution.
* The strategy's risk cannot be controlled.
* The strategy has no sufficiently plausible rationale.
* The complexity required is disproportionate to the observed edge.

Rejection is a valid research outcome.

---

# 33. Strategy Improvement

When a strategy fails, researchers should diagnose the failure before modifying it.

Potential causes:

```text
No edge
Wrong market
Wrong timeframe
Wrong entry
Wrong exit
Poor risk model
Excessive transaction costs
Execution assumptions
Regime dependency
Data quality
Overfitting
Insufficient sample
```

The researcher must identify the suspected cause before introducing modifications.

---

# 34. Avoiding Strategy Frankenstein

Researchers must avoid repeatedly adding filters to a weak strategy.

Example:

```text
Base strategy
+
RSI
+
MACD
+
ATR
+
Volume filter
+
Trend filter
+
Time filter
+
News filter
+
ML filter
```

If every modification exists solely to improve historical performance, the strategy is likely becoming increasingly exposed to overfitting.

---

# 35. Ablation Testing

When a strategy contains multiple components, each important component should be tested independently where practical.

Example:

```text
Full strategy
Remove filter A
Remove filter B
Remove filter C
Remove risk modification
```

This determines which components actually contribute value.

---

# 36. Benchmarking

Every strategy should be compared against appropriate baselines.

Possible benchmarks:

* Buy and hold where applicable.
* Random-entry strategy with equivalent risk.
* Simple moving-average strategy.
* Simple breakout.
* Simple mean reversion.
* Market return.
* Volatility-adjusted benchmark.

The purpose is to determine whether the complexity of the strategy is justified.

---

# 37. Paper Trading

Paper trading should reproduce the production architecture as closely as possible.

It should use:

* Real-time or near-real-time market data.
* Production-like execution logic.
* Production-like risk controls.
* Production-like logging.
* Production-like monitoring.

The objective is to identify differences between theoretical and operational behavior.

---

# 38. Forward Validation

During forward validation compare:

```text
Expected signal
Actual signal

Expected entry
Actual entry

Expected exit
Actual exit

Expected slippage
Actual slippage

Expected trade frequency
Actual trade frequency

Backtest P&L
Paper P&L
Live P&L
```

Significant deviations require investigation.

---

# 39. Research Stopping Rules

Research must have explicit stopping conditions.

Stop modifying a strategy when:

* The hypothesis is disproven.
* Additional complexity provides negligible benefit.
* Robustness cannot be demonstrated.
* Data quality is insufficient.
* The strategy repeatedly fails OOS.
* The edge disappears after realistic costs.
* The strategy becomes excessively complex.

Not every strategy needs to be rescued.

---

# 40. Research Integrity

The following principles are mandatory:

```text
Record negative results.
Record failed experiments.
Do not hide inconvenient periods.
Do not modify assumptions after seeing results without recording the change.
Do not remove losing trades without objective justification.
Do not change OOS classification retroactively.
Do not present selected results as representative if they were selected from a larger experiment set.
```

---

# 41. Research Deliverable

A strategy is considered sufficiently documented for validation when the research package contains:

```text
Strategy ID
Hypothesis
Market mechanism
Data source
Dataset version
Instrument
Timeframe
Entry logic
Exit logic
Risk model
Execution assumptions
Cost assumptions
Baseline results
Optimization methodology
Sensitivity analysis
OOS results
Walk-forward results
Monte Carlo results
Stress-test results
Known failure modes
Known limitations
Decision
```

---

# 42. Minimum Acceptance Gate

Before a strategy can progress toward paper trading, the research team must be able to answer:

### 1. What is the hypothesis?

### 2. Why might the edge exist?

### 3. What data supports it?

### 4. Does it survive realistic costs?

### 5. Does it survive parameter perturbation?

### 6. Does it survive OOS testing?

### 7. Does it survive walk-forward testing where appropriate?

### 8. What does Monte Carlo imply about its risk distribution?

### 9. Under which market regimes does it fail?

### 10. What happens under adverse execution?

### 11. How many experiments led to this strategy?

### 12. Can the entire result be reproduced?

If these questions cannot be answered, the strategy is not ready for the next validation stage.

---

# 43. Golden Research Rule

> **A strategy is not valuable because it has found the best historical parameters. A strategy is valuable when its underlying hypothesis survives increasingly realistic attempts to disprove it.**

The research process should therefore be designed primarily as a process of **falsification and robustness testing**, not optimization.

---

# 44. Relationship With Other Documents

This document must be used together with:

```text
00_MASTER_SPECIFICATION.md
01_GENERAL_RULES.md
03_RISK_MANAGEMENT.md
04_BACKTEST_VALIDATION.md
09_STRATEGY_LIFECYCLE.md
```

This document defines **how research is performed**.

It does not replace the dedicated Risk Management, Backtest/Validation, or Strategy Lifecycle documents.
