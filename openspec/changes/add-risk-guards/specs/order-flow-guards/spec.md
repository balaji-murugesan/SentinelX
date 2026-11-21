# Spec Delta

## Purpose

Guards the flow of orders rather than their size: it slows bursts of entries and stops new entries while the broker keeps rejecting orders, so a runaway signal or a broker-side problem cannot turn into a storm of trades.

## ADDED Requirements

### Requirement: Entries per rolling minute are capped
An entry SHALL be refused with `RISK_ENTRY_RATE` if the entries the gate allowed in the 60 seconds before it already number `max_entries_per_minute`. Exits SHALL NOT count and SHALL NOT be refused.

#### Scenario: Fourth entry in a minute
- **GIVEN** `max_entries_per_minute` = 3 and entries allowed at 10:00:05, 10:00:30 and 10:00:50
- **WHEN** an entry arrives at 10:00:58
- **THEN** it is refused with `RISK_ENTRY_RATE`

#### Scenario: Window has moved on
- **GIVEN** the same three entries
- **WHEN** an entry arrives at 10:01:06
- **THEN** it is not refused by the per-minute cap

### Requirement: Entries keep a minimum gap
An entry SHALL be refused with `RISK_ENTRY_GAP` if fewer than `min_entry_gap_s` seconds have passed since the last entry the gate allowed today. The time of the last allowed entry SHALL be persisted with the day's state and survive a restart.

#### Scenario: Too soon after the last entry
- **GIVEN** `min_entry_gap_s` = 20 and an entry allowed at 10:00:00
- **WHEN** an entry arrives at 10:00:12
- **THEN** it is refused with `RISK_ENTRY_GAP`

#### Scenario: Gap survives a restart
- **GIVEN** `min_entry_gap_s` = 20 and an entry allowed at 10:00:00
- **WHEN** the engine restarts and an entry arrives at 10:00:15
- **THEN** it is refused with `RISK_ENTRY_GAP`

#### Scenario: An exit right after an entry
- **WHEN** an exit arrives 2 seconds after an entry was allowed
- **THEN** the gap rule does not refuse it

### Requirement: Broker rejections trip a breaker
When `reject_breaker_n` broker rejections have occurred within the last 5 minutes, new entries SHALL be refused with `RISK_REJECT_BREAKER` and a critical alert SHALL be logged once when the breaker trips. The refusal SHALL clear by itself once fewer than `reject_breaker_n` rejections remain in the window. Exits SHALL still be accepted.

#### Scenario: Margin rejection storm
- **GIVEN** `reject_breaker_n` = 3
- **WHEN** the broker rejects three orders within 2 minutes and an entry then arrives
- **THEN** the entry is refused with `RISK_REJECT_BREAKER` and one critical alert was logged

#### Scenario: Breaker clears
- **GIVEN** the breaker tripped on three rejections at 10:00, 10:01 and 10:02
- **WHEN** an entry arrives at 10:05:30
- **THEN** it is not refused by the breaker

#### Scenario: Exit while tripped
- **WHEN** the breaker has tripped and an exit arrives
- **THEN** the breaker does not refuse it

### Requirement: Only rejections by the broker count
A rejection SHALL count toward the breaker only when the broker refused an order the engine sent, at placement or through an order update, whether it was an entry or an exit. Refusals by the risk gate, by the engine's rate limiter, and orders marked rejected during restart recovery SHALL NOT count.

#### Scenario: Gate refusals do not count
- **WHEN** the gate refuses five entries in a minute
- **THEN** the breaker has counted no rejections

#### Scenario: Rate-limited entry does not count
- **WHEN** an entry is refused with `RISK_RATE_LIMITED`
- **THEN** the breaker has counted no rejection

#### Scenario: Rejected exit counts
- **WHEN** the broker rejects an exit
- **THEN** the breaker counts one rejection

### Requirement: Order-flow limits are required and tighten in their direction
`max_entries_per_minute` (COUNT, `down`), `min_entry_gap_s` (SECONDS, `up`) and `reject_breaker_n` (COUNT, `down`) SHALL be required limits.

#### Scenario: Shortening the gap mid-day
- **WHEN** at 11:00 IST an edit lowers `min_entry_gap_s` from 20 to 10
- **THEN** the edit is rejected with `TIGHTEN_ONLY`

#### Scenario: Lowering the breaker threshold mid-day
- **WHEN** at 11:00 IST an edit lowers `reject_breaker_n` from 3 to 2
- **THEN** the edit is accepted

### Requirement: Recent broker rejections are visible
The snapshot's risk block SHALL report the number of broker rejections in the last 5 minutes.

#### Scenario: Snapshot after rejections
- **GIVEN** two broker rejections in the last 5 minutes
- **WHEN** a snapshot is written
- **THEN** its risk block shows 2 recent rejections
