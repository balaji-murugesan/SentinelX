# Spec Delta

## Purpose

Defines the day's profit-and-loss measure the risk engine watches and the equity floor, a level of that measure that only ever rises. Falling to the floor triggers the kill switch. This replaces a flat daily maximum loss and keeps the owner from giving back a day's gains unnoticed.

## ADDED Requirements

### Requirement: The day's P&L is net of charges
`MTM_day` SHALL be the account's P&L for the current trading day: realized P&L plus the P&L of open positions at current prices, minus the charges already paid on today's fills, minus the estimated charge to close each open position at its current price. All of it SHALL be computed by the engine with the same charge model as the paper broker, in exact decimals.

#### Scenario: Loss net of estimated exit charge
- **WHEN** an account holds only a carried long, no trade happened today, the position is down 500.00 from its previous close, and the estimated exit charge is 10.00
- **THEN** `MTM_day` is −510.00

#### Scenario: Charges paid today
- **WHEN** the day's only trade is a round trip with 200.00 gross profit and 60.00 of charges
- **THEN** `MTM_day` is 140.00

### Requirement: Carried positions are measured from the previous close
For the quantity a position held at the start of the day, the day's P&L SHALL count only the move since the previous close, not since entry. Quantity bought or sold today SHALL count from its fill prices. The previous-close reference SHALL be the engine's own last price for that position, recorded when the market closes, and SHALL be persisted. If a reference is missing the engine SHALL use the position's average price instead and record `RISK_REFERENCE_MISSING`.

#### Scenario: Carried position gaps down
- **WHEN** a carried long of 100 closed yesterday at 500.00 and trades at 495.00 today with no fills today
- **THEN** its contribution to `MTM_day` before charges is −500.00, whatever its entry price

#### Scenario: Reference missing
- **WHEN** the engine was down at yesterday's close and holds a carried position
- **THEN** the average price is the reference and `RISK_REFERENCE_MISSING` is recorded

### Requirement: The floor starts at the maximum loss and only rises
For each account and trading day the floor SHALL start at the negative of the configured daily maximum loss. The day's peak `MTM_day` SHALL start at zero and rise to each new higher value, using only fresh, non-outlier prices. The floor SHALL be the highest of its starting value and every configured term below, and SHALL NEVER decrease during the day, whatever path `MTM_day` takes or limit edits are made.

#### Scenario: Floor after a fall
- **WHEN** the maximum loss is 3,000, the trailing-loss term is 500 per 1,000 of peak, the peak reaches 3,000 and `MTM_day` then falls back to 500
- **THEN** the floor stays at −1,500, the level set at the peak

### Requirement: Floor terms are optional and off unless configured
Beyond the starting value, the floor MAY be raised by a trailing-loss term, a lock term, a trail term and a giveback term, each defined below. A term that is not configured SHALL have no effect. The floor SHALL be the highest of the starting value and every configured term.

#### Scenario: No terms configured
- **WHEN** only the maximum loss is configured and the peak reaches 5,000
- **THEN** the floor stays at the starting value

### Requirement: Trailing-loss term
The trailing-loss term SHALL raise the floor above its starting value by a configured amount for each configured amount of peak, counted in whole steps and only while the peak is positive.

#### Scenario: Trailing-loss term
- **WHEN** the maximum loss is 3,000, the trailing-loss term is 500 per 1,000 of peak, and the peak is 2,500
- **THEN** the floor is −2,000

### Requirement: Lock term
The lock term SHALL set the floor to a configured level once the peak reaches a configured trigger.

#### Scenario: Lock reached
- **WHEN** the lock level is 200, the trigger is 1,000 and the peak reaches 1,500
- **THEN** the floor is 200

### Requirement: Trail term
The trail term SHALL raise the floor by a configured amount for each configured step of peak, counted in whole steps.

#### Scenario: Trail steps
- **WHEN** the trail raises the floor by 500 for each 1,000 of peak and the peak is 2,500
- **THEN** the floor is 1,000

### Requirement: Giveback term
Once the peak reaches a configured arm level, the giveback term SHALL set the floor to the peak minus the larger of an absolute amount and a percentage of the peak.

#### Scenario: Giveback term
- **WHEN** the giveback is the larger of 1,000 and 30% of peak, it arms at 2,000, and the peak is 4,000
- **THEN** the floor is 2,800

### Requirement: Reaching the floor triggers the kill switch
When `MTM_day` is at or below the floor, the engine SHALL trigger the kill switch. The loss side SHALL NOT be debounced: one fresh reading at or below the floor is enough. The floor is a trigger, not a price guarantee.

#### Scenario: Breach
- **WHEN** the floor is −2,000 and an update puts `MTM_day` at −2,000
- **THEN** the kill switch is triggered

### Requirement: Entries must leave headroom above the floor
An entry SHALL be rejected with `RISK_FLOOR_HEADROOM` if `MTM_day` minus its per-trade risk would be at or below the floor.

#### Scenario: Not enough headroom
- **WHEN** `MTM_day` is −1,700, the floor is −2,000 and an entry risks 400
- **THEN** it is rejected with `RISK_FLOOR_HEADROOM`

### Requirement: Floor and peak are persisted before the engine acts
Changes to the floor and the peak SHALL be written to Postgres before the engine acts on them, as a write that can only raise the stored value, so a stale or out-of-order write can never lower it. On restart the engine SHALL resume with the higher of the stored value and the value recomputed from the day's fills and current prices. If the write fails the engine SHALL still act on a breach.

#### Scenario: Restart after a high peak
- **WHEN** the stored peak is 3,000 and a restart recomputes 1,200 from current prices
- **THEN** the engine resumes with a peak of 3,000

#### Scenario: Out-of-order write
- **WHEN** a delayed write carries a lower floor than the stored one
- **THEN** the stored floor is unchanged

#### Scenario: Persistence down at a breach
- **WHEN** the floor is breached while Postgres is unreachable
- **THEN** the kill switch still runs and the write is retried

### Requirement: A limits edit may not lower the floor
An edit to the floor's configured terms SHALL be rejected with `TIGHTEN_ONLY` during market hours if re-simulating the floor with the new values and today's peak gives a value below the current floor.

#### Scenario: Edit would lower the floor
- **WHEN** an edit removes the lock term after the lock has raised the floor
- **THEN** the edit is rejected with `TIGHTEN_ONLY`
