# 06_EXECUTION_OPERATION.md

**Project:** Modular Multi-Market Algorithmic Trading System
**Document:** Execution & Operations Specification
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Date:** 2026-09-18
**Authority:** Master Specification

---

# 1. PURPOSE

This document defines the operational and execution requirements for transitioning the trading system from research and paper trading toward reliable production execution.

It governs:

* order generation
* order validation
* order management
* broker communication
* execution
* position synchronization
* state management
* reconnection
* duplicate-order prevention
* partial fills
* execution monitoring
* operational alerts
* incident handling
* kill switches
* recovery procedures
* paper/live separation

The primary objective is:

> Execute authorized trading decisions reliably while minimizing operational and execution risk.

Execution reliability is considered part of the trading system itself.

A profitable strategy is not production-ready if the system cannot execute and control it reliably.

---

# 2. EXECUTION PRINCIPLES

The execution architecture follows these principles:

1. Risk controls have authority over strategy signals.
2. Strategies do not communicate directly with brokers.
3. Every order must have a traceable origin.
4. Every order must have a unique identifier.
5. The system must prevent accidental duplicate orders.
6. Broker state must be reconciled with internal state.
7. Fail-safe behavior is preferred over uncontrolled execution.
8. Network failures must be expected.
9. Partial fills must be supported.
10. Restart recovery must be deterministic.
11. Paper and live environments must remain isolated.
12. Every material execution event must be logged.
13. No single software component should silently assume that another component is healthy.
14. The system must be able to stop opening new positions without necessarily closing existing positions.
15. Emergency shutdown procedures must be explicitly defined and tested.

---

# 3. EXECUTION ARCHITECTURE

The logical execution flow is:

```text
Market Data
    ↓
Strategy
    ↓
Signal
    ↓
Risk Engine
    ↓
Portfolio Constraints
    ↓
Order Manager
    ↓
Broker Adapter
    ↓
Broker / Exchange
    ↓
Execution Report
    ↓
Order Manager
    ↓
Position State
    ↓
Portfolio / Risk / Monitoring
```

No strategy should bypass this architecture.

---

# 4. COMPONENT RESPONSIBILITIES

## 4.1 Strategy Engine

Responsible for:

* generating trading signals
* defining intended position
* specifying entry logic
* specifying exit logic
* providing strategy metadata

The Strategy Engine must not:

* calculate final account-level risk limits
* directly submit broker orders
* bypass the Order Manager
* disable the Risk Engine

---

## 4.2 Risk Engine

Responsible for determining whether a proposed action is permitted.

It evaluates:

* position size
* stop distance
* account risk
* portfolio exposure
* daily loss
* drawdown
* leverage
* concentration
* strategy limits
* external constraints

The Risk Engine has veto authority.

---

## 4.3 Order Manager

The Order Manager is the central authority for order lifecycle management.

It is responsible for:

* creating orders
* validating order state
* assigning identifiers
* tracking status
* submitting orders
* handling acknowledgements
* processing fills
* handling cancellations
* handling replacements
* recovering state
* preventing duplicates

---

## 4.4 Broker Adapter

The Broker Adapter translates the internal order model into broker-specific API calls.

Examples:

```text
Internal Order
      ↓
MT5 Adapter
      ↓
MT5

Internal Order
      ↓
Futures Adapter
      ↓
Broker / Exchange

Internal Order
      ↓
IBKR Adapter
      ↓
Interactive Brokers
```

The strategy should not know which adapter is being used.

---

# 5. ABSTRACT ORDER MODEL

The system should maintain a broker-independent order representation.

Minimum fields:

```text
order_id
strategy_id
account_id
instrument
side
quantity
order_type
limit_price
stop_price
time_in_force
reduce_only
parent_order_id
client_order_id
broker_order_id
signal_id
risk_check_id
created_at
submitted_at
acknowledged_at
filled_at
cancelled_at
status
fill_quantity
average_fill_price
commission
slippage
error_code
```

Additional broker-specific fields may exist inside the adapter layer.

---

# 6. ORDER IDENTIFICATION

Every order must have a unique internal identifier.

Example:

```text
ORD-20260918-000001
```

A client-generated identifier should also be transmitted whenever the broker supports it.

The system should retain:

* internal order ID
* broker order ID
* client order ID
* parent order ID
* strategy ID
* signal ID

