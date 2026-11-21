# Spec Delta

## ADDED Requirements

### Requirement: A stop rejected after acceptance is re-placed with a growing delay
When the broker rejects a catastrophic stop after accepting it, the engine SHALL treat its position as unprotected and log a critical alert. It SHALL then place a new stop after a delay that starts at 1 s, doubles with each further such rejection for that position, and is capped at 30 s. The delay SHALL reset when the position changes by a fill or goes flat. No stop SHALL be placed on every sweep.

#### Scenario: Stop rejected once
- **GIVEN** a long position of 65 with a resting stop, in market hours
- **WHEN** the broker rejects the stop at 10:00:00
- **THEN** the position is unprotected, entries are refused, a critical alert is logged, and a new stop is placed no earlier than 10:00:01

#### Scenario: Broker keeps rejecting the stop
- **WHEN** the broker rejects every new stop for a position soon after accepting it
- **THEN** the waits before each new stop are 1, 2, 4, 8, 16, 30, 30 … seconds, never shorter

#### Scenario: A fill resets the delay
- **GIVEN** a position whose stop was rejected three times, so the next wait is 8 s
- **WHEN** an entry for that position fills and its new stop is then rejected
- **THEN** the next wait is 1 s

#### Scenario: Rejected stop on a flat book
- **WHEN** the broker rejects a stop whose position has meanwhile gone flat
- **THEN** no new stop is placed
