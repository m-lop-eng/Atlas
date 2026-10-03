# 04 — BACKTEST & VALIDATION FRAMEWORK

**Project:** Modular Algorithmic Trading System
**Document:** Backtest and Validation Framework
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Last Updated:** 2026-09-18

---

# 1. Purpose

This document defines the technical and quantitative requirements for:

* Historical backtesting.
* Strategy validation.
* Out-of-sample testing.
* Walk-forward analysis.
* Monte Carlo analysis.
* Stress testing.
* Execution simulation.
* Robustness testing.
* Paper-trading validation.
* Forward validation.
* Strategy acceptance and rejection.

The objective is to determine whether a trading strategy demonstrates sufficient evidence of robustness to justify progression toward live deployment.

The framework must prevent the system from treating historical profitability as evidence of guaranteed future profitability.

---

# 2. Fundamental Principle

> **The purpose of validation is to attempt to disprove a strategy, not to prove that it will make money.**

A strategy should progressively survive increasingly difficult tests.

The validation process is:

```text
BACKTEST
    ↓
COST VALIDATION
    ↓
PARAMETER ROBUSTNESS
    ↓
OUT-OF-SAMPLE
    ↓
WALK-FORWARD
    ↓
MONTE CARLO
    ↓
STRESS TEST
    ↓
PAPER TRADING
    ↓
FORWARD VALIDATION
    ↓
LIVE APPROVAL
```

---

# 3. Separation of Backtesting and Validation

The system must distinguish between:

### Backtesting

> "What would have happened under a defined historical simulation?"

and:

### Validation

> "How much confidence should we have that the observed behavior is robust enough to justify the next stage?"

A profitable backtest does not automatically constitute validation.

---

# 4. Backtest Engine Architecture

The Backtest Engine should contain at least:

```text id="qk7j7n"
Historical Data
      ↓
Data Validation
      ↓
Market Simulation
      ↓
Strategy
      ↓
Signal
      ↓
Risk Engine
      ↓
Order Simulation
      ↓
Position Manager
      ↓
P&L Engine
      ↓
Metrics
      ↓
Report
```

The same strategy logic should, where practical, be usable in:

* Backtest.
* Paper trading.
* Live trading.

Only the execution/data layer should change.

---

# 5. Backtest Reproducibility

Every backtest must be reproducible.

A backtest record must identify:

```text id="l7q9z3"
Backtest ID
Strategy ID
Strategy version
Code version
Dataset ID
Dataset version
Instrument
Timeframe
Date range
Timezone
Initial capital
Parameters
Risk configuration
Commission
Spread
Slippage
Execution assumptions
Broker/account assumptions
Software version
Timestamp
```

---

# 6. Historical Data Requirements

Data must contain sufficient information for the strategy and execution model.

Depending on the strategy:

* OHLCV.
* Tick data.
* Bid/ask.
* Level 2, where justified.
* Contract specifications.
* Trading sessions.
* Corporate actions.
* Futures contract metadata.
* Roll information.
* Financing.
* Exchange calendars.

The minimum required data resolution must be determined by the strategy.

---

# 7. Data Integrity

Before a dataset can be used, the system must check:

```text id="7t8xg5"
Missing observations
Duplicate observations
Timestamp order
Invalid timestamps
Invalid OHLC
Price anomalies
Volume anomalies
Session gaps
Timezone consistency
Contract continuity
Corporate actions
```

Any known data issue must be recorded.

---

# 8. Timestamp Integrity

The backtest must strictly preserve temporal ordering.

For each simulated decision:

```text id="8t4l3y"
Available Information
        ↓
Signal
        ↓
Order
        ↓
Execution
```

No information generated after the decision timestamp may influence that decision.

---

# 9. Look-Ahead Prevention

The system must explicitly protect against:

* Future bars.
* Future indicator values.
* Future closing prices.
* Future volume.
* Future market regimes.
* Future corporate actions.
* Future contract information.
* Future data cleaning decisions that would not have been known at the time.

Where possible, tests should deliberately attempt to detect look-ahead bias.

---

# 10. Survivorship Bias

Where the strategy uses an instrument universe, the system must avoid using only securities that survived to the present when historical constituents are required.

For equities, this may require:

* Historical constituents.
* Delisted securities.
* Historical corporate actions.

The data methodology must explicitly state whether survivorship bias is present or controlled.

---

# 11. Futures Contract Handling

For futures strategies, the system must properly handle:

* Contract expiration.
* Contract rollover.
* Contract specifications.
* Tick size.
* Tick value.
* Liquidity.
* Volume.
* Open interest where relevant.
* Continuous-contract construction.

Continuous futures data must not create artificial tradability.

The backtester must distinguish:

```text
Historical contract
vs
Synthetic continuous series
```

---

# 12. Market Sessions

The backtester must understand market sessions.

Depending on the instrument:

```text id="9w4d2c"
Regular session
Extended session
Overnight
Pre-market
Post-market
Holiday
Early close
```

Session assumptions must be explicitly configured.

---

# 13. Corporate Actions

For equities, the system must correctly handle where applicable:

* Stock splits.
* Reverse splits.
* Dividends.
* Spin-offs.
* Mergers.
* Delistings.

The appropriate adjustment methodology depends on the strategy and must be documented.

---

# 14. Transaction Costs

Every realistic backtest must account for:

```text id="z0u5jf"
Commission
Exchange fees
Bid/ask spread
Slippage
Financing
Borrow costs where applicable
Market impact where relevant
```

The model must use the appropriate assumptions for the asset class.

---

# 15. Slippage Model

The system should support configurable slippage models.

Examples:

### Fixed

```text
X ticks
```

### Percentage

```text
X% of price
```

### Volatility-based

Slippage changes with volatility.

### Liquidity-based

Slippage depends on available liquidity.

### Randomized

A distribution of execution prices.

The selected model must be documented.

---

# 16. Adverse Execution

The backtester should support asymmetric slippage.

For example:

```text id="1c9d1p"
Expected entry slippage
Expected exit slippage
Gap slippage
High-volatility slippage
```

This prevents unrealistic assumptions that execution will always occur favorably.

---

# 17. Order Types

The simulator should support, where relevant:

* Market.
* Limit.
* Stop.
* Stop-limit.
* Bracket.
* OCO.
* Trailing stop.

Each order type must have realistic fill rules.

---

# 18. Limit Order Simulation

A limit order must not automatically be assumed to fill simply because historical price crossed the limit.

The simulator should consider:

* Price path.
* Market liquidity.
* Order queue where data permits.
* Volume.
* Time at price where available.

The degree of realism depends on available data.

---

# 19. Stop Order Simulation

Stop orders must account for:

* Trigger conditions.
* Price gaps.
* Slippage.
* Market liquidity.

A stop price is not necessarily the execution price.

---

# 20. Partial Fills

Where relevant, the simulator should support partial fills.

This is particularly important for:

* Less liquid instruments.
* Larger position sizes.
* Limit orders.
* Market-impact-sensitive strategies.

---

# 21. Latency

The framework should support execution latency.

Potential components:

```text id="2v6v88"
Signal generation latency
Network latency
Broker latency
Exchange latency
Order processing latency
```

Latency assumptions must be documented.

---

# 22. Market Impact

For sufficiently large orders, market impact should be modeled where data permits.

The backtester must not assume unlimited liquidity.

---

# 23. Position Accounting

The simulator must correctly track:

```text id="c4w5e6"
Position
Average entry
Quantity
Direction
Realized P&L
Unrealized P&L
Commission
Fees
Financing
Stop
Target
```

The accounting model must be deterministic.

---

# 24. P&L Calculation

The system must correctly calculate:

* Realized P&L.
* Unrealized P&L.
* Gross P&L.
* Net P&L.
* Commission.
* Fees.
* Financing.
* Slippage cost.

Currency conversion must be handled where required.

---

# 25. Backtest Metrics

Every meaningful backtest should report:

### Performance

```text id="x1s4o8"
Net Profit
Total Return
Annualized Return
CAGR where applicable
```

### Risk

```text id="q7s5c1"
Maximum Drawdown
Drawdown Duration
Volatility
VaR / CVaR where appropriate
```

### Risk-adjusted performance

```text id="2h7v4p"
Sharpe
Sortino
Calmar
```

### Trading statistics

