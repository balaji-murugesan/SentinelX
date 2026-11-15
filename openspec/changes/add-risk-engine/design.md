# Design

## Context

**What exists** (all from `add-order-position-core`, merged on `main`):
- **The seam.** `OrderService.place` runs `idempotency → fail-closed flags → validation → RiskGate → persist → submit` under one `asyncio.Lock`. `RiskGate` is a `Protocol` with one method, `check(order) -> str | None`. `PaperOnlyGate` is the stub.
- **State.** The service holds `orders` and `positions` in memory (signed quantity, average price, cumulative `realized_pnl` and `costs` per account, instrument and product). It has no per-day figures, no stop price, and no `STOP` order type (`OrderType` is `MARKET | LIMIT`).
- **Degraded states.** `persistence_ok` false or `reconciled` false makes the service refuse *every* order, exits included.
- **Data.** `Tick` has `ltp`, `exchange_time` and `received_at`, and no previous close. `TickBus` drops the oldest item when a consumer's queue is full. `engine.broker.costs.fill_costs` is the paper cost model; its docstring says the FUTURE and EQUITY_DELIVERY rates are not verified.
- **Persistence.** Tables `0002` (trading) and `0003` (recovery halt); the engine role gets DML on new tables through default privileges.
- **Commands.** One Redis stream, a discriminated union (`PlaceOrder | CancelOrder | ResumeRecovery`), per-command reply streams.

Motivation: see proposal.md. Requirements: the eight spec deltas. Owner decisions taken for this change (2026-11-15): Q18 net MTM, Q13 previous-close basis with carried positions counted and flattened, Q12 morning re-placement, Q17 assume SL-M behind a gate.

## Goals / Non-Goals

**Goals:**
- **One gate, many small checks.** Adding a rule in `add-risk-guards` means adding a check, not touching the order service.
- **State that can only tighten.** Floor, peak, halt state and day counters are written monotonically and survive `kill -9`.
- **Protection that outlives its own failure.** A kill, a stop and an exit still work with Postgres down, Redis down or the dashboard absent, within the limits of what the paper broker lets us test.
- **Testable without a broker or wall clock.** The clock, the tick bus and both repositories are injected.

**Non-Goals:**
- **Exit manager and trailing** (S4). The kill's flattener is deliberately simple and replaceable.
- **Anything that talks to Dhan.** Broker lock, Exit All, P&L-exit backstop, Telegram, REST quotes (S7).
- **Per-strategy or household floors** (S8–S10). The floor is per account and day (D-026 "Revisit when").

## Decisions

### 1. Module layout

| Path | Contents |
|---|---|
| `engine/orders/intent.py` | Pure `classify(order, positions) -> ENTRY \| EXIT`, shared by the service, the gate and the limiter |
| `engine/risk/decision.py` | `Severity` (ranked), `Decision`, reason-code constants |
| `engine/risk/limits.py` | `RiskLimits` model, parameter registry (unit, direction), `validate_edit` |
| `engine/risk/floor.py` | `FloorParams`, `LockMode` and the pure `floor_of(params, peak)` and `next_floor(previous, params, peak)`. `RiskLimits` (task 4.1) will hand these parameters over, so the floor math has no dependency on the limits model |
| `engine/risk/pnl.py` | The pure `day_pnl(...)` (cash-flow formula, previous-close basis, net of charges) and `estimate_exit_cost` |
| `engine/risk/mtm.py` | `MtmTracker`: latest and accepted price per instrument, outlier filter, `MTM_day` and the day's peak |
| `engine/risk/checks/` | One module per rule, each a `RiskCheck` |
| `engine/risk/gate.py` | `RiskEngine` (implements `RiskGate`), audit, shadow/enforce |
| `engine/risk/monitor.py` | The continuous loop: ticks and a 1 s timer |
| `engine/risk/kill.py` | `KillSwitch`, the `Flattener` Protocol and its simple S3 implementation |
| `engine/risk/stops.py` | `CatastrophicStops` |
| `engine/risk/ratelimit.py` | `RateLimitedAdapter`, a `BrokerAdapter` wrapper |
| `engine/risk/state.py` | `DayState` and the pure `merge_day_state` (the one-way rule for persisted day state) |
| `engine/risk/repository.py` | `RiskRepository` Protocol and its records (`StoredLimits`, `RiskEvent`). As for the order repository, the Postgres implementation is `engine/persistence/postgres_risk.py` and the in-memory fake is `tests/engine/risk_fakes.py`, so production can never fall back to in-memory storage |

