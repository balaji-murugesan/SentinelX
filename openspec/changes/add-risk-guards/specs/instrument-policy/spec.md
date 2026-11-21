# Spec Delta

## Purpose

Decides which instruments may be entered at all: the underlyings the owner allows or denies, the exchange's F&O ban list, how far from the money an option may be, and how cheap it may be. It never stands in the way of an exit.

## ADDED Requirements

### Requirement: Allow and deny lists of underlyings
`allowed_underlyings` and `denied_underlyings` SHALL be optional set limits of underlying symbols. An entry SHALL be refused with `RISK_DENIED` if its underlying is in the deny list, otherwise with `RISK_NOT_ALLOWED` if an allow list is set and its underlying is not in it. An unset allow list SHALL allow every underlying.

#### Scenario: Not on the allow list
- **GIVEN** `allowed_underlyings` = {NIFTY, BANKNIFTY}
- **WHEN** an entry in a FINNIFTY option arrives
- **THEN** it is refused with `RISK_NOT_ALLOWED`

#### Scenario: Deny wins over allow
- **GIVEN** NIFTY is in both lists
- **WHEN** an entry in a NIFTY option arrives
- **THEN** it is refused with `RISK_DENIED`

### Requirement: The F&O ban list is loaded by the owner for each trading day
The owner SHALL load the ban list for an IST date through a ban-list command, sent from the CLI with a file of underlying symbols. The command SHALL carry an idempotency key. It SHALL be refused if the date is earlier than today. A command with a date earlier than today, a blank symbol, or another account's id SHALL be refused with `RISK_BAN_LIST_INVALID`. An empty list SHALL be accepted as "no bans that day". The list SHALL be stored and survive a restart.

#### Scenario: Loading tomorrow's list in the evening
- **WHEN** at 20:00 IST the owner loads a list dated the next trading day
- **THEN** it is stored and applies from that date

#### Scenario: Past date
- **WHEN** a ban-list command names yesterday's date
- **THEN** it is refused with `RISK_BAN_LIST_INVALID` and nothing is stored

#### Scenario: Blank symbol
- **WHEN** a ban-list command holds a symbol that is blank
- **THEN** it is refused with `RISK_BAN_LIST_INVALID` and nothing is stored

#### Scenario: Repeated command
- **WHEN** the same ban-list command key arrives twice
- **THEN** the second returns the first result and changes nothing

### Requirement: Today's ban list only grows during market hours
Between 09:15 and 15:30 IST, a ban-list command for today SHALL be refused with `TIGHTEN_ONLY` if it leaves out any symbol already loaded for today. Outside those hours it SHALL replace the stored list for that date. Every accepted or refused load SHALL append a risk record with the date, the symbols and the outcome.

#### Scenario: Removing a symbol mid-day
- **GIVEN** today's list holds {ABC, XYZ}
- **WHEN** at 11:00 IST a list of {ABC} for today is loaded
- **THEN** it is refused with `TIGHTEN_ONLY` and the stored list is unchanged

#### Scenario: Adding a symbol mid-day
- **GIVEN** today's list holds {ABC}
- **WHEN** at 11:00 IST a list of {ABC, XYZ} for today is loaded
- **THEN** it is accepted

### Requirement: Entries in banned underlyings are refused
An entry in the `NSE_FNO` or `BSE_FNO` segment SHALL be refused with `RISK_FNO_BAN` when its underlying is on today's ban list. The exchange's F&O ban list does not cover commodity (`MCX_COMM`) or currency segments.

#### Scenario: Banned stock option
- **GIVEN** today's ban list holds XYZ
- **WHEN** an entry in an XYZ option arrives
- **THEN** it is refused with `RISK_FNO_BAN`

#### Scenario: Equity in a banned name
- **GIVEN** today's ban list holds XYZ
- **WHEN** an entry in XYZ cash equity arrives
- **THEN** the ban list does not refuse it

#### Scenario: Commodity contract
- **GIVEN** no ban list is loaded for today
- **WHEN** an entry in an `MCX_COMM` contract arrives
- **THEN** neither `RISK_FNO_BAN` nor `RISK_BAN_LIST_MISSING` refuses it

### Requirement: F&O entries fail closed without today's ban list
When no ban list is stored for today, or the stored lists cannot be read, every entry in the `NSE_FNO` or `BSE_FNO` segment SHALL be refused with `RISK_BAN_LIST_MISSING`.

#### Scenario: Morning without a list
- **WHEN** at 09:20 IST no list has been loaded for today and an option entry arrives
- **THEN** it is refused with `RISK_BAN_LIST_MISSING`

#### Scenario: Storage down at start
- **WHEN** the engine starts and the stored lists cannot be read
- **THEN** option entries are refused with `RISK_BAN_LIST_MISSING` until the lists are read

### Requirement: Option strikes stay within a distance from the underlying
When `max_strike_distance_pct` is set, an option entry SHALL be refused with `RISK_STRIKE_DISTANCE` if the strike is more than that percent away from the underlying's latest price. The underlying's price SHALL come from its index or stock instrument in the master. Without a price for it no older than `stale_s`, the entry SHALL be refused with `RISK_UNDERLYING_UNPRICED`.

#### Scenario: Far out of the money
- **GIVEN** `max_strike_distance_pct` = 3 and NIFTY at 24,000
- **WHEN** an entry in a 25,000 call arrives
- **THEN** it is refused with `RISK_STRIKE_DISTANCE`

#### Scenario: Underlying not priced
- **GIVEN** `max_strike_distance_pct` is set and the NIFTY index has no fresh price
- **WHEN** an entry in a NIFTY option arrives
- **THEN** it is refused with `RISK_UNDERLYING_UNPRICED`

### Requirement: Option entries meet a minimum premium
When `min_premium` is set, an option entry SHALL be refused with `RISK_MIN_PREMIUM` if its price is below it. The price SHALL be the order's limit price, or the latest price for an order without one.

#### Scenario: Premium too low
- **GIVEN** `min_premium` = ₹5
- **WHEN** a limit entry at ₹3.50 in an option arrives
- **THEN** it is refused with `RISK_MIN_PREMIUM`

### Requirement: Instrument policy never applies to exits
No part of the instrument policy, including a missing ban list, SHALL refuse an exit.

#### Scenario: Exiting a banned position
- **GIVEN** an open XYZ option position and XYZ on today's ban list
- **WHEN** an exit for it arrives
- **THEN** the instrument policy does not refuse it

### Requirement: Instrument policy limits tighten in their direction
`max_strike_distance_pct` (PCT, `down`) and `min_premium` (INR, `up`) SHALL be optional limits; setting one SHALL count as tightening and removing one as loosening. The allow list SHALL tighten by shrinking and the deny list by growing.

#### Scenario: Adding to the allow list mid-day
- **WHEN** at 11:00 IST an edit adds FINNIFTY to `allowed_underlyings`
- **THEN** the edit is rejected with `TIGHTEN_ONLY`

### Requirement: The ban list's date is visible
The snapshot's risk block SHALL report whether a ban list is loaded for today.

#### Scenario: Snapshot without a list
- **WHEN** no list is loaded for today and a snapshot is written
- **THEN** its risk block shows that today's ban list is missing
