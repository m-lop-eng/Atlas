# 03 — RISK MANAGEMENT

**Project:** Modular Algorithmic Trading System
**Document:** Risk Management Framework
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Last Updated:** 2026-09-18

---

# 1. Purpose

This document defines the risk-management framework for the algorithmic trading system.

The Risk Engine is responsible for ensuring that:

* Individual trades remain within predefined risk limits.
* Strategies cannot exceed their allocated risk.
* Portfolio exposure remains controlled.
* Correlated positions are identified.
* Daily and cumulative losses are controlled.
* Leverage remains within permitted limits.
* Prop-firm constraints can be enforced.
* Abnormal market or execution conditions can trigger protective actions.
* The system fails safely when risk cannot be calculated reliably.

The Risk Engine is an independent system component.

Trading strategies generate trading intentions.

The Risk Engine determines whether those intentions are allowed.

---

# 2. Core Principle

> **No trading signal has priority over the Risk Engine.**

A valid strategy signal does not guarantee that an order may be submitted.

The execution flow is:

```text id="s5z5bv"
Strategy
    ↓
Signal
    ↓
Risk Engine
    ↓
APPROVED / REJECTED / MODIFIED
    ↓
Order Manager
    ↓
Broker
```

The Risk Engine may:

* Approve the trade.
* Reject the trade.
* Reduce position size.
* Reduce leverage.
* Require a different stop distance.
* Block the instrument.
* Block the strategy.
* Block the account.
* Trigger a kill switch.

---

# 3. Risk Hierarchy

Risk must be evaluated at multiple levels.

```text id="r2t3bg"
LEVEL 1 — Trade
LEVEL 2 — Position
LEVEL 3 — Strategy
LEVEL 4 — Instrument
LEVEL 5 — Market
LEVEL 6 — Portfolio
LEVEL 7 — Account
LEVEL 8 — Prop-Firm / External Constraints
LEVEL 9 — Global System
```

A trade must satisfy all applicable levels.

---

# 4. Risk Units

The system must distinguish between:

### Account Equity

Current marked-to-market account value.

### Account Balance

Realized account value excluding unrealized P&L, depending on broker/account definition.

### Available Equity

Capital available for new exposure after existing requirements and restrictions.

### Initial Capital

Starting capital used for a specific backtest or trading account.

### Risk Capital

Capital that the system is explicitly permitted to expose to trading losses.

### Drawdown

Decline from a defined reference equity or balance point.

These concepts must not be treated as interchangeable.

---

# 5. Risk-per-Trade

The primary initial risk model is percentage-based.

Default development parameter:

```text id="u8n3a2"
INITIAL_RISK_PER_TRADE = 0.25%
```

This means the theoretical maximum loss associated with a normal trade should initially be approximately:

```text id="b4g7ll"
Trade Risk = Account Equity × Risk %
```

Example:

```text id="n1t7z8"
Account Equity = €100,000
Risk = 0.25%

Nominal risk = €250
```

This is a configurable system parameter.

It is not a universal recommendation and must be evaluated against the strategy, instrument, execution environment, and account constraints.

---

# 6. Position Sizing

Position sizing should be derived from predefined risk rather than from an arbitrary number of contracts or shares.

Generic model:

```text id="2e4r6p"
Position Size =
Maximum Monetary Risk
/
Monetary Loss Per Unit at Stop
```

For example:

```text id="xq7r8d"
Maximum risk = $250
Risk per contract = $125

Maximum position = 2 contracts
```

The actual calculation must account for:

* Contract specifications.
* Tick value.
* Pip value.
* Share price.
* Stop distance.
* Currency conversion.
* Fees.
* Slippage.
* Minimum contract size.
* Lot size.
* Margin requirements.

---

# 7. Risk Is Based on Actual Loss Potential

The system must not calculate risk solely from the distance between entry and stop.

Where appropriate, expected execution costs must be included.

Approximate model:

```text id="v7qj9a"
Total Trade Risk =
Stop Loss Risk
+
Expected Slippage
+
Estimated Commission
+
Relevant Fees
```

For highly liquid instruments, the additional components may be small.

For less liquid instruments, they may become material.

---

# 8. Stop Loss Requirement

