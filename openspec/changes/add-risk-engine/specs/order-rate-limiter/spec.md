# Spec Delta

## Purpose

Keeps the engine's calls to the broker's order API inside the broker's limits and well inside SEBI's 10-orders-per-second threshold for self-built algos, while guaranteeing that exits and cancels always get through before entries do.

## ADDED Requirements

### Requirement: Every order call to the broker is rate limited
Every place and cancel call the engine makes to a broker adapter SHALL pass a client-side limiter with a per-second and a per-minute cap, both configured. No order path SHALL bypass it.

#### Scenario: Burst of entries
- **WHEN** more entries are sent in one second than the per-second cap allows
- **THEN** the excess entries are not sent in that second

### Requirement: A hard ceiling that configuration cannot raise
The per-second cap SHALL NOT be configurable above a ceiling fixed in the engine's code at a value below SEBI's 10 orders per second, and the per-minute cap SHALL NOT be configurable above the broker's published per-minute limit. The engine SHALL refuse to start with a configured cap above its ceiling and SHALL say which setting is wrong.

#### Scenario: Cap above the ceiling
- **WHEN** the per-second cap is configured at 10
- **THEN** the engine refuses to start with a message naming the setting and its ceiling

### Requirement: A reserved share for exits and cancels
A configured share of the tokens in each window SHALL be reserved for exits and cancels. Entries SHALL NOT use the reserved share. An entry that finds no token in its share SHALL be rejected with `RISK_RATE_LIMITED` and SHALL NOT wait. An exit or cancel that finds no token SHALL wait for one, in the order it asked, and SHALL NOT be dropped.

#### Scenario: Entries exhaust their share
- **WHEN** entries have used all tokens outside the reserved share and one more entry arrives
- **THEN** it is rejected with `RISK_RATE_LIMITED`

#### Scenario: Exit during an entry burst
- **WHEN** entries have exhausted their share and an exit arrives
- **THEN** the exit takes a reserved token and is sent

### Requirement: A kill is never dropped by the limiter
When a kill flattens many positions at once, the limiter SHALL pace the exits and cancels within the ceiling and SHALL send every one of them.

#### Scenario: Ten positions
- **WHEN** a kill flattens ten positions and the cap is lower than ten per second
- **THEN** all ten exits are sent, spread over more than one second, and none is dropped
