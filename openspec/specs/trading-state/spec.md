# trading-state Specification

## Purpose
Keeps orders, fills, positions and command results durable in Postgres, so a crash or restart never loses or duplicates trading state, and refuses new orders whenever that durability cannot be guaranteed.

## Requirements

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
On start, before consuming any command, the engine SHALL load open orders, today's fills, positions and processed command keys. It SHALL reconcile open and `Unknown` orders with the broker adapter. If any order or position differs from the broker, or a recovery halt is already stored, the engine SHALL enter HALTED: reject new entry orders with a reconciliation reason, keep processing fills and cancellations, and log each difference.

#### Scenario: Kill -9 and restart
- **WHEN** the engine is killed with `kill -9` while holding open positions and a resting order, and is restarted
- **THEN** orders, positions (quantity, average, realized P&L, costs) and command keys are identical to before the kill, and commands are processed again

#### Scenario: Mismatch after restart
- **WHEN** reconciliation finds an order the broker reports differently from the engine's records
- **THEN** the engine logs the difference, rejects new orders with a reconciliation reason, and keeps processing fills and cancels

### Requirement: Persisted recovery halt
When recovery enters HALTED, the engine SHALL store the halt (with each difference and the time) in Postgres. A stored halt SHALL survive restarts: a later recovery that finds no differences SHALL NOT clear it. Only the explicit resume command (see order-commands) SHALL clear it, and never automatically. The halt applies to new entries only: exit and kill-switch paths added later SHALL NOT be gated by it.

#### Scenario: Halt survives restart
- **WHEN** the engine is HALTED, is restarted, and reconciliation now finds no differences
- **THEN** the engine is still HALTED, rejects new entry orders with a reconciliation reason, and still lists the stored differences

#### Scenario: Clean reconciliation does not clear a halt
- **WHEN** recovery finds no differences but a halt is stored
- **THEN** the halt remains until the resume command clears it

### Requirement: Halt is visible
While halted, the startup banner and the Redis snapshot SHALL show HALTED and list every difference.

#### Scenario: Halt is prominent
- **WHEN** the engine starts HALTED
- **THEN** an error-level startup banner and the snapshot both show HALTED with each difference

### Requirement: Recovery completes before trading
The engine SHALL build its repository from `DATABASE_URL` and SHALL NOT fall back to in-memory storage in production. If the stored state cannot be loaded, the engine SHALL keep running, retry the load, and reject new orders until recovery has completed. A missing `DATABASE_URL` SHALL stop the engine from starting, with a clear error.

#### Scenario: Postgres unreachable at start
- **WHEN** the engine starts while Postgres is down and Postgres later becomes reachable
- **THEN** new orders are rejected until the stored state has been loaded and reconciled, and are accepted only after that

#### Scenario: Missing DATABASE_URL
- **WHEN** the engine is started without `DATABASE_URL`
- **THEN** it refuses to start and prints which setting is missing

### Requirement: State snapshot in Redis
The engine SHALL write a snapshot of open orders and positions (with MTM, realized and net P&L) to Redis, marked with `schema_version` and the time it was taken. It SHALL rewrite the snapshot when state changes, at most every `SNAPSHOT_INTERVAL_S` (default 1 s). The snapshot is a read model only; losing it SHALL NOT affect trading.

#### Scenario: Redis down
- **WHEN** Redis is unreachable
- **THEN** orders and positions keep updating and persisting, snapshot writes are retried, and a warning is logged
