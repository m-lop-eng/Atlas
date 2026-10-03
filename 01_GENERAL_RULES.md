# 01 — GENERAL RULES

**Project:** Modular Algorithmic Trading System
**Document:** General Rules
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Last Updated:** 2026-09-18

---

## 1. Purpose

This document defines the mandatory general rules governing the development, research, validation, deployment, operation, and maintenance of the algorithmic trading system.

These rules apply to:

* Quantitative research.
* Strategy development.
* Backtesting.
* Optimization.
* Validation.
* Risk management.
* Portfolio construction.
* Broker integration.
* Order execution.
* Paper trading.
* Live trading.
* Prop-firm evaluation.
* Monitoring.
* Infrastructure.
* Software development.
* AI-assisted development.

The purpose of these rules is to maximize:

1. Reproducibility.
2. Robustness.
3. Risk control.
4. Maintainability.
5. Traceability.
6. Operational safety.
7. Scalability.

The objective is **not** to maximize historical backtest profitability.

---

# 2. Core Principles

## RULE-001 — Capital preservation has priority

The system must prioritize preservation of trading capital over short-term profitability.

No strategy, optimization, parameter, or execution improvement may justify an unacceptable increase in risk.

---

## RULE-002 — Robustness over optimization

A strategy with slightly lower historical performance but significantly greater robustness is preferable to a highly optimized strategy with fragile historical performance.

The system must actively search for evidence of robustness.

---

## RULE-003 — No strategy is assumed to work

No strategy family is considered profitable by default.

Trend following, momentum, mean reversion, breakout, statistical arbitrage, machine learning, or any other approach must be treated as a hypothesis until supported by appropriate evidence.

---

## RULE-004 — Backtest performance is not proof

A positive backtest does not demonstrate that a strategy will be profitable in live markets.

Every strategy must pass progressively more demanding validation stages before live deployment.

---

## RULE-005 — No look-ahead bias

No strategy, feature, indicator, model, filter, or execution rule may use information that was not available at the exact moment at which the decision would have been made historically.

This includes:

* Future prices.
* Future indicators.
* Future volume.
* Future market states.
* Future corporate information.
* Future contract information.
* Future data corrections.
* Information created after the simulated decision timestamp.

---

## RULE-006 — No data leakage

Information from validation, test, or future periods must never influence model development, parameter selection, feature selection, strategy selection, or optimization.

---

## RULE-007 — OOS data is sacred

Out-of-sample data must not be used for optimization.

If OOS data is used to modify the strategy, that dataset is no longer considered truly out-of-sample and must be reclassified.

---

## RULE-008 — Every experiment must be reproducible

Any backtest or research result considered relevant must be reproducible from:

* Code version.
* Strategy version.
* Parameter set.
* Dataset version.
* Data source.
* Instrument.
* Timeframe.
* Trading session.
* Transaction-cost assumptions.
* Slippage assumptions.
* Execution assumptions.
* Initial capital.
* Date range.
* Software/environment version.

---

# 3. Research Rules

## RULE-009 — Hypothesis before implementation

A strategy should begin with a documented hypothesis.

At minimum:

```text
Hypothesis
Market mechanism
Expected edge
Market/instrument
Timeframe
Entry logic
Exit logic
Risk model
Expected failure conditions
Transaction costs
Validation methodology
```

Coding should follow the hypothesis, not replace it.

---

## RULE-010 — No indicator shopping

The system must not blindly test thousands of indicators and parameter combinations simply to find a profitable historical configuration.

Large-scale automated searches must have a clearly documented research objective and appropriate controls against multiple-testing and overfitting.

---

## RULE-011 — No cherry-picking

Negative experiments must not be hidden merely because they are unsuccessful.

Relevant experiments should remain traceable.

Failed research is useful information.

---

## RULE-012 — Parameter regions are preferred

The system should prefer stable parameter regions over isolated optimal parameters.

Example:

Bad:

```text
Lookback = 37
```

Potentially better:

```text
Lookback = 30–50
```

if performance remains reasonably stable throughout the region.

---

## RULE-013 — Simplicity is preferred