`engine.risk` imports `engine.orders` and `engine.broker`; the reverse never happens, except through the existing `RiskGate` Protocol and two small hooks (decision 4). `contracts` stays free of internal imports (import-linter, unchanged).

### 2. `RiskCheck` and the gate

A check is a `Protocol` with a name (its stable rule id) and either or both methods:

```python
class PreTradeCheck(Protocol):
    name: str

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision: ...


class ContinuousCheck(Protocol):
    name: str

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision: ...
```

`Decision` carries a `Severity`, a reason code and an `inputs` dict for the audit. `RiskContext` is an immutable view built per evaluation: limits, today's state, MTM figures, positions, open orders, `now`, instrument facts. Checks do no I/O, so every rule is a pure function of its context and is unit-tested directly.

`RiskEngine.check(order)` builds the context, runs the pre-trade checks of every rule in `enforce` or `shadow` mode, acts on the most severe decision and returns `None` or a reason that starts with the code. An exception inside a check becomes REJECT (pre-trade) or HALT_NEW_ENTRIES (continuous). `Severity` is an `IntEnum`, so "most severe" is `max()`.

*Why a context object rather than checks reading engine state:* it keeps checks pure, makes shadow mode trivial (same context, decision logged and ignored), and makes the fail-closed path obvious (no context can be built, so reject).
*Alternative:* checks holding a reference to the service. Rejected: hidden I/O, harder property tests, and a circular import.

### 3. Binding the gate to trading state without changing the seam

`OrderService` needs a gate at construction; the gate needs the service's `orders` and `positions`. The gate is built first with an unbound `TradingView` (a `Protocol` of two read-only mappings); `main` calls `risk.bind(service)` once the service exists. Until bound, every entry is rejected (fail closed, spec "Fail closed"). The gate runs inside the service's lock, so its reads are consistent. The monitor reads between awaits, which are atomic in asyncio, and it never mutates service state; it only issues orders through `service.place`.

### 4. Two hooks in `OrderService`, and the exit exception

1. **Fill listeners.** After `_on_fill` applies a fill, the service awaits each registered listener with `(fill, position)`. The risk engine uses it to keep today's fills, detect a position returning to flat (cooldown, stop cancel) and resize stops.
2. **Exits when degraded.** The `persistence_ok` and `reconciled` checks move *after* classification: entries are refused as today; an exit is accepted. If its create-write fails, the order is kept in memory, the write is queued behind earlier writes (the existing pending-write queue), and it is submitted. If the engine then crashes, the broker holds an order the database never saw. Restart reconciliation already treats that as a difference and HALTs entries, which is the safe outcome.

*Why change the order path at all:* safety rule 9 says "keep managing existing exits", and the S2 behaviour makes the kill switch unable to flatten when Postgres is down. This is the one place where S3 weakens the "persist before send" rule from S2, and only for exits. It is flagged for the owner's review (see Risks).

### 5. Entry versus exit

`classify` looks only at the position in the same instrument and product. An order is an exit if its side is opposite to the position's sign and its quantity is at most the position's absolute quantity. Otherwise it is an entry, which includes a flip, and a new position. Resting stops and exit orders are not netted: two exits that both fill could flip a position. Mitigation: the stop is cancelled when the position goes flat, and S4's exit manager sizes exits against pending ones.

### 6. Day state, and how the "post-close reset" works

One row per `(account_id, trade_date)` in `risk_day_state`, with `trade_date = IST calendar date`. A new date has no row, so counters, halt and peak start fresh: that is the reset. A kill therefore clears at the first evaluation on the next IST date, which matches "locked until the post-close reset" because nothing trades after 15:30 anyway. Columns:

| Column | Write rule |
|---|---|
| `peak`, `floor` | upsert with `GREATEST(existing, new)` |
| `halt_level` and `halt_reason` | `GREATEST` on the level. Stored as 0 (none), 10 (MTM-blind) and 30 (kill); 20 is reserved for the profit-target halt that `add-risk-guards` adds (PRD ranking: HALT_NEW_ENTRIES < TARGET_HALT < KILL), so a persisted value never has to be renumbered |
| `entries_today` | `GREATEST` |
| `cooldown_until` | `GREATEST` |
| `loss_streak` | plain set under the service lock (it legitimately resets on a win) |
| `close_marks` | set once at the close: last price per open position, used as the next day's reference |

The in-memory copy is authoritative while the engine runs. It is persisted at most every `RISK_PERSIST_INTERVAL_S` and always before acting on HALT or worse. On restart: `peak` and `floor` resume as `max(stored, recomputed)`; counters and halt level come from the row.

### 7. `MTM_day` from fills, net of charges, previous-close basis

For each position, the day's gross P&L is computed from cash flows rather than from the average price, so it is correct for carried, scaled and flipped positions alike:

```
qty_open  = qty_now - Σ signed fills today                 # quantity held at the start of the day
day_gross = Σ (−signed_fill_qty × fill_price) over today's fills
          + qty_now × last_price
          − qty_open × reference_price                     # reference = previous close (see below)
MTM_day   = Σ day_gross − Σ fill costs today − Σ estimated exit cost of open positions
```

The estimated exit cost is `fill_costs(category, closing side, |qty_now|, last_price).total`, from the paper cost model, so there is one cost table to keep right (Q18). Today's fills come from the fill listener, and from the loaded state on restart.

*Reference price.* `Tick` carries no previous close, and there is no REST quote client. The engine records its own last price per open position at the 15:30 close (`close_marks`) and uses it the next day. If the engine was down at the close, the average price is the fallback and `RISK_REFERENCE_MISSING` is recorded. *Alternative:* take the previous close from Dhan history (REST). Rejected for S3: it needs the adapter that `add-dhan-readonly` builds, and the engine's own mark is what the owner would see on the day anyway.

### 8. The floor

`floor_of(params, peak)` implements PRD §7.1 as a pure function over `Decimal`, with two deliberate refinements. The trail term applies only once a whole step of peak is reached (the literal formula is 0 at peak 0 and would put the floor at 0 from the first tick). `FloorParams` refuses settings that would put the floor at or above the peak that armed it (an instant kill), and amounts that would overflow the 28-digit arithmetic. `giveback_pct` is a percent number like every `_pct` limit, strictly between 0 and 100. The runtime floor is `max(floor_prev, floor_of(params, peak))`. The pre-trade headroom check, the breach check and the edit re-simulation all call the same function, so the three can't disagree. Property tests (`hypothesis`) generate MTM paths and assert the floor never decreases and equals `max` of the formula terms.

### 9. Limits storage and tightening

`risk_limits` holds one JSON document per account plus a version. The registry gives each parameter a unit and a direction, which makes `validate_edit` mechanical: for every changed parameter, compare old and new in its direction. Outside 09:15–15:30 IST the direction check is skipped but type, range and consistency checks still apply. Then the floor re-simulation runs (spec "A limits edit may not lower the floor") with today's peak.

