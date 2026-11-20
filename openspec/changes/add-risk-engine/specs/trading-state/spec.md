# Spec Delta

## MODIFIED Requirements

### Requirement: Durable trading records
Orders, every order state transition (as an audit record), fills, positions and command results SHALL be stored in Postgres, each carrying `account_id`. They are created by a new migration applied by the migrate service. An order's trigger price and initial stop, when it has them, SHALL be stored with it, and an order stored without them SHALL load with both absent. An order SHALL be persisted as `Requested` before it is sent to the broker, except an exit placed while the database cannot be written or earlier writes are still queued, which, once it has been validated and allowed by the gate, SHALL be sent first and stored as soon as the database accepts writes (see "Fail closed when persistence is unavailable"). Every later state change SHALL be persisted before it is published.

#### Scenario: Audit trail
- **WHEN** an order goes Requested → Submitted → Open → Filled
- **THEN** four transition records exist for it, in order, with timestamps (UTC) and reasons where given

#### Scenario: Order with a stop
- **WHEN** a stop-market order with a trigger price and an entry order with an initial stop are stored and loaded
- **THEN** each comes back with the same trigger price or initial stop, and an order stored before these fields existed loads with both absent

### Requirement: Fail closed when persistence is unavailable
If Postgres cannot be reached or a write fails, the engine SHALL reject every new entry order with a reason saying trading state cannot be saved. This state alone SHALL NOT refuse an order that only reduces a position (an exit): the exit SHALL be validated and judged by the gate like any other exit and, if both allow it, sent to the broker, and the engine SHALL then hold it in memory, queue its record behind the earlier pending writes so that the database sees them in order, log that it is not yet stored, and answer a repeat of its idempotency key from the orders it holds. A cancel of an existing order SHALL also still be sent, without waiting for its idempotency key to be looked up, and a repeat of that key SHALL get the first answer from memory. The engine SHALL keep handling fills and cancellations for existing orders in memory, retry the pending writes, and log an error. New orders SHALL be accepted again once writes succeed.

#### Scenario: Postgres down
- **WHEN** Postgres is stopped and a place-order command for an entry arrives
- **THEN** the command is rejected with the persistence reason, nothing is sent to the broker, and an error is logged

#### Scenario: Postgres back
- **WHEN** Postgres becomes reachable again and the pending writes succeed
- **THEN** the next valid order is accepted

#### Scenario: Exit while Postgres is down
- **WHEN** Postgres is stopped and a place-order command arrives that only reduces an open position
- **THEN** the order is not refused for that reason: it is validated and judged by the gate like any other exit and, if both allow it, it is sent to the broker, its record is queued, a warning says it is not yet stored, and it is stored when Postgres returns

#### Scenario: Gate refuses an exit while Postgres is down
- **WHEN** Postgres is stopped and an order that only reduces an open position arrives and the gate refuses it
- **THEN** the order ends in `Rejected` with the gate's reason, is never sent to the broker, and its rejection is stored when Postgres returns

#### Scenario: Repeat of an exit during the outage
- **WHEN** the same exit command arrives again while Postgres is still away
- **THEN** the first answer is returned and no second order is sent

#### Scenario: Cancel while Postgres is down
- **WHEN** Postgres is stopped and a cancel command arrives for an open order
- **THEN** the cancel is sent to the broker, its result is queued for writing, and a repeat of the same key gets the first answer

### Requirement: Restart recovery
On start, before consuming any command, the engine SHALL load open orders, today's fills, positions and processed command keys. It SHALL reconcile open and `Unknown` orders with the broker adapter. If any order or position differs from the broker, or a recovery halt is already stored, the engine SHALL enter HALTED: reject new entry orders with a reconciliation reason, keep processing fills and cancellations, and log each difference.

#### Scenario: Kill -9 and restart
- **WHEN** the engine is killed with `kill -9` while holding open positions and a resting order, and is restarted
- **THEN** orders, positions (quantity, average, realized P&L, costs) and command keys are identical to before the kill, and commands are processed again

#### Scenario: Mismatch after restart
- **WHEN** reconciliation finds an order the broker reports differently from the engine's records
- **THEN** the engine logs the difference, rejects new entry orders with a reconciliation reason, does not refuse an order that only reduces a position for that reason, and keeps processing fills and cancels

