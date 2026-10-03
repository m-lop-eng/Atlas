# 07_PROP_FIRM_RULES_FRAMEWORK.md

**Project:** Modular Multi-Market Algorithmic Trading System
**Document:** Prop Firm Rules Framework
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Date:** 2026-09-18
**Authority:** Master Specification

---

# 1. PURPOSE

This document defines the architecture for integrating proprietary trading firm rules into the trading system.

The objective is to allow the system to:

* model external trading constraints
* simulate prop-firm accounts
* validate strategies against account rules
* calculate remaining risk capacity
* prevent rule violations
* monitor account state
* support multiple prop firms
* support multiple account types
* update rules without modifying strategy code

Prop-firm rules are treated as an **external constraint layer**.

They must not be embedded directly into trading strategies.

---

# 2. FUNDAMENTAL PRINCIPLE

A trading strategy should answer:

> "What trade does the strategy want to take?"

The Risk Engine should answer:

> "How much risk is acceptable?"

The Prop Firm Rule Engine should answer:

> "Is this action permitted under the external account rules?"

The execution layer should answer:

> "How can the permitted action be executed?"

Architecture:

```text
Strategy
   ↓
Risk Engine
   ↓
Prop Firm Rule Engine
   ↓
Order Manager
   ↓
Broker Adapter
   ↓
Execution
```

---

# 3. PROP FIRM RULE ENGINE

The Prop Firm Rule Engine is responsible for evaluating external constraints.

It should be implemented independently from:

* strategy logic
* portfolio construction
* broker adapters
* execution code

It may consume information from those systems but must not modify their fundamental responsibilities.

---

# 4. PROP FIRM PROFILE

Every supported prop-firm account should have a versioned profile.

Example conceptual structure:

```yaml
prop_firm:
  id: example_firm

account_type:
  id: example_account

rules_version:
  version: "YYYY-MM-DD"

account:
  nominal_size:
  currency:

drawdown:
  type:
  maximum:
  calculation_method:
  reset_conditions:

daily_loss:
  enabled:
  maximum:
  calculation_method:

position:
  maximum_size:
  maximum_positions:

leverage:
  maximum:

trading_hours:
  restrictions:

news:
  restrictions:

overnight:
  allowed:

weekend:
  allowed:

consistency:
  enabled:

profit_target:
  enabled:

minimum_trading_days:
  enabled:

other_rules:
```

This is a conceptual schema.

Actual fields must be adapted to the rules of each provider.

---

# 5. SOURCE OF TRUTH

Prop-firm rules must be obtained from authoritative sources.

Priority:

1. Official prop-firm documentation
2. Official account agreement
3. Official trading rules
4. Official FAQ
5. Official API/documentation where applicable

Third-party summaries may be used for discovery but should not be considered authoritative when rules affect live capital.

---

# 6. RULE VERSIONING

Every rule set must have:

```text id="wqg2ve"
Provider
Account Type
Rule Version
Effective Date
Source
Retrieved Date
Reviewed Date
Reviewer
Status
```

Example:

```text
Provider:
Example Firm

Account:
100K Futures

Rules Version:
2026-09-18

Effective:
2026-09-01

Source:
Official documentation

Status:
VERIFIED
```

---

# 7. RULE CLASSIFICATION

Rules should be classified according to their function.

## 7.1 ACCOUNT RULES

Examples:

* nominal account size
* starting balance
* profit target
* minimum trading days

---

## 7.2 LOSS RULES

Examples:

* maximum daily loss
* maximum total loss
* trailing drawdown
* static drawdown
* equity drawdown
* balance drawdown

---

## 7.3 EXPOSURE RULES

Examples:

* maximum position size
* maximum contracts
* leverage
* maximum simultaneous exposure

---

## 7.4 TIME RULES

Examples:

* trading session restrictions
* overnight restrictions
* weekend restrictions
* mandatory flat periods

---

## 7.5 EVENT RULES

Examples:

* news restrictions
* economic-event restrictions
* earnings restrictions

---

## 7.6 STRATEGY / BEHAVIOR RULES

Examples:

* consistency requirements
* maximum daily profit concentration
* prohibited trading behaviors
* prohibited execution techniques

---

## 7.7 OPERATIONAL RULES

Examples:

* platform restrictions
* broker restrictions
* API restrictions
* automation restrictions

---

# 8. NOMINAL ACCOUNT SIZE VS RISK CAPACITY

The nominal account size must not be interpreted as available loss capacity.

For example:

```text
Nominal account:
$100,000

Maximum permitted drawdown:
$5,000
```

The practical risk capacity is therefore substantially smaller than the nominal account value.

Risk calculations must use the appropriate rule-defined loss capacity.

---

# 9. DRAWdown MODEL

The Rule Engine must explicitly identify the drawdown methodology.

Possible models include:

* static drawdown
* trailing drawdown
* balance-based
* equity-based
* end-of-day
* intraday
* high-water-mark based

The system must never assume that "maximum drawdown" has the same meaning across providers.

---

# 10. HIGH-WATER MARK

Where applicable, the system should track:

```text
High Water Mark
Current Equity
Current Balance
Drawdown
Remaining Drawdown Capacity
```

For a simplified model:

$$
Drawdown =
HighWaterMark - CurrentEquity
$$

However, the actual calculation must follow the provider's official methodology.

---

# 11. TRAILING DRAWDOWN

For trailing drawdown accounts, the system must model:

* initial threshold
* high-water mark
* threshold movement
* whether unrealized P&L affects the threshold
* whether threshold stops trailing
* reset conditions

These details can materially affect account survival.

They must be explicitly configured.

---

# 12. DAILY LOSS LIMIT

If applicable, the system must calculate the current daily loss according to the provider's methodology.

Potential components include:

* realized P&L
* unrealized P&L
* commissions
* fees
* swaps
* financing
* previous-day balance/equity

The exact methodology must be provider-specific.

---

# 13. DAILY RESET

The Rule Engine must explicitly define the daily reset time.

Important considerations:

* exchange timezone
* broker timezone
* provider timezone
* daylight-saving time
* holidays
* session boundaries

The system should use explicit timestamps rather than assumptions based on local computer time.

---

# 14. LOSS BUFFER

The system should distinguish:

```text
Maximum Allowed Loss
Current Loss
Remaining Loss Capacity
Safety Buffer
```

Example:

```text
Maximum loss capacity:
$5,000

Current drawdown:
$3,200

Remaining:
$1,800

Internal safety buffer:
$300
```

The system may block trading before the external limit is reached.

This is intentional.

---

# 15. INTERNAL VS EXTERNAL LIMITS

External prop-firm limits are not necessarily the internal limits used by the trading system.

Example:

```text
External daily loss limit:
$2,500

Internal daily loss limit:
$1,750
```

The internal limit can provide protection against:

* slippage
* execution delay
* data discrepancies
* calculation differences
* unexpected volatility

The internal limit must never exceed a verified external restriction.

---

# 16. POSITION SIZE LIMITS

The Rule Engine should verify:

```text
Requested Position Size
≤
Prop Firm Maximum Position Size
```

This check must consider:

* current exposure
* pending orders
* partial fills
* multiple strategies
* multiple instruments
* netting/hedging structure

---

# 17. LEVERAGE AND MARGIN

Where applicable, the system must model:

* maximum leverage
* margin requirements
* available margin
* margin utilization
* instrument-specific requirements

The Risk Engine should also impose internal limits below external maximums where appropriate.

---

# 18. TRADING HOURS

Rules may restrict trading to specific periods.

The system must represent:

* allowed sessions
* prohibited sessions
* mandatory flat periods
* market holidays
* daylight-saving transitions

The rule must be evaluated using the appropriate market/provider timezone.

---

# 19. OVERNIGHT TRADING

If overnight positions are restricted, the system should automatically detect positions that would violate the rule.

Potential actions:

```text
BLOCK_NEW_POSITION
REDUCE_POSITION
CLOSE_POSITION
ALERT_OPERATOR
```

The exact action must be explicitly configured.

---

# 20. WEEKEND EXPOSURE

If weekend holding is prohibited, the system must define:

* cutoff time
* timezone
* required flat state
* behavior if an order remains open
* emergency handling

The system should verify both:

* open positions
* pending orders

---

# 21. NEWS RESTRICTIONS

Some account programs may impose restrictions around specified economic events.

The framework should support:

```text
event_id
event_type
release_time
timezone
affected_market
restriction_before
restriction_after
allowed_actions
```

The system must distinguish between:

* scheduled news
* unexpected news
* market events
* provider-defined restricted events

Current event restrictions must be obtained from appropriate authoritative sources.

---

# 22. PROFIT TARGET

Some account types may require a target before progression or payout.

The system should track:

```text
Starting Value
Current Value
Profit
Profit Target
Remaining Target
```

Profit targets must not cause the strategy to take excessive risk.

The target is an external account constraint, not a strategy objective.

---

# 23. CONSISTENCY RULES

Some programs may impose consistency requirements.

Examples may include constraints related to:

* percentage of total profits
* largest winning day
* largest winning trade
* daily distribution
* concentration of profits

The exact formula must be represented explicitly.

The system must not infer a consistency rule from generic descriptions.

---

# 24. MINIMUM TRADING DAYS

Where applicable, track:

```text
Required Trading Days
Completed Trading Days
Remaining Trading Days
```

The system must not force trades merely to satisfy a minimum-day requirement.

If a trading day requires a particular definition of activity, that definition must be encoded explicitly.

---

# 25. PROHIBITED BEHAVIOR

If a provider prohibits specific behaviors, they should be represented as explicit constraints.

Potential categories include:

* latency exploitation
* arbitrage
* prohibited news trading
* account manipulation
* copy trading restrictions
* trade copying between accounts
* prohibited execution patterns
* prohibited automation

The system must use the provider's actual definitions.

---

# 26. MULTI-ACCOUNT SUPPORT

The framework should support multiple prop accounts.

Each account must maintain independent:

* balance
* equity
* drawdown
* daily loss
* positions
* orders
* limits
* rule version
* state

A portfolio-level controller may additionally enforce aggregate limits.

---

# 27. CROSS-ACCOUNT RISK

Multiple accounts can create hidden concentration.

For example:

```text
Account A → Long NQ
Account B → Long NQ
Account C → Long NQ
```

Although each account may independently satisfy its rules, the combined exposure may be large.

The system should therefore optionally track:

* aggregate instrument exposure
* aggregate directional exposure
* aggregate strategy exposure
* aggregate market risk

---

# 28. PROP ACCOUNT STATES

Recommended states:

```text
EVALUATION
FUNDED
RESTRICTED
AT_RISK
HALTED
FAILED
PAYOUT_PENDING
CLOSED
```

Actual lifecycle states should be adapted to the provider.

---

# 29. ACCOUNT HEALTH

A standardized health metric can be displayed without replacing the underlying rules.

For example:

```text
Drawdown Capacity Remaining
Daily Loss Capacity Remaining
Position Capacity Remaining
Margin Capacity Remaining
```

The system should expose the underlying values rather than relying only on a composite score.

---

# 30. RULE ENGINE DECISION

The Rule Engine should return deterministic results.

Example:

```json
{
  "status": "REJECT",
  "rule_id": "DAILY_LOSS_LIMIT",
  "reason": "Projected loss exceeds remaining permitted capacity",
  "remaining_capacity": 450.00,
  "projected_risk": 600.00
}
```

Possible statuses:

```text
ALLOW
ALLOW_WITH_RESTRICTIONS
REJECT
HALT
UNKNOWN
```

---

# 31. UNKNOWN RULE STATE

If a required rule cannot be evaluated reliably, the system should not assume that the trade is allowed.

Example:

```text
Provider rule unavailable
        ↓
Rule status = UNKNOWN
        ↓
Block affected action
```

This follows the fail-safe principle.

---

# 32. RULE PRIORITY

When constraints conflict:

```text
Law / Regulation
      ↓
Broker / Exchange Restrictions
      ↓
Prop Firm Rules
      ↓
Internal Risk Rules
      ↓
Strategy Constraints
      ↓
Execution Preferences
```

A lower-level component cannot override a higher-level restriction.

---

# 33. INTERNAL SAFETY MARGINS

The system may apply internal buffers.

Examples:

* drawdown buffer
* daily loss buffer
* position-size buffer
* margin buffer
* trading-hour buffer

Buffers should be configurable and documented.

They should not be confused with official provider rules.

---

# 34. PROP ACCOUNT SIMULATOR

Before opening an account, the system should support simulation.