| Parameter | Unit | Tightens when |
|---|---|---|
| `per_trade_max_risk`, `daily_max_loss` | INR | down |
| `catastrophic_sl_pct`, `price_band_pct`, `outlier_pct` | PCT | down |
| `max_trades_per_day`, `loss_streak_limit`, `max_open_total`, `max_open_per_underlying` | COUNT | down |
| `cooldown_seconds` | SECONDS | up |
| `entry_cutoff`, `square_off_time` | TIME_OF_DAY | down (earlier) |
| `stale_s`, `feed_down_s`, `mtm_blind_s`, `chase_interval_s`, `exit_escalate_s` | SECONDS | down |
| `trail_loss_x`, `lock_trigger`, `trail_step`, `giveback_arm`, `giveback_abs` | INR | down |
| `trail_loss_y`, `lock_at`, `trail_by` | INR | up |
| `giveback_pct` | PCT | down |
| `exit_buffer_pct` | PCT | up (more aggressive exits) |
| `max_chase_steps` | COUNT | up |
| `lock_mode` | ENUM | `enum_order` OFF < LOCK < LOCK_AND_TRAIL. TRAIL counts its steps from a different start than LOCK_AND_TRAIL, so the ranking cannot place it: only OFF → TRAIL tightens, and every other move involving TRAIL waits for the close (stricter than the floor re-simulation alone, which only looks at today's peak) |
| each optional floor term (on or off) | BOOL | `enable_only`, except a *leg* of the giveback term (`giveback_abs`, `giveback_pct`): the giveback is the larger of its legs, so adding a leg to a term that is already on loosens, and dropping one tightens |
| each rule's `mode` | ENUM | shadow → enforce only |

*Why the floor re-simulation is not enough on its own:* it replays only today's peak, so an edit that lowers the floor at a higher peak than today's passes it. The directions above therefore have to be right at **every** peak, and a `hypothesis` property test checks that: for random pairs of floor settings, an edit that `loosened_keys` calls a tightening never gives a lower floor at any peak.

The table is a proposal for the owner to review: a wrong direction here is a hole in the tighten-only rule. `TIME_OF_DAY` is a new unit beyond PRD §7.2's list, so §7.2 is updated in the docs task.

Seeding: there is no default. A fresh account has no `risk_limits` row and rejects entries until `cli risk set-limits --file …` runs. This also avoids baking research numbers in as defaults.

### 10. The monitor

One supervised task. It subscribes to the tick bus as consumer `risk` and also wakes every second. On each wake it updates `MtmTracker`, builds the context, runs the continuous checks (equity floor, feed-down, MTM-blind, square-off, cooldown expiry, day rollover) and acts on the most severe decision. Tick-path work is arithmetic only; persistence is throttled (decision 6). Because the bus drops the oldest tick when the queue is full, the monitor needs only the latest price per instrument, which this suits.

*Known limitation.* The first tick of an instrument is accepted with nothing to compare it with, so a corrupt but positive and finite first tick (say ten times the real price) could raise the peak for good. Ticks that are not finite or not above zero are rejected outright, but this case is not caught by the tracker alone: task 7.1 seeds a baseline for each open position's instrument from its previous close or average price, so such a tick needs a confirming tick like any other jump.

*Liveness.* The monitor stamps a heartbeat each loop. The gate rejects entries with `RISK_STATE_UNAVAILABLE` if the stamp is older than three intervals, so a dead monitor can't leave the account unwatched. `_keep_alive` restarts it, as it does for the snapshot writer. Because that rule multiplies the interval, `RISK_MONITOR_INTERVAL_S` is capped at 5 s (a stalled monitor is noticed within 15 s) and `RISK_PERSIST_INTERVAL_S` at 10 s (at most that much peak movement can be lost on a crash); configuration cannot loosen either.

### 11. Kill switch and the flattener

`KillSwitch.trigger(reason, source)`:
1. Read the day's `halt_level`; if it is already KILL return the stored result (idempotent). Set KILL and persist (a failed write is retried, the kill continues).
2. Cancel every open entry order.
3. Hand every open position to a `Flattener` (a `Protocol`). The S3 implementation places an aggressive limit at `last_price ∓ exit_buffer_pct`, waits `chase_interval_s`, cancels (waiting for the confirmation, never blind), and places a new one at the current price, up to `max_chase_steps`. It loops until flat, re-reading positions so an entry that fills during its cancel is caught.
4. After `exit_escalate_s` without a flat book it logs a critical alert and keeps going. Dhan's Exit All is S7.
5. Stops are cancelled by the stop manager once a position is flat (decision 12).

Every order uses a derived idempotency key, `risk:kill:{date}:{instrument}:{product}:{step}`, so a restart mid-flatten repeats nothing. On startup, after reconciliation, a day that is still at KILL with an open position resumes at step 3. The state lives in `risk_day_state`, not `recovery_state`, so the `ResumeRecovery` command cannot clear it by construction.

The automatic kill (floor breach) is internal to the engine. The manual kill command arrives over the Redis stream, so it needs the consumer: safety rule 8's independence holds for automatic kills only until Telegram `/kill` in S7 (PRD Q8). Dhan's own kill switch is the manual backstop until then.

*Why not wait for S4's exit manager:* the S3 acceptance requires "kill switch flattens all". The seam keeps S4's replacement a drop-in.

### 12. Catastrophic stops

`CatastrophicStops` reacts to fills (listener) and to `ensure_all()`. For each position it computes the desired stop (quantity, trigger) and compares it with the resting stop order from `orders` (source `risk-stop`, state Open). On a difference it places the new stop, then cancels the old. When a position is flat it cancels the stop first. Orders go through `service.place` and the gate, as exits, with derived idempotency keys.

- **Order type in one place.** `STOP_ORDER_TYPE = OrderType.STOP_MARKET` is a constant in `stops.py`. If Dhan converts or rejects SL-M under MPP (PRD §12 Q17), the change is a new `STOP_LIMIT` type and this constant.
- **Q17 gate.** Live orders stay closed by `RISK_LIVE_NOT_ENABLED` (risk-gate spec). `add-live-trading` must not lift it until the recorded Dhan responses from `add-dhan-readonly` confirm the stop behaviour, recorded as a new decision entry (D-034) that either confirms SL-M or refines D-007.
- **Market hours only.** Stops are placed between 09:15 and 15:30 IST. S2's expiry cancels all open orders at 15:30, stops included; the manager does not re-place them after the close or treat their expiry as "unprotected". At the open it runs `ensure_all()` (Q12). The same call runs after startup reconciliation.
- **Unprotected.** A failed placement sets a per-position flag the gate reads (`RISK_STATE_UNAVAILABLE`-style rejection of entries) and retries with backoff.
- **Paper broker.** `PaperBroker` rests a stop-market order and triggers it on a trade at or through the trigger, then fills like a market order with slippage. It must also restore resting stops after a restart (as it does for limits).

### 13. Rate limiter

`RateLimitedAdapter` wraps the `BrokerAdapter`; `place` and `cancel` acquire a token first, `order_book` and `updates` pass through. It keeps two sliding windows (1 s, 60 s). A share of each window is reserved for exits and cancels. An entry that finds no non-reserved token raises `BrokerRejected("RISK_RATE_LIMITED …")`, which the service already turns into a `Rejected` order. An exit or cancel waits for the next token, in FIFO order. Intent comes from `classify`; every cancel counts as exit priority. Ceilings are constants in code (`ORDERS_PER_S_CEILING`, `ORDERS_PER_MIN_CEILING`), validated at startup in `EngineSettings`.

### 14. Contracts, commands and snapshot

All additions keep `schema_version = 1` because they are new optional fields or new union members:
- `OrderType.STOP_MARKET`; `Order` and `PlaceOrder` gain optional `initial_stop` and `trigger_price` (a stop-market needs a trigger and no limit price; `check_limit_price` is extended accordingly).
- New commands: `KillSwitch`, `SetRiskLimits`, `SizeOrder`. `CommandResult` gains optional `sized_quantity` and `risk_amount` for the sizing reply.
- `Snapshot` gains an optional `risk` block: `MTM_day`, floor, peak, `halt_level` and reason, `entries_today`, `loss_streak`, `cooldown_until`, positions without a stop. The UI never computes any of it.
- CLI: `kill`, `risk set-limits`, `risk status`, `risk size`, and `--stop` on `order place`.

### 15. Audit

`risk_event` is append-only: the migration revokes UPDATE and DELETE from the engine role. Writes are queued and retried like order writes, and a failed audit write never blocks an exit. One row per gate verdict, plus one per non-ALLOW rule decision, plus limit edits and `RISK_REFERENCE_MISSING`-type notices.

### 16. Testing

- **Unit:** one test file per check, covering allow, reject and boundary values; limits validation including every direction; the order classifier.
- **Property (`hypothesis`):** the floor never decreases for any MTM path and equals the formula; sizing never exceeds the budget and rounds down to whole lots; `day_gross` equals realized plus unrealized for positions opened today.
- **Integration (Redis + Postgres, `SimulatedFeed`, `PaperBroker`):** `kill -9` during a flatten and restart; Postgres down (entries rejected, kill still flattens, writes drained afterwards); feed silent (halt, then clears); rate limiter with a fake clock; a test that no order path reaches the broker without the gate, updated for the real gate.
- **Mutation checks** the `reviewer` subagent asks for, run on the floor ratchet, tighten-only validation and exit-never-blocked.

## Risks / Trade-offs

- **Exits accepted before they are stored** (decision 4) → entries stay refused; reconciliation HALTs on a broker order the engine has no record of; an owner-visible alert is logged. The owner should confirm this exception.
- **Net MTM depends on the cost model** (Q18) → `costs.py` says FUTURE and EQUITY_DELIVERY rates are unverified, and carried CNC positions use the delivery rates. The error is small against a floor, and the owner verifies rates against Dhan's calculator before live. Tests pin the formula, not the rates.
- **Manual kill depends on the Redis consumer until S7** → automatic kills do not; Dhan's kill switch is the stated backstop (PRD Q8).
- **A bad low tick can trip the kill** → the loss side is deliberately not debounced (PRD §7.1) and only the peak is protected from outliers. S1's feed validation is the first defence; the false-kill cost is a locked day, which is accepted.
- **Persistence lag on peak and floor** → at most `RISK_PERSIST_INTERVAL_S` of peak movement can be lost on a crash; the floor re-derives from fills and prices on restart. It is always written before a HALT or kill.
- **The limiter can make an exit wait inside the service lock** → fills for other orders are delayed by up to a second or two during a large kill. Accepted at phase-1 volumes; acquiring outside the lock is a follow-up if the live latency data says so.
- **Position at or above the freeze quantity** → S2 validation rejects any single order at or above it, and slicing is not built. Sizing and per-trade risk keep positions far below it; a pathological scale-in could still create an unexitable position. Flagged for S4.
- **Pre-open stop placement at Dhan is unverified** → in paper it is fine. S7's morning sequence owns the real timing; until then a carried position is unprotected from the open until `ensure_all` places its stop.
- **MTM-blind locks entries for the day without a broker lock** → an addition beyond the PRD's one line ("flatten"); chosen so a feed outage cannot be followed by a fresh entry, while not burning Dhan's one-way kill switch on a data problem. Easy to relax in the spec if the owner prefers.
- **Old tests place entries without a stop** → they gain an `initial_stop`; the fixtures change, no behaviour does.

## Migration Plan

1. Merge after `add-order-position-core` is archived; add the `MODIFIED` deltas (tasks 1.1).
2. Migration `0004` runs in the migrate service: new tables, two nullable columns on `orders`, REVOKE on `risk_event`. It is forward-only; `downgrade` drops what it created.
3. Deploy the engine. With no `risk_limits` row it rejects every entry. The owner runs `cli risk set-limits` (example file in `docs/`) before the first paper trade.
4. Rollback: stop the engine, `alembic downgrade 0003`, redeploy the previous image. Paper only, so no live exposure; open paper positions are re-protected on the next start of the new version.

## Open Questions

- Default values for the three rate-limiter settings (per second, per minute, reserved exit share), and the ceilings. Proposal: ceiling 5 per second and 200 per minute, defaults 3 per second, 120 per minute and a 40% exit share. The owner confirms at review; none of it changes a spec.
- Whether `cli kill` asks for `--yes` (the hold-to-confirm is a dashboard idea). Proposal: yes.
