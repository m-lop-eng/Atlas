# 08_DEVELOPMENT_WORKFLOW.md

**Project:** Modular Multi-Market Algorithmic Trading System
**Document:** Development Workflow & Engineering Standards
**Version:** 1.0.0
**Status:** APPROVED FOR DEVELOPMENT
**Date:** 2026-09-18
**Authority:** Master Specification

---

# 1. PURPOSE

This document defines the software-development workflow for the trading system.

It establishes standards for:

* architecture changes
* coding
* repository management
* Git workflow
* testing
* AI-assisted development
* code review
* configuration management
* database changes
* releases
* deployment
* debugging
* documentation
* rollback

The objective is to ensure that the system remains:

* reproducible
* testable
* maintainable
* auditable
* modular
* secure
* scalable

The trading system must be developed as production-grade software, not as a collection of independent scripts.

---

# 2. DEVELOPMENT PRINCIPLES

Development follows these principles:

1. Correctness before speed.
2. Simplicity before unnecessary abstraction.
3. Tests before production deployment.
4. Small changes before large uncontrolled changes.
5. Reproducibility before convenience.
6. Explicit interfaces before hidden dependencies.
7. Documentation before irreversible architectural decisions.
8. Automation before repetitive manual procedures.
9. Security by default.
10. No unreviewed AI-generated code in production.

---

# 3. SOURCE CONTROL

Git is the authoritative source-control system.

All relevant source code must be version controlled.

This includes:

* Python code
* MQL5 code
* C# code
* configuration templates
* database migrations
* tests
* infrastructure definitions
* documentation
* research code where appropriate
* strategy definitions

---

# 4. REPOSITORY STRUCTURE

The repository should follow the architecture defined in:

`00_MASTER_SPECIFICATION.md`

Conceptually:

```text
trading_system/
├── config/
├── data/
├── research/
├── strategies/
├── backtesting/
├── validation/
├── portfolio/
├── risk/
├── execution/
├── brokers/
├── monitoring/
├── database/
├── reporting/
├── tests/
├── deployment/
├── docs/
├── README.md
├── pyproject.toml
└── docker-compose.yml
```

The structure may evolve, but architectural changes must be intentional.

---

# 5. BRANCHING MODEL

The project should use a controlled Git workflow.

Recommended branches:

```text
main
develop
feature/*
fix/*
research/*
release/*
hotfix/*
```

---

## 5.1 MAIN

`main` contains production-ready code.

Direct commits should normally be prohibited.

---

## 5.2 DEVELOP

`develop` contains integrated development code that has passed the appropriate automated tests.

---

## 5.3 FEATURE

Feature branches are used for new functionality.

Example:

```text
feature/data-validation
feature/risk-engine
feature/mt5-adapter
feature/backtest-engine
```

---

## 5.4 FIX

Used for non-critical corrections.

Example:

```text
fix/order-state-transition
fix/timezone-conversion
```

---

## 5.5 RESEARCH

Research branches may be used for experimental work.

Research code must not automatically become production code.

---

## 5.6 HOTFIX

Used for urgent production corrections.

Hotfixes must receive additional review after stabilization.

---

# 6. COMMIT PRINCIPLES

Commits should be:

* small
* focused
* understandable
* reproducible

Avoid commits that combine unrelated changes.

Bad:

```text
Update strategy + database + Docker + risk + UI
```

Preferred:

```text
Add order lifecycle state model
```

followed by:

```text
Add unit tests for order lifecycle
```

---

# 7. COMMIT MESSAGE FORMAT

Recommended format:

```text
<type>: <description>
```

Examples:

```text
feat: add futures contract metadata model
fix: prevent duplicate order submission
test: add risk sizing edge cases
refactor: isolate broker adapter interface
docs: update execution architecture
research: add breakout baseline experiment
```

---

# 8. ISSUE / TASK STRUCTURE

Every meaningful development task should contain:

```text
Task ID
Title
Objective
Context
Requirements
Constraints
Inputs
Expected outputs
Acceptance criteria
Tests required
Affected components
Risks
Dependencies
```

---

# 9. TASK SCOPE

Tasks should be sufficiently small to verify independently.

Avoid tasks such as:

> "Build the entire trading engine."

Prefer:

```text
Implement abstract Order model.
```