When two models provide comparable robustness and performance, the simpler model should be preferred.

Complexity must have a demonstrable purpose.

---

## RULE-014 — Economic/statistical rationale

Whenever reasonably possible, a strategy should have an identifiable market, statistical, behavioral, or structural rationale.

A strategy must not be accepted solely because:

> "The backtest makes money."

---

# 4. Data Rules

## RULE-015 — Data provenance

Every dataset must have identifiable metadata.

At minimum:

```text
Source
Instrument
Asset class
Timeframe
Timezone
Start date
End date
Acquisition date
Data version
Data format
Adjustments
Known limitations
```

---

## RULE-016 — Data quality validation

Before being used for research, data must be checked for:

* Missing values.
* Duplicates.
* Invalid timestamps.
* Incorrect ordering.
* Impossible prices.
* Incorrect OHLC relationships.
* Abnormal gaps.
* Session inconsistencies.
* Contract-roll issues.
* Corporate-action issues where applicable.
* Timezone inconsistencies.

---

## RULE-017 — Internal time standard

The internal system should use UTC as its canonical timestamp standard.

Conversion to exchange-local time or user-local time must occur only at the appropriate presentation or session-management layer.

---

## RULE-018 — Historical data must represent tradable conditions

Whenever possible, historical datasets must reflect the actual market conditions that would have been available to the strategy.

This includes, where relevant:

* Bid/ask spread.
* Commissions.
* Slippage.
* Liquidity.
* Trading hours.
* Contract specifications.
* Futures rollovers.
* Financing.
* Corporate actions.

---

# 5. Backtesting Rules

## RULE-019 — Realistic transaction costs

Backtests must incorporate realistic assumptions for:

* Commission.
* Spread.
* Slippage.
* Market impact where relevant.
* Financing.
* Exchange fees where applicable.

---

## RULE-020 — Execution must be simulated realistically

The backtesting engine must not assume that every order is filled at the theoretical signal price.

Execution assumptions must reflect the order type and market.

---

## RULE-021 — No impossible fills

The simulator must not allow executions that could not realistically have occurred given:

* OHLC data.
* Market liquidity.
* Order type.
* Session.
* Spread.
* Price movement.

---

## RULE-022 — Backtest engine must be independent from strategy logic

The strategy should generate signals and decisions.

The execution simulator should determine how those decisions would have been executed.

This separation must be maintained architecturally.

---

# 6. Validation Rules

## RULE-023 — Validation is sequential

A strategy must progress through predefined validation stages.

Minimum lifecycle:

```text
Research
    ↓
Prototype
    ↓
Backtest
    ↓
Robustness
    ↓
OOS
    ↓
Walk-Forward
    ↓
Monte Carlo
    ↓
Stress Testing
    ↓
Paper Trading
    ↓
Forward Validation
    ↓
Live Approval
```

A strategy should not skip stages without documented justification.

---

## RULE-024 — Multiple dimensions of performance

Strategy evaluation must not rely exclusively on net profit.

Relevant metrics include:

* CAGR / annualized return.
* Volatility.
* Sharpe ratio.
* Sortino ratio.
* Calmar ratio.
* Maximum drawdown.
* Drawdown duration.
* Profit factor.
* Expectancy.
* Win rate.
* Average win.
* Average loss.
* Payoff ratio.
* Number of trades.
* Exposure.
* Turnover.
* MAE.
* MFE.
* Monthly performance.
* Performance stability.

---

## RULE-025 — Stress testing is mandatory

Strategies must be tested under adverse assumptions.

Examples:

```text
Higher slippage
Higher commissions
Wider spreads
Lower liquidity
Missed trades
Execution delays
Higher volatility
Lower volatility
Different market regimes
```

---

## RULE-026 — Monte Carlo analysis

Where statistically appropriate, Monte Carlo analysis should be used to estimate the distribution of possible outcomes rather than relying on a single historical equity curve.

The analysis may include:

* Trade-order randomization.
* Return randomization.
* Slippage variation.
* Cost variation.
* Parameter variation.
* Execution variation.

---

# 7. Risk Rules

