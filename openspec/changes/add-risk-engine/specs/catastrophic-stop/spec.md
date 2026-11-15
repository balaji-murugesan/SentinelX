# Spec Delta

## Purpose

Defines the wide stop-loss resting at the exchange for every open position. The trailing stop lives only in the engine, so this order is the worst-case backstop if the engine, the network or the feed fails. It is not the trade's real stop and it is never modified tick by tick.

## ADDED Requirements

### Requirement: Every open position has one stop sized to it
When an entry fills and for as long as a position is open, the engine SHALL keep one resting stop-market order for the position's full quantity, on the opposite side, in the same product. The stop SHALL be placed after the fill, never before. Its trigger SHALL be the average entry price moved against the position by the configured catastrophic percentage, rounded to the tick grid toward the entry price so it is never wider than configured.

#### Scenario: Stop after entry
- **WHEN** a buy of 65 fills at 200.00 and the catastrophic percentage is 20%
- **THEN** a sell stop-market for 65 is placed with trigger 160.00

#### Scenario: Short position
- **WHEN** a sell entry of 100 fills at 500.00 and the catastrophic percentage is 10%
- **THEN** a buy stop-market for 100 is placed with trigger 550.00

### Requirement: Resizing never leaves the position unprotected
When a position's quantity changes while it stays open, the engine SHALL place a new stop for the new quantity first and cancel the old stop only after the new one is open. The stop SHALL change only on fills, never on ticks.

#### Scenario: Scale in
- **WHEN** a position of 65 with a stop for 65 buys 65 more
- **THEN** a stop for 130 is placed, and the stop for 65 is cancelled afterwards

### Requirement: A stop never rests on a flat book
When a position becomes flat by any route, the engine SHALL cancel its stop before any other action on that position. A resting stop on a flat book would open a new position if it triggered.

#### Scenario: Position closed by an exit
- **WHEN** an exit fills and the position becomes flat
- **THEN** its stop is cancelled

### Requirement: Stops pass the gate as exits
A stop SHALL go through the same gate as every order and be classified as an exit. It SHALL use the exit priority of the rate limiter and SHALL NOT be blocked by entry limits, a halt or a day lock.

#### Scenario: Stop on a halted day
- **WHEN** the account is HALTED and a stop must be re-sized
- **THEN** the stop order is accepted

### Requirement: Carried positions get a fresh stop each morning
A stop is valid for one trading day, so a position carried overnight has none at the next open. After reconciliation at engine start, and again at the start of each trading day, the engine SHALL place a stop for every open position that lacks one, `CNC` and `MARGIN` included. Between the close and that placement the carried position has no exchange-side protection; this is accepted.

#### Scenario: Restart with an unprotected position
- **WHEN** the engine restarts with an open position whose stop was lost and reconciliation is clean
- **THEN** a stop is placed for it before new entries are accepted

#### Scenario: Carried CNC holding at the open
- **WHEN** a new trading day begins with a carried `CNC` holding
- **THEN** a stop is placed for the holding

### Requirement: An unprotected position blocks new entries
If a stop cannot be placed, the engine SHALL log a critical alert, treat the position as unprotected, reject new entries until every open position has a stop, and keep retrying with backoff. Exits are never blocked.

#### Scenario: Broker rejects the stop
- **WHEN** the broker rejects a stop order
- **THEN** a critical alert is logged, entries are rejected, and the stop is retried

### Requirement: Stop-market orders in paper mode
The paper broker SHALL accept stop-market orders. A resting sell stop SHALL trigger when a tick trades at or below its trigger, and a buy stop when a tick trades at or above it. Once triggered it SHALL fill like a market order: at the latest price moved against the trader by the configured slippage, which may be worse than the trigger after a gap.

#### Scenario: Gap through the stop
- **WHEN** a sell stop has trigger 160.00 and the next tick is 150.00
- **THEN** it fills near 150.00 (less slippage), not at 160.00