Then:

```text
Implement Order lifecycle state machine.
```

Then:

```text
Add persistence for Order state.
```

Then:

```text
Add broker adapter interface.
```

---

# 10. AI AGENT DEVELOPMENT

AI agents may be used extensively for development.

However, AI-generated output is treated as:

> proposed implementation

rather than:

> verified implementation

The human/project controller remains responsible for approving production behavior.

---

# 11. AI AGENT TASK CONTRACT

Every AI coding task should provide:

```text
Objective
Relevant architecture
Relevant files
Constraints
Interfaces
Expected behavior
Tests
Acceptance criteria
Non-goals
```

The agent should not be asked to infer critical architecture when the architecture has already been defined.

---

# 12. AI AGENT RULES

AI agents must:

1. Read relevant project documentation before modifying code.
2. Respect existing interfaces.
3. Avoid unnecessary architectural changes.
4. Explain material assumptions.
5. Add or update tests.
6. Avoid changing unrelated files.
7. Never introduce secrets.
8. Never disable risk controls to make tests pass.
9. Never optimize code by changing trading behavior without explicit authorization.
10. Report uncertainty.

---

# 13. AI AGENT OUTPUT FORMAT

For significant coding tasks, the agent should report:

```text
Summary
Files changed
Architecture impact
Implementation details
Tests added
Tests executed
Results
Known limitations
Potential risks
Next recommended step
```

---

# 14. NO SILENT ARCHITECTURAL CHANGES

An AI agent must not silently:

* change database schemas
* change risk formulas
* change position sizing
* change execution semantics
* modify strategy logic
* alter broker behavior
* change production configuration

without explicitly identifying the change.

---

# 15. CODE QUALITY

Production code should prioritize:

* readability
* explicit behavior
* type safety
* modularity
* testability
* observability
* deterministic behavior

Premature optimization should be avoided.

---

# 16. TYPE SAFETY

Python code should use type hints where practical.

Example:

```python
def calculate_position_size(
    equity: float,
    risk_fraction: float,
    stop_distance: float,
) -> float:
    ...
```

Types should communicate intent.

---

# 17. CONFIGURATION OVER HARD-CODING

Trading parameters should normally be configurable.

Avoid:

```python
risk = 0.0025
```

Prefer configuration such as:

```yaml
risk:
  default_risk_fraction: 0.0025
```

Critical production parameters must have explicit configuration and validation.

---

# 18. SECRETS MANAGEMENT

Secrets must never be committed to Git.

Examples:

* broker API keys
* passwords
* tokens
* account credentials
* database passwords

Use:

* environment variables
* secret managers
* protected deployment configuration

`.env.example` may contain variable names but never real secrets.

---

# 19. ENVIRONMENT CONFIGURATION

At minimum:

```text
config/
    development/
    paper/
    production/
```

Configuration should clearly identify its environment.

Production configuration must not accidentally be loaded during development.

---

# 20. DATABASE CHANGES

Database schema changes must use versioned migrations.

Never modify production databases manually without recording the change.

A migration should define:

* version
* purpose
* forward migration
* rollback strategy where practical
* affected tables
* compatibility considerations

---

# 21. BACKWARD COMPATIBILITY

When changing data structures, consider:

* existing historical data
* running services
* reports
* research notebooks
* strategy versions
* migrations

Breaking changes must be intentional.

---

# 22. TESTING PYRAMID

Testing should include:

```text
          End-to-End
         /           \
    Integration      System
       /                 \
    Unit Tests       Regression
```

Unit tests should form the largest base.

---

# 23. UNIT TESTS

Unit tests should cover isolated logic such as:

* position sizing
* risk calculations
* indicators
* signal generation
* order state transitions
* portfolio calculations
* drawdown calculations
* timestamp handling

---

# 24. INTEGRATION TESTS

Integration tests should verify interactions between components.

Examples:

```text
Strategy → Risk
Risk → Order Manager
Order Manager → Broker Adapter
Database → Order Manager
Data Engine → Strategy
```

---

# 25. EXECUTION TESTS

Execution tests should simulate:

* order rejection
* timeout
* partial fill
* delayed acknowledgement
* cancellation
* reconnect
* duplicate prevention
* state mismatch

---

# 26. BACKTEST REGRESSION TESTS