## RULE-027 — Risk must be explicit

Every live strategy must have explicitly defined:

```text
Risk per trade
Maximum position size
Maximum daily loss
Maximum strategy loss
Maximum portfolio loss
Maximum drawdown
Maximum exposure
Maximum leverage
Maximum correlated exposure
```

---

## RULE-028 — Risk parameters are independent from strategy logic

Trading logic must not directly hard-code account-specific risk limits.

Risk should be managed by the dedicated Risk Engine.

---

## RULE-029 — Initial risk must be conservative

The project will initially use a configurable default risk-per-trade of:

```text
0.25% of relevant account equity
```

This is a development baseline, not a universal optimal risk level.

Risk may only be increased following appropriate validation and documented approval.

---

## RULE-030 — Correlation matters

Risk must not be assessed only at individual-trade level.

The system must consider aggregate exposure.

Example:

```text
Long NQ
+
Long ES
+
Long technology equities
+
Long BTC
```

may represent significantly more common directional exposure than the individual positions suggest.

---

## RULE-031 — Kill switches are mandatory

The system must support kill switches at multiple levels:

```text
Strategy
Instrument
Market
Account
Global
```

A kill switch must be capable of preventing new orders.

Depending on configuration, it may also:

* Cancel pending orders.
* Close positions.
* Notify the operator.
* Record the event.
* Disable the affected component.

---

# 8. Software Architecture Rules

## RULE-032 — Strategies must not access brokers directly

Strategies must never contain direct broker/API calls.

Required architecture:

```text
Strategy
   ↓
Risk Engine
   ↓
Order Manager
   ↓
Broker Adapter
   ↓
Broker
```

---

## RULE-033 — Broker abstraction

The system must use broker/exchange adapters.

Example:

```text
MT5Adapter
FuturesAdapter
IBKRAdapter
CryptoAdapter
```

The strategy should remain independent of the underlying broker.

---

## RULE-034 — Separation of environments

At minimum:

```text
DEV
PAPER
LIVE
```

must be logically separated.

Development code must never accidentally submit live orders.

---

## RULE-035 — Secrets must never be committed

API keys, passwords, tokens, account credentials, and other secrets must never be stored directly in source code or Git repositories.

Secrets must use appropriate environment variables or secret-management mechanisms.

---

# 9. Execution Rules

## RULE-036 — Fail safe

If the system cannot reliably determine:

* Market data.
* Broker connection.
* Account state.
* Current positions.
* Open orders.
* Risk status.

the system must default to:

```text
DO NOT OPEN NEW POSITIONS
```

until the state is safely recovered.

---

## RULE-037 — Duplicate orders must be prevented

The Order Management System must contain mechanisms to prevent duplicate order submission.

---

## RULE-038 — State recovery

After a restart, the system must reconcile:

```text
Internal state
      ↕
Broker state
```

before resuming trading.

---

## RULE-039 — Every order must be traceable

The system must be able to identify:

```text
Strategy
Strategy version
Signal
Timestamp
Instrument
Order
Quantity
Entry
Stop
Target
Execution
Slippage
Commission
Position
P&L
Risk state
```

---

# 10. AI Development Rules

AI may be used to accelerate:

* Coding.
* Research.
* Documentation.
* Testing.
* Data analysis.
* Debugging.
* Refactoring.

However:

## RULE-040 — AI output is not automatically trusted

AI-generated code, strategies, parameters, financial conclusions, and architectural changes must be reviewed and validated.

---

## RULE-041 — AI must not silently modify trading logic

An AI agent must clearly identify:

```text
WHAT changed
WHY it changed
WHICH files changed
WHICH assumptions changed
WHICH tests were executed
WHICH results changed
```

---

## RULE-042 — AI must not optimize for backtest appearance

The AI must not select a strategy or parameter set simply because it produces a better historical equity curve.

---

## RULE-043 — AI must disclose uncertainty

When evidence is insufficient, the agent must explicitly state:

```text
INSUFFICIENT EVIDENCE
```

rather than presenting an assumption as fact.

---

# 11. Live Trading Rules

