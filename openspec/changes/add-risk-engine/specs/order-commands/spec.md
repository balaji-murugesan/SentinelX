# Spec Delta

## MODIFIED Requirements

### Requirement: Commands through the Redis command stream
The engine SHALL accept order commands (place order, cancel order) and risk commands (kill switch, set risk limits, size order) only from its Redis command stream, as versioned contract models. Each command SHALL be answered on its own reply stream, which expires after a fixed time. A place-order command SHALL carry a trigger price for a `STOP_MARKET` order and none for any other order type, and SHALL carry no limit price for a `STOP_MARKET` order; a command that breaks this is not a valid command model. A command whose source is one that the risk engine stamps on its own orders (`risk-stop`, `risk-kill`, `risk-squareoff`) SHALL be rejected with a reason and SHALL NOT be processed further. The answer to a size command SHALL carry the sized quantity and the rupees at risk. The answer to a kill command SHALL say so when the market is closed and open positions cannot be exited. The engine SHALL be the only component that changes order or position state.

#### Scenario: Place command answered
- **WHEN** a valid place-order command is added to the command stream
- **THEN** the engine writes one reply for that command id stating whether the order was accepted (with its order id) or rejected (with a reason)

#### Scenario: Malformed command
- **WHEN** a stream entry is not a valid command model
- **THEN** it is logged as malformed without its content, no order is created, and the engine keeps reading

#### Scenario: Trigger price on an order that is not a stop-market
- **WHEN** a place-order command for a `LIMIT` or `MARKET` order carries a trigger price
- **THEN** it is not a valid command model: it is logged as malformed without its content, no order is created, and the engine keeps reading

#### Scenario: Limit price on a stop-market order
- **WHEN** a place-order command for a `STOP_MARKET` order carries a limit price
- **THEN** it is not a valid command model: it is logged as malformed without its content, no order is created, and the engine keeps reading

#### Scenario: Source reserved for the risk engine
- **WHEN** a command arrives whose source is `risk-stop`, `risk-kill` or `risk-squareoff`
- **THEN** the reply is a rejection saying the source is reserved for the risk engine, and nothing is created, changed or cancelled

#### Scenario: Kill command answered
- **WHEN** a kill-switch command is added to the command stream
- **THEN** the engine writes one reply stating it was accepted, and a kill that already ran the same day is accepted with a note that the day is already stopped

#### Scenario: Kill command with the market closed
- **WHEN** a kill-switch command arrives while the market is closed and a position is open
- **THEN** the day is locked, no order is sent, and the reply is accepted with a note that the market is closed and the open positions cannot be exited now

#### Scenario: Limits command answered
- **WHEN** a set-risk-limits command is added to the command stream
- **THEN** the engine writes one reply stating it was accepted, or rejected with a reason that starts with its reason code

#### Scenario: Size command answered
- **WHEN** a size command is added for an entry whose risk for one lot is within the per-trade maximum
- **THEN** the reply is accepted and carries the sized quantity and the rupees at risk, and no order is created and no state changes

### Requirement: Idempotency key on every command
Every command SHALL carry an idempotency key. A command whose key was already processed SHALL receive the original result again and SHALL NOT create, change or cancel anything. Keys and results SHALL be persisted so that this also holds after a restart. The one exception is a size command, which changes nothing: its result is not stored, and a repeat of its key SHALL be answered afresh from the limits then in force.

#### Scenario: Same key twice
- **WHEN** two place-order commands arrive with the same idempotency key
- **THEN** exactly one order exists, and both commands get the same order id and result

#### Scenario: Duplicate after restart
- **WHEN** a command is repeated with a key the engine processed before it restarted
- **THEN** the original result is returned and no second order is placed

#### Scenario: Size command repeated
- **WHEN** two size commands arrive with the same idempotency key and the per-trade maximum risk is tightened between them
- **THEN** the second reply is computed from the tightened limit and nothing is stored or changed

### Requirement: Commands survive a busy or restarting engine
The engine SHALL read commands as a consumer group and acknowledge each only after its result is persisted, except a size command, which stores no result and is acknowledged once its reply is written. A command delivered but not acknowledged before a crash SHALL be processed after restart, with idempotency preventing a duplicate effect.

#### Scenario: Crash between persist and acknowledge
- **WHEN** the engine is killed after persisting a command's result but before acknowledging it
- **THEN** after restart the command is redelivered, the stored result is replied again, and no second order is created

#### Scenario: Crash before a size command is acknowledged
- **WHEN** the engine is killed after replying to a size command but before acknowledging it
- **THEN** after restart the command is redelivered and answered again from the limits then in force, and nothing is created, stored or changed

### Requirement: Developer CLI uses the command stream
The developer CLI SHALL place and cancel orders, send the kill switch, send risk limits and ask for sizing by writing commands to the command stream with a fresh idempotency key per invocation, and SHALL print the engine's reply. `order place` SHALL send the initial stop given with `--stop`. `kill` SHALL send nothing unless the owner confirms with `--yes`. The CLI SHALL show orders, positions and the risk figures (`risk status`) from the engine's Redis snapshot, never computing P&L or risk itself. A limits file for `risk set-limits` is one JSON object of limit names and values. A key that starts with `_` SHALL be a comment and SHALL be dropped before anything is sent. Money and percentages SHALL be written as strings or whole numbers: a number with a fraction SHALL be refused before anything is sent.

#### Scenario: CLI order round trip
- **WHEN** the owner runs the CLI place-order command for a paper market order
- **THEN** the CLI prints the accepted order id, and the CLI positions command then shows the position with the engine's MTM P&L

#### Scenario: Engine not answering
- **WHEN** no reply arrives within the CLI's wait time
- **THEN** the CLI prints that the result is unknown and how to look the order up, and does not resend the command

#### Scenario: Kill without confirmation
- **WHEN** the owner runs `kill` without `--yes`
- **THEN** nothing is sent, the CLI says the kill cannot be undone for the day, and the command exits with an error

#### Scenario: Fractional number in a limits file
- **WHEN** the owner runs `risk set-limits` with a file that holds a number with a fraction
- **THEN** nothing is sent and the CLI says to write the value as a string

#### Scenario: Comment key in a limits file
- **WHEN** the owner runs `risk set-limits` with a file that holds a key starting with `_` next to real limits
- **THEN** the command carries only the real limits, so the engine never sees the comment

#### Scenario: Risk figures from the snapshot
- **WHEN** the owner runs `risk status`
- **THEN** the CLI prints the risk block of the engine's snapshot, or says that no snapshot exists when the engine has written none