A change to the backtesting engine must not silently change historical results.

Maintain selected reference tests such as:

```text
Dataset version
Strategy version
Expected metrics
Expected trade count
Expected equity characteristics
```

Material deviations require investigation.

---

# 27. RISK REGRESSION TESTS

Risk calculations are critical.

Changes must be tested against known cases:

```text
Normal position
Large stop
Small stop
Insufficient capital
Daily loss near limit
Maximum drawdown
Multiple positions
Correlated exposure
```

---

# 28. STRATEGY REGRESSION TESTS

A strategy change should distinguish between:

* intentional behavior change
* accidental behavior change

Strategy versions must be explicit.

Example:

```text
STRAT-NQ-BREAKOUT-v1.2
```

---

# 29. DATA REGRESSION TESTS

Data transformations should be tested for:

* timestamps
* missing values
* duplicates
* OHLC integrity
* timezone conversion
* contract rollover
* corporate actions where applicable

---

# 30. CONTINUOUS INTEGRATION

The repository should eventually use CI to run:

* formatting
* linting
* type checking
* unit tests
* integration tests
* regression tests
* security checks

A pull request should not be merged when mandatory CI checks fail.

---

# 31. CODE REVIEW

Material production changes should be reviewed.

Review should examine:

* correctness
* architecture
* tests
* security
* performance
* maintainability
* trading impact
* risk impact

---

# 32. TRADING-LOGIC REVIEW

Any change affecting:

* entries
* exits
* position sizing
* stops
* portfolio allocation
* risk
* execution

requires explicit identification of the trading impact.

---

# 33. RESEARCH CODE VS PRODUCTION CODE

Research notebooks are not automatically production code.

Typical workflow:

```text
Notebook
   ↓
Research Finding
   ↓
Formal Specification
   ↓
Production Implementation
   ↓
Unit Tests
   ↓
Backtest
   ↓
Validation
```

---

# 34. NOTEBOOK RULES

Research notebooks should record:

* dataset version
* code version
* experiment ID
* parameters
* date
* outputs
* conclusions

Important findings should eventually be transferred into reproducible source code.

---

# 35. EXPERIMENT TRACKING

Every significant experiment should have an ID.

Example:

```text
EXP-20260918-001
```

Track:

```text
Experiment ID
Hypothesis
Dataset
Strategy version
Parameters
Code version
Metrics
Validation method
Result
Conclusion
```

---

# 36. NO MANUAL RESULT EDITING

Research results should be generated programmatically where possible.

Do not manually modify:

* performance metrics
* equity curves
* drawdown
* trade statistics
* validation results

If a result changes, the underlying calculation or dataset should explain why.

---

# 37. REPRODUCIBILITY

A historical experiment should be reproducible using:

* code version
* dataset version
* configuration
* random seeds where relevant
* dependency versions

The target is:

> Same inputs + same code + same configuration → same result within defined numerical tolerances.

---

# 38. RANDOMNESS

Where randomness is used:

* Monte Carlo
* stochastic optimization
* ML
* simulation

the random seed should be recorded.

Production systems should distinguish deterministic testing from stochastic research.

---

# 39. DEPENDENCY MANAGEMENT

Dependencies must be version controlled.

Avoid uncontrolled use of:

```text
latest
```

for production-critical packages.

Dependencies should be reviewed periodically for:

* security
* compatibility
* maintenance
* reproducibility

---

# 40. DOCKER

Docker should be used where it improves reproducibility and deployment consistency.

Potential services:

```text
PostgreSQL
Redis
Trading application
Research services
Monitoring
Supporting APIs
```

Not every component needs to be containerized.

---

# 41. DEVELOPMENT ENVIRONMENT

The recommended development stack is:

* Python
* Git
* PostgreSQL
* Docker
* VS Code or equivalent IDE
* Jupyter for research
* pytest
* static analysis/type checking
* Linux-compatible production environment

Additional technologies should be introduced only when justified.

---

# 42. MQL5 AND C#

MQL5 and C# should be used where their ecosystem provides a clear advantage.

Examples:

### MQL5

* MT5 Expert Advisors
* MT5-specific execution
* broker/platform integration

### C#

* futures infrastructure
* specialized execution components
* performance-sensitive services where justified

