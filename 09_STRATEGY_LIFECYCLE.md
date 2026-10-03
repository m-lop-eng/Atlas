# 09 — STRATEGY LIFECYCLE

**Version:** 1.0.0
**Status:** Approved for development baseline
**Document Type:** Strategy Governance & Lifecycle Standard
**Applies To:** Research, Backtesting, Validation, Paper Trading, Live Trading, Portfolio Management, AI-Assisted Development

---

# 1. Purpose

This document defines the complete lifecycle of an algorithmic trading strategy, from the initial hypothesis through research, validation, paper trading, controlled production deployment, monitoring, degradation management, suspension, and retirement.

The objective is to prevent unvalidated strategies, undocumented changes, overfitted models, and uncontrolled strategy modifications from reaching production.

A strategy is treated as a controlled software and quantitative research asset.

No strategy is considered production-ready simply because it produces attractive backtest results.

---

# 2. Core Principle

The strategy lifecycle is a controlled sequence:

```text
IDEA
  ↓
HYPOTHESIS
  ↓
RESEARCH SPECIFICATION
  ↓
PROTOTYPE
  ↓
BASELINE
  ↓
BACKTEST
  ↓
ROBUSTNESS
  ↓
OUT-OF-SAMPLE
  ↓
WALK-FORWARD
  ↓
MONTE CARLO / STRESS
  ↓
PAPER TRADING
  ↓
LIVE PILOT
  ↓
CONTROLLED SCALING
  ↓
PRODUCTION
  ↓
MONITORING
  ↓
REVALIDATION / SUSPENSION / RETIREMENT
```

A strategy must earn progression through each stage.

The default behavior is **not to promote** a strategy until the required evidence exists.

---

# 3. Strategy Identity

Every strategy must have a unique identity.

Minimum identity:

```text
strategy_id
strategy_name
strategy_family
market
instrument
timeframe
version
parameter_set_version
data_version
code_version
status
creation_date
owner
```

Example:

```text
strategy_id:
NQ_BREAKOUT_001

strategy_name:
NQ Session Breakout

strategy_family:
Breakout

market:
US Futures

instrument:
NQ

timeframe:
5m

version:
1.2.0

parameter_set_version:
P03

status:
PAPER
```

The strategy ID must remain stable throughout its lifecycle.

Changes that materially alter the strategy logic should create a new strategy version or, when sufficiently different, a new strategy ID.

---

# 4. Strategy Versioning

The system must distinguish between different types of changes.

## 4.1 Code Version

Defines the implementation of the strategy.

Example:

```text
code_version = git commit / release
```

---

## 4.2 Strategy Version

Defines the actual trading logic.

Examples of material changes:

* changing entry logic
* changing exit logic
* adding or removing a feature
* changing market regime filters
* changing position management
* changing signal generation
* changing execution assumptions

---

## 4.3 Parameter Set Version

Defines numerical parameters.

Example:

```text
Strategy Version: 1.0
Parameter Set: P01
Lookback: 20
ATR period: 14
Breakout threshold: 1.5 ATR
```

A new parameter set must not overwrite historical experiments.

---

## 4.4 Data Version

The exact data used for research must be identifiable.

Minimum information:

```text
data_source
instrument
date_range
timeframe
timezone
cleaning_version
corporate_action_version
roll_method
data_snapshot
```

A strategy result without identifiable data provenance is not reproducible.

---

# 5. Lifecycle States

The strategy registry should support states such as:

```text
IDEA
RESEARCH
PROTOTYPE
BASELINE
BACKTEST
ROBUSTNESS
OOS
WALK_FORWARD
STRESS_TEST
PAPER
LIVE_PILOT
PRODUCTION
MONITORING
DEGRADED
SUSPENDED
REVALIDATION
RETIRED
REJECTED
ARCHIVED
```

State transitions must be explicit and logged.

Example:

```text
RESEARCH
   ↓
PROTOTYPE
   ↓
BASELINE
   ↓
BACKTEST
   ↓
ROBUSTNESS
   ↓
OOS
   ↓
WALK_FORWARD
   ↓
STRESS_TEST
   ↓
PAPER
   ↓
LIVE_PILOT
   ↓
PRODUCTION
```

A failed validation can result in:

```text
RESEARCH
   ↓
REJECTED
```

