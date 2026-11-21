# Tasks

## 1. Scope record

- [x] 1.1 Update `PRD.md` for the owner's 2026-11-21 scope decisions:
  - in the §7 table, mark the bid-ask spread part of "Instrument policy" as deferred until a depth feed exists, and move "Modification cap" to S4 (`add-trailing-exits`);
  - in §11, update the S3 stage-table row and the `add-risk-guards` scope line to match, and add the modification cap to S4's scope and acceptance.

  Verify the links still resolve and `openspec validate add-risk-guards --strict` passes.

## 2. Contracts and persistence

- [x] 2.1 Make additive contract changes (`schema_version` stays 1):
  - `HaltLevel.TARGET_HALT = 20` in `contracts/snapshot.py`;
  - `RiskStatus` fields `entry_lots_today`, `ban_list_loaded` and `broker_rejections_5m` with defaults;
  - `LimitValue` gains `list[str]`;
  - a `SetBanList` command (`trade_date`, `symbols`, idempotency key) in the command union.

  Verify with `tests/contracts` round-trip tests, including that an older snapshot without the new fields still parses.
- [x] 2.2 Add `entry_lots_today`, `last_entry_at` and `expiry_exposure` to `DayState`, and extend `merge_day_state`: maximum, maximum, OR. Verify with the merge unit tests: a lower or older value never wins, and `expiry_exposure` never turns off within a day.
- [x] 2.3 Write Alembic migration `0005`:
  - the three `risk_day_state` columns;
  - the `risk_ban_list` table keyed by `(account_id, trade_date)`.

  Extend `engine/persistence/postgres_risk.py` (day-state upsert with `GREATEST`; `load_ban_list` and `save_ban_list` on `RiskRepository`) and the in-memory fake. Verify the shared repository contract suite passes against both stores with `TEST_DATABASE_URL`, and that `alembic upgrade head` then `downgrade 0004` runs cleanly.

## 3. Limits

- [x] 3.1 Extend `engine/risk/limits.py`:
  - add `Unit.LOTS` and `Unit.SYMBOLS`, and `Direction.SUBSET` and `SUPERSET`;
  - add the rows in design §8;
  - add the new rule names (`lot_caps`, `entry_pacing`, `reject_breaker`, `profit_target`, `instrument_policy`) to `RULES`, all shadowable;
  - extend `RiskLimits` and `to_values`.

  Verify the per-row direction tests cover every new key, and that `parse_limits(limits.to_values()) == limits` holds with symbol lists.
- [x] 3.2 Add the consistency rules to `_consistency_problems` (spec `lot-caps`, `expiry-day-profile`, `profit-target`):
  - per-underlying cap ≤ total cap, for the base pair and the expiry pair;
  - each set expiry override no looser than its set base;
  - a giveback leg override only for a leg the base term sets;
  - effective expiry cutoff earlier than the effective expiry square-off;
  - a target requires a confirmation count.

  Verify one unit test per rule, each naming the scenario it covers.
- [x] 3.3 Implement `RiskLimits.floor_params(expiry: bool)`, so the expiry giveback overrides replace their base values when `expiry` is true. Extend the `hypothesis` property "a tightening never lowers the floor at any peak" to the expiry keys with `expiry` drawn both ways, and add a property that the expiry floor is never below the base floor. Verify both properties pass, and run the reviewer's mutation checks on the new consistency rules.
- [x] 3.4 Update `docs/risk-limits.example.json` with every new key, keeping it marked "example values, not a recommendation". Extend the README's "Setting risk limits" section: the new required keys, the after-the-close upgrade step, and that strike distance needs the underlying's index in `FEED_INSTRUMENTS`. Verify the example file is accepted by `parse_limits` in a test, and `cli risk set-limits --file` accepts it against `TEST_REDIS_URL`.

## 4. Day counters and context

- [x] 4.1 Change `DayBook.entry_allowed` to `entry_allowed(lots, expiring)`. One persisted write counts the entry, adds the lots, stamps `last_entry_at` and switches on `expiry_exposure`; a failed write undoes the count, the lots and the time (only while still the same IST date), and the one-way expiry flag stays on. Keep the 60-second window of allowed-entry times in memory. Update the `RiskState` Protocol, the gate's call and the test fakes. Verify with day-book tests: a failed save raises nothing and refuses the entry with `RISK_STATE_UNAVAILABLE`, and the counters survive a reload.
- [x] 4.2 Add `recent_entries`, `broker_rejections`, `target_confirmations`, `ban_list` and `spot_key` to `RiskContext` with defaults. Fill `recent_entries` in both the gate's and the monitor's context builders now; `broker_rejections`, `target_confirmations`, `ban_list` and `spot_key` keep their safe defaults until tasks 6.2, 9.1, 7.2 and 7.4/7.1 feed them. Report `entry_lots_today` in the snapshot risk block. Verify the existing risk tests pass unchanged, and a snapshot round-trip test shows the gross-lot count.