Strategies classified as fixed-risk strategies should have an objectively defined maximum loss mechanism.

This may be:

* Hard stop.
* Broker-side stop.
* System-managed stop.
* Volatility-based stop.
* Structural stop.

If a strategy intentionally operates without a conventional stop, its alternative loss-control mechanism must be explicitly documented and approved.

---

# 9. Position Risk

For every open position, the system must calculate:

```text id="0njm5f"
Current Unrealized P&L
Distance to Stop
Estimated Loss at Stop
Worst-Case Execution Loss
Position Notional
Margin
Leverage
```

Where relevant, risk should also account for:

* Gaps.
* Slippage.
* Market closure.
* Overnight exposure.
* Weekend exposure.
* News events.

---

# 10. Portfolio Risk

Individual trade risk does not represent total portfolio risk.

The Risk Engine must calculate aggregate portfolio exposure.

At minimum:

```text id="rq2r6a"
Total Gross Exposure
Total Net Exposure
Long Exposure
Short Exposure
Market Exposure
Strategy Exposure
Instrument Exposure
Currency Exposure
Sector Exposure where applicable
```

---

# 11. Correlation Risk

The system must identify correlated exposures.

Example:

```text id="b9w6jk"
LONG NQ
LONG ES
LONG Semiconductor Equity
LONG BTC
```

Although these are different instruments, they may contain significant common directional risk.

The portfolio engine should therefore calculate:

* Pairwise correlations.
* Rolling correlations.
* Cluster correlations where appropriate.
* Aggregate directional exposure.

Correlation must not be treated as static.

---

# 12. Correlation Matrix

Where sufficient data exists, the system should maintain rolling correlation matrices.

Possible windows:

```text id="s3j2la"
Short-term
Medium-term
Long-term
```

The exact windows must be strategy- and market-dependent.

The system should be capable of identifying changes in correlation regimes.

---

# 13. Strategy-Level Risk

Each strategy must have independent risk limits.

Example configuration:

```yaml id="4i4r5m"
strategy:
  id: STRAT-NQ-001
  risk_per_trade: 0.25%
  max_open_positions: 3
  max_strategy_risk: 1.00%
  max_daily_loss: 1.00%
```

These values are examples of configuration structure, not permanent project-wide limits.

---

# 14. Instrument-Level Risk

Each instrument may have independent constraints.

Examples:

```text id="l8h4w0"
Maximum position
Maximum notional
Maximum contracts
Maximum leverage
Maximum daily risk
Trading hours
Overnight permission
Weekend permission
```

This allows the same strategy to operate differently across instruments.

---

# 15. Market-Level Risk

Markets may have aggregate limits.

Example:

```text id="r6r7r3"
US Futures:
Maximum aggregate risk = X%

Forex:
Maximum aggregate risk = X%

Crypto:
Maximum aggregate risk = X%
```

The actual values must be configurable.

---

# 16. Portfolio Risk Budget

The portfolio should operate using a predefined risk budget.

Conceptually:

```text id="0s2h9c"
TOTAL PORTFOLIO RISK BUDGET
        │
        ├── Strategy A
        ├── Strategy B
        ├── Strategy C
        └── Reserve
```

The system must avoid allowing every strategy to independently consume the entire account risk budget.

---

# 17. Concurrent Risk

The system must calculate the potential loss if multiple open positions simultaneously reach their predefined stops.

Example:

```text id="4qlt7u"
Trade A = 0.25%
Trade B = 0.25%
Trade C = 0.25%

Gross theoretical stop risk = 0.75%
```

This must then be adjusted where necessary for:

* Correlation.
* Shared market exposure.
* Portfolio limits.
* Hedging.
* Netting.

---

# 18. Risk Concentration

The system must detect concentration by:

### Instrument

Too much exposure to one instrument.

### Strategy

Too much exposure to one strategy.

### Market

Too much exposure to one market.

### Direction

Too much long or short exposure.

### Currency

Too much exposure to one currency.

### Economic factor

Too much exposure to the same underlying risk factor.

---

# 19. Leverage

Leverage must be explicitly monitored.

The system must distinguish:

```text
Notional Exposure
Margin Requirement
Account Equity
Effective Leverage
```

Effective leverage:

```text id="y4l9v2"
Effective Leverage =
Gross Notional Exposure
/
Account Equity
```

Leverage limits must be configurable by:

* Account.
* Instrument.
* Strategy.
* Broker.
* Prop firm.

---

# 20. Margin Risk

A trade may have acceptable stop-loss risk but unacceptable margin requirements.

Therefore the Risk Engine must check:

```text id="p5k2j4"
Required Margin
Available Margin
Margin Level
Projected Margin After Order
```

A trade must be rejected if executing it could produce unacceptable margin conditions.

---

# 21. Daily Loss Limit

The system must maintain a real-time daily P&L calculation.

Daily loss may include:

* Realized P&L.
* Unrealized P&L where required by the account rules.
* Commissions.
* Fees.
* Financing.
* Other relevant account charges.

The definition must be configurable because brokers and prop firms may calculate daily loss differently.

---

# 22. Daily Loss Protection

The system must support configurable thresholds:

```text id="5ozjzq"
WARNING_LEVEL
SOFT_LIMIT
HARD_LIMIT
```

Example:

```text id="xk2h6a"
At 50% of limit:
WARNING

At 75%:
REDUCE / RESTRICT

At 100%:
STOP NEW TRADES
```

Actual thresholds must be configurable.

---

# 23. Drawdown

The system must track multiple drawdown definitions.

### Absolute Drawdown

Loss relative to initial capital.

### Peak-to-Trough Drawdown

Loss relative to the highest observed equity.

### Daily Drawdown

Loss relative to the daily reference value.

### Trailing Drawdown

Loss relative to a dynamically moving high-water mark.

These definitions must never be mixed.

---

# 24. Drawdown Formula

Generic peak-to-trough drawdown:

```text id="9k5v2a"
Drawdown =
(Peak Equity - Current Equity)
/
Peak Equity
```

The system must store:

* Peak equity.
* Current equity.
* Current drawdown.
* Maximum historical drawdown.
* Drawdown duration.
* Recovery duration.

---

# 25. Drawdown States

The system should classify drawdown levels.

Example:

```text id="d4f2y8"
NORMAL
CAUTION
REDUCED_RISK
HALTED
```

Example behavior:

```text id="8w9c5h"
NORMAL
    ↓
CAUTION
    ↓
REDUCED_RISK
    ↓
HALTED
```

The exact thresholds must be configurable.

---

# 26. Dynamic Risk Reduction

The Risk Engine may reduce risk when:

* Drawdown increases.
* Volatility becomes abnormal.
* Execution quality deteriorates.
* Correlation concentration increases.
* Daily loss approaches its limit.
* Market liquidity deteriorates.
* Operational conditions become unstable.

Risk reduction should be explicit and deterministic.

---

# 27. Risk Scaling

Risk scaling should be conservative.

The system must not automatically increase risk simply because recent performance has been strong.

Potential scaling criteria may include:

```text id="o3t6gq"
Minimum live sample
Stable execution
Acceptable drawdown
OOS consistency
No major model degradation
Operational stability
```

Any automated risk scaling must be independently tested.

---

# 28. Loss Streak

The system should monitor consecutive losses.

Metrics:

```text id="8svm9w"
Current losing streak
Maximum historical losing streak
Expected losing streak distribution
Probability of extended losing streak
```

A losing streak alone should not automatically imply strategy failure.

However, it may be relevant to dynamic risk controls.

---

# 29. Risk of Ruin

The system should estimate the probability of reaching defined loss thresholds under the strategy's observed or modeled distribution.

The analysis should consider:

* Win probability.
* Payoff ratio.
* Risk per trade.
* Trade frequency.
* Correlation.
* Drawdown.
* Distribution of returns.

Risk of ruin should be evaluated using simulation where appropriate rather than relying exclusively on simplified formulas.

---

# 30. Kelly Criterion

Kelly-based calculations may be used as an analytical reference.

However:

> Full Kelly sizing must not automatically be used for live trading.

Kelly estimates can be extremely sensitive to estimation error.

If Kelly-derived sizing is investigated, the system should consider fractional Kelly and uncertainty in the underlying estimates.

---

# 31. Volatility-Adjusted Risk

Where appropriate, position size may be adjusted according to market volatility.

Example:

```text id="o1y2la"
Higher volatility
→ Smaller position

Lower volatility
→ Potentially larger position
```

However, volatility targeting must not be used to circumvent portfolio or drawdown limits.

---

# 32. Gap Risk

The system must distinguish theoretical stop risk from actual possible loss.

A stop cannot guarantee execution at the stop price under:

* Market gaps.
* Extreme volatility.
* Illiquidity.
* Trading halts.
* Broker outages.
* Market closures.

The risk model must account for this where relevant.

---

# 33. News and Event Risk

For instruments affected materially by scheduled events, the Risk Engine should support configurable event restrictions.

Possible events:

* Central bank decisions.
* Employment data.
* Inflation releases.
* Major economic releases.
* Earnings.
* Corporate announcements.
* Exchange events.

Whether trading is permitted around these events must be configurable.

The Risk Engine must not assume that news restrictions are universal.

---

# 34. Overnight Risk

Each account and strategy must explicitly define whether overnight positions are permitted.

If permitted, the risk model must consider:

* Gaps.
* Liquidity.
* Financing.
* Session transitions.
* Broker rules.
* Prop-firm rules.

---

# 35. Weekend Risk

Weekend exposure must be explicitly controlled.

The system must know whether:

```text id="u8c4hl"
Weekend holding allowed?
Market closed?
Reduced liquidity?
Gap risk?
Broker restrictions?
Prop restrictions?
```

---

# 36. Kill Switch Architecture

Kill switches must exist at:

```text id="9o2hbb"
STRATEGY
INSTRUMENT
MARKET
ACCOUNT
GLOBAL
```

Possible triggers:

```text id="7x6s4h"
Daily loss limit
Maximum drawdown
Connection loss
Data failure
Execution anomaly
Unexpected position
Duplicate order
Excessive slippage
Excessive latency
Margin danger
Prop-firm limit proximity
System integrity failure
```

---

# 37. Kill Switch Behavior

The system must define what each kill switch does.

Minimum behavior:

```text id="4k0t0d"
1. Block new orders.
2. Cancel pending orders where appropriate.
3. Log event.
4. Notify operator.
5. Mark affected component as HALTED.
```

Whether existing positions should be closed must be configurable according to the risk event.

---

# 38. Fail-Safe Principle

If the Risk Engine cannot calculate risk reliably, the default behavior is:

```text id="l2r0j9"
DO NOT TRADE
```

Examples:

* Missing account equity.
* Missing stop price.
* Unknown position.
* Unknown contract specification.
* Missing market data.
* Unavailable exchange rate.
* Broken risk calculation.
* Inconsistent broker state.

---

# 39. Risk Engine Decision States

Every order request should produce a deterministic decision.

Possible states:

```text id="z2w0cn"
APPROVED
APPROVED_WITH_REDUCED_SIZE
REJECTED
BLOCKED
HALTED
ERROR
```

The decision should include a reason.

Example:

```json id="2pm1m5"
{
  "decision": "REJECTED",
  "reason": "PORTFOLIO_RISK_LIMIT_EXCEEDED"
}
```

---

# 40. Risk Checks

Before order submission, the Risk Engine should perform checks including:

```text id="u2a9rj"
[ ] Account active
[ ] Strategy active
[ ] Instrument permitted
[ ] Market open
[ ] Trading session permitted
[ ] Position limit
[ ] Risk per trade
[ ] Strategy risk
[ ] Portfolio risk
[ ] Daily loss
[ ] Drawdown
[ ] Leverage
[ ] Margin
[ ] Correlation
[ ] News restrictions
[ ] Overnight restrictions
[ ] Weekend restrictions
[ ] Prop-firm rules
[ ] Kill switches
[ ] Data validity
[ ] Broker connectivity
```

---

# 41. Risk Reservation

When multiple orders are generated simultaneously, the system should reserve their expected risk before final submission.

This prevents multiple strategies from independently assuming the same unused risk budget.

Example:

```text id="j9v6yo"
Available Risk Budget = 1.00%

Order A requests = 0.40%
Order B requests = 0.40%
Order C requests = 0.40%

Total requested = 1.20%

→ C must be rejected or reduced.
```

---

# 42. Pending Orders

Pending orders must be included in risk calculations where they can generate future exposure.