```text id="7k5v1q"
Trade Count
Win Rate
Average Win
Average Loss
Payoff Ratio
Profit Factor
Expectancy
```

### Operational

```text id="4t8m0d"
Exposure
Turnover
Average Holding Time
Trade Frequency
Slippage
Commission
```

---

# 26. Equity Curve Analysis

The system must generate:

* Equity curve.
* Drawdown curve.
* Rolling returns.
* Rolling volatility.
* Monthly performance.
* Yearly performance.
* Trade distribution.

The shape and stability of performance must be analyzed, not only final return.

---

# 27. Drawdown Analysis

For every meaningful backtest calculate:

```text id="f7j9p2"
Maximum Drawdown
Average Drawdown
Median Drawdown
Drawdown Duration
Maximum Recovery Time
Number of Drawdowns
```

The system should identify whether the strategy's drawdowns are concentrated in specific periods.

---

# 28. Trade Distribution

Analyze:

* Distribution of returns per trade.
* Largest winners.
* Largest losers.
* Losing streaks.
* Winning streaks.
* Average holding time.
* Outlier dependence.

A strategy whose entire profitability comes from a very small number of exceptional trades requires additional scrutiny.

---

# 29. Outlier Dependence

The system should perform tests such as:

```text id="8w0m5p"
Remove top 1 trade
Remove top 5 trades
Remove top 10 trades
```

and evaluate how performance changes.

This does not mean profitable trades should be artificially removed from the official backtest.

The purpose is diagnostic.

---

# 30. Baseline Comparison

Every strategy should be compared with an appropriate baseline.

Examples:

```text id="4w6l2n"
Buy-and-hold
Random-entry
Simple trend
Simple breakout
Simple mean reversion
Market benchmark
```

The baseline should be selected according to the strategy's market and objective.

---

# 31. Parameter Sensitivity

Important parameters must be tested across a reasonable range.

Example:

```text id="v4q1d0"
Parameter = 20

Test:
10
15
20
25
30
35
40
```

The objective is to identify:

* Stable regions.
* Performance cliffs.
* Overfit parameters.
* Interaction effects.

---

# 32. Parameter Surface

Where computationally practical, generate parameter surfaces.

Example:

```text id="6m4r9v"
Parameter A
      +
Parameter B
      ↓
Performance Surface
```

The system should prefer broad stable plateaus over isolated peaks.

---

# 33. Optimization Rules

Optimization must not be performed against the final OOS dataset.

The optimization dataset must be explicitly defined.

Optimization results must record:

```text id="h5w2q9"
Objective function
Parameters searched
Search space
Number of trials
Optimization algorithm
Training period
Validation period
```

---

# 34. Objective Functions

Optimization must not automatically use maximum net profit.

Potential objective functions:

* Risk-adjusted return.
* Sharpe.
* Sortino.
* Calmar.
* Profit factor with drawdown constraint.
* Return/drawdown ratio.
* Multi-objective functions.

The objective must be selected based on the strategy and project goals.

---

# 35. Multiple Testing

The system must track the number of experiments.

This is necessary because the probability of finding apparently strong historical results increases with the number of tests.

The experiment database must therefore retain:

```text id="6x2z9a"
Number of experiments
Number of parameter trials
Number of strategies
Number of features
Number of datasets
```

---

# 36. Data-Snooping Control

The system must distinguish between:

### Exploratory data

Used to discover hypotheses.

### Development data

Used to construct the strategy.

### Validation data

Used for model/parameter validation.

### OOS data

Reserved for final evaluation.

Once OOS data influences development, it loses its OOS status.

---

# 37. Temporal Train/Test Split

Time series must be split chronologically.

Never randomly shuffle financial time-series observations for ordinary strategy validation unless the specific methodology explicitly justifies it.

Example:

```text id="8q7z1j"
2015 ───── 2021
TRAIN

2022 ───── 2023
VALIDATION

2024 ───── 2025
OOS
```

Exact periods depend on data availability and strategy characteristics.

---

# 38. Walk-Forward Validation

Walk-forward validation should simulate the process of repeatedly estimating or calibrating a strategy and then deploying it on unseen data.

Example:

```text id="j0r5k6"
TRAIN 2015–2018
TEST  2019

TRAIN 2016–2019
TEST  2020

TRAIN 2017–2020
TEST  2021

TRAIN 2018–2021
TEST  2022
```

