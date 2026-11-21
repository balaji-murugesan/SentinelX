# Spec Delta

## Purpose

Stops trading for the day once the account has made its target, so a good day is banked instead of given back. The stop is an automatic risk halt: it flattens the book and locks entries until the next trading day.

## ADDED Requirements

### Requirement: The profit target is optional and off unless set
The daily profit target SHALL be an optional limit in rupees of `MTM_day`. While it is not set, no profit-target halt SHALL occur. When it is set, a confirmation count of at least 1 SHALL also be set, or the limits SHALL be rejected as inconsistent.

#### Scenario: No target set
- **WHEN** no daily profit target is set and `MTM_day` rises to any value
- **THEN** no profit-target halt occurs

#### Scenario: Target without a confirmation count
- **WHEN** an edit sets a daily profit target and no confirmation count
- **THEN** the edit is rejected and the stored limits are unchanged

### Requirement: The target is reached only after consecutive confirmations
The target SHALL count as reached only when `MTM_day` is at or above the target on the configured number of consecutive monitor passes. A pass on which `MTM_day` is below the target or unknown SHALL reset the count to zero. The count SHALL NOT survive a restart.

#### Scenario: Confirmed target
- **GIVEN** a target of ₹4,000 and a confirmation count of 2
- **WHEN** two consecutive passes see `MTM_day` at ₹4,050 and ₹4,010
- **THEN** the target is reached

#### Scenario: A dip resets the count
- **GIVEN** a target of ₹4,000 and a confirmation count of 2
- **WHEN** consecutive passes see ₹4,050, then ₹3,990, then ₹4,020
- **THEN** the target is not yet reached

#### Scenario: Unknown MTM resets the count
- **GIVEN** a confirmation count of 2 and one pass at or above the target
- **WHEN** the next pass has `MTM_day` unknown
- **THEN** the count returns to zero

### Requirement: Reaching the target locks the day at TARGET_HALT
When the target is reached, the engine SHALL persist the day's halt level as `TARGET_HALT` with the reason `RISK_TARGET_HALT` before acting, then cancel pending entries and flatten every open position through the kill switch's flattener. `TARGET_HALT` SHALL rank above the MTM-blind lock and below a kill.

#### Scenario: Target flattens the book
- **GIVEN** an open position and a confirmed target
- **WHEN** the monitor acts
- **THEN** the halt is stored, pending entries are cancelled and the position is exited

#### Scenario: A kill outranks the target
- **GIVEN** the day is locked at `TARGET_HALT`
- **WHEN** a kill is triggered
- **THEN** the day's halt level becomes the kill level

#### Scenario: A breach and the target together
- **WHEN** one pass sees both a floor breach and a confirmed target
- **THEN** the engine acts on the kill

### Requirement: The target halt holds for the rest of the day
After a target halt, every entry SHALL be refused with `RISK_HALTED_DAY` until the next IST trading date, even if `MTM_day` falls back below the target. Exits SHALL still be accepted. The halt SHALL survive a restart, and the recovery resume command SHALL NOT clear it.

#### Scenario: MTM falls back
- **GIVEN** a target halt at ₹4,000
- **WHEN** `MTM_day` falls to ₹2,500 and an entry arrives
- **THEN** the entry is refused with `RISK_HALTED_DAY`

#### Scenario: Restart keeps the halt
- **WHEN** the engine restarts on the same day after a target halt
- **THEN** entries are still refused and any open position is flattened after reconciliation

#### Scenario: Next trading day
- **WHEN** the first evaluation of the next IST date runs
- **THEN** entries are judged normally again

### Requirement: The target limits are tighten-only during market hours
During market hours, lowering the target, setting a target that was not set, or lowering the confirmation count SHALL be accepted as tightening. Raising the target, removing it, or raising the confirmation count SHALL be rejected with `TIGHTEN_ONLY`.

#### Scenario: Raising the target mid-day
- **WHEN** at 11:00 IST an edit raises the target from ₹4,000 to ₹6,000
- **THEN** the edit is rejected with `TIGHTEN_ONLY`

#### Scenario: Setting a target mid-day
- **WHEN** at 11:00 IST an edit sets a target where none was set
- **THEN** the edit is accepted

### Requirement: The profit-target state is visible
The snapshot's risk block SHALL report the `TARGET_HALT` level and its reason while the day is locked by the target.

#### Scenario: Snapshot after a target halt
- **WHEN** the day is locked by the target and a snapshot is written
- **THEN** its risk block shows halt level `TARGET_HALT` with reason `RISK_TARGET_HALT`