or:

```text
PRODUCTION
   ↓
DEGRADED
   ↓
REVALIDATION
```

---

# 6. Stage 1 — Idea

An idea may originate from:

* market observation
* academic research
* empirical analysis
* microstructure behavior
* economic rationale
* statistical relationship
* execution behavior
* previous research
* documented external research
* AI-generated hypothesis

An idea is not yet a strategy.

At this stage there is no assumption that the idea contains a tradable edge.

Minimum documentation:

```text
Idea
Why it might work
Market
Expected mechanism
Potential failure conditions
Initial evidence
Source
```

---

# 7. Stage 2 — Hypothesis

The idea must be converted into a falsifiable hypothesis.

Example structure:

```text
Hypothesis:

Under condition X,
market behavior Y tends to occur,
creating opportunity Z,
because mechanism M.
```

The hypothesis must define:

* market
* timeframe
* observable condition
* expected behavior
* proposed mechanism
* expected direction
* expected holding period
* possible failure conditions

The hypothesis should be testable without relying on subjective interpretation.

---

# 8. Stage 3 — Research Specification

Before implementation, define the strategy conceptually.

Minimum specification:

### Market

```text
Instrument
Exchange
Session
Trading hours
Timezone
```

### Signal

```text
Entry conditions
Long conditions
Short conditions
Filters
Confirmation rules
```

### Exit

```text
Stop loss
Take profit
Time exit
Signal reversal
Trailing logic
Emergency exit
```

### Risk

```text
Position sizing method
Maximum position
Risk per trade
Portfolio interaction
```

### Execution

```text
Order type
Expected spread
Expected slippage
Latency assumptions
Liquidity assumptions
```

### Costs

```text
Commission
Fees
Spread
Slippage
Financing
Other applicable costs
```

### Failure Conditions

Examples:

```text
High volatility
Low liquidity
Regime change
News events
Large spreads
Execution degradation
Structural market change
```

---

# 9. Stage 4 — Prototype

The prototype is the simplest implementation capable of testing the hypothesis.

The objective is research speed, not production quality.

The prototype should prioritize:

* correctness
* transparency
* reproducibility
* simplicity

Avoid unnecessary:

* indicators
* filters
* optimization
* machine learning
* complex execution logic

The prototype should answer:

> Does the proposed mechanism appear to exist at all?

---

# 10. Stage 5 — Baseline

Every strategy must have a baseline.

The baseline should be intentionally simple.

Example:

```text
Entry:
20-period breakout

Exit:
ATR stop + fixed target

Risk:
fixed fractional risk

No optimization
No machine learning
No advanced filters
```

The baseline provides a reference against which later modifications can be evaluated.

Any improvement must demonstrate value relative to the baseline.

---

# 11. Stage 6 — Backtest

The strategy enters formal backtesting only after the research specification and baseline exist.

The backtest must use the standardized backtesting engine.

The strategy must not implement its own independent P&L accounting.

The backtest must include realistic assumptions for:

* spread
* commissions
* slippage
* latency where relevant
* contract specifications
* market sessions
* gaps
* partial fills where relevant
* rollover
* financing
* execution constraints

Backtest results must be reproducible.

---

# 12. Stage 7 — Robustness Analysis

A strategy must demonstrate that its results are not dependent on a narrow parameter combination.

Testing should include:

* parameter sensitivity
* neighboring parameter values
* execution-cost variation
* slippage variation
* spread variation
* market-condition variation
* timeframe variation where meaningful
* trade subset analysis
* regime analysis
* removal of individual filters
* feature ablation
* alternative reasonable implementation assumptions

The objective is to identify whether the strategy represents a robust region rather than a single optimized point.

---

# 13. Stage 8 — Out-of-Sample Validation

The OOS dataset must be protected from strategy development decisions.

The OOS dataset must not be repeatedly used to optimize the strategy.

A basic structure is:

```text
Development Data
        ↓
Research
        ↓
Optimization
        ↓
Final Candidate
        ↓
LOCK
        ↓
Out-of-Sample Data
```

Once OOS performance is inspected, changes motivated by the OOS result may constitute a new research cycle.

The OOS dataset should therefore be treated as controlled evidence.

---

# 14. Stage 9 — Walk-Forward Validation