## 5. Lot caps and entry pacing

- [x] 5.1 Implement `LotCaps` (`checks/lots.py`, design §5): per-underlying, total and daily gross caps, pending entries reserved until final, an unknown master record refused, and the expiry caps for an order whose contract expires today (using `engine/risk/expiry.py`, created here with `expires_today` and the effective-value helpers). Append it to `default_checks()`. Switch the inline expiry test in the gate's entry counting to `expires_today`. Verify every `lot-caps` scenario and the expiry-cap scenario as unit tests, plus a `hypothesis` property: over random place, fill, reject and cancel sequences, open plus pending lots never exceed a cap. Add a test that, with `lot_caps` in shadow mode, an unknown-instrument entry is still refused (fail closed), and record which code wins.
- [x] 5.2 Implement `EntryPacing` (`checks/flow.py`): `max_entries_per_minute` over `recent_entries`, and `min_entry_gap_s` from `day.last_entry_at`. Append it to the registry. Verify the `order-flow-guards` pacing scenarios as unit tests, with window edges at exactly 60 s and the gap boundary, and that an exit is never refused.

## 6. Rejection breaker

- [x] 6.1 Add `OrderService.add_rejection_listener`, called inside the lock when the adapter raises `BrokerRejected` and when a broker `Rejected` update is applied. Make the rate limiter raise a new `RateLimited(BrokerRejected)` subclass, and do not report it to listeners. Verify with order-service tests: the listener fires for both broker paths, and not for gate refusals, `RateLimited`, `settle_in_flight` or `reconcile_unknown`. Existing rejection behaviour is unchanged.
- [x] 6.2 Implement `RejectionWindow` (`engine/risk/rejections.py`, 300 s, pruned on read) and `RejectBreaker`, both pre-trade and continuous. Register the window as a rejection listener in `RiskRuntime`, fill `RiskContext.broker_rejections` in both the gate's and the monitor's context builders, add the check to both registries, and report `broker_rejections_5m` in the snapshot. Verify the `order-flow-guards` breaker scenarios as unit tests (trip, clear at the window edge, exits accepted), and a monitor test that the critical log is written once per trip.

## 7. Instrument policy and the ban list

- [x] 7.1 Add `InstrumentMaster.spot_of(symbol)`: the `IDX_I` index row, else the `NSE_EQ` equity row, with that `underlying_symbol`. Verify against the recorded instrument-master fixture that NIFTY, BANKNIFTY and FINNIFTY options each map to exactly one index row. If a name differs (for example "NIFTY BANK"), add an explicit mapping in the master code with a comment and a test, never a guess at runtime.
- [x] 7.2 Implement `BanListService` (`engine/risk/banlist.py`):
  - load at start, retried while unread;
  - lists kept in memory for today and later dates;
  - the `SetBanList` handler (date not in the past; today during market hours must keep every stored symbol, else `TIGHTEN_ONLY`; an empty list accepted);
  - a `risk_event` per load.

  Route the command in `CommandConsumer`, wire it in `RiskRuntime`, and fill `RiskContext.ban_list` in both the gate's and the monitor's context builders. Verify every ban-list scenario in `instrument-policy` as unit tests, including the repeated-key and storage-down cases.
- [x] 7.3 Add CLI `risk set-ban-list --file PATH [--date YYYY-MM-DD]`: one symbol per line, `#` comments and blank lines ignored, upper-cased, date defaulting to today in IST. Report `ban_list_loaded` in the snapshot and in `risk status`. Add a short morning-checklist note to the README. Verify `--help` lists the command, the CLI round trip against `TEST_REDIS_URL`, and that the file parser rejects an empty symbol.
- [x] 7.4 Implement `InstrumentPolicy` (`checks/policy.py`, design §7): deny → allow → ban list (F&O segments) → minimum premium, including the expiry minimum → strike distance from a fresh spot price. Use `applies_to_exits = False`, and append it to the registry. Fill `RiskContext.spot_key` (via `InstrumentMaster.spot_of` from 7.1) in both the gate's and the monitor's context builders. Verify every `instrument-policy` entry scenario and the expiry minimum-premium scenario as unit tests, plus a test that no part of the policy refuses an exit, a missing ban list included.

## 8. Expiry-day profile

