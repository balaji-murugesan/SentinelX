# Spec Delta

## ADDED Requirements

### Requirement: Set-valued limits declare a subset or superset direction
A limit whose value is a set of underlying symbols SHALL use the unit SYMBOLS and the direction `subset` (a set that keeps only some of the old members is tighter) or `superset` (a set that keeps every old member is tighter). Setting an unset `subset` limit SHALL count as tightening; removing it SHALL count as loosening. Symbols SHALL be compared case-insensitively.

#### Scenario: Shrinking an allow list mid-day
- **GIVEN** an allow list of {NIFTY, BANKNIFTY}
- **WHEN** at 11:00 IST an edit sets it to {NIFTY}
- **THEN** the edit is accepted

#### Scenario: Removing from a deny list mid-day
- **GIVEN** a deny list of {XYZ, ABC}
- **WHEN** at 11:00 IST an edit sets it to {XYZ}
- **THEN** the edit is rejected with `TIGHTEN_ONLY` naming the deny list

#### Scenario: Swapping members mid-day
- **GIVEN** an allow list of {NIFTY}
- **WHEN** at 11:00 IST an edit sets it to {BANKNIFTY}
- **THEN** the edit is rejected with `TIGHTEN_ONLY`

#### Scenario: Empty or malformed symbol
- **WHEN** an edit sets an allow list containing an empty string
- **THEN** the whole edit is rejected and the stored limits are unchanged