The simulator should reproduce:

* account balance
* equity
* drawdown
* daily loss
* profit target
* position limits
* trading restrictions
* consistency rules
* account state transitions

The objective is to answer:

> Would this strategy and risk configuration have remained compliant under the modeled account rules?

---

# 35. CHALLENGE SIMULATION

Challenge simulation should use historical strategy trades.

For each historical sequence, calculate:

* account equity
* drawdown
* daily loss
* target progress
* rule violations
* failure date
* maximum exposure
* recovery periods

The result should distinguish:

```text
Strategy performance
VS
Account-rule survivability
```

---

# 36. RULE-ADJUSTED BACKTEST

A strategy backtest can be run through the Prop Firm Rule Engine.

Example:

```text
Raw Strategy
     ↓
Backtest
     ↓
Risk Engine
     ↓
Prop Rules
     ↓
Rule-Adjusted Equity Curve
```

This can reveal that a profitable strategy may nevertheless violate account constraints.

---

# 37. RULE-ADJUSTED METRICS

Useful outputs include:

* gross strategy return
* rule-adjusted return
* maximum drawdown
* maximum daily loss
* rule violations
* days blocked
* forced exits
* rejected trades
* profit target achievement
* account failure rate
* payout eligibility where modeled

---

# 38. IMPORTANT DISTINCTION

The following are different concepts:

```text
Strategy profitability
Account survivability
Rule compliance
Payout eligibility
```

A strategy can be profitable while failing a prop-firm rule.

A strategy can comply with rules while having poor economics.

The system must report these dimensions separately.

---

# 39. RULE CHANGE MANAGEMENT

When provider rules change:

1. Create a new rule version.
2. Preserve the previous version.
3. Record effective date.
4. Record source.
5. Compare differences.
6. Identify affected strategies/accounts.
7. Re-run relevant simulations.
8. Review risk configuration.
9. Approve deployment.
10. Archive the previous configuration.

Historical results should remain associated with the rule version used at the time.

---

# 40. RULE VERIFICATION

Before enabling a prop account:

```text
[ ] Provider verified
[ ] Account type verified
[ ] Rules source verified
[ ] Rule version recorded
[ ] Effective date recorded
[ ] Drawdown methodology verified
[ ] Daily loss methodology verified
[ ] Position limits verified
[ ] Trading hours verified
[ ] Overnight rules verified
[ ] Weekend rules verified
[ ] News rules verified
[ ] Consistency rules verified
[ ] Payout rules documented
[ ] Automation restrictions verified
```

---

# 41. LIVE RULE MONITORING

The Rule Engine should continuously monitor:

* daily loss
* total drawdown
* trailing threshold
* position size
* leverage
* trading hours
* restricted events
* account state

Monitoring must be based on current account state.

---

# 42. PRE-TRADE CHECK

Before every new order:

```text
Strategy Signal
      ↓
Risk Check
      ↓
Prop Rule Check
      ↓
Execution Check
      ↓
Order
```

The trade must be rejected if a mandatory check fails.

---

# 43. POST-TRADE CHECK

After execution:

* update account state
* update drawdown
* update daily loss
* update exposure
* update rule metrics
* verify compliance
* persist result

---

# 44. PROJECT CONFIGURATION

Prop-firm configuration should be separated from source code.

Recommended structure:

```text
config/
    prop_firms/
        provider_a/
            account_type_a.yaml
            account_type_b.yaml
        provider_b/
            account_type_a.yaml
```

The exact implementation may evolve.

---

# 45. EXAMPLE CONFIGURATION

Illustrative only:

```yaml
provider: example_provider

account:
  type: example_100k
  currency: USD

rules:
  max_total_drawdown:
    value: 5000
    methodology: provider_defined

  daily_loss:
    enabled: true
    value: 2500
    methodology: provider_defined

  overnight:
    allowed: false

  weekend:
    allowed: false

  max_position_size:
    enabled: true

internal_safety:
  drawdown_buffer: 500
  daily_loss_buffer: 250
```

This example does not represent any actual provider.

---

# 46. PROP-SPECIFIC ADAPTATION

The framework should support differences between:

* futures prop firms
* forex/CFD prop firms
* equity prop programs
* evaluation accounts
* funded accounts

The underlying architecture should remain common.