This enables complete traceability.

---

# 7. ORDER LIFECYCLE

A standard order lifecycle should support:

```text
CREATED
   ↓
RISK_CHECKED
   ↓
APPROVED
   ↓
SUBMITTING
   ↓
SUBMITTED
   ↓
ACKNOWLEDGED
   ↓
PARTIALLY_FILLED
   ↓
FILLED
```

Alternative terminal states include:

```text
CANCELLED
REJECTED
EXPIRED
FAILED
UNKNOWN
```

The `UNKNOWN` state is critical.

The system must never assume that an order failed merely because a network request timed out.

---

# 8. UNKNOWN ORDER STATE

A network timeout can produce an ambiguous condition:

```text
Application
   ↓
Submit Order
   ↓
Network interruption
   ↓
No response
```

The order may have:

* not reached the broker
* reached the broker but not been acknowledged
* been accepted
* been filled

Therefore:

> A timeout is not proof of order failure.

The system must reconcile broker state before resubmitting.

---

# 9. DUPLICATE ORDER PREVENTION

Duplicate orders are a critical operational risk.

Before submitting an order, the system should check:

* existing active orders
* recent submissions
* client order ID
* signal ID
* strategy state
* intended position
* broker state

A retry mechanism must not blindly submit the same order again.

---

# 10. IDEMPOTENCY

Where possible, order submission should be idempotent.

The system should be able to retry an operation without unintentionally creating additional exposure.

Idempotency mechanisms may include:

* unique client order IDs
* persistent order state
* broker-side identifiers
* transaction records
* reconciliation

---

# 11. POSITION MANAGEMENT

The system must maintain an internal representation of:

* position quantity
* average entry price
* current price
* unrealized P&L
* realized P&L
* stop
* target
* strategy ownership
* instrument
* account
* exposure
* timestamp

However:

> Broker/exchange position state is the external source of truth for actual live exposure.

Internal state must be reconciled against it.

---

# 12. POSITION RECONCILIATION

Reconciliation should compare:

```text
Internal Position
        VS
Broker Position
```

Differences may arise from:

* manual trading
* partial fills
* rejected orders
* external execution
* system restart
* API delays
* broker-side modifications
* corporate actions
* contract rollover

Any unexplained discrepancy must trigger an alert and potentially block new trading.

---

# 13. RESTART RECOVERY

The system must survive application restarts.

After restart:

```text
Start
 ↓
Load persisted state
 ↓
Connect to broker
 ↓
Retrieve open orders
 ↓
Retrieve positions
 ↓
Retrieve account state
 ↓
Reconcile
 ↓
Validate consistency
 ↓
Resume / Halt
```

The system must not immediately start trading after reconnecting.

---

# 14. RECOVERY STATES

Recommended operational states:

```text
STARTING
CONNECTING
SYNCHRONIZING
READY
TRADING
DEGRADED
HALTED
EMERGENCY
SHUTDOWN
```

---

## 14.1 STARTING

Application is initializing.

No orders may be submitted.

---

## 14.2 SYNCHRONIZING

Broker and internal state are being reconciled.

No new orders should normally be submitted.

---

## 14.3 READY

System has passed operational checks and is authorized to process signals.

---

## 14.4 DEGRADED

One or more non-critical systems are impaired.

Possible actions:

* reduce functionality
* block new entries
* continue managing existing positions
* increase monitoring

---

## 14.5 HALTED

New trading is disabled.

Existing positions may continue to be monitored and managed depending on the cause.

---

## 14.6 EMERGENCY

Critical failure or risk condition.

The system follows predefined emergency procedures.

---

# 15. MARKET DATA HEALTH

Before allowing strategy execution, the system should validate market data.

Checks may include:

* data freshness
* timestamp progression
* price validity
* bid/ask availability
* spread
* expected trading session
* connection status

Example:

```text
Last market update:
10 seconds ago

Expected:
< 2 seconds
```

Potential action:

```text
BLOCK_NEW_ENTRIES
```

---

# 16. STALE DATA

Stale data must not be treated as current data.

A strategy must not generate live orders from market information whose freshness exceeds its configured tolerance.

Tolerance should be strategy-specific.

---

# 17. SPREAD PROTECTION

The system may prevent entries when:

```text
Current Spread > Maximum Allowed Spread
```

The threshold should be configurable per:

* instrument
* strategy
* session
* account

Spread protection should be validated empirically where used as a strategy condition.

---

# 18. VOLATILITY PROTECTION

Operational protection may be applied during abnormal conditions.

Examples:

* extreme spread
* extreme price movement
* trading halt
* missing data
* abnormal tick frequency
* broker instability

These controls are operational safeguards and should remain distinct from strategy logic.

---

# 19. PARTIAL FILLS

The system must support partial execution.

Example:

```text
Requested:
10 contracts

Filled:
4 contracts

Remaining:
6 contracts
```

The Order Manager must maintain:

* requested quantity
* filled quantity
* remaining quantity
* average execution price

Risk calculations must account for actual exposure rather than requested exposure.

---

# 20. ORDER CANCELLATION

Cancellation requests must be tracked.

Possible lifecycle:

```text
CANCEL_REQUESTED
       ↓
CANCEL_ACKNOWLEDGED
       ↓
CANCELLED
```

The system must handle races such as:

```text
Cancel requested
      ↓
Order fills
      ↓
Cancel arrives too late
```

The resulting position must be reconciled.

---

# 21. ORDER MODIFICATION

Order modification may be implemented as:

* native modification
* cancel-and-replace
* new order with explicit linkage

Broker-specific behavior must remain inside the Broker Adapter.

---

# 22. STOP-LOSS MANAGEMENT

Stops are part of risk control.

Where a strategy requires a stop, the system should determine whether it is:

* broker-side
* exchange-side
* synthetic/internal

Broker-side protection may provide protection if the application disconnects, subject to broker/exchange behavior.

Synthetic stops depend on the application remaining operational.

The selected architecture must be explicitly documented.

---

# 23. TAKE-PROFIT MANAGEMENT

Take-profit orders should be managed using the same lifecycle controls as other orders.

The system must account for:

* partial fills
* cancellation
* replacement
* position changes
* restart recovery

---

# 24. BRACKET ORDERS

Where supported, bracket structures may contain:

```text
Entry
 ├── Stop Loss
 └── Take Profit
```

The system must understand parent-child relationships.

If the parent is partially filled, child quantities may need to be adjusted.

---

# 25. HEDGING AND NETTING

Broker/account structures may support:

* netting
* hedging
* multiple independent positions

The internal position model must explicitly support the relevant account mode.

The system must never assume that all brokers represent positions identically.

---

# 26. MANUAL INTERVENTION

Manual trading should generally be prohibited on accounts controlled exclusively by the algorithmic system.

If manual intervention is permitted, it must be explicitly detectable.

The system should detect unexpected:

* positions
* orders
* cancellations
* quantity changes

Unexpected external activity should trigger reconciliation.

---

# 27. BROKER CONNECTIVITY

Connectivity monitoring should include:

* connection status
* heartbeat
* API response time
* authentication status
* market-data connection
* order-entry connection
* rate-limit status where available

---

# 28. RECONNECTION POLICY

A standard sequence should be:

```text
Connection Lost
      ↓
Mark system DEGRADED
      ↓
Block new entries
      ↓
Attempt reconnect
      ↓
Synchronize broker state
      ↓
Reconcile positions/orders
      ↓
Validate risk
      ↓
Return to READY
```

The system must not resume unrestricted trading solely because network connectivity has returned.

---

# 29. RATE LIMITS

Broker and exchange APIs may impose rate limits.

The system must implement:

* request throttling
* retry policies
* exponential backoff where appropriate
* rate-limit monitoring
* request prioritization

Critical risk and execution requests should receive appropriate priority.

---

# 30. RETRY POLICY

Retries must be classified.

### Safe-to-retry

Operations known to be idempotent.

### Unsafe-to-retry

Operations where duplication could create exposure.

### Reconciliation-required

Operations whose final state is unknown.

Order submission should generally fall into the third category unless broker-side idempotency is guaranteed.

---

# 31. EXECUTION QUALITY

Live execution should be compared against expected execution.

Track:

* expected price
* actual price
* slippage
* spread
* latency
* fill time
* fill probability
* partial-fill rate

This creates an execution-quality dataset.

---

# 32. SLIPPAGE MONITORING

Monitor:

$$
Slippage =
ActualExecutionPrice - ReferencePrice
$$

The sign convention must be standardized internally.

Metrics should be available by:

* instrument
* strategy
* order type
* time of day
* market regime
* volatility
* broker
* account

---

# 33. LATENCY MONITORING

Track timestamps such as:

```text
Signal generated
Risk approved
Order created
Order submitted
Broker acknowledged
Order filled
```

Derived measurements may include:

```text
Signal → Order
Order → Broker
Broker → Fill
Signal → Fill
```

Latency should be monitored for degradation.

---

# 34. AUDIT LOG

Every important execution event must be recorded.

Examples:

```text
SIGNAL_CREATED
RISK_APPROVED
RISK_REJECTED
ORDER_CREATED
ORDER_SUBMITTED
ORDER_ACKNOWLEDGED
ORDER_PARTIALLY_FILLED
ORDER_FILLED
ORDER_CANCELLED
ORDER_REJECTED
POSITION_OPENED
POSITION_MODIFIED
POSITION_CLOSED
RECONCILIATION_STARTED
RECONCILIATION_FAILED
KILL_SWITCH_TRIGGERED
SYSTEM_RESTARTED
```

Logs must be timestamped.

---

# 35. EXECUTION TRACEABILITY

A complete chain should be reconstructable:

```text
Strategy
 ↓
Signal
 ↓
Risk Decision
 ↓
Order
 ↓
Broker Order
 ↓
Fill
 ↓
Position
 ↓
P&L
```

This chain is essential for debugging and auditability.

---

# 36. KILL SWITCHES

The system must implement multiple levels of emergency control.

## Level 1 — Strategy Kill

Stops one strategy.

## Level 2 — Instrument Kill

Stops trading one instrument.

## Level 3 — Market Kill

Stops trading an entire market.

## Level 4 — Account Kill

Stops trading one account.

## Level 5 — Global Kill

Stops all new trading activity.

---

# 37. KILL SWITCH TRIGGERS

Potential triggers include:

* daily loss limit
* maximum drawdown
* unexplained position
* broker-state mismatch
* stale data
* excessive spread
* abnormal execution
* repeated API failures
* duplicate-order detection
* system integrity failure
* risk-engine failure
* configuration mismatch
* unexpected manual intervention

---

# 38. OPEN POSITIONS DURING KILL

A kill switch must explicitly define what happens to existing positions.

Possible modes:

```text
BLOCK_NEW_ENTRIES
MANAGE_EXISTING
CANCEL_PENDING_ORDERS
CLOSE_POSITIONS
FULL_EMERGENCY_FLATTEN
```

The correct action depends on the trigger.

This behavior must never be ambiguous.

---

# 39. EMERGENCY FLATTEN

An emergency flatten procedure should be available where technically supported.

The procedure should:

1. stop new entries
2. identify all open positions
3. cancel relevant pending orders
4. submit closing orders
5. verify fills
6. reconcile final positions
7. alert the operator
8. persist the incident

Emergency flattening itself carries execution risk and must therefore be tested in paper/simulation environments.

---

# 40. MONITORING

The production dashboard should display at minimum:

### Account

* balance
* equity
* available margin
* drawdown
* daily P&L

### Positions

* instrument
* quantity
* direction
* entry
* current price
* unrealized P&L
* stop
* target

### Orders

* pending
* submitted
* partially filled
* rejected
* cancelled

### Risk

* current risk
* reserved risk
* daily loss
* portfolio exposure
* strategy exposure
* margin utilization

### Infrastructure

* broker connection
* data connection
* API latency
* CPU
* memory
* database
* process health

---

# 41. ALERT LEVELS

Alerts should be classified.

## INFO

Normal operational event.

## WARNING

Potential degradation requiring attention.

## CRITICAL

Risk or execution condition requiring immediate intervention.

## EMERGENCY

Potential uncontrolled exposure or major system failure.

---

# 42. ALERT EXAMPLES

### WARNING

```text
Market data latency above threshold.
```

### CRITICAL

```text
Internal position differs from broker position.
```

### EMERGENCY

```text
Risk Engine unavailable while live exposure exists.
```

---

# 43. HEARTBEATS

Critical services should expose health status.

Examples:

```text
Data Engine
Strategy Engine
Risk Engine
Order Manager
Broker Adapter
Database
Monitoring
```