Python remains the central research and orchestration language unless a specific requirement dictates otherwise.

---

# 43. ARCHITECTURAL DECISION RECORDS

Important architecture decisions should be documented.

Example:

```text
ADR-001
Title: Python as central research/orchestration layer

Decision:
Use Python for research, backtesting, risk orchestration and portfolio logic.

Reason:
...
Alternatives:
...
Consequences:
...
```

---

# 44. WHEN TO CREATE AN ADR

Create an Architecture Decision Record when a decision affects:

* architecture
* technology selection
* database design
* execution architecture
* broker integration
* deployment
* risk architecture
* data architecture

---

# 45. CHANGE CLASSIFICATION

Changes should be classified.

## LEVEL 1 — LOW RISK

Examples:

* documentation
* formatting
* comments
* non-functional refactoring

---

## LEVEL 2 — MODERATE RISK

Examples:

* data pipeline changes
* monitoring changes
* non-critical infrastructure

---

## LEVEL 3 — HIGH RISK

Examples:

* risk calculations
* order management
* broker adapters
* position sizing
* strategy logic
* portfolio allocation

---

## LEVEL 4 — CRITICAL

Examples:

* live risk limits
* kill switches
* emergency execution
* production credentials
* global order controls

Level 4 changes require explicit authorization and extensive testing.

---

# 46. RELEASE PROCESS

A production release should follow:

```text
Development
 ↓
Tests
 ↓
Code Review
 ↓
Validation
 ↓
Release Candidate
 ↓
Paper
 ↓
Production Approval
 ↓
Deployment
 ↓
Monitoring
```

---

# 47. RELEASE VERSIONING

Use semantic versioning where appropriate:

```text
MAJOR.MINOR.PATCH
```

Example:

```text
1.4.2
```

Strategy versions should additionally be explicit.

---

# 48. DEPLOYMENT CHECKLIST

Before production:

```text
[ ] Git commit identified
[ ] Tests passing
[ ] Risk regression passing
[ ] Execution tests passing
[ ] Configuration reviewed
[ ] Secrets verified
[ ] Database migrations reviewed
[ ] Monitoring active
[ ] Rollback available
[ ] Paper validation complete
[ ] Strategy version identified
[ ] Production authorization obtained
```

---

# 49. DEPLOYMENT STRATEGY

Where practical, use controlled deployment.

Possible approaches:

* staged deployment
* canary strategy
* limited account exposure
* limited instrument exposure
* gradual activation

A new component should not immediately control the maximum available capital.

---

# 50. ROLLBACK

Every release must have a rollback path.

Rollback should restore:

* previous code
* previous configuration
* previous strategy version
* database compatibility where necessary

After rollback, the system must be reconciled with the broker.

---

# 51. DEBUGGING PROCESS

When an issue occurs:

```text
Observe
 ↓
Reproduce
 ↓
Isolate
 ↓
Identify root cause
 ↓
Implement fix
 ↓
Add regression test
 ↓
Validate
 ↓
Deploy
 ↓
Monitor
```

Do not fix symptoms without understanding the underlying cause.

---

# 52. PRODUCTION LOGGING

Logs should provide enough information to reconstruct important events.

Avoid logging sensitive credentials.

Important fields may include:

* timestamp
* component
* environment
* account
* strategy
* instrument
* event
* correlation ID
* order ID
* error
* state transition

---

# 53. CORRELATION IDS

A correlation ID should allow an event chain to be traced.

Example:

```text
Signal
→ Risk Decision
→ Order
→ Broker Request
→ Fill
```

All related events should share a traceable identifier.

---

# 54. ERROR HANDLING

Errors should be:

* classified
* logged
* observable
* recoverable where appropriate
* escalated when necessary

Avoid broad silent exception handling.

Bad:

```python
try:
    ...
except Exception:
    pass
```

This is prohibited in critical execution paths.

---

# 55. FAIL-SAFE DEVELOPMENT

When uncertain, production components should fail toward reduced exposure.

Examples:

```text
Risk unavailable
→ block new trades

Broker state unknown
→ block new trades

Market data stale
→ block affected strategy

Configuration invalid
→ do not start trading
```

---

# 56. SECURITY DEVELOPMENT

Development must include:

* dependency scanning
* secret detection
* access control
* least privilege
* secure configuration
* authentication
* encrypted communication

