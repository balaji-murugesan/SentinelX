# Proposal

## Why

The order state machine has no `Open → Rejected` transition. So when the broker rejects an order *after* acknowledging it (for example, its risk system or the exchange refusing it on the order-update stream), the engine logs `invalid_transition` and keeps the order `Open`. That one gap has knock-on effects:

- A rejected catastrophic stop still looks resting, so its position looks protected when it is not.
- A rejected chase exit is waited on, so the flatten stalls.
- A rejected entry keeps its lot-cap and exposure reservation.
- The rejection breaker never counts it.
- A restart halts on the difference against the broker's order book, and the resume command can never clear that halt.

add-risk-guards recorded this (design, Risks: "A broker rejection after the acknowledgement is not applied") with a strict xfail test. It must be fixed before S7 enables real `DhanBroker` order placement.

## What Changes

- The order state machine gains `Open → Rejected`. A broker `Rejected` update for an acknowledged order is applied, stored with its reason and an audit record, and reported to the rejection listeners, so the breaker counts it.
- An order rejected this way stops counting as resting everywhere the engine looks at resting orders: lot caps, exposure, protection, the flatten and the snapshot.
- When the broker rejects a catastrophic stop after accepting it, the position is unprotected again and the stop is re-placed. A stop the broker keeps rejecting is re-placed with a growing delay (capped at `RETRY_MAX_S`), never on every sweep.
- When the broker rejects a chase exit after accepting it, the exit is placed again after the chase interval. This is the existing rule for a refused exit, now reachable after an ack.
- Restart recovery applies a rejection it finds in the broker's order book for an order the engine still holds `Open` with no fills, instead of halting on it. The resume command does the same, so a halt left by such a difference can clear. A rejection applied at recovery is not counted by the breaker.
- PRD §5 diagram gains `Open --> Rejected: broker reject after ack`.
- Out of scope: `PartiallyFilled → Rejected`. When a partly filled order's remainder ends, the broker reports it cancelled; S7 confirms this for Dhan. Also out of scope: any other recovery difference, such as an order cancelled at the broker while the engine was down (it still halts). The paper broker does not change; it never rejects after an ack.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orders`: "Order state machine" adds `Open → Rejected`. A new requirement covers applying a broker rejection after the acknowledgement.
- `trading-state`: a new requirement says a broker rejection missed while the engine was down is applied at recovery and by the resume command, not reported as a difference.
- `catastrophic-stop` (created by `add-risk-engine`, not archived yet): a new requirement says a stop rejected after acceptance is re-placed with a capped, growing delay.
- `kill-switch` (created by `add-risk-engine`, not archived yet): a new requirement says a rejected exit is placed again after the chase interval.

The `order-flow-guards` requirement "Only rejections by the broker count" already covers a rejection that arrives through an order update, and already excludes rejections applied during restart recovery. It does not change; this change makes that path reachable.

## Impact

- Code:
  - `engine/orders/machine.py` gains the new transition.
  - `engine/orders/service.py` gains a method that applies broker-book rejections without reporting them, and `settled()`, which waits until updates already queued on the service lock have been applied.
  - `engine/orders/recovery.py` calls that method in `recover` and, while halted, in `_resume`.
  - `engine/risk/stops.py` gains a rejection listener and a per-position late-rejection delay. During a resize it keeps the old stop when the new stop was refused (design §4).
  - `engine/risk/runtime.py` registers the listener.
- Tests:
  - `tests/engine/test_order_service_rejections.py`: the xfail test becomes a passing test.
  - New unit tests for the state machine, the stops, the flatten, lot caps, exposure and recovery.
  - An end-to-end test through the risk runtime.
- Docs: the PRD §5 diagram, and the `orders/machine.py` docstring.
- No migration: order states are stored as text with no CHECK constraint. No contract, API or config change.
- Dependencies: builds on the `feat/risk-guards` branch (PR #10). Archive order: `add-risk-engine`, then `add-risk-guards`, then this change.