- [ ] 8.1 Teach `EntryCutoff` the expiry cutoff (`RISK_EXPIRY_CUTOFF` for an entry whose contract expires today), and teach `FloorHeadroom` to judge with `floor_params(expiry=day.expiry_exposure or expires_today(order))`. Verify the `expiry-day-profile` cutoff, holiday-moved-expiry and headroom scenarios as unit tests.
- [ ] 8.2 In the monitor, switch `expiry_exposure` on when an open position's contract expires today (persisted, one-way), and ratchet the floor with `floor_params(expiry=day.expiry_exposure)`. Make the limits edit re-simulation use the same flag. Verify with monitor tests: the giveback scenarios, including "Expiring position closed" (the floor does not fall) and the restart case.
- [ ] 8.3 Add an optional `positions` filter to the `Flattener` Protocol and `ChaseFlattener.flatten`. Make `SquareOff` exit each intraday position at its effective time: the expiry time for a contract expiring today or with no master record, otherwise the base time. Verify the "Expiry square-off" scenario: only the expiring intraday position is exited at 14:30, and the other at 15:10. The existing square-off and kill tests must pass unchanged.

## 9. Profit target

- [ ] 9.1 Implement the monitor's `target_confirmations` counter (reset on a dip, on unknown MTM, or with no target set) and `ProfitTarget` (`checks/target.py`, continuous, `Severity.TARGET_HALT`, `RISK_TARGET_HALT`), and append it to `continuous_checks()`. Fill `RiskContext.target_confirmations` in the monitor's context builder only: the target is continuous, so the gate keeps the default. Verify the `profit-target` confirmation scenarios as unit tests.
- [ ] 9.2 Add a `TARGET_HALT` branch to `Monitor._act`, after the KILL branch: `raise_halt(HaltLevel.TARGET_HALT)`, `flush()` before acting, then the existing flatten. Verify with monitor tests:
  - "Target flattens the book";
  - "A kill outranks the target" and "A breach and the target together";
  - "MTM falls back" (entry refused with `RISK_HALTED_DAY`);
  - a restart keeps the halt and resumes flattening;
  - the recovery resume command does not clear it;
  - the snapshot shows `TARGET_HALT`.

## 10. Integration

- [ ] 10.1 Run the integration suite against Redis and Postgres:
  - the day-state columns persist and merge one-way across a `kill -9` and restart;
  - a target halt survives a restart;
  - the ban list fails closed with Postgres unreachable and recovers when it returns;
  - an end-to-end entry through the real gate, order service and day book is refused by each new rule with its code;
  - exits are never blocked, as a property over every registered rule, the new ones included.

  Apply the mutation checks the reviewer requests.
- [ ] 10.2 Definition of done: `uv run ruff format .`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest` and `openspec validate add-risk-guards --strict` all pass, and CI is green on the branch.
- [ ] 10.3 (**owner**) Paper smoke test in market hours:
  - load the new limits file and today's ban list;
  - see an F&O entry refused before the list is loaded;
  - see a lot-cap, a pacing and an instrument-policy refusal;
  - set a low profit target and watch the book flatten and the day lock at `TARGET_HALT`;
  - restart and confirm it stays locked.

## Workflow follow-up

- Start implementing only after PR #9 (`add-risk-engine`) is reviewed and merged. Every group from 2 to 9 touches a migration, the `RiskGate` and its rules, the order path or the kill switch, so each pauses for the owner's review after the reviewer approves (CLAUDE.md commit policy).
- Archive after `add-risk-engine` is archived (`openspec archive add-risk-guards --yes`), because the `risk-limits` delta adds to a spec that change creates.
- Tell `add-trailing-exits` (S4) that it now owns the modification cap, and that its exit manager must keep the flattener's `positions` filter.
- Propose a separate OpenSpec change for broker rejections after the acknowledgement (design, Risks: "A broker rejection after the acknowledgement is not applied"). It must land before S7 enables `DhanBroker` order placement. Minimum content:
  - add `S.REJECTED` to `TRANSITIONS[S.OPEN]` in `engine/orders/machine.py`;
  - a MODIFIED `orders` spec delta, and the PRD §5 diagram line `Open --> Rejected: broker reject after ack`;
  - tests through a real acknowledgement (the order reaches `Open` the normal way) covering: the rejection listener fires (turn `test_an_acknowledged_order_rejected_by_the_broker_counts` from xfail into a passing test); the lot-cap reservation is released; a rejected catastrophic SL makes its position unprotected and is re-placed; a rejected chase exit is re-placed;
  - a cap on how often a stop that the broker keeps rejecting may be re-placed.
