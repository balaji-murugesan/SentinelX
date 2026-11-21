# Design

## Context

**What exists** (from `add-risk-engine`, on `feat/risk-engine`, PR #9, not yet merged):
- **One gate, many checks.** `RiskEngine.check` (`engine/risk/gate.py`) classifies an order as entry or exit and runs the pre-trade checks in `checks/registry.py`. Exits run only the checks with `applies_to_exits = True`. Every check is a pure function of an immutable `RiskContext` (`decision.py`). The most severe enforced decision wins. Each rule has a stable name, which is also its `mode.<name>` key in the limits (`limits.RULES`).
- **The monitor** (`monitor.py`) wakes on each tick and every second. It builds the context and runs the continuous checks. It acts on KILL through `KillSwitch.on_kill`, locks the day at `HaltLevel.MTM_BLIND` itself, and otherwise holds a standing, self-clearing halt that the gate reads through `entry_halt()`.
- **Day state** (`state.py`, `daybook.py`) is one row per account and IST date, merged one-way (`merge_day_state` in Python, `GREATEST` in SQL, one contract suite for both). `DayBook.entry_allowed()` counts an entry and persists the count before the entry is sent. The gate calls it inside the order service's lock, which is what makes a count-then-check atomic.
- **Halt levels.**
  - `HaltLevel` (stored): `NONE 0`, `MTM_BLIND 10`, `KILL 30`, with 20 reserved for the target halt.
  - `Severity` (decisions): `… HALT_NEW_ENTRIES 20 < TARGET_HALT 30 < KILL 40`.
  - The kill switch is idempotent only at `KILL` (`kill.py:419`), and the flatten resumes while the stored level is at least `MTM_BLIND` (`kill.py:489`).
- **Square-off** (`kill.SquareOff`) flattens `INTRADAY` positions through the shared `ChaseFlattener`. Its filter is a product set.
- **Limits** (`limits.py`) are a registry of `LimitSpec(key, unit, direction, optional, …)`. `parse_limits` refuses missing required keys and unknown keys. A stored document that no longer parses makes the limits `UNUSABLE` (entries refused), and it can be replaced only after the close (`limits_service.py:190-198`). `LimitValue` (`contracts/commands.py`) is `str | int | bool | None`.
- **Gaps found while planning:**
  - `Tick` has no bid or ask.
  - No order is ever modified.
  - No ban list exists.
  - Order quantity is in units, a multiple of the lot size.
  - `OrderService` has fill listeners but no hook for broker rejections.
  - The instrument master can be searched but cannot map an underlying symbol to its spot instrument.

Motivation and scope: proposal.md. Requirements: the six spec deltas. Owner decisions taken for this change (2026-11-21): spread check deferred, ban list loaded by CLI file, modification cap moved to S4, expiry giveback applies from the moment the account has expiring exposure, lot caps count every segment except cash equity and index (§5) while the F&O ban list covers only `NSE_FNO` and `BSE_FNO`.

## Goals / Non-Goals

**Goals:**
- **Add rules, not machinery.** Each new rule is a check appended to the registry. The gate's and the order service's control flow stays as it is, apart from one new listener hook.
- **Counters persist like the existing ones.** Anything a restart must not forget (gross lots, last entry time, expiry exposure) goes in the day row with a one-way merge.
- **Expiry overrides cannot loosen.** That is enforced at validation time, so no runtime path has to choose the tighter value.

**Non-Goals:**
- A latching rejection breaker, persisted rolling windows, or a holiday calendar (see Risks).
- Fetching the ban list or a previous close from any outside source.

## Decisions

### 1. Module layout

| Path | Contents |
|---|---|
| `engine/risk/checks/lots.py` | `LotCaps` (`lot_caps`): per-underlying, total and daily gross caps; the expiry caps when the order's contract expires today |
| `engine/risk/checks/flow.py` | `EntryPacing` (`entry_pacing`) and `RejectBreaker` (`reject_breaker`, pre-trade and continuous) |
| `engine/risk/checks/target.py` | `ProfitTarget` (`profit_target`, continuous) |
| `engine/risk/checks/policy.py` | `InstrumentPolicy` (`instrument_policy`): deny, allow, ban list, minimum premium, strike distance |
| `engine/risk/expiry.py` | `expires_today(instrument, now)` and the effective-value helpers (cutoff, square-off time, lot caps, minimum premium) |
| `engine/risk/rejections.py` | `RejectionWindow`: the broker rejections of the last 300 s, fed by the order service's rejection listener |
| `engine/risk/banlist.py` | `BanListService`: load at start, the `SetBanList` command, `for_date(d) -> frozenset[str] \| None` |
| `engine/persistence/postgres_risk.py` | Ban-list load and save on the existing `RiskRepository`, plus the new day-state columns; the in-memory fake in `tests/engine/risk_fakes.py` follows |

The existing `EntryCutoff` and `FloorHeadroom` checks, `SquareOff` and `RiskLimits.floor_params` learn the expiry overrides; no new rule names are needed for them. The new rule names are added to `limits.RULES`, and all of them may run in shadow mode, since the PRD lets a new rule be tried out first.

### 2. Context and state additions

`RiskContext` gains four fields with safe defaults, so existing tests still build it:
- `recent_entries`: the times of entries allowed in the last 60 s.
- `broker_rejections`: the times of broker rejections in the last 300 s.
- `target_confirmations`: an int.
- `ban_list`: `frozenset[str] | None`, where None means not loaded.

It also gains one lookup, `spot_key(symbol) -> InstrumentKey | None`. `DayState` gains `entry_lots_today: int = 0`, `last_entry_at: datetime | None` and `expiry_exposure: bool = False`. `merge_day_state` takes the maximum of the first two and ORs the third, and Postgres uses `GREATEST` for all three (Postgres orders `false < true`, so `GREATEST` is OR). The existing contract suite gains a case for each.

`RiskState.entry_allowed()` becomes `entry_allowed(lots, expiring)`. One persisted write counts the entry, adds its lots, stamps `last_entry_at` and switches `expiry_exposure` on when the contract expires today. A failed write undoes all four in memory and refuses the entry with `RISK_STATE_UNAVAILABLE`, as today. The per-minute window lives in `DayBook` memory, and only times whose write succeeded go in.

*Why at gate-allow and not at fill:* it matches `entries_today`, which the spec defines as "allowed, whether or not the broker takes it". It is also the only point where count-then-check is atomic, because the gate runs inside the service lock. The cost is that a rejected entry uses up budget, which is the conservative side.

### 3. Profit target

The monitor keeps a `target_confirmations` counter. On each pass where `MTM_day` is known and at or above the target, it adds one. Any other pass, or a target that isn't set, resets it to zero. `ProfitTarget.continuous` returns `Decision(Severity.TARGET_HALT, "RISK_TARGET_HALT", …)` once the count reaches `target_confirm_ticks`.

`Monitor._act` gets a branch after the KILL branch, so a KILL in the same pass wins. It mirrors `_lock_blind_day`: `raise_halt(HaltLevel.TARGET_HALT, reason)`, `flush()` before acting, then the existing `_keep_flattening`, which already flattens at any level of `MTM_BLIND` or above. Because 20 sits between 10 and 30, the kill switch still escalates a target-halted day to `KILL`, and the gate's day-lock check (`halt_level > NONE` → `RISK_HALTED_DAY`) refuses entries with no new code. `HaltLevel.TARGET_HALT = 20` is added to the contract.

*Alternative:* trigger the kill switch with reason `RISK_TARGET_HALT`. Rejected. It would store `KILL`, which loses the distinction S7 needs: D-030 locks the broker after either stop, but the dashboard and the audit must say which stop it was.

### 4. Broker rejections

`OrderService.add_rejection_listener(listener)` is called inside the lock in two places: when `_submit` catches `BrokerRejected` from the adapter, and when `_apply_update` applies a broker `Rejected`. It is not called for gate refusals, for `settle_in_flight`, or for `reconcile_unknown`.

The rate limiter's refusal becomes `RateLimited(BrokerRejected)`, a subclass that the service still treats as a rejection, so the listener can exclude it by type rather than by parsing the reason string.

`RejectionWindow` holds timestamps and prunes them on read. `RejectBreaker` judges at both phases: pre-trade, so the entry right after the N-th rejection is already refused, and continuous, so the monitor's standing halt and its edge-triggered critical log (`_audit_changes`) report the trip once.

### 5. Lot caps and pacing

```
lots(q, k)    = 0 if k is cash equity or index, else ⌈|q| / lot_size(k)⌉   # master lookup; unknown → refuse
pending(u)    = Σ lots(|o.quantity| - o.filled_quantity, o.key) for non-final orders whose unfilled part classifies as ENTRY, underlying(o) = u, o ≠ order
open(u)       = Σ lots(p.quantity, p.key) for open positions on u (carried included)
per-underlying: open(u) + pending(u) + lots(order) ≤ cap_u      # cap_u = expiry cap if order expires today
total:          Σ_u open(u) + Σ_u pending(u) + lots(order) ≤ cap_total
daily gross:    day.entry_lots_today + lots(order) ≤ max_gross_lots_per_day
```

The caps count the lots of every segment except cash equity and index (`NSE_EQ`, `BSE_EQ`, `IDX_I`), read from the instrument key, so an unknown equity is skipped and an unknown contract in any capped segment (MCX_COMM included) is refused. The exemption is a deny-list so that a segment added later is capped unless someone exempts it: forgetting fails closed (owner ruling, 2026-11-21). An equity entry is not judged by them and adds 0 to `entry_lots_today`, though it still counts as an entry for `entries_today` and pacing. A non-final order is reclassified on its unfilled part against the current positions with the same `classify`, so a resting order that only reduces a position does not reserve lots. Pacing reads `recent_entries` (60 s window, `max_entries_per_minute`) and `day.last_entry_at` (`min_entry_gap_s`).

### 6. Expiry profile

- `expires_today(instrument, now)` is `instrument.expiry == ist_date(now)`.
- Effective values come from small pure helpers, so `EntryCutoff`, `LotCaps`, `InstrumentPolicy` and `SquareOff` share one definition.
- **Floor.** `RiskLimits.floor_params(expiry: bool)` replaces each set giveback override in the giveback term. `expiry` is `day.expiry_exposure` in the monitor's ratchet and in the edit re-simulation. In `FloorHeadroom` it is `day.expiry_exposure or expires_today(order)`, the spec rule "An expiring entry must leave headroom under the expiry floor". The runtime floor stays `max(floor_prev, floor_of(…))`, so turning the term on can only raise it.
- **Square-off.** `SquareOff` computes each intraday position's time: the earlier of the base and the override when its contract expires today or has no master record. It asks the flattener for exactly those positions. `Flattener.flatten` gains an optional `positions: frozenset[PositionId]` filter alongside the product set. S4's replacement must keep that parameter.
- **Validation** lives in `_consistency_problems`:
  - each set override is no looser than its set base;
  - a giveback leg override needs its base leg when the base term is on;
  - effective expiry cutoff < effective expiry square-off;
  - per-underlying cap ≤ total cap, for the base pair and the expiry pair;
  - a target needs a confirmation count.
- **Tightening directions.** The `hypothesis` property "a tightening never lowers the floor at any peak" is extended to the expiry giveback keys, with `expiry` drawn both ways.

### 7. Instrument policy and the ban list

The order of checks inside `InstrumentPolicy` is deny → allow → ban list (F&O segments: `NSE_FNO`, `BSE_FNO`) → minimum premium → strike distance. The first refusal is returned and the audit gets one row, which keeps it to one rule id. Options are instruments with an `option_type`.

`BanListService` loads today's list at start (`None` on a storage error, retried by the monitor like the day book) and keeps the lists for today and later dates in memory. `SetBanList(trade_date, symbols, idempotency_key)` is a new member of the command union, routed by the consumer like `SetRiskLimits`. The CLI `risk set-ban-list --file PATH [--date YYYY-MM-DD]` reads one symbol per line, ignores blank lines and lines starting with `#`, and upper-cases each symbol. The date defaults to today in IST. The tighten-only rule for today during market hours is new ⊇ stored. Each load writes a `risk_event` (rule `ban_list`).

**Spot price for strike distance.** `InstrumentMaster.spot_of(symbol)` returns the `IDX_I` index row, or else the `NSE_EQ` equity row, whose `underlying_symbol` equals `symbol`. The price comes from `ctx.quote` and must be no older than `stale_s`. The index must be subscribed: the owner lists it in `FEED_INSTRUMENTS`, and the README says so. Without it the check fails closed with `RISK_UNDERLYING_UNPRICED`.

### 8. Limits

| Key | Unit | Direction | Required |
|---|---|---|---|
| `max_open_lots_per_underlying`, `max_open_lots_total`, `max_gross_lots_per_day` | LOTS | down | yes |
| `max_entries_per_minute`, `reject_breaker_n` | COUNT | down | yes |
| `min_entry_gap_s` | SECONDS | up | yes |
| `daily_profit_target` | INR | down | no |
| `target_confirm_ticks` | COUNT | down | no (required with a target) |
| `entry_cutoff_expiry`, `square_off_time_expiry` | TIME_OF_DAY | down | no |
| `max_open_lots_per_underlying_expiry`, `max_open_lots_total_expiry` | LOTS | down | no |
| `min_premium`, `min_premium_expiry` | INR | up | no |
| `giveback_arm_expiry`, `giveback_abs_expiry`, `giveback_pct_expiry` | INR, INR, PCT | down | no (floor terms) |
| `max_strike_distance_pct` | PCT | down | no |
| `allowed_underlyings` | SYMBOLS | subset | no |
| `denied_underlyings` | SYMBOLS | superset | no |

- **New units.** `Unit.LOTS` is a whole number of at least 1. `Unit.SYMBOLS` is a non-empty list of non-empty symbols, upper-cased, held as a `frozenset[str]` and stored as a sorted list.
- **Set directions.** `Direction.SUBSET` and `SUPERSET` are compared in `_is_tightening`. For an optional `SUPERSET` limit, removing it loosens and setting it tightens, which is the existing `enable_only` rule.
- **Contract.** `LimitValue` gains `list[str]`, so the change to `contracts/commands.py` is additive.
- **Floor flag.** The expiry giveback keys carry `floor_term=True`, so an edit to them is re-simulated against today's floor.

*Why required:* PRD §7 defines no defaults, and a cap that is silently absent is a missing guard. The profit target is optional because PRD R1 frames it as a choice, and the expiry overrides fall back to their base values by definition.

### 9. Contracts, snapshot, CLI, persistence

- **Contracts.** `HaltLevel.TARGET_HALT = 20`. `RiskStatus` gains `entry_lots_today: int = 0`, `ban_list_loaded: bool = False` and `broker_rejections_5m: int = 0`. The `SetBanList` command is added. `schema_version` stays 1.
- **CLI.** `risk status` prints the new fields; `risk set-ban-list` as above.
- **Migration `0005`.** `risk_day_state` gets `entry_lots_today INT NOT NULL DEFAULT 0`, `last_entry_at TIMESTAMPTZ NULL` and `expiry_exposure BOOLEAN NOT NULL DEFAULT false`. A new table `risk_ban_list(account_id, trade_date, symbols JSONB, loaded_at, PRIMARY KEY (account_id, trade_date))` follows the account-id rule (D-020). The engine role gets DML through the existing default privileges. `downgrade` drops what it adds.

### 10. Testing

- **Unit:** one test file per new check, covering allow, refuse and boundary values (at the cap, one over; window edges at exactly 60 s and 300 s); the expiry helpers; every new limit row's direction; each consistency rule; the `SYMBOLS` parser.
- **Property (`hypothesis`):**
  - The lot caps never let open plus pending lots exceed a cap over random sequences of place, fill, reject and cancel.
  - The expiry-giveback floor is never below the base floor at any peak.
  - Tighten-only directions never lower the floor, with the expiry keys included.
- **Monitor:** target confirmation, reset on a dip and on unknown MTM, `TARGET_HALT` then a kill escalating to `KILL`, restart keeping the target halt, expiry square-off picking only expiring intraday positions.
- **Order service:** the rejection listener fires for an adapter rejection and a `Rejected` update, and not for gate refusals, `RateLimited`, `settle_in_flight` or `reconcile_unknown`.
- **Integration (Redis + Postgres):** day-state columns round trip and merge one-way; ban-list load, restart and fail-closed; the CLI round trip for `set-ban-list`.
- **Mutation checks** the reviewer requests, on the lot-cap arithmetic, the expiry consistency rules and the target confirmation.

## Risks / Trade-offs

- **[The rolling windows are in memory only]** A restart forgets entries counted for the per-minute cap and rejections counted for the breaker. → The minimum gap is persisted, and a restart is a deliberate act that takes longer than either window's effect would matter. Persisting the windows would need a table of timestamps for little gain.
- **[The breaker clears by itself]** After 5 minutes without rejections, entries reopen even if the cause (say, no margin) persists, and the next entry may be rejected again. → This follows PRD R6 (HALT_NEW_ENTRIES). Owner decision: keep it, or latch for the day at a new halt level below `MTM_BLIND` (a spec change).
- **[A broker rejection after the acknowledgement is not applied]** The order state machine has no `Open → Rejected` transition, so a broker `Rejected` update for an order the broker had already acknowledged (for example, a rejection on Dhan's order-update stream after the ack; when Dhan rejects is to be confirmed in the S7 adapter work) is logged as `invalid_transition` and changes nothing. The rejection listener's call in `_apply_update` therefore cannot fire in production today: the breaker counts placement-time broker rejections only, until `Open → Rejected` exists. Knock-on risks of the same gap, beyond the breaker: a rejected catastrophic SL still looks resting, so its position looks protected; a rejected chase exit stalls the flatten; a lot-cap reservation for a rejected entry is never released; and restart recovery finds differences against the broker's order book. → Fixed by a separate OpenSpec change (see tasks.md, Workflow follow-up), not here. That fix must land before S7 enables `DhanBroker` order placement.
- **[Fast confirmation]** On a busy tick stream two monitor passes can be milliseconds apart, so `target_confirm_ticks` = 2 confirms almost at once. → This is PRD's "consecutive MTM updates" read literally. A time-based confirmation would be a spec change, if the owner wants one.
- **[The target halt flattens carried positions too]** It uses the kill's flattener, which exits every product, CNC holdings included. → This is consistent with the kill and with PRD R1 ("flatten"). Owner awareness.
- **[An unread ban list is retried at most every 15 s]** The retry runs inside the monitor's pass, so retrying on every pass would let a hung Postgres slow every pass. → Retried at most once per `BAN_LIST_RETRY_S` (15 s): at most one pass per window can be slowed, by up to the repository timeout (3 s), always after the pass has acted on kills and flattens, at the cost of F&O entries recovering up to 15 s later.
- **[A forgotten ban list blocks all F&O entries]** → Intended (fail closed). The README's morning checklist and `risk status` show it, and S7's morning sequence can automate it.
- **[The underlying-to-spot mapping is unverified against the real master]** Index rows may not use the same `underlying_symbol` as their options (for example "BANKNIFTY" vs "NIFTY BANK"). → Task 7.1 checks it against the recorded instrument-master fixture. A miss fails closed with `RISK_UNDERLYING_UNPRICED`; it never allows an entry.
- **[The expiry-exposure write can be lost]** The monitor writes `expiry_exposure` as soon as an open expiring position switches it on. If that flush fails and the engine restarts after the expiring position has closed, the flag is lost. → The stored floor does not fall (it is max-merged), but later ratchets use the base giveback until the next expiring position or expiring entry switches the flag on again.
- **[Expiry exposure switches on for an entry the broker later rejects]** → Conservative: the giveback is tighter than necessary for the rest of that day, never looser.
- **[A stored limits document becomes unusable on upgrade]** → This is the BREAKING step in the proposal: entries are refused until the owner loads a new limits file after the close. See the migration plan.
- **[The flattener gains a filter parameter]** S4's exit manager replaces the flattener. → The parameter is part of the `Flattener` Protocol, so the replacement inherits it, and add-trailing-exits should list it.
- **[The modification cap's absence before S4]** Nothing modifies an order today, so nothing can exceed Dhan's 25. → The PRD row moves to S4 together with the modify call.

## Migration Plan

1. Merge after `add-risk-engine` (PR #9) is merged. Archive after it, because the `risk-limits` delta adds to a spec that change creates.
2. Migration `0005` runs in the migrate service: three nullable or defaulted columns, one new table. It is forward-only in practice; `downgrade` drops them.
3. Deploy outside market hours. With the old limits document the engine refuses entries (`RISK_LIMITS_INVALID`) and accepts exits. The owner loads the new example-based limits file (`cli risk set-limits`) and today's ban list (`cli risk set-ban-list`) before the next open.
4. Rollback: stop the engine, `alembic downgrade 0004`, redeploy the previous image, and reload a limits file without the new keys. Paper only, so there is no live exposure.