## RULE-044 — No direct transition from backtest to live

A strategy must pass paper/forward validation before live deployment unless a documented exception is approved.

---

## RULE-045 — Start small

Initial live exposure must be conservative.

Scaling should occur progressively based on:

* Live performance.
* Execution quality.
* Drawdown.
* Stability.
* Operational reliability.

---

## RULE-046 — Live performance must be compared with expectations

The system should compare:

```text
Backtest
vs
OOS
vs
Walk-Forward
vs
Paper
vs
Live
```

Important deviations must trigger investigation.

---

# 12. Prop-Firm Rules

## RULE-047 — Prop-firm rules are external constraints

Prop-firm rules must never be embedded directly into strategy logic.

They belong to the Prop Firm Rule Engine.

---

## RULE-048 — Rules must be verified

Before trading a specific prop-firm account, current rules must be verified against authoritative provider documentation.

Rules must not be assumed from:

* Old documentation.
* Forum posts.
* Social media.
* Memory.
* Third-party summaries.

---

## RULE-049 — Challenge optimization is prohibited

The system must not modify a strategy solely to maximize the probability of passing a prop-firm challenge if doing so compromises the underlying strategy's robustness.

The economic/statistical strategy must remain independently justified.

---

# 13. Documentation Rules

## RULE-050 — Everything important is documented

Important decisions must be documented.

Examples:

```text
Strategy changes
Risk changes
Architecture changes
Data changes
Broker changes
Parameter changes
Validation results
Production incidents
Strategy retirement
```

---

## RULE-051 — Version everything relevant

At minimum:

```text
Code
Strategy
Parameters
Data
Configuration
Documentation
```

must be versioned or otherwise traceable.

---

## RULE-052 — Documentation must reflect reality

Documentation must be updated when the implementation changes.

Outdated documentation must not be treated as authoritative.

---

# 14. Change Management

Changes are classified as:

### LEVEL 1 — Minor

Examples:

* Documentation.
* Logging improvements.
* Non-critical refactoring.

May normally be implemented directly.

### LEVEL 2 — Functional

Examples:

* New indicator.
* New execution rule.
* New risk parameter.
* New data transformation.

Requires tests and documented impact.

### LEVEL 3 — Architectural / Critical

Examples:

* Risk Engine modification.
* Order Manager modification.
* Broker Adapter modification.
* Backtest engine modification.
* Portfolio risk modification.
* Live execution modification.

Requires:

1. Technical explanation.
2. Impact analysis.
3. Tests.
4. Regression validation.
5. Explicit approval before production deployment.

---

# 15. Prohibited Practices

The following practices are prohibited:

* Look-ahead bias.
* Data leakage.
* Cherry-picking.
* Survivorship bias where avoidable.
* Curve fitting without robustness analysis.
* Undocumented parameter optimization.
* Hidden risk increases.
* Hard-coded live credentials.
* Direct strategy-to-broker integration.
* Uncontrolled live deployment.
* Trading without defined risk limits.
* Ignoring execution costs.
* Ignoring failed experiments.
* Modifying OOS results to improve presentation.
* Claiming profitability based solely on backtest results.
* Deploying code that has not passed the required tests.

---

# 16. Decision Hierarchy

When two objectives conflict, use the following priority:

```text
1. Safety
2. Capital preservation
3. Data integrity
4. Risk control
5. Robustness
6. Reproducibility
7. Operational reliability
8. Maintainability
9. Scalability
10. Performance optimization
```

Historical profitability must never override a higher-priority requirement.

---

# 17. Golden Rule

The project follows this principle:

> **We are not building a machine that finds the most profitable backtest. We are building a controlled quantitative research and execution system capable of identifying, validating, deploying, monitoring, and retiring trading strategies under uncertainty.**

Every technical and quantitative decision should be evaluated against this objective.

---

# 18. Compliance With This Document

Any strategy, module, algorithm, integration, or operational procedure that conflicts with these rules must be considered **non-compliant** until the conflict is explicitly reviewed and resolved.

The latest approved version of this document takes precedence over informal assumptions or undocumented practices.