The system must distinguish:

```text id="l3i6f5"
Current Risk
Reserved Risk
Potential Risk
```

---

# 43. Hedging

The Risk Engine must distinguish between:

* Gross exposure.
* Net exposure.
* Hedged exposure.

A hedge must not automatically be considered zero risk.

Correlations can change, basis risk can exist, and execution can differ.

---

# 44. Multi-Account Risk

When multiple accounts are operated, risk must be monitored:

```text id="t5d1v0"
Per Account
Per Strategy
Across Accounts
Across Brokers
Across Prop Firms
```

The system should detect duplicated exposure across accounts.

Example:

```text id="0j4w7x"
Account A → LONG NQ
Account B → LONG NQ
Account C → LONG ES
```

These positions may represent a larger consolidated risk than any single account indicates.

---

# 45. Prop-Firm Risk

Prop-firm accounts require special handling because:

```text id="3f7y1j"
Nominal Account Size
≠
Actual Loss Capacity
```

The Risk Engine must model the actual permitted drawdown.

For example, a nominal account of:

```text id="z4o1kn"
$100,000
```

does not imply that the system may risk $100,000.

The relevant constraints may instead be defined by:

* Maximum daily loss.
* Maximum drawdown.
* Trailing drawdown.
* Position limits.
* News restrictions.
* Overnight restrictions.
* Other provider-specific rules.

---

# 46. Prop-Firm Risk Model

Each prop-firm account must have a dedicated configuration.

Example:

```yaml id="n1x7r6"
account:
  provider:
  account_id:
  currency:
  nominal_balance:

risk:
  max_daily_loss:
  max_drawdown:
  trailing_drawdown:
  max_position:
  max_leverage:

restrictions:
  overnight:
  weekend:
  news:
  ea:
  api:
```

Actual rules must be verified against current provider documentation before use.

---

# 47. Account Risk Profiles

The system should support different risk profiles.

Example:

```text id="2l8r5k"
CONSERVATIVE
STANDARD
AGGRESSIVE
CUSTOM
```

These profiles should be configuration presets.

They must not bypass hard account or prop-firm constraints.

---

# 48. Risk Budget Allocation

For a multi-strategy portfolio, the system should eventually support:

```text id="2g0p0h"
Strategy Risk Budget
        +
Correlation Adjustment
        +
Portfolio Risk Budget
        +
Account Constraints
        =
Allowed Risk
```

Allocation methods may later include:

* Equal risk.
* Volatility scaling.
* Correlation-adjusted allocation.
* Risk parity.
* Optimization-based allocation.

These methods require independent research before production use.

---

# 49. Risk Attribution

The system should eventually be able to explain portfolio risk.

Example:

```text id="qv5l9f"
Total portfolio risk: 1.35%

NQ strategy:          0.45%
ES strategy:          0.30%
EUR/USD strategy:     0.20%
BTC strategy:         0.15%
Correlation effect:   0.25%
```

The exact calculation method must be defined during portfolio-engine development.

---

# 50. Risk Reporting

The monitoring system should expose at minimum:

```text id="k4v4la"
Account equity
Balance
Daily P&L
Open P&L
Drawdown
Maximum drawdown
Risk per trade
Open risk
Reserved risk
Portfolio risk
Gross exposure
Net exposure
Leverage
Margin
Strategy risk
Daily loss utilization
Prop-firm limit utilization
```

---

# 51. Risk Alerts

Alerts should be generated for:

```text id="v3z7se"
Risk limit approaching
Risk limit exceeded
Daily loss approaching
Drawdown approaching
Excessive leverage
Margin deterioration
Unexpected position
Unexpected order
Excessive correlation
Excessive slippage
Abnormal volatility
Risk Engine failure
Account state mismatch
```

---

# 52. Risk Logging

Every Risk Engine decision must be logged.

Required fields should include:

```text id="u9k0qu"
Timestamp
Account
Strategy
Instrument
Requested quantity
Approved quantity
Risk before order
Risk after order
Decision
Decision reason
Risk limits
Relevant market state
```

---

# 53. Risk Backtesting

Risk management itself must be backtested.

It is not sufficient to backtest only the strategy.

The system should test:

* Risk limits.
* Position sizing.
* Drawdown controls.
* Daily loss controls.
* Risk reduction.
* Kill switches.
* Portfolio exposure.
* Correlation controls.

---

# 54. Risk Simulation

Before live deployment, simulate adverse scenarios such as:

```text id="l2t5f8"
10 consecutive losses
20 consecutive losses
Extreme volatility
Gap against position
Broker disconnection
Execution delay
Slippage spike
Multiple correlated strategies losing simultaneously
Daily loss threshold reached
Drawdown threshold reached
```

The purpose is to verify that the Risk Engine behaves correctly.

---

# 55. Risk Governance

Risk parameters must have ownership and versioning.

Every important risk parameter should have:

```text id="b7v4y3"
Parameter
Value
Unit
Scope
Reason
Effective date
Version
Approval status
```

---

# 56. Changing Risk Parameters

Changing a critical risk parameter requires:

1. Documenting the proposed change.
2. Explaining the reason.
3. Testing the impact.
4. Evaluating historical consequences.
5. Updating configuration.
6. Versioning the change.
7. Recording approval.

Risk changes must never be introduced silently.

---

# 57. Risk Parameter Categories

Parameters should be separated into:

### HARD LIMITS

Cannot be exceeded.

### SOFT LIMITS

Trigger warnings or risk reduction.

### TARGETS

Desired operating levels.

### DEFAULTS

Initial configuration values.

### STRATEGY-SPECIFIC PARAMETERS

Defined by individual strategies.

This distinction is mandatory.

---

# 58. No Risk Circumvention

No strategy, broker adapter, execution component, or AI agent may bypass the Risk Engine.

Examples of prohibited behavior:

```text id="c8qf5p"
Direct broker order from strategy
Direct position sizing override
Ignoring account limits
Ignoring kill switch
Submitting order after risk rejection
Creating hidden exposure
Changing risk configuration during execution
```

---

# 59. Risk Engine Independence

The Risk Engine must remain logically independent from alpha generation.

The strategy can propose:

```text id="t6y7ce"
BUY 10 contracts
```

The Risk Engine may respond:

```text id="0p5n5u"
APPROVED: 4 contracts
```

or:

```text id="d3t6vn"
REJECTED: PORTFOLIO_RISK_LIMIT
```

---

# 60. Initial Risk Policy

Until sufficient strategy-specific evidence exists, the project will use a conservative initial framework:

```text id="1xj7b2"
Default risk per trade:
0.25%

No automatic risk increase.

No live risk scaling based solely on recent profitability.

No strategy may override account-level hard limits.

No order may bypass the Risk Engine.

No trading when risk cannot be calculated reliably.
```

These are initial system policies and may be revised through documented governance.

---

# 61. Minimum Risk Approval Checklist

Before a strategy can trade live, verify:

```text id="5m0m0k"
[ ] Risk per trade defined
[ ] Position sizing validated
[ ] Stop/loss mechanism validated
[ ] Strategy risk limit defined
[ ] Daily loss limit defined
[ ] Maximum drawdown defined
[ ] Portfolio exposure defined
[ ] Correlation exposure evaluated
[ ] Leverage checked
[ ] Margin checked
[ ] Slippage evaluated
[ ] Gap risk evaluated
[ ] Kill switch tested
[ ] Risk Engine tested
[ ] Failure scenarios tested
[ ] Account rules verified
[ ] Prop-firm rules verified where applicable
[ ] Monitoring active
[ ] Risk logging active
```

---

# 62. Golden Risk Rule

> **The system must always know how much it can lose before it knows how much it can make.**

A trading opportunity is only valid when the associated risk is measurable, bounded, and compatible with the current account and portfolio constraints.

---

# 63. Relationship With Other Documents

This document must be used together with:

```text id="o6x6uk"
00_MASTER_SPECIFICATION.md
01_GENERAL_RULES.md
02_QUANT_RESEARCH_METHODOLOGY.md
04_BACKTEST_VALIDATION.md
06_EXECUTION_OPERATION.md
07_PROP_FIRM_RULES_FRAMEWORK.md
09_STRATEGY_LIFECYCLE.md
```

This document defines **how risk is measured, controlled, limited, and enforced**.

It does not define the alpha logic of individual strategies.