Walk-forward testing evaluates whether the strategy can adapt to changing historical conditions without using future information.

Generic structure:

```text
TRAIN → TEST
TRAIN → TEST
TRAIN → TEST
TRAIN → TEST
```

Each training period must precede its corresponding test period chronologically.

The system should record:

```text
Training period
Testing period
Parameters
Performance
Drawdown
Trade count
Market regime
Execution assumptions
```

The aggregate result must not hide severe instability between individual windows.

---

# 15. Stage 10 — Monte Carlo and Stress Testing

Monte Carlo analysis should investigate uncertainty around the historical trade sequence and expected risk.

Potential methods include:

* trade-order randomization
* bootstrap sampling
* return perturbation
* slippage perturbation
* cost perturbation
* parameter perturbation
* execution degradation

Stress tests may include:

```text
Higher spreads
Higher slippage
Lower liquidity
Higher volatility
Lower volatility
Delayed execution
Larger gaps
Losing streaks
Drawdown clustering
Regime deterioration
```

The objective is not to generate an attractive theoretical scenario.

The objective is to understand how the strategy can fail.

---

# 16. Stage 11 — Strategy Acceptance

A strategy may only progress to paper trading when the required validation evidence exists.

Acceptance should consider:

```text
Hypothesis clarity
Data integrity
Backtest integrity
Realistic costs
Parameter robustness
OOS performance
Walk-forward behavior
Monte Carlo results
Stress results
Trade count
Drawdown behavior
Execution assumptions
Risk compatibility
Portfolio compatibility
Operational reliability
```

No single metric should automatically determine acceptance.

Universal performance thresholds must not be hard-coded into this document.

Strategy-specific acceptance criteria should be defined in the strategy research specification and validation configuration.

---

# 17. Rejection Criteria

A strategy should normally be rejected or returned to research when evidence indicates:

* look-ahead bias
* data leakage
* unrealistic execution
* insufficient data
* extreme parameter sensitivity
* severe OOS degradation
* unstable walk-forward behavior
* dependence on a small number of trades
* excessive concentration of profits
* unexplained performance
* unacceptable drawdown
* poor liquidity compatibility
* excessive transaction-cost sensitivity
* operational complexity without measurable benefit
* inability to reproduce results
* undocumented assumptions
* strategy logic that cannot be independently explained

Rejection is a valid research outcome.

A rejected strategy may be archived and revisited later if new evidence becomes available.

---

# 18. Stage 12 — Paper Trading

Paper trading is a forward validation stage.

The objective is to compare expected behavior with real-time market conditions.

Monitor:

```text
Signal timing
Execution timing
Expected vs actual spread
Expected vs simulated slippage
Trade frequency
Latency
Position sizing
Risk controls
Order lifecycle
Broker synchronization
System stability
```

Paper trading should use the same production architecture whenever practical.

The strategy should not receive special treatment simply because it is in paper mode.

---

# 19. Stage 13 — Live Pilot

A strategy that passes paper validation may enter a controlled live pilot.

The initial deployment should use conservative capital and controlled exposure.

The objective is to validate:

* real execution
* actual costs
* broker behavior
* latency
* order handling
* operational stability
* risk enforcement
* real-time monitoring

The live pilot is not primarily a profit-maximization stage.

It is an operational and execution validation stage.

---

# 20. Stage 14 — Controlled Scaling

Scaling must be gradual.

Potential scaling dimensions:

```text
Position size
Capital allocation
Number of accounts
Number of instruments
Number of markets
Trading frequency
Portfolio weight
```

Scaling should be performed only after verifying:

```text
Execution quality
Risk behavior
Infrastructure stability
Broker synchronization
Drawdown behavior
Portfolio interaction
Operational capacity
```

Scaling should never be based solely on recent profitability.

---

# 21. Production Strategy

A production strategy must have:

```text
Validated version
Approved parameter set
Known data requirements
Defined risk configuration
Defined execution configuration
Monitoring
Logging
Kill switch
Rollback capability
Documentation
Owner
Version identifier
```

Production configuration must be immutable during normal operation.

Changes must go through the development and validation workflow.

---

# 22. Strategy Monitoring

Production strategies must be monitored continuously.

Monitoring categories:

### Performance

