# Spec Delta

## Purpose

Keeps orders, fills, positions and command results durable in Postgres, so a crash or restart never loses or duplicates trading state, and refuses new orders whenever that durability cannot be guaranteed.

## ADDED Requirements

### Requirement: Durable trading records
Orders, every order state transition (as an audit record), fills, positions and command results SHALL be stored in Postgres, each carrying `account_id`. They are created by a new migration applied by the migrate service. An order SHALL be persisted as `Requested` before it is sent to the broker. Every later state change SHALL be persisted before it is published.

#### Scenario: Audit trail
- **WHEN** an order goes Requested → Submitted → Open → Filled
- **THEN** four transition records exist for it, in order, with timestamps (UTC) and reasons where given

### Requirement: Fail closed when persistence is unavailable
If Postgres cannot be reached or a write fails, the engine SHALL reject every new order with a reason saying trading state cannot be saved. It SHALL keep handling fills and cancellations for existing orders in memory, retry the pending writes, and log an error. New orders SHALL be accepted again once writes succeed.

#### Scenario: Postgres down
- **WHEN** Postgres is stopped and a place-order command arrives
- **THEN** the command is rejected with the persistence reason, nothing is sent to the broker, and an error is logged

#### Scenario: Postgres back
- **WHEN** Postgres becomes reachable again and the pending writes succeed
- **THEN** the next valid order is accepted

### Requirement: Restart recovery
On start, before consuming any command, the engine SHALL load open orders, today's fills, positions and processed command keys. It SHALL reconcile open and `Unknown` orders with the broker adapter. If any order or position differs from the broker, the engine SHALL reject new orders until the owner resolves it, while still managing existing orders, and SHALL log each difference.

#### Scenario: Kill -9 and restart
- **WHEN** the engine is killed with `kill -9` while holding open positions and a resting order, and is restarted
- **THEN** orders, positions (quantity, average, realized P&L, costs) and command keys are identical to before the kill, and commands are processed again

#### Scenario: Mismatch after restart
- **WHEN** reconciliation finds an order the broker reports differently from the engine's records
- **THEN** the engine logs the difference, rejects new orders with a reconciliation reason, and keeps processing fills and cancels

### Requirement: State snapshot in Redis
The engine SHALL write a snapshot of open orders and positions (with MTM, realized and net P&L) to Redis, marked with `schema_version` and the time it was taken. It SHALL rewrite the snapshot when state changes, at most every `SNAPSHOT_INTERVAL_S` (default 1 s). The snapshot is a read model only; losing it SHALL NOT affect trading.

#### Scenario: Redis down
- **WHEN** Redis is unreachable
- **THEN** orders and positions keep updating and persisting, snapshot writes are retried, and a warning is logged