The exact windows must be appropriate for the strategy.

---

# 39. Walk-Forward Metrics

Report:

```text id="6j2s1y"
Each test-period return
Each test-period drawdown
Aggregate OOS return
Aggregate OOS drawdown
Sharpe
Sortino
Profit factor
Trade count
Stability
```

The system must not only report the aggregate result.

---

# 40. Monte Carlo Validation

Monte Carlo simulations should estimate the range of plausible outcomes.

Minimum outputs where appropriate:

```text id="7y1v9b"
Median return
5th percentile
25th percentile
75th percentile
95th percentile
Median drawdown
95th percentile drawdown
Worst simulated drawdown
Maximum losing streak
Recovery time
```

---

# 41. Monte Carlo Trade Randomization

A standard analysis should randomize the order of historical trades while preserving the underlying trade distribution.

Purpose:

> Determine how much of the observed equity curve depends on the specific historical sequence of trades.

---

# 42. Monte Carlo Cost Stress

Simulations should optionally increase:

```text id="s8r6e0"
Spread
Slippage
Commission
Execution delay
```

The objective is to estimate sensitivity to execution deterioration.

---

# 43. Stress Testing

The strategy must be tested under adverse assumptions.

Examples:

```text id="7d9w2j"
1.5× normal spread
2× normal spread
1.5× normal slippage
2× normal slippage
Execution delay
Missed trades
Reduced liquidity
Higher volatility
Lower volatility
Large gaps
Regime change
```

The exact scenarios must be recorded.

---

# 44. Regime Analysis

Performance should be segmented by relevant market conditions.

Possible segmentation:

```text id="2x5j7b"
Trend
Range
High volatility
Low volatility
Bull
Bear
Crisis
Recovery
```

Where objective regime definitions exist, they should be used instead of subjective labels.

---

# 45. Stress Scenario Framework

The system should eventually support scenario definitions such as:

```yaml id="t2f6g1"
scenario:
  name: HIGH_SLIPPAGE
  spread_multiplier: 2.0
  slippage_multiplier: 2.0
  commission_multiplier: 1.0
```

Scenarios must be versioned and reproducible.

---

# 46. Robustness Score

The system may eventually calculate a composite robustness indicator.

However:

> A composite score must not replace the underlying evidence.

The report must always expose the individual components.

Potential dimensions:

```text id="3x1f7v"
OOS stability
Parameter stability
Cost sensitivity
Drawdown robustness
Monte Carlo robustness
Regime robustness
Execution robustness
```

No universal threshold should be hard-coded before sufficient research.

---

# 47. Strategy Acceptance Gates

A strategy must pass progressively stricter gates.

## Gate 1 — Technical Validity

```text
[ ] Code works
[ ] Data valid
[ ] No look-ahead
[ ] No leakage
[ ] Backtest reproducible
```

## Gate 2 — Economic/Statistical Evidence

```text
[ ] Hypothesis documented
[ ] Baseline tested
[ ] Costs included
[ ] Sufficient sample
[ ] Performance measurable
```

## Gate 3 — Robustness

```text
[ ] Parameter sensitivity
[ ] Cost sensitivity
[ ] Stress tests
[ ] Regime analysis
```

## Gate 4 — OOS

```text
[ ] OOS isolated
[ ] OOS tested
[ ] No OOS optimization
```

## Gate 5 — Walk-Forward

```text
[ ] Appropriate windows
[ ] Repeated unseen tests
[ ] Stable performance
```

## Gate 6 — Monte Carlo

```text
[ ] Drawdown distribution acceptable
[ ] Losing streak analyzed
[ ] Recovery analyzed
```

## Gate 7 — Paper

```text
[ ] Production-like architecture
[ ] Execution monitored
[ ] Slippage measured
[ ] Operational stability
```

---

# 48. No Universal Pass/Fail Threshold

The project must avoid defining arbitrary universal thresholds such as:

```text
Sharpe > 2 = GOOD
Profit Factor > 1.5 = PASS
Drawdown < 10% = PASS
```

These metrics must be interpreted according to:

* Strategy type.
* Market.
* Timeframe.
* Trade frequency.
* Costs.
* Execution.
* Portfolio role.
* Risk profile.

A strategy must be evaluated holistically.

---

# 49. Statistical Significance

Where appropriate, statistical tests may be used to assess whether observed relationships are distinguishable from noise.

However, statistical significance must not be treated as proof of tradability.

A statistically significant effect may still be:

* Too small.
* Too unstable.
* Too expensive to trade.
* Too difficult to execute.

---

# 50. Effect Size

The research process should distinguish between:

```text
Statistical significance
```

and:

```text
Economic significance
```

A tiny statistically detectable edge may have no practical trading value after costs.

---

# 51. Sample Size

Strategies must have sufficient observations for the conclusions being drawn.

Particular caution is required when:

* Trade count is low.
* The strategy trades only a few times per year.
* Most profits occur in a small number of trades.
* The available history is short.

Low sample size increases uncertainty.

---

# 52. Confidence Intervals

Where statistically appropriate, performance estimates should include uncertainty intervals.

Examples:

* Expected return.
* Win rate.
* Mean trade return.
* Sharpe estimates.
* Drawdown estimates.

The system should avoid presenting uncertain estimates as exact quantities.

---

# 53. Backtest Report

Every final backtest report should contain:

```text id="7s3r5m"
1. Executive Summary
2. Strategy Description
3. Hypothesis
4. Market
5. Dataset
6. Date Range
7. Parameters
8. Execution Model
9. Cost Model
10. Risk Model
11. Performance Metrics
12. Drawdown Analysis
13. Trade Analysis
14. Parameter Sensitivity
15. OOS Results
16. Walk-Forward Results
17. Monte Carlo
18. Stress Tests
19. Regime Analysis
20. Failure Modes
21. Limitations
22. Conclusion
23. Decision
```

---

# 54. Validation Decision

The strategy must receive one of the following statuses:

```text id="6g2q8s"
PASS
PASS_WITH_RESTRICTIONS
RESEARCH_REQUIRED
REJECT
```

The decision must include an explanation.

---

# 55. PASS

A strategy may receive PASS when the evidence supports progression to the next validation stage.

PASS does not mean:

> "The strategy will be profitable live."

It means:

> "The strategy has passed the current validation gate."

---

# 56. PASS_WITH_RESTRICTIONS

Examples:

```text id="2g4z6p"
Restricted instrument
Restricted session
Reduced risk
Restricted position size
Paper trading only
```

The restrictions must be documented.

---

# 57. RESEARCH_REQUIRED

Use when:

* Results are promising but incomplete.
* Sample size is insufficient.
* OOS is inconclusive.
* Execution assumptions require additional testing.
* Market-regime dependence needs further investigation.

---

# 58. REJECT

Use when there is sufficient evidence that the strategy does not justify further development.

Rejection should be documented.

A rejected strategy may be revisited only if a materially new hypothesis or evidence emerges.

---

# 59. Paper Trading Validation

Paper trading must measure:

```text id="0n5m7z"
Signal timing
Execution timing
Expected price
Actual simulated price
Slippage
Trade frequency
Latency
Order rejection
Position synchronization
Risk controls
P&L
Drawdown
```

---

# 60. Backtest-to-Paper Comparison

The system must compare:

```text id="f7h5s4"
Backtest trade frequency
vs
Paper trade frequency

Backtest slippage
vs
Paper slippage

Backtest average trade
vs
Paper average trade

Backtest drawdown
vs
Paper drawdown
```

Large deviations must trigger investigation.

---

# 61. Forward Validation

Forward validation must be performed using unseen future market data.

The strategy must not be modified continuously based on every new result.

Otherwise the forward period becomes another optimization dataset.

---

# 62. Live Transition

Before live deployment:

```text id="8c7w4f"
Backtest
        ✓
Robustness
        ✓
OOS
        ✓
Walk-Forward
        ✓
Monte Carlo
        ✓
Stress
        ✓
Paper
        ✓
Operational Validation
        ✓
Risk Approval
        ✓
Broker Approval
        ✓
```

Only then may the strategy be considered for live deployment.

---

# 63. Post-Live Validation

Validation does not end after deployment.

Live performance must continuously be compared against:

* Backtest.
* OOS.
* Walk-forward.
* Paper.
* Expected execution.

The strategy may be suspended when live behavior deviates materially from validated assumptions.

---

# 64. Strategy Degradation

Potential signs of degradation include:

```text id="8q7j2n"
Persistent negative expectancy
Major increase in drawdown
Execution deterioration
Slippage deterioration
Trade frequency collapse
Unexpected regime behavior
Correlation change
Signal distribution change
Market structure change
```

Degradation does not automatically prove that the strategy has permanently failed.

It triggers investigation.

---

# 65. Revalidation

A strategy must be revalidated after significant changes to:

* Entry logic.
* Exit logic.
* Features.
* Parameters.
* Risk model.
* Execution model.
* Instrument.
* Data source.
* Broker.
* Market session.

The required validation level depends on the magnitude of the change.

---

# 66. Regression Testing

Changes to the Backtest Engine must trigger regression testing.

Historical benchmark strategies should be rerun.

Unexpected changes in results must be investigated.

---

# 67. Backtest Integrity Tests

The Backtest Engine should eventually include automated tests for:

```text id="0r7j1m"
Look-ahead detection
Duplicate data
Timestamp ordering
Position accounting
Commission calculation
Slippage calculation
Order execution
Partial fills
Stop execution
Limit execution
Gap handling
Session handling
P&L calculation
Drawdown calculation
```

---

# 68. Reproducibility Test

Running the same backtest twice with:

```text id="j8v5z0"
Same code
Same dataset
Same parameters
Same configuration
Same environment
```

must produce identical results unless a documented stochastic component exists.

If stochastic components exist, their random seeds must be controlled for reproducibility.

---

# 69. Randomness

Where stochastic simulations are used, the system must record:

```text id="w5q4z2"
Random seed
Number of simulations
Distribution
Parameters
Version
```

---

# 70. Validation Database

The project should maintain a database of experiments.

Each experiment should be queryable by:

```text id="m9k2y5"
Strategy
Market
Instrument
Version
Dataset
Date
Parameters
Result
Validation Stage
Decision
```

This database becomes the historical research record of the project.

---

# 71. Research Audit Trail

The system must make it possible to reconstruct:

> How did we arrive at this strategy?

This should include:

```text id="g7v1s8"
Original hypothesis
Experiments
Rejected versions
Accepted versions
Parameter changes
OOS periods
Validation results
Final configuration
```

---

# 72. Validation Independence

Where practical, final validation should be performed using automated processes that minimize manual intervention.

This reduces the risk of selectively interpreting results.

---

# 73. No Manual Result Editing

Raw backtest results must be immutable.

Reports may contain interpretation, but the underlying data must remain unchanged.

Any transformation must be reproducible.

---

# 74. Performance Presentation

Reports must distinguish between:

```text id="6v3w1p"
Gross performance
Net performance
Backtest performance
OOS performance
Paper performance
Live performance
```

These values must never be mixed.

---

# 75. Strategy Validation Philosophy

A strategy is considered stronger when it demonstrates:

```text id="q5y0w4"
Simple rationale
+
Reproducible implementation
+
Realistic execution
+
Stable parameters
+
OOS evidence
+
Walk-forward evidence
+
Acceptable Monte Carlo distribution
+
Stress resilience
+
Paper/live consistency
```

No single metric is sufficient.

---

# 76. Golden Validation Rule

> **Every additional validation stage should make it harder for a fragile strategy to survive. A strategy that survives progressively realistic attempts to disprove it earns the right to be considered for the next stage.**

The system must never reverse this philosophy by designing validation criteria around the desire to approve a particular strategy.

---

# 77. Relationship With Other Documents

This document must be used together with:

```text id="4y8p2r"
00_MASTER_SPECIFICATION.md
01_GENERAL_RULES.md
02_QUANT_RESEARCH_METHODOLOGY.md
03_RISK_MANAGEMENT.md
06_EXECUTION_OPERATION.md
07_PROP_FIRM_RULES_FRAMEWORK.md
09_STRATEGY_LIFECYCLE.md
```

This document defines **how a strategy is simulated, tested, challenged, and validated**.

It does not define the strategy's alpha logic or the detailed operational procedures of the live trading infrastructure.
