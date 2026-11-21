# Spec Delta

## ADDED Requirements

### Requirement: A rejection missed while the engine was down is applied at recovery
During restart recovery, and again when the resume command re-checks, an order the engine holds `Open` with nothing filled that the broker's order book reports `Rejected` SHALL be moved to `Rejected` with the reason "broker reports Rejected" and stored, before any difference is computed. It SHALL NOT be reported as a difference and SHALL NOT count as a broker rejection for the breaker. Every other difference SHALL still halt as before.

#### Scenario: Stop rejected while the engine was down
- **GIVEN** the engine stored a catastrophic stop as `Open`, then stopped
- **WHEN** the broker rejected the stop while the engine was down, and the engine restarts in market hours with an otherwise matching book
- **THEN** the stop is `Rejected` with reason "broker reports Rejected", the engine is not halted, the breaker has counted no rejection, and the position is treated as unprotected

#### Scenario: Resume clears a halt left by a missed rejection
- **GIVEN** a stored recovery halt whose only difference is an `Open` order the broker reports `Rejected` with nothing filled
- **WHEN** the resume command runs
- **THEN** the order is stored as `Rejected` and the halt is cleared

#### Scenario: Other differences still halt
- **WHEN** at recovery the broker's order book reports `Cancelled` for an order the engine holds `Open`
- **THEN** the engine halts and lists the difference, as before
