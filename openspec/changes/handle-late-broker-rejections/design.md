# Design

## Context

See proposal.md, Why. What the code does today:

- **The order path.** `engine/orders/machine.py` `TRANSITIONS[S.OPEN]` is `{PARTIALLY_FILLED, FILLED, CANCELLED}`. `OrderService._apply_update` already handles a broker `Rejected` update by calling `_advance(order, REJECTED)` and then `_report_rejection`. For an `Open` order the transition raises `InvalidTransition`, which is logged and changes nothing. So the call site and the rejection listener exist; only the transition is missing.
- **Code that already handles a final order correctly.** Once the order can become `Rejected`, every reader of resting orders treats it as gone without a code change:
  - lot caps (`checks/lots.py`, `is_final`) and exposure (`checks/exposure.py`, `is_final`);
  - the snapshot (`orders/snapshot.py`, `is_final`) and the Postgres open-orders load (`_OPEN_STATES`);
  - the stop manager's resting stops (`stops.py`, `OPEN`/`PARTIALLY_FILLED` only);
  - the kill's entry cancels (`kill.py`, `OPEN`/`PARTIALLY_FILLED` only);
  - the flatten's chase (`kill.py` `_work`), which already re-places an exit whose order is `REJECTED`, but only after `chase_interval_s` since it was placed.
- **Restart recovery** (`orders/recovery.py`) compares every `Open`/`PartiallyFilled` order with the broker's order book. Any mismatch halts the engine. `_resume` re-runs the same comparison against the *database*, so an order stored `Open` that the broker reports `Rejected` stays a difference for ever.
- **The stop manager** (`risk/stops.py`) backs off with `_delay`/`_retry_at` only when *placement* fails. `_sync` calls `_forget` after a successful placement, which clears both.
- **Persistence.** Order states are stored as text with no CHECK constraint, so the new transition needs no migration.

## Goals / Non-Goals

**Goals:**
- Make a late broker rejection take effect everywhere through the one existing path (`_apply_update`), so no reader of resting orders needs its own special case.
- Bound how often the engine re-sends an order the broker keeps rejecting: stops by a growing delay, exits by the chase interval.
- Keep "Only rejections by the broker count" exact: rejections on the update stream count, rejections applied during recovery do not.

**Non-Goals:**
- `PartiallyFilled → Rejected`, and any recovery difference other than an `Open`, unfilled order that the broker reports `Rejected`.
- Reconciling `Unknown` orders during the day. Today that happens only at recovery and at resume; that is unchanged.
- Making the paper broker reject after an ack. Tests drive the update stream directly.
- Any new configurable limit. The stop delays reuse `RETRY_MIN_S` and `RETRY_MAX_S` in `stops.py`.

## Decisions

### 1. Add only `Open → Rejected` to the transition table

`TRANSITIONS[S.OPEN]` gains `S.REJECTED`. The module docstring, the orders spec and PRD §5 are updated to match.

- **Why only `Open`:** an `Open` order has nothing filled, so rejecting it changes no position. A `PartiallyFilled` order has filled units. If the broker ends its remainder, that is a cancel (which already exists), and S7 confirms how Dhan reports it.
- **Alternative considered:** `PartiallyFilled → Rejected`. It would need to say what the filled part means and how fills that arrive later are applied. That is speculative until S7 shows Dhan does it.

### 2. The update-stream path needs no new service code

`_apply_update` already advances, stores the audit record, and calls `_report_rejection` after the transition succeeds. A repeat `Rejected` for an order already final still raises `InvalidTransition` and reports nothing. The strict xfail test `test_an_acknowledged_order_rejected_by_the_broker_counts` loses its marker and must pass.

Remove the helper `awaiting_update` in that test file, which builds an unreachable `Unknown`-with-broker-id order by hand. Rewrite the tests that use it to use a real acknowledgement. One exception: a test that only checks the listener runs inside the lock may keep the helper if no real path suffices, but its docstring must then say why.

### 3. Recovery applies a broker-book rejection instead of halting on it

A new `OrderService` method takes the broker's order book, keyed by engine order id. It is named `apply_book_rejections` here; the implementer may rename it.

- **What it changes:** under the service lock, every order that is `Open` with `filled_quantity == 0`, and whose book entry is `REJECTED` with `filled_quantity == 0`, is moved to `Rejected` with reason `"broker reports Rejected"`. The move goes through `_advance`, so the order is stored and gets an audit record. It does **not** call `_report_rejection` (order-flow-guards: rejections applied during restart recovery do not count). It logs a warning per order, with ids only.
- **Where `recover` calls it:** right after it fetches the book and before `_order_differences`. The book is fetched once and passed in.
- **Where `_resume` calls it:** after it fetches the book and **before** `repository.load`, so the database it then compares already holds the `Rejected` state. It does this **only while the engine is halted** (`not service.reconciled`). On a running engine, a rejection in the book is still on its way down the update stream. The stream must apply it, so the breaker counts it and the stop manager backs off.
- **If the write is queued** (the database is failing), resume is refused with "trading state cannot be saved: resume is refused until it recovers" before the load, and the halt stays with its original reason. That fails closed.

**Why this is safe:** both sides agree nothing was filled, and `Rejected` is the broker's own final state. This is the same trust `reconcile_unknown` already places in the book for `Unknown` orders.

