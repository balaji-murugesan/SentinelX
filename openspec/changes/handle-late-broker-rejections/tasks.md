# Tasks

Start both test containers first (sx-redis-test on 6380, sx-pg-test on 5433). Run one pytest process at a time. Every group touches the order path, the risk engine or recovery, so each pauses for the owner's review after the reviewer approves (CLAUDE.md commit policy).

## 1. The `Open → Rejected` transition

- [x] 1.1 Add `S.REJECTED` to `TRANSITIONS[S.OPEN]` in `engine/orders/machine.py`, and add it to the module docstring's diagram. Add the PRD §5 diagram line `Open --> Rejected: broker reject after ack`. Verify with state-machine unit tests: `Open → Rejected` is allowed; `PartiallyFilled → Rejected` is still refused; every final state still refuses every target.
- [x] 1.2 In `tests/engine/test_order_service_rejections.py`, remove the xfail marker from `test_an_acknowledged_order_rejected_by_the_broker_counts`. Remove the `awaiting_update` helper, and rewrite its users to go through a real acknowledgement (design §2). Verify:
  - the formerly xfail test passes;
  - the stored audit trail reads Requested → Submitted → Open → Rejected, with the broker's reason;
  - a repeat `Rejected` for that order reports nothing and logs `invalid_transition` (spec "Repeat of the rejection");
  - a `Rejected` update for a `PartiallyFilled` order leaves it `PartiallyFilled` with its filled quantity (spec "Rejection after a partial fill is refused").
- [x] 1.3 Tests for the resting-order readers after a late rejection, each through a real acknowledgement then a stream `Rejected`. Verify:
  - lot caps no longer reserve the rejected entry (spec "Rejected entry releases its reservation", open-lots cap 2);
  - the exposure check no longer counts it;
  - the snapshot's open orders no longer list it;
  - the kill's entry cancels skip it.

## 2. Recovery applies broker-book rejections

- [ ] 2.1 Add the `OrderService` method of design §3. Under the lock it moves `Open` orders with zero fills, whose book entry is `REJECTED` with zero fills, to `Rejected` with reason `"broker reports Rejected"`, stored through `_advance`. It does not call the rejection listeners, and it logs a warning with ids only. Verify with unit tests:
  - it moves only that case;
  - an `Open` order the book reports `Cancelled`, `Open`, or `Rejected` with a fill, and an order missing from the book, are all left unchanged;
  - no rejection listener fires.
- [ ] 2.2 Call the method in `recover` (after the book is fetched, before `_order_differences`), and in `_resume` (after the book is fetched, before `repository.load`). Verify with tests in `tests/engine/test_recovery*.py`:
  - spec "Stop rejected while the engine was down": not halted, order `Rejected`, breaker count 0;
  - spec "Resume clears a halt left by a missed rejection";
  - spec "Other differences still halt", where the book reports `Cancelled`;
  - with the database failing during resume, the halt stays.
- [ ] 2.3 Add a Postgres integration test in `tests/integration/`. Store an `Open` order, restart recovery against a scripted book that reports it `Rejected`, and verify the stored order and its audit record are `Rejected` after recovery.

## 3. Stops rejected after acceptance

- [ ] 3.1 Add `CatastrophicStops.on_rejected` (design §4). It is a plain, non-`async` function. It reacts only to the manager's own stop-market orders that carry a broker order id; it keeps a per-position late-rejection count; it sets `_retry_at` to `min(RETRY_MIN_S * 2**(n-1), RETRY_MAX_S)`; it logs a critical alert; and it does nothing when the position is flat or gone. The count resets in `on_fill` and when `_sync` finds the position flat, not in `_forget`. Register it in `RiskRuntime` beside `self.rejections.record`. Verify with unit tests on a fake clock:
  - spec "Stop rejected once": unprotected, entries refused, a new stop no earlier than 1 s later;
  - spec "Broker keeps rejecting the stop": waits of 1, 2, 4, 8, 16, 30, 30 s, with no placement on the sweeps in between;
  - spec "A fill resets the delay";
  - spec "Rejected stop on a flat book";
  - a stop refused at placement (`BrokerRejected`, no broker id) is still backed off once by `_sync` and not counted by `on_rejected`;
  - the listener never awaits, and is safe inside the service lock.

## 4. Exits rejected after acceptance

- [ ] 4.1 Add tests in `tests/engine/test_risk_kill*.py`, driving the `Flattener` through a real acknowledgement followed by a stream `Rejected`. This is no code change unless a test fails; if one does, fix the code, never the test. Verify:
  - spec "Exit rejected after the acknowledgement", chase interval 5 s: no exit before 10:00:05, then one for the current quantity at the current price;
  - spec "Rejections do not use up the chase steps", with `max_chase_steps` 3 and four rejections, after which a fifth exit is placed;
  - the flatten still finishes once the position is flat.

## 5. End to end and checks

- [ ] 5.1 Add an end-to-end test through the risk runtime (the `Engine` harness in `tests/engine/test_risk_end_to_end.py`). An entry fills, so its stop is placed and acknowledged; the broker then rejects the stop on the stream. Verify that:
  - the snapshot's risk block lists the position as unprotected;
  - an entry is refused for the missing stop;
  - the breaker's 5-minute count is 1;
  - a new stop is placed after the delay, and once it rests, entries are accepted again.
- [ ] 5.2 Run all checks: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest` (with the integration services up), and `openspec validate handle-late-broker-rejections --strict`. Then push and confirm CI is green on the PR.

## Workflow follow-up

- Archive after `add-risk-engine` and `add-risk-guards` are archived, in that order (`openspec archive handle-late-broker-rejections --yes`). The `catastrophic-stop` and `kill-switch` deltas add to specs that `add-risk-engine` creates.
- Tell the S7 `DhanBroker` work that it must map Dhan's order-update `REJECTED` status to `Rejected`. If it finds Dhan rejects a partly filled order, it must propose a change rather than map that silently (design, Risks). It must also confirm the stream never sends a fill after a rejection for the same order: such a fill is only kept in memory as an unapplied fill, with no position and no stop (the same as a fill after `Cancelled` today).
