# Spec Delta

## Purpose

Caps how large the book can grow, measured in lots, per underlying, in total and in gross entries per day, so position size stays bounded even when every single trade passes its per-trade risk check.

## ADDED Requirements

### Requirement: Lot caps count F&O contract lots only
The lot caps SHALL count only instruments in an F&O (derivative) segment, decided from the instrument key's segment. An entry in a non-F&O instrument (an equity) SHALL NOT be judged by any lot cap, SHALL add no lots to the day's gross lots, and SHALL still count as an entry for the other entry counters. Non-F&O positions and orders SHALL count no lots, and a non-F&O instrument with no master record SHALL NOT cause a lot-cap refusal.

#### Scenario: Equity holdings do not count
- **GIVEN** `max_open_lots_total` = 8 and a carried CNC holding of 500 shares
- **WHEN** a 2-lot NIFTY option entry is submitted
- **THEN** it is not refused by any lot cap

#### Scenario: An equity entry is not judged
- **GIVEN** `max_gross_lots_per_day` = 20 and 20 lots allowed today
- **WHEN** an entry for 100 shares of an equity is allowed
- **THEN** no lot cap refuses it and the day's gross lots stay at 20

### Requirement: Lots are counted from the instrument master
An F&O order's or position's lots SHALL be its absolute quantity divided by the instrument's lot size from the instrument master. An F&O entry whose instrument, or any counted F&O instrument, has no master record SHALL be refused with the lot-cap code of the cap being checked.

#### Scenario: Lots of an option order
- **GIVEN** an option with a lot size of 75
- **WHEN** an entry for 150 units is judged
- **THEN** it counts as 2 lots

#### Scenario: Unknown instrument in the book
- **GIVEN** an open F&O position whose instrument is missing from the master
- **WHEN** an entry on the same underlying is judged
- **THEN** it is refused with `RISK_LOTS_UNDERLYING`

### Requirement: Open lots per underlying are capped, counting pending entries
An entry SHALL be refused with `RISK_LOTS_UNDERLYING` if the open lots on its underlying, plus the lots of every unfilled entry order on that underlying, plus its own lots, exceed `max_open_lots_per_underlying`. A pending entry SHALL reserve its lots until it reaches a final state.

#### Scenario: Pending entry reserves its lots
- **GIVEN** `max_open_lots_per_underlying` = 4 and a pending 3-lot NIFTY entry
- **WHEN** a 2-lot NIFTY entry is submitted
- **THEN** it is refused with `RISK_LOTS_UNDERLYING`

#### Scenario: Reservation released on rejection
- **GIVEN** the pending 3-lot entry has been rejected
- **WHEN** the 2-lot NIFTY entry is submitted again
- **THEN** it is not refused by the per-underlying lot cap

#### Scenario: Exactly at the cap
- **GIVEN** `max_open_lots_per_underlying` = 4 and 2 open NIFTY lots
- **WHEN** a 2-lot NIFTY entry is submitted
- **THEN** it is not refused by the per-underlying lot cap

### Requirement: Open lots in total are capped, counting pending entries
An entry SHALL be refused with `RISK_LOTS_TOTAL` if the open lots across all underlyings, plus the lots of every unfilled entry order, plus its own lots, exceed `max_open_lots_total`. Positions carried from earlier days SHALL count.

#### Scenario: Total cap across underlyings
- **GIVEN** `max_open_lots_total` = 8, 5 open NIFTY lots and 2 open BANKNIFTY lots
- **WHEN** a 2-lot FINNIFTY entry is submitted
- **THEN** it is refused with `RISK_LOTS_TOTAL`

### Requirement: Gross entry lots per day are capped
An entry SHALL be refused with `RISK_LOTS_DAY` if the lots of every entry allowed today plus its own lots exceed `max_gross_lots_per_day`. An entry's lots SHALL count from the moment the gate allows it, whether or not the broker takes it or it fills. The count SHALL be persisted before the entry is sent, only rise during the day, and survive a restart.

#### Scenario: Daily gross cap
- **GIVEN** `max_gross_lots_per_day` = 20 and 19 lots allowed today
- **WHEN** a 2-lot entry is submitted
- **THEN** it is refused with `RISK_LOTS_DAY`

#### Scenario: Count survives a restart
- **GIVEN** 12 lots allowed today
- **WHEN** the engine restarts on the same day
- **THEN** the day's gross lots are still 12

#### Scenario: Count cannot be saved
- **WHEN** an entry passes every rule but the new gross-lot count cannot be persisted
- **THEN** the entry is refused with `RISK_STATE_UNAVAILABLE` and the count is not raised

### Requirement: Lot caps never apply to exits
An exit SHALL NOT be refused by any lot cap and SHALL NOT add to the day's gross lots.

#### Scenario: Exit above the cap
- **GIVEN** the open lots already exceed a cap that was tightened mid-day
- **WHEN** an exit for part of the position is submitted
- **THEN** no lot cap refuses it and the day's gross lots are unchanged

### Requirement: Lot caps are required limits that tighten downward
`max_open_lots_per_underlying`, `max_open_lots_total` and `max_gross_lots_per_day` SHALL be required limits in unit LOTS with direction `down`. The per-underlying cap SHALL NOT exceed the total cap.

#### Scenario: Per-underlying above total
- **WHEN** an edit sets the per-underlying cap to 10 and the total cap to 8
- **THEN** the edit is rejected as inconsistent

#### Scenario: Lowering a cap mid-day
- **WHEN** at 11:00 IST an edit lowers `max_open_lots_total`
- **THEN** the edit is accepted

### Requirement: The day's gross lots are visible
The snapshot's risk block SHALL report the gross entry lots allowed today.

#### Scenario: Snapshot shows the count
- **GIVEN** 6 lots allowed today
- **WHEN** a snapshot is written
- **THEN** its risk block shows 6 gross entry lots
