# Spec Delta

## Purpose

Defines the pre-trade gate every order passes in the engine and the shape of a risk rule: how rules are ranked, how entries differ from exits, how the gate fails closed, how a rule can run in shadow, and how every decision is audited. It replaces the paper-only stub gate of Stage 2.

## ADDED Requirements

### Requirement: Live mode stays closed until a live broker exists
While the engine has no live broker adapter, the gate SHALL reject every order when the trading mode is live, with reason code `RISK_LIVE_NOT_ENABLED`, and no broker call SHALL be made. Paper-mode orders SHALL be judged by the risk rules. Opening live orders is a later, separate change.

#### Scenario: Live order before a live adapter
- **WHEN** the engine runs with `TRADING_MODE=live` and `LIVE_CONFIRM=yes` and any order arrives
- **THEN** the order is rejected with `RISK_LIVE_NOT_ENABLED` and nothing reaches the broker

#### Scenario: Paper order judged by rules
- **WHEN** a paper order arrives while limits are set
- **THEN** it is accepted or rejected by the risk rules, not by its mode

### Requirement: Rules are checks with ranked decisions
Every risk rule SHALL be a check with a pre-trade phase, a continuous phase (on a tick, MTM update, order update or timer), or both. A check SHALL return one decision with a reason code, ranked ALLOW < REJECT < HALT_NEW_ENTRIES < TARGET_HALT < KILL. The gate SHALL act on the most severe decision among all checks. A check that raises SHALL count as REJECT when it judges an entry order in the pre-trade phase, and as HALT_NEW_ENTRIES in the continuous phase.

#### Scenario: Most severe wins
- **WHEN** one check returns REJECT and another returns KILL for the same event
- **THEN** the engine acts on KILL

#### Scenario: A check crashes before a trade
- **WHEN** a check raises an exception while judging an entry order
- **THEN** the order is rejected with a reason code naming that check

### Requirement: A faulty check never blocks an exit
A check that raises while judging an exit SHALL NOT refuse the exit. The fault SHALL be audited and the exit SHALL go through, because a bug in a rule must never leave the account unable to reduce its risk.

#### Scenario: A check crashes before an exit
- **WHEN** a check raises an exception while judging an order that only reduces a position
- **THEN** the order is accepted and an audit record shows the failed check

### Requirement: Entries and exits are told apart by position effect
An order SHALL be an exit only if it reduces the absolute quantity of the account's existing position in that instrument and product, up to its size. Any quantity that opens a position or grows one SHALL make the order an entry, including the part of an order that flips through zero. Orders from the engine's own protective logic SHALL be classified the same way.

#### Scenario: Plain exit
- **WHEN** the account is long 65 and a sell of 65 arrives
- **THEN** the order is an exit

#### Scenario: Flip is an entry
- **WHEN** the account is long 65 and a sell of 130 arrives
- **THEN** the order is an entry and every entry rule applies to it

### Requirement: Exits are never blocked by entry rules
Entry limits, cutoffs, caps, cooldowns, halts and the equity-floor pre-trade check SHALL NOT reject an exit. Exits SHALL still pass through the gate (price sanity, rate limiter and audit apply) and SHALL be accepted during a day lock, a halt and after the entry cutoff.

#### Scenario: Exit after the cutoff on a halted day
- **WHEN** the account is HALTED for the day and an order that reduces an open position arrives
- **THEN** the order is accepted by the gate

### Requirement: Fail closed for entries, keep managing exits
If risk state, limits or persistence are unavailable, or the gate has not yet been connected to trading state, the gate SHALL reject every entry with a reason code naming what is missing. It SHALL keep accepting exits. A gate that is not yet connected to trading state cannot tell entries from exits, so it SHALL reject every order until it is connected. A continuous check that cannot run SHALL raise the account to at least HALT_NEW_ENTRIES.

#### Scenario: Gate not yet connected
- **WHEN** an order arrives before the gate has been connected to trading state
- **THEN** the order is rejected with `RISK_STATE_UNAVAILABLE`

#### Scenario: Postgres down
- **WHEN** risk state cannot be loaded and an entry arrives
- **THEN** the entry is rejected with `RISK_STATE_UNAVAILABLE`

#### Scenario: Feed silent
- **WHEN** a continuous check cannot run because its inputs are missing
- **THEN** the account is HALT_NEW_ENTRIES and an alert is logged

### Requirement: Exits are accepted while persistence is down or reconciliation is pending
While persistence is unavailable or a recovery halt is in force, the order path SHALL still accept an exit. It SHALL hold the order in memory, queue its record for writing in order, send it to the broker, and log that the order is not yet stored. Entries SHALL still be refused in these states.

#### Scenario: Kill with Postgres down
- **WHEN** Postgres is unreachable and the kill switch needs to exit a position
- **THEN** the exit order is sent to the broker, its write is queued, and it is stored when Postgres returns

#### Scenario: Entry in the same state
- **WHEN** Postgres is unreachable and an entry arrives
- **THEN** the entry is rejected with the persistence reason

### Requirement: Shadow and enforce modes per rule
Each rule SHALL run in `enforce` mode (its decision binds) or `shadow` mode (its decision is audited with `enforced = false` and treated as ALLOW). A new rule SHALL be switchable between modes through the limits. The equity floor, the stale-data guard, the kill switch and the fail-closed checks SHALL NOT be switchable to shadow.

#### Scenario: Shadow rule would reject
- **WHEN** a rule in shadow mode would reject an order
- **THEN** the order proceeds and an audit record shows the rule's decision with `enforced = false`

#### Scenario: Equity floor to shadow
- **WHEN** a limits edit sets the equity floor rule to shadow
- **THEN** the edit is rejected

### Requirement: Every risk decision is audited
The engine SHALL append a record to the `risk_event` table for each gate verdict (including ALLOW) and for every non-ALLOW decision of a rule. A record SHALL carry the time, `account_id`, rule, phase, decision, a stable reason code (for example `RISK_OPEN_POSITIONS`), the inputs the rule used, whether it was enforced, and the check's latency. Skipped and rejected orders SHALL be recorded the same way. The engine's database role SHALL NOT be able to update or delete these records.

#### Scenario: Why didn't it trade
- **WHEN** an entry is rejected for exceeding the per-trade risk
- **THEN** a record exists with the rule, `RISK_PER_TRADE`, the computed risk and the limit

#### Scenario: Audit is append-only
- **WHEN** the engine role tries to update or delete a `risk_event` row
- **THEN** the database refuses

### Requirement: Rejections carry reason and code
A gate rejection SHALL be final: the order SHALL be stored as `Rejected` with a reason that begins with its stable reason code, and SHALL never be sent to the broker.

#### Scenario: Rejection reason
- **WHEN** the gate rejects an order for the entry cutoff
- **THEN** the stored reason starts with `RISK_ENTRY_CUTOFF`