```text
P&L
Drawdown
Return distribution
Win/loss behavior
Expectancy
Trade frequency
```

### Statistical

```text
Return distribution
Volatility
Autocorrelation
Signal frequency
Feature distribution
Regime behavior
```

### Execution

```text
Slippage
Spread
Latency
Fill rate
Rejected orders
Partial fills
Execution deviations
```

### Risk

```text
Current risk
Portfolio exposure
Daily loss
Drawdown
Margin
Leverage
Concentration
Correlation
```

### Operational

```text
Data freshness
Broker connectivity
System heartbeat
CPU/memory
Database health
Error rates
Order synchronization
```

---

# 23. Strategy Health

Each production strategy should have a health state.

Example:

```text
HEALTHY
WATCH
DEGRADED
SUSPENDED
REVALIDATION
RETIRED
```

The health state must be based on documented indicators.

The system should distinguish between:

```text
Strategy problem
Execution problem
Market-data problem
Broker problem
Infrastructure problem
Portfolio problem
```

A poor trading result should not automatically be interpreted as strategy failure.

---

# 24. Strategy Degradation

Strategy degradation may occur through:

### Statistical degradation

The observed distribution differs materially from the validated distribution.

### Economic degradation

The underlying market mechanism appears weaker.

### Execution degradation

Actual execution costs exceed assumptions.

### Structural degradation

Market structure changes.

### Regime change

The market enters conditions poorly represented in historical validation.

### Operational degradation

Infrastructure or data quality compromises execution.

These categories should be diagnosed separately.

---

# 25. Revalidation Triggers

A strategy should enter revalidation when predefined conditions are reached.

Possible triggers:

```text
Material strategy-code change
Material parameter change
Material data-source change
Broker/exchange change
Execution-cost deterioration
Significant drawdown
Persistent statistical deviation
Major market-structure change
Major instrument specification change
Portfolio interaction change
Prop-firm rule change
Infrastructure change affecting execution
```

Triggers should be configured rather than hidden inside strategy code.

---

# 26. Suspension

A strategy may be suspended when:

```text
Risk limits are reached
Execution becomes unreliable
Market data becomes invalid
Broker synchronization fails
Strategy behavior deviates materially
Infrastructure is unhealthy
Validation assumptions become invalid
External constraints prohibit trading
```

Suspension should prevent new exposure where appropriate.

Existing positions must be handled according to the risk and execution procedures.

Suspension must be logged with:

```text
Timestamp
Strategy
Reason
Trigger
Account
Open positions
Action taken
Operator/system
Resolution
```

---

# 27. Strategy Revalidation

Revalidation should determine whether the original strategy thesis remains supported.

The process should compare:

```text
Original hypothesis
Original validation
Current market behavior
Current execution conditions
Current risk
Current portfolio interaction
Current performance
```

Possible outcomes:

```text
RETURN_TO_PRODUCTION
CONTINUE_WITH_RESTRICTIONS
RESEARCH_REQUIRED
SUSPEND
RETIRE
```

Revalidation must not become an excuse to repeatedly optimize the strategy until the results look attractive.

---

# 28. Strategy Retirement

A strategy may be retired when:

* its underlying mechanism is no longer supported
* performance degradation persists
* execution becomes economically unviable
* risk characteristics become unacceptable
* superior architecture replaces it
* the market/instrument becomes unsuitable
* maintenance cost exceeds justified value
* required data becomes unavailable
* the strategy cannot satisfy operational requirements

Retirement should preserve historical information.

The strategy should not be deleted merely because it failed.

---

# 29. Strategy Archiving

Archived strategies must preserve:

```text
Strategy specification
Code version
Parameter sets
Data versions
Backtest results
Validation reports
Paper-trading results
Live results
Risk configurations
Execution assumptions
Experiment history
Decision logs
Retirement reason
```

The purpose is reproducibility and institutional knowledge.

---

# 30. Strategy Portfolio Interaction

A strategy must never be evaluated exclusively in isolation once it enters portfolio consideration.

The system must evaluate:

```text
Correlation
Concurrent exposure
Common factors
Common instruments
Common currencies
Common trading sessions
Common volatility exposure
Common execution dependencies
Drawdown clustering
Liquidity concentration
```

Two apparently independent strategies may contain the same underlying risk.

Portfolio-level validation is therefore required before material capital allocation.