Security should be treated as part of system reliability.

---

# 57. DOCUMENTATION REQUIREMENTS

Every major module should document:

* purpose
* inputs
* outputs
* dependencies
* configuration
* failure modes
* tests

Public interfaces should be documented clearly.

---

# 58. DEFINITION OF DONE

A task is considered complete only when:

```text
[ ] Implementation complete
[ ] Tests added
[ ] Tests passing
[ ] Documentation updated
[ ] Configuration updated if required
[ ] No unintended architecture changes
[ ] No secrets introduced
[ ] Review completed
[ ] Acceptance criteria satisfied
```

For trading-critical components, additional validation is required.

---

# 59. TRADING-CRITICAL DEFINITION OF DONE

For changes affecting trading behavior:

```text
[ ] Unit tests
[ ] Integration tests
[ ] Regression tests
[ ] Backtest comparison
[ ] Risk impact reviewed
[ ] Execution impact reviewed
[ ] Validation impact reviewed
[ ] Strategy version updated if applicable
[ ] Documentation updated
```

---

# 60. AI-GENERATED CODE DEFINITION OF DONE

AI-generated code is not complete merely because it executes successfully.

It must:

```text
[ ] Match architecture
[ ] Have understandable behavior
[ ] Have tests
[ ] Avoid hidden assumptions
[ ] Avoid unnecessary complexity
[ ] Pass static checks
[ ] Pass relevant regression tests
[ ] Be reviewed
[ ] Have documented limitations
```

---

# 61. PROHIBITED DEVELOPMENT PRACTICES

The following are prohibited:

* committing secrets
* bypassing tests to deploy faster
* disabling risk checks to make a strategy work
* changing backtester behavior to improve historical results
* modifying historical results manually
* silently changing strategy logic
* deploying untested broker integrations
* using production credentials during development
* deleting audit logs to hide errors
* introducing unexplained dependencies
* merging critical changes without review

---

# 62. DEVELOPMENT METRICS

The project may track:

* test coverage
* defect rate
* deployment frequency
* failed deployments
* mean recovery time
* technical debt
* research-to-production time
* strategy validation cycle time

Metrics should support engineering decisions rather than become targets that encourage undesirable behavior.

---

# 63. TECHNICAL DEBT

Technical debt should be documented.

Each item should include:

```text
Description
Impact
Risk
Priority
Estimated effort
Proposed solution
```

Technical debt affecting:

* risk
* execution
* data integrity
* security

should receive higher priority.

---

# 64. PROJECT DEVELOPMENT ORDER

Development should broadly follow:

```text
1. Architecture
2. Data foundation
3. Database
4. Backtest engine
5. Research framework
6. Risk engine
7. Strategy framework
8. Validation framework
9. Paper execution
10. Broker adapters
11. Monitoring
12. Production deployment
13. Portfolio expansion
```

Do not begin with multiple live strategies.

---

# 65. MVP PRINCIPLE

The first working version should be deliberately small.

Recommended MVP:

```text
Python
+
PostgreSQL
+
Data Engine
+
Backtest Engine
+
Research Framework
+
Risk Engine
+
One Market
+
One Experimental Strategy
+
Paper Trading
+
Reporting
+
Tests
```

The MVP should prove the architecture before scaling it.

---

# 66. SCALING PRINCIPLE

New capabilities should be added incrementally:

```text
One market
 ↓
Multiple markets

One strategy
 ↓
Multiple strategies

One broker
 ↓
Multiple brokers

Paper
 ↓
Small live exposure
 ↓
Controlled scaling
```

Each expansion requires appropriate regression and validation.

---

# 67. DEVELOPMENT GOLDEN RULE

> Never allow development speed to bypass the controls that protect capital.

A feature that takes one additional week to implement correctly is preferable to a fast implementation that introduces uncontrolled financial risk.

---

# 68. FINAL PRINCIPLE

The development process exists to ensure that every transition:

```text
Idea
 ↓
Code
 ↓
Experiment
 ↓
Validation
 ↓
Paper
 ↓
Production
```

is controlled, reproducible, documented, and testable.

The objective is not simply to write code quickly.

The objective is to build a system whose behavior can be understood, tested, reproduced, monitored, and trusted before capital is exposed.