**Alternatives considered:**
- *Keep halting.* The resume command could then never clear the halt, because the database says `Open` and the broker says `Rejected` for ever. Only a hand edit of trading records would clear it.
- *Mark such orders `Unknown` and let `reconcile_unknown` handle them.* There is no `Open → Unknown` transition, and adding one is a bigger change to the state machine.

### 4. The stop manager listens for rejections of its own accepted stops

`CatastrophicStops` gains a plain (not `async`) rejection listener, `on_rejected(order)`. `RiskRuntime` registers it next to `self.rejections.record`.

- **What it reacts to:** only an order with `source == SOURCE`, `order_type is STOP_ORDER_TYPE` and a `broker_order_id`. A stop the broker refused *at placement* has no broker id. That path is already backed off by `_sync` through `_place` returning `None`, so filtering on the broker id stops it being counted twice.
- **What it does:** if the position is flat or gone, nothing; the sweep cancels anything left. Otherwise it increments a per-position late-rejection count `n`, sets `_retry_at[pid] = now + min(RETRY_MIN_S * 2**(n-1), RETRY_MAX_S)`, and logs a critical alert. The sweep then queues the position (it has no current stop), and `_sync` returns while `_retry_at` is in the future. Placing happens when the delay is due.
- **When the count resets:** in `on_fill`, and when `_sync` finds the position flat. It does **not** reset in `_forget` after a successful placement. Otherwise a broker that accepts and then rejects every stop would reset the delay each time, and the engine would re-place about every second for ever.
- **A stale stop:** if the position still has a correct resting stop, a late rejection of a leftover or extra stop does nothing (no count, no delay, no alert).
- **The delay's exponent is capped at 16** (`2 ** min(n - 1, 16)`). The delays are unchanged, since anything past `2**5` is already `RETRY_MAX_S`, and a count that runs for days cannot overflow the float.
- **A rejection that lands while `_sync` is awaiting:** after placing a new stop, `_sync` re-reads it before each old-stop cancel and before `_forget`. If it has been rejected or cancelled, the old stops stay and `_retry_at` (set by `on_rejected`) is kept. If it has filled, in whole or in part, it did its job, so the old stops are cancelled as before. The re-read first waits for `OrderService.settled()`, so every update already queued on the service lock is applied first. The same check guards `_forget` after a stray stop is cancelled. A rejection that reaches the lock after the re-read but before the cancel still lets that cancel go out; only its delay is kept.
- **Open item for the owner:** the late-rejection count is in memory only. For a carried position (`CNC`, `MARGIN`) it carries across the close into the next session, and it resets when the engine restarts. This change keeps that behaviour.
- **Why it is safe in the listener:** it only touches the manager's dicts and logs. It never awaits, so it is safe inside the service lock, as `add_rejection_listener` requires.
- **Alternative considered:** reuse `_delay`/`_backoff`. That is rejected for the reset reason above. Changing when `_forget` resets `_delay` would also change the existing placement-failure behaviour, which this change has no reason to touch.

### 5. The flatten's chase is unchanged; tests prove it

`Flattener._work` already treats a `REJECTED` order as refused, and places again once `chase_interval_s` has passed since `chase.placed_at`. It does not increment `chase.replacements`, so rejections never use up `max_chase_steps`. The new kill-switch requirement records this behaviour. Tests drive it through a real acknowledgement followed by a stream rejection.

### 6. The rejection breaker needs no change

`RejectionWindow.record` is already registered and already counts entries and exits alike. Late-rejected stops and exits now count too, which is what "Only rejections by the broker count" says. While stops are being rejected, entries are refused anyway, because the position is unprotected.

## Risks / Trade-offs

- **[When Dhan rejects after an ack, and how it reports it, is unconfirmed]** → The S7 `DhanBroker` adapter maps Dhan's order-update `REJECTED` status to `Rejected`. If S7 finds Dhan rejects partially filled orders, or reports a rejection as something else, it must propose a change rather than map it silently.
- **[A rejection that arrives after a fill]** (out-of-order stream) → For a `PartiallyFilled` order it stays an invalid transition, logged as an error (spec scenario "Rejection after a partial fill is refused"). The order's remainder is still cancelled at the close by `expire_at_close`.
- **[A stop the broker keeps rejecting leaves the position unprotected]** → The position is unprotected from the first rejection, so new entries are refused, the critical alert repeats on each try, and the breaker counts each rejection. Re-placing slows to one every 30 s, which bounds the load on the broker but does not fix the cause. The owner acts on the alert, for example with Exit All or `/kill`.
- **[A rejected exit is re-sent once per chase interval for as long as the position is open]** → This is intended: exits are never given up. The interval bounds the rate, and the rate limiter still applies.
- **[Resume can race the update stream while halted]** The stream keeps applying updates during a recovery halt. If resume applies a book `Rejected` before the stream delivers the same rejection, the stream update then logs `invalid_transition`. The breaker misses that one rejection, and the stop manager's `on_rejected` does not run, so a rejected stop is re-placed at the next sweep without the 1 s delay. → Accepted. It can happen at most once per resume, and the trading-state requirement already exempts recovery-applied rejections from the breaker. The next late rejection goes through the stream and starts the delay at 1 s.
- **[Recovery now trusts the book for one more case]** → This is limited to zero fills on both sides and the broker's final `Rejected`. Every other difference still halts.

## Migration Plan

No migration and no config change. Deploy is the normal engine rebuild. Rollback means reverting the commits. An order already stored `Rejected` by this change stays valid under the old code, because `Rejected` is a final state there too.
