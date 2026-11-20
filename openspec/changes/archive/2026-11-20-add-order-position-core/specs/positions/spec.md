# Spec Delta

## Purpose

Turns fills into positions with exact P&L, kept current on every tick by the engine (never computed by any client), as the basis for risk limits, exits and the dashboard.

## ADDED Requirements

### Requirement: Net position per account, instrument and product
The engine SHALL keep one position per account, instrument key and product type. A position holds the signed net quantity in units (positive long, negative short), its average open price, its realized P&L and its accumulated costs, all `Decimal`. A fill that increases the position SHALL update the quantity-weighted average price. A fill that reduces it SHALL realize P&L on the closed quantity at that average.

#### Scenario: Scale in then partly out
- **WHEN** an account buys 65 at 100.00, buys 65 at 110.00, then sells 65 at 120.00
- **THEN** the average is 105.00 after the second buy, the sell realizes (120.00 − 105.00) × 65 = 975.00 gross, and 65 remain long at 105.00

### Requirement: Flipping through zero
A fill larger than the open quantity on the opposite side SHALL close the position, realizing P&L on the closed part, and open the remainder in the new direction at the fill price.

#### Scenario: Long 65, sell 130
- **WHEN** an account long 65 at 100.00 sells 130 at 98.00
- **THEN** −130.00 is realized on the 65 closed, and the position is short 65 at 98.00

### Requirement: Mark-to-market on every tick
For every open position the engine SHALL recompute MTM P&L on each tick of its instrument. MTM is (last price − average price) × quantity for longs, and (average − last) × |quantity| for shorts. Net P&L is realized plus MTM minus costs. No client SHALL compute P&L.

#### Scenario: Short loses as price rises
- **WHEN** a position is short 65 at 200.00 and a tick arrives at 205.00
- **THEN** its MTM P&L is −325.00

### Requirement: Open positions stay subscribed
The engine SHALL keep every instrument with an open position subscribed on the feed under a positions owner. It SHALL release the subscription when the position is flat.

#### Scenario: Position closed
- **WHEN** a position returns to zero quantity
- **THEN** the positions owner releases that instrument's feed subscription, and other owners' subscriptions stay

### Requirement: Fills update positions exactly once
Each fill SHALL be applied to its position exactly once, in fill order. Applying fills SHALL be repeatable from the persisted fills, giving identical positions.

#### Scenario: Rebuild from fills
- **WHEN** positions are rebuilt from the persisted fills of the day
- **THEN** the rebuilt quantities, average prices, realized P&L and costs equal the persisted positions
