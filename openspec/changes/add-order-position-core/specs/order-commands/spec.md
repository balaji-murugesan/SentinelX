# Spec Delta

## Purpose

Defines how order commands reach the engine over Redis and how each one is answered exactly once, so that every source (the developer CLI now, the API later) uses one safe path with idempotency.

## ADDED Requirements

### Requirement: Commands through the Redis command stream
The engine SHALL accept order commands (place order, cancel order) only from its Redis command stream, as versioned contract models. Each command SHALL be answered on its own reply stream, which expires after a fixed time. The engine SHALL be the only component that changes order or position state.

#### Scenario: Place command answered
- **WHEN** a valid place-order command is added to the command stream
- **THEN** the engine writes one reply for that command id stating whether the order was accepted (with its order id) or rejected (with a reason)

#### Scenario: Malformed command
- **WHEN** a stream entry is not a valid command model
- **THEN** it is logged as malformed without its content, no order is created, and the engine keeps reading

### Requirement: Idempotency key on every command
Every command SHALL carry an idempotency key. A command whose key was already processed SHALL receive the original result again and SHALL NOT create, change or cancel anything. Keys and results SHALL be persisted so that this also holds after a restart.

#### Scenario: Same key twice
- **WHEN** two place-order commands arrive with the same idempotency key
- **THEN** exactly one order exists, and both commands get the same order id and result

#### Scenario: Duplicate after restart
- **WHEN** a command is repeated with a key the engine processed before it restarted
- **THEN** the original result is returned and no second order is placed

### Requirement: Commands for this account only
Every command SHALL carry an `account_id`. Commands whose `account_id` is not the engine's configured `ACCOUNT_ID` SHALL be rejected with a reason and SHALL NOT be processed further.

#### Scenario: Other account
- **WHEN** a command names an account other than the configured one
- **THEN** the reply is a rejection naming the account mismatch, and no order is created

### Requirement: Commands survive a busy or restarting engine
The engine SHALL read commands as a consumer group and acknowledge each only after its result is persisted. A command delivered but not acknowledged before a crash SHALL be processed after restart, with idempotency preventing a duplicate effect.

#### Scenario: Crash between persist and acknowledge
- **WHEN** the engine is killed after persisting a command's result but before acknowledging it
- **THEN** after restart the command is redelivered, the stored result is replied again, and no second order is created

### Requirement: Developer CLI uses the command stream
The developer CLI SHALL place and cancel orders by writing commands to the command stream with a fresh idempotency key per invocation, and SHALL print the engine's reply. It SHALL show orders and positions from the engine's Redis snapshot, never computing P&L itself.

#### Scenario: CLI order round trip
- **WHEN** the owner runs the CLI place-order command for a paper market order
- **THEN** the CLI prints the accepted order id, and the CLI positions command then shows the position with the engine's MTM P&L

#### Scenario: Engine not answering
- **WHEN** no reply arrives within the CLI's wait time
- **THEN** the CLI prints that the result is unknown and how to look the order up, and does not resend the command
