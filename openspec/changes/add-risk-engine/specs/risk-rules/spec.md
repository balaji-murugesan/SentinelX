# Spec Delta

## Purpose

Defines the pre-trade rules that decide whether a new entry may be sent, and the scheduled square-off of intraday positions. These are the tier-A rules of PRD §7. Every limit named here is a configured limit (see risk-limits), never a built-in value.

## ADDED Requirements

### Requirement: Every entry carries an initial stop on the loss side
An entry order SHALL carry an initial stop price. The stop SHALL be below the entry price for a buy and above it for a sell, on the instrument's tick grid. An entry without a valid stop SHALL be rejected with `RISK_NO_STOP`. The entry price is the limit price for a limit order and the latest price for a paper market order.

#### Scenario: Missing stop
- **WHEN** an entry arrives with no initial stop
- **THEN** it is rejected with `RISK_NO_STOP`

#### Scenario: Stop on the wrong side
- **WHEN** a buy at 200.00 carries a stop of 205.00
- **THEN** it is rejected with `RISK_NO_STOP`

### Requirement: Per-trade maximum risk
An entry SHALL be rejected with `RISK_PER_TRADE` if its risk, the distance between entry price and initial stop multiplied by the quantity in units, exceeds the per-trade maximum risk.

#### Scenario: Risk above the limit
- **WHEN** the limit is 1,000 and a buy of 65 units at 200.00 has a stop at 180.00
- **THEN** its risk is 1,300.00 and it is rejected with `RISK_PER_TRADE`

#### Scenario: Risk within the limit
- **WHEN** the same buy has a stop at 190.00
- **THEN** its risk is 650.00 and this rule allows it

### Requirement: Initial stop inside the catastrophic stop
An entry SHALL be rejected with `RISK_STOP_BEYOND_CATASTROPHIC` if its initial stop is as far from the entry price as the catastrophic stop would be, or farther. The catastrophic stop must always be the wider backstop.

#### Scenario: Stop wider than the backstop
- **WHEN** the catastrophic distance is 20% and a buy at 200.00 has a stop at 150.00
- **THEN** it is rejected with `RISK_STOP_BEYOND_CATASTROPHIC`

### Requirement: Position sizing rounds down to whole lots
A sizing request, given an instrument, side, entry price and initial stop, SHALL return the largest whole number of lots whose risk does not exceed the per-trade maximum, in units. It SHALL change no state. If that is fewer than one lot it SHALL return a rejection with `RISK_SIZE_BELOW_ONE_LOT`. For instruments with lot size 1 (cash equity) the size is the whole number of shares.

#### Scenario: Round down
- **WHEN** the limit is 1,000, the lot is 65 and the risk per unit is 10.00
- **THEN** the size is 65 units (1 lot, risk 650.00), because 2 lots would risk 1,300.00

#### Scenario: Less than a lot
- **WHEN** the risk of one lot already exceeds the limit
- **THEN** the request is rejected with `RISK_SIZE_BELOW_ONE_LOT`

### Requirement: Maximum trades per day
The engine SHALL count each entry the gate allows, and reject further entries with `RISK_MAX_TRADES` once the count reaches the daily maximum. The count SHALL be persisted before the entry is sent, SHALL survive a restart, and SHALL reset on the next trading day. An entry the broker later rejects still counts.

#### Scenario: Limit reached
- **WHEN** the maximum is 10 and ten entries were allowed today
- **THEN** the eleventh entry is rejected with `RISK_MAX_TRADES`

#### Scenario: Count after a crash
- **WHEN** seven entries were allowed and the engine is killed and restarted
- **THEN** three more entries are allowed today, not ten

### Requirement: Consecutive-loss cooldown
A closed trade is a position that returned to flat. It is a loss if its P&L net of charges is below zero. After the configured number of losses in a row the engine SHALL reject entries with `RISK_COOLDOWN` until the cooldown period has passed. A winning trade SHALL reset the streak. The streak and the cooldown end time SHALL be persisted. Exits are unaffected.

#### Scenario: Third loss in a row
- **WHEN** the limit is 3 losses with a 30-minute cooldown and the third consecutive loss closes at 10:00
- **THEN** entries are rejected with `RISK_COOLDOWN` until 10:30

#### Scenario: A win in between
- **WHEN** two losses are followed by a win and then a loss
- **THEN** the streak is 1 and no cooldown applies

### Requirement: Maximum open positions
An entry that would open a position in a new instrument SHALL be rejected with `RISK_OPEN_POSITIONS` if the number of open positions plus unfilled entry orders in other instruments has reached the total limit, or with `RISK_OPEN_POSITIONS_UNDERLYING` for the per-underlying limit. Carried positions count. Adding to a position already held does not count as a new one.

#### Scenario: Total reached
- **WHEN** the total limit is 5 and five positions are open
- **THEN** an entry in a sixth instrument is rejected with `RISK_OPEN_POSITIONS`

#### Scenario: Carried position counts
- **WHEN** one of the five positions is a `CNC` holding carried from yesterday
- **THEN** it counts toward the limit of 5

#### Scenario: Two entries in flight
- **WHEN** an entry is open but unfilled and the limit would be exceeded by a second entry in another instrument
- **THEN** the second entry is rejected

### Requirement: Entry cutoff
An entry SHALL be rejected with `RISK_ENTRY_CUTOFF` at or after the configured cutoff time in IST.

#### Scenario: After the cutoff
- **WHEN** the cutoff is 15:00 and an entry arrives at 15:01 IST
- **THEN** it is rejected with `RISK_ENTRY_CUTOFF`

### Requirement: Price sanity
A limit order SHALL be rejected with `RISK_PRICE_SANITY` if its price is more than the configured percentage away from the latest price. An entry with no fresh latest price is rejected by the stale-data guard. An exit with no fresh latest price SHALL be accepted and the skipped check audited.

#### Scenario: Fat finger
- **WHEN** the band is 2% and a buy limit arrives 5% above the latest price
- **THEN** it is rejected with `RISK_PRICE_SANITY`

#### Scenario: Exit without a fresh price
- **WHEN** an exit arrives and the instrument has had no recent tick
- **THEN** it is accepted and an audit record notes the price could not be verified

### Requirement: Own square-off of intraday positions
At the configured square-off time the engine SHALL exit every open position whose product is `INTRADAY`, using the same flatten as the kill switch. Positions held under `CNC` or `MARGIN` SHALL NOT be squared off. A square-off SHALL NOT lock the day.

#### Scenario: Mixed book at square-off
- **WHEN** the square-off time arrives with one `INTRADAY` position and one `CNC` position open
- **THEN** only the `INTRADAY` position is exited and the `CNC` position and its stop remain

#### Scenario: Not a halt
- **WHEN** a square-off completes
- **THEN** the account is not HALTED and no broker lock is requested