A missing heartbeat should produce an appropriate operational state transition.

---

# 44. DATABASE PERSISTENCE

Critical execution state must be persisted.

At minimum:

* orders
* fills
* positions
* signals
* risk decisions
* execution events
* system states
* incidents

In-memory state alone is insufficient for production execution.

---

# 45. CLOCK SYNCHRONIZATION

System clocks should be synchronized.

Accurate timestamps are required for:

* market data
* order events
* latency measurements
* audit trails
* reconciliation
* incident analysis

---

# 46. CONFIGURATION MANAGEMENT

Production configuration must be explicit and version controlled.

Configuration should include:

* account
* broker
* instruments
* strategy activation
* risk limits
* execution parameters
* data sources
* monitoring thresholds

Secrets must not be committed to source control.

---

# 47. ENVIRONMENT SEPARATION

At minimum:

```text
DEVELOPMENT
PAPER
PRODUCTION
```

Each environment should have separate:

* credentials
* databases where appropriate
* configuration
* broker connections
* risk settings
* logs

Production credentials must never be used accidentally in development.

---

# 48. PAPER TRADING

Paper trading should reproduce the production architecture as closely as possible.

The objective is not simply to verify that the strategy generates signals.

It must test:

* data flow
* risk checks
* order lifecycle
* state management
* reconciliation
* monitoring
* alerts
* restart recovery
* execution assumptions

---

# 49. FORWARD VALIDATION

Paper/live-simulation performance should be compared against the validated research expectations.

Compare:

* trade frequency
* win rate
* average win
* average loss
* slippage
* execution latency
* drawdown
* exposure
* signal timing

Material deviations require investigation.

---

# 50. DEPLOYMENT CHECKLIST

Before production deployment:

```text
[ ] Strategy version identified
[ ] Data source verified
[ ] Broker adapter tested
[ ] Risk configuration verified
[ ] Account configuration verified
[ ] Position sizing verified
[ ] Kill switches tested
[ ] Order lifecycle tested
[ ] Reconciliation tested
[ ] Restart recovery tested
[ ] Alerts tested
[ ] Logging tested
[ ] Secrets configured securely
[ ] Paper validation completed
[ ] Backtest/OOS/WFO evidence available
[ ] Production configuration reviewed
[ ] Rollback procedure available
```

---

# 51. PRODUCTION STARTUP

Production startup should follow:

```text
Start application
      ↓
Validate configuration
      ↓
Validate secrets
      ↓
Connect to infrastructure
      ↓
Connect to broker
      ↓
Load state
      ↓
Synchronize
      ↓
Reconcile
      ↓
Validate risk
      ↓
Validate market data
      ↓
Enter READY state
      ↓
Enable strategy
```

No trading should occur before all mandatory checks pass.

---

# 52. GRADUAL DEPLOYMENT

New strategies should not immediately receive maximum intended capital.

Deployment should progress through controlled stages:

```text
Research
 ↓
Backtest
 ↓
OOS
 ↓
Walk-Forward
 ↓
Monte Carlo
 ↓
Paper
 ↓
Small Live Exposure
 ↓
Controlled Scaling
```

Scaling requires evidence.

---

# 53. ROLLBACK

Every production release must have a rollback mechanism.

Rollback may involve:

* previous software version
* previous strategy version
* previous configuration
* disabling a strategy
* disabling an instrument
* stopping the system

The rollback procedure must be documented before deployment.

---

# 54. INCIDENT MANAGEMENT

Every material incident should record:

```text
Incident ID
Date/time
Environment
Affected account
Affected strategy
Affected instrument
Description
Detection method
Initial impact
Root cause
Immediate action
Final resolution
Preventive action
Required revalidation
```

---

# 55. POST-INCIDENT REVIEW

After a significant incident:

1. Preserve logs.
2. Preserve relevant database state.
3. Identify the sequence of events.
4. Determine root cause.
5. Determine whether risk controls worked.
6. Determine whether the incident could have been detected earlier.
7. Implement corrective action.
8. Add regression tests where appropriate.
9. Revalidate affected strategies or components.
10. Document the result.

---

# 56. MANUAL EMERGENCY PROCEDURES

The project should maintain documented procedures for:

* broker outage
* internet outage
* VPS failure
* database failure
* API failure
* incorrect position
* stuck order
* excessive loss
* unexpected market behavior
* software crash

The objective is to ensure that an operator does not need to improvise during a critical event.

---

# 57. OPERATIONAL TESTING

Before live deployment, simulate:

### Connectivity

* broker disconnect
* data disconnect
* reconnect

### Orders

* rejected order
* timeout
* duplicate submission
* partial fill
* delayed fill
* cancellation race

### State

* application restart
* database restart
* broker state mismatch

### Risk

* daily loss limit
* maximum drawdown
* excessive exposure
* risk engine unavailable

### Infrastructure

* CPU saturation
* memory pressure
* database unavailable
* network interruption

---

# 58. FAILURE MODE PRINCIPLE

The system should be designed around the assumption that failures will occur.

The objective is not:

> "Prevent every failure."

The objective is:

> "Ensure that failures result in controlled and observable behavior."

---

# 59. EXECUTION INTEGRITY RULES

The system must never:

* submit orders without passing required risk checks
* trade while broker state is unknown
* blindly retry uncertain order submissions
* ignore position discrepancies
* silently change live configuration
* continue unrestricted trading after a critical system failure
* assume a network timeout means an order was rejected
* assume an acknowledgement means the order was filled
* assume an internal position equals actual broker exposure

---

# 60. OPERATIONAL SLOs

As the system matures, operational targets should be defined for:

* system uptime
* data freshness
* order latency
* broker connectivity
* reconciliation time
* alert delivery
* recovery time

These targets should be based on the requirements of the actual strategies rather than arbitrary values.

---

# 61. SECURITY

Production execution must protect:

* broker credentials
* API keys
* account identifiers
* database credentials
* infrastructure credentials

Security controls should include:

* secret management
* least privilege
* credential rotation
* encrypted communication
* restricted access
* audit logs

Credentials must never be hard-coded into source code.

---

# 62. EXECUTION DATASET

The system should maintain an execution dataset containing:

* intended order
* actual order
* expected price
* actual price
* latency
* spread
* slippage
* fill ratio
* commissions
* market conditions

This dataset can later be used to improve execution models.

---

# 63. EXECUTION MODEL IMPROVEMENT

Execution improvements should follow the same research discipline as trading strategies.

For example:

```text
Observation:
Slippage increases during a specific condition.

↓
Hypothesis:
Alternative execution logic may reduce slippage.

↓
Experiment:
Compare execution methods.

↓
Validation:
Evaluate out-of-sample.

↓
Deployment:
Only if improvement is robust.
```

Execution optimization must not compromise risk controls.

---

# 64. RELATIONSHIP WITH RISK MANAGEMENT

Execution and risk are tightly coupled.

For example:

```text
Expected Risk
+
Expected Slippage
+
Commission
+
Liquidity Risk
```

must be considered before approving an order where appropriate.

The Risk Engine remains the authority for whether the order is permitted.

---

# 65. RELATIONSHIP WITH STRATEGIES

Strategies provide intent.

Execution determines implementation.

Therefore:

```text
Strategy:
"I want long exposure."

Risk:
"Exposure is permitted."

Order Manager:
"This is the required order."

Broker Adapter:
"This is how the broker accepts that order."

Execution:
"This is what actually happened."
```

The system must preserve this separation.

---

# 66. LIVE TRADING AUTHORIZATION

A strategy may trade live only if all required conditions are satisfied:

```text
Strategy validation PASS
        AND
Risk configuration valid
        AND
Execution infrastructure healthy
        AND
Broker connected
        AND
Market data healthy
        AND
Account synchronized
        AND
No active kill condition
        AND
Production authorization enabled
```

If any mandatory condition fails, live entry must be blocked.

---

# 67. GOLDEN EXECUTION RULE

> Never assume an order was executed. Verify it.

The system must distinguish between:

* intention
* submission
* acknowledgement
* fill
* position

Each represents a different state.

---

# 68. FINAL OPERATIONAL PRINCIPLE

The execution system must be designed so that:

**strategy failure is controlled by risk,**

**execution failure is controlled by state management,**

**infrastructure failure is controlled by fail-safe behavior,**

**and uncertainty is controlled by reconciliation.**

The production system should always prefer:

**known state + controlled exposure**

over:

**unknown state + continued trading.**