Only the relevant rule configuration and adapters should differ.

---

# 47. STRATEGY INDEPENDENCE

Strategies must not contain code such as:

```python
if prop_firm == "...":
```

or:

```python
if daily_loss > ...:
```

unless the logic genuinely belongs to the strategy.

External constraints belong to the appropriate control layer.

---

# 48. RISK ENGINE INDEPENDENCE

The Risk Engine should receive rule constraints from the Prop Firm Rule Engine rather than hard-coding provider-specific logic.

Example:

```text
Prop Rule Engine
        ↓
Maximum Allowed Risk
        ↓
Risk Engine
        ↓
Approved Quantity
```

This keeps the architecture modular.

---

# 49. PROP-FIRM OPTIMIZATION WARNING

The system must not optimize a strategy solely to pass a specific evaluation.

Examples of undesirable behavior:

* increasing risk near a profit target
* deliberately concentrating profits
* changing strategy behavior only to satisfy consistency metrics
* exploiting rule quirks without economic justification
* increasing leverage because the account is near failure
* designing parameters specifically around one historical challenge path

The primary objective remains robust trading.

---

# 50. CHALLENGE-SPECIFIC RISK

If an evaluation account has unusual constraints, the system may create a dedicated account-risk profile.

However:

```text
Challenge-specific configuration
≠
Strategy logic
```

The same strategy should remain independently testable.

---

# 51. PAYOUT ANALYSIS

Where relevant, the system may model:

* profit target
* payout threshold
* payout frequency
* maximum withdrawal
* consistency conditions
* account reset behavior

These are account economics and must remain separate from strategy performance.

---

# 52. ACCOUNT ECONOMICS

The system should distinguish:

```text
Trading P&L
Broker Costs
Prop Fees
Evaluation Fees
Payouts
Other Account Costs
```

This allows analysis of actual economic performance.

---

# 53. PROP FIRM COMPARISON

The framework may support side-by-side factual comparison of account structures.

Examples:

| Dimension         | Provider A | Provider B |
| ----------------- | ---------- | ---------- |
| Account type      | Configured | Configured |
| Drawdown model    | Configured | Configured |
| Daily loss        | Configured | Configured |
| Position limit    | Configured | Configured |
| Overnight         | Configured | Configured |
| News restrictions | Configured | Configured |
| Consistency       | Configured | Configured |

The system must use verified current rules.

---

# 54. AUDIT TRAIL

Every prop-rule decision should be traceable.

Record:

```text
timestamp
account_id
rule_version
rule_id
input_state
decision
reason
affected_order
```

This allows post-event verification.

---

# 55. RULE ENGINE TESTING

Tests should include:

### Drawdown

* below threshold
* exactly at threshold
* above threshold

### Daily loss

* normal
* near limit
* exceeded

### Position

* permitted
* maximum
* exceeded

### Trading hours

* allowed
* prohibited
* boundary condition

### Overnight

* allowed
* prohibited
* cutoff transition

### Rule changes

* old version
* new version

### Unknown data

* missing account state
* missing rule
* unavailable provider information

---

# 56. FAIL-SAFE BEHAVIOR

When the system cannot reliably determine whether a trade is compliant:

```text
UNKNOWN
   ↓
BLOCK NEW TRADE
   ↓
ALERT
   ↓
RECONCILE
```

The system must never interpret missing information as permission.

---

# 57. RELATIONSHIP WITH RISK MANAGEMENT

The Prop Firm Rule Engine extends the project's risk architecture.

The hierarchy is:

```text
Strategy Risk
     ↓
Portfolio Risk
     ↓
Account Risk
     ↓
Prop Firm Constraint
     ↓
Execution Constraint
```

The most restrictive applicable limit should govern the trade.

---

# 58. RELATIONSHIP WITH EXECUTION

Execution must receive only orders that have passed:

```text
Strategy Validation
+
Risk Validation
+
Prop Rule Validation
+
Operational Validation
```

The Broker Adapter should not be responsible for interpreting prop-firm rules.

---

# 59. PRODUCTION AUTHORIZATION

A prop account can be activated only when:

```text
[ ] Rule profile verified
[ ] Rule version recorded
[ ] Account synchronized
[ ] Risk configuration approved
[ ] Prop simulator tested
```