### Requirement: Persisted recovery halt
When recovery enters HALTED, the engine SHALL store the halt (with each difference and the time) in Postgres. A stored halt SHALL survive restarts: a later recovery that finds no differences SHALL NOT clear it. Only the explicit resume command (see order-commands) SHALL clear it, and never automatically. The halt applies to new entry orders only: an order that only reduces a position (an exit), a cancel and the kill switch SHALL NOT be gated by it.

#### Scenario: Halt survives restart
- **WHEN** the engine is HALTED, is restarted, and reconciliation now finds no differences
- **THEN** the engine is still HALTED, rejects new entry orders with a reconciliation reason, and still lists the stored differences

#### Scenario: Clean reconciliation does not clear a halt
- **WHEN** recovery finds no differences but a halt is stored
- **THEN** the halt remains until the resume command clears it

#### Scenario: Exit during a halt
- **WHEN** the engine is HALTED and an order that only reduces an open position arrives
- **THEN** the order is not refused because of the halt: it is validated and judged by the gate like any other exit and sent if both allow it, while an entry is rejected with a reconciliation reason

### Requirement: Halt is visible
While halted, the startup banner and the Redis snapshot SHALL show HALTED and list every difference. When the engine loads a risk day state that is already locked by a kill or a blind-price halt earlier the same day, it SHALL log an error-level banner saying the day is locked and that new entries are refused until the next trading day, and the snapshot's risk block SHALL show the halt level and its reason.

#### Scenario: Halt is prominent
- **WHEN** the engine starts HALTED
- **THEN** an error-level startup banner and the snapshot both show HALTED with each difference

#### Scenario: Day lock is visible
- **WHEN** the engine starts on a day that the risk engine has already locked
- **THEN** an error-level banner says the day is locked and entries are refused until the next trading day, and the snapshot's risk block shows the halt level and reason

### Requirement: Recovery completes before trading
The engine SHALL build its repository from `DATABASE_URL` and SHALL NOT fall back to in-memory storage in production. If the stored state cannot be loaded, the engine SHALL keep running, retry the load, and reject new entry orders until recovery has completed; while no positions are loaded no order can be recognised as an exit, so every order is judged an entry and refused. Once the state is loaded, an exit is handled as "Fail closed when persistence is unavailable" and "Persisted recovery halt" describe. A missing `DATABASE_URL` SHALL stop the engine from starting, with a clear error.

#### Scenario: Postgres unreachable at start
- **WHEN** the engine starts while Postgres is down and Postgres later becomes reachable
- **THEN** new entry orders are rejected until the stored state has been loaded and reconciled, and are accepted only after that; while nothing is loaded no order counts as an exit, so none is accepted

#### Scenario: Missing DATABASE_URL
- **WHEN** the engine is started without `DATABASE_URL`
- **THEN** it refuses to start and prints which setting is missing

### Requirement: State snapshot in Redis
The engine SHALL write a snapshot of open orders and positions (with MTM, realized and net P&L) to Redis, marked with `schema_version` and the time it was taken. It SHALL rewrite the snapshot when state changes, at most every `SNAPSHOT_INTERVAL_S` (default 1 s). The snapshot SHALL also carry a risk block computed by the engine, never by a client: the day's P&L net of charges, the equity floor, the day's peak, the halt level and reason, the entries so far today, the loss streak, the cooldown end and the open positions that have no resting stop. The day's P&L and the floor SHALL be absent while unknown, never zero, and the whole block SHALL be absent until the engine has loaded the day's risk state. The snapshot is a read model only; losing it SHALL NOT affect trading.

#### Scenario: Redis down
- **WHEN** Redis is unreachable
- **THEN** orders and positions keep updating and persisting, snapshot writes are retried, and a warning is logged

#### Scenario: Risk block in the snapshot
- **WHEN** the engine has loaded the day's risk state
- **THEN** each snapshot carries the risk block with the figures the engine computed

#### Scenario: Risk block before the day is loaded
- **WHEN** the day's risk state has not been loaded yet
- **THEN** the snapshot is written without a risk block