---

# 31. Strategy Attribution

Production results should be attributable to:

```text
Strategy
Version
Parameter set
Instrument
Market
Trade
Signal
Execution
Risk decision
Portfolio allocation
```

This allows the system to answer:

> Where did the profit or loss come from?

and:

> Was the observed result caused by the strategy, execution, risk allocation, or another component?

---

# 32. Change Management

Any material strategy modification must create a new experiment.

Examples:

```text
Adding an indicator
Changing entry conditions
Changing exit conditions
Changing position sizing
Changing stop logic
Changing timeframe
Changing instrument
Changing session
Adding ML features
Changing execution model
Changing risk assumptions
```

The change must be documented.

Minimum record:

```text
Previous version
New version
Reason
Hypothesis
Expected impact
Experiment ID
Validation results
Decision
Approval
```

No silent production modifications are permitted.

---

# 33. Strategy Registry

The system should maintain a central strategy registry.

Example:

```text
strategy_registry
-----------------
strategy_id
name
family
market
instrument
timeframe
version
parameter_set
data_version
code_version
status
health
risk_profile
validation_status
paper_status
live_status
created_at
updated_at
owner
notes
```

The registry becomes the authoritative source for strategy state.

---

# 34. Minimum Strategy Card

Every strategy should have a compact strategy card.

Example:

```text
Strategy ID:
NQ_BREAKOUT_001

Name:
NQ Session Breakout

Family:
Breakout

Market:
US Futures

Instrument:
NQ

Timeframe:
5m

Hypothesis:
Opening-session price expansion may create
short-term directional continuation.

Entry:
Defined breakout condition.

Exit:
Defined stop and exit conditions.

Risk:
Managed externally by Risk Engine.

Execution:
Market/limit order according to execution specification.

Primary Risks:
False breakouts, volatility shifts, slippage.

Validation:
OOS + Walk-Forward + Monte Carlo + Stress.

Current State:
PAPER

Current Health:
HEALTHY

Version:
1.0.0
```

The strategy card is a summary, not a replacement for full documentation.

---

# 35. Lifecycle Decision Gates

Each stage should have an explicit gate.

Example:

```text
GATE 01
Is the hypothesis clear and falsifiable?

GATE 02
Is the implementation reproducible?

GATE 03
Does the baseline provide evidence worth investigating?

GATE 04
Does the strategy survive realistic backtesting?

GATE 05
Is the strategy robust to reasonable parameter variation?

GATE 06
Does it survive OOS testing?

GATE 07
Does it survive walk-forward testing?

GATE 08
Does it survive Monte Carlo and stress analysis?

GATE 09
Does paper trading agree sufficiently with expectations?

GATE 10
Is the live pilot operationally safe?

GATE 11
Does the strategy justify additional capital?

GATE 12
Does ongoing monitoring support continued deployment?
```

Failure at any gate should stop automatic progression.

---

# 36. No Automatic Promotion

The system must never automatically promote a strategy from:

```text
RESEARCH → LIVE
```

or:

```text
BACKTEST → PRODUCTION
```

Promotion must require explicit authorization according to the project's governance model.

Automation may prepare evidence and recommendations.

Automation must not bypass governance.

---

# 37. AI-Assisted Strategy Development

AI systems may assist with:

* literature research
* hypothesis generation
* code generation
* feature exploration
* experiment design
* statistical analysis
* documentation
* debugging
* test generation
* result interpretation
* failure analysis

AI systems must not:

* silently modify production strategy logic
* bypass validation
* optimize exclusively for backtest performance
* remove losing periods without documented justification
* modify OOS data
* fabricate research results
* invent data
* hide uncertainty
* disable risk controls
* directly authorize live deployment

AI-generated strategies must enter the same lifecycle as human-generated strategies.

---

# 38. Experiment Lineage

Every meaningful strategy experiment should be traceable.

Example:

```text
Hypothesis H014
      ↓
Experiment E032
      ↓
Strategy NQ_BREAKOUT_001 v0.1
      ↓
Baseline B001
      ↓
Experiment E041
      ↓
Parameter Set P03
      ↓
Validation V012
      ↓
Paper Run PR004
      ↓
Live Pilot LP001
```

This creates a research lineage.

The system should be able to reconstruct how the production strategy originated.

