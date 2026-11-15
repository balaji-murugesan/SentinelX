# Spec Delta

## Purpose

Defines where risk limits live, how they are changed, and the rule that during market hours they can only get tighter. Limits are configuration the owner sets; the engine never invents them and never lets a mid-day edit loosen protection.

## ADDED Requirements

### Requirement: Limits are stored in Postgres with no built-in defaults
The risk limits for an account SHALL be stored in Postgres and loaded at engine start. The engine SHALL NOT supply a default value for any limit. While no limits are stored, every entry SHALL be rejected with `RISK_LIMITS_NOT_SET` and exits SHALL still be accepted.

#### Scenario: Fresh install
- **WHEN** the engine starts with no stored limits and an entry arrives
- **THEN** the entry is rejected with `RISK_LIMITS_NOT_SET`

#### Scenario: Limits survive restart
- **WHEN** limits are set and the engine restarts
- **THEN** the same limits apply without being set again

### Requirement: Every limit declares a unit and a tightening direction
Each limit SHALL declare a unit (INR, INR_PER_LOT, PCT, LOTS, SECONDS, COUNT, TIME_OF_DAY, ENUM or BOOL) and a tightening direction: `up` (a larger value is tighter), `down` (a smaller value is tighter), `enable_only` (turning it on is tightening) or `enum_order` (a declared ranking of values). An edit SHALL be rejected if a value is outside its unit's valid range.

#### Scenario: Out-of-range value
- **WHEN** an edit sets a percentage limit to a negative number
- **THEN** the whole edit is rejected and the stored limits are unchanged

### Requirement: Limits change only through a validated edit command
Limits SHALL be changed only by a limit-edit command that arrives like any other command and carries an idempotency key. The command SHALL be applied completely or not at all. The engine-owned state (the equity floor, the day's peak, halt state and the day's counters) SHALL NOT be editable through it or through any other command.

#### Scenario: Repeated edit
- **WHEN** the same edit command key arrives twice
- **THEN** the second returns the first result and changes nothing

#### Scenario: Attempt to set the floor
- **WHEN** an edit tries to set the equity floor or the day's peak directly
- **THEN** the edit is rejected as an unknown limit

### Requirement: Tighten-only during market hours
Between 09:15 and 15:30 IST, an edit SHALL be accepted only if every changed limit moves in its declared tightening direction. A rejected edit SHALL carry reason code `TIGHTEN_ONLY` and name each offending limit. Outside those hours any valid edit SHALL be accepted. Setting limits for the first time SHALL be accepted at any hour, because there is nothing to loosen.

#### Scenario: Loosening mid-day
- **WHEN** at 11:00 IST an edit raises the per-trade maximum risk
- **THEN** the edit is rejected with `TIGHTEN_ONLY` naming that limit

#### Scenario: Tightening mid-day
- **WHEN** at 11:00 IST an edit lowers the per-trade maximum risk
- **THEN** the edit is accepted and applies to the next order

#### Scenario: Loosening after the close
- **WHEN** at 16:00 IST an edit raises the per-trade maximum risk
- **THEN** the edit is accepted

### Requirement: Inconsistent limit sets are rejected
An edit SHALL be rejected if the resulting set is inconsistent: the entry cutoff is not earlier than the own square-off time; the stale, feed-down and MTM-blind thresholds are not in increasing order; or the exit price buffer is wider than the price-sanity band.

#### Scenario: Buffer wider than the band
- **WHEN** an edit sets the exit buffer to 3% and the price-sanity band to 2%
- **THEN** the edit is rejected, because the kill switch's own exits would fail price sanity

### Requirement: Limit edits are audited
Every accepted or rejected edit SHALL append a risk record carrying the old values, the new values and the outcome.

#### Scenario: Rejected edit is recorded
- **WHEN** an edit is rejected with `TIGHTEN_ONLY`
- **THEN** a record shows the attempted values and the reason code