---

# 39. Strategy Performance vs Strategy Validity

Performance and validity are different concepts.

A strategy can:

```text
Have good historical performance
but poor robustness.
```

or:

```text
Have temporary live losses
while remaining structurally valid.
```

or:

```text
Have strong backtest performance
but invalid execution assumptions.
```

Therefore strategy evaluation must consider the complete evidence chain.

---

# 40. Production Review

Production strategies should undergo periodic review.

Review areas:

```text
Performance
Risk
Execution
Data
Market regime
Portfolio interaction
Infrastructure
Strategy assumptions
Parameter stability
Operational incidents
```

The review should determine whether the strategy should:

```text
CONTINUE
CONTINUE_WITH_RESTRICTIONS
REVALIDATE
SUSPEND
RETIRE
```

The decision must be documented.

---

# 41. Strategy Lifecycle State Machine

The conceptual state machine is:

```text
                  ┌──────────────┐
                  │     IDEA     │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │  HYPOTHESIS  │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │   RESEARCH   │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │  PROTOTYPE   │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │   BASELINE   │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │   BACKTEST   │
                  └──────┬───────┘
                         ↓
             ┌─────────────────────────┐
             │ ROBUSTNESS / OOS / WF   │
             └────────────┬────────────┘
                          ↓
                  ┌──────────────┐
                  │ STRESS TEST  │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │    PAPER     │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │ LIVE PILOT   │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │   SCALING    │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │ PRODUCTION   │
                  └──────┬───────┘
                         ↓
                  ┌──────────────┐
                  │  MONITORING  │
                  └──────┬───────┘
                         ↓
             ┌─────────────────────────┐
             │ HEALTHY / DEGRADED     │
             └────────────┬────────────┘
                          ↓
                    REVALIDATION
                          ↓
              ┌───────────┴───────────┐
              ↓                       ↓
        PRODUCTION                 RETIRED
```

At any stage, the strategy may also transition to:

```text
REJECTED
```

or:

```text
ARCHIVED
```

---

# 42. Minimum Evidence for Production

Before production deployment, the strategy package should contain at least:

```text
1. Strategy specification
2. Hypothesis
3. Baseline
4. Backtest report
5. Robustness analysis
6. OOS report
7. Walk-forward report
8. Monte Carlo analysis
9. Stress analysis
10. Execution assumptions
11. Risk configuration
12. Paper-trading report
13. Operational test results
14. Strategy card
15. Version information
16. Approval record
```

Missing evidence must be explicitly identified rather than silently assumed.

---

# 43. Relationship With Other System Components

The Strategy Lifecycle does not replace:

```text
02_QUANT_RESEARCH_METHODOLOGY.md
03_RISK_MANAGEMENT.md
04_BACKTEST_VALIDATION.md
06_EXECUTION_OPERATION.md
07_PROP_FIRM_RULES_FRAMEWORK.md
08_DEVELOPMENT_WORKFLOW.md
```

Instead, it connects them.

Conceptually:

```text
Research Methodology
        ↓
Strategy Lifecycle
        ↓
Backtest & Validation
        ↓
Risk Management
        ↓
Execution
        ↓
Prop Constraints
        ↓
Production Monitoring
```

The strategy lifecycle is the governance layer connecting research to production.

---

# 44. Golden Lifecycle Rule

> A strategy must earn the right to progress.

No strategy progresses because:

* the backtest looks impressive
* the Sharpe ratio is high
* an AI model generated it
* optimization improved historical returns
* the strategy recently made money
* another strategy performed well
* capital is available
* a prop-firm challenge requires a strategy

Progression requires evidence appropriate to the stage.

---

# 45. Final Principle

The purpose of the strategy lifecycle is not to maximize the number of strategies deployed.

It is to maximize the probability that strategies reaching production are:

```text
Understandable
Reproducible
Robust
Risk-controlled
Operationally reliable
Properly validated
Appropriately monitored
Governed throughout their lifetime
```

A strategy is not a finished object.

It is a continuously monitored quantitative hypothesis whose validity must be periodically reassessed.

**Final rule:**

> Research creates hypotheses.
> Validation challenges them.
> Risk constrains them.
> Execution implements them.
> Monitoring evaluates them.
> Governance decides whether they continue.
