# Design

## Context

**What exists.**
- **Engine:** `engine.main.run` runs three tasks (the heartbeat, the diagnostic ping responder and `MarketDataService`), all in one asyncio `TaskGroup`.
- **Redis:** used only for the diagnostic ping and pong. Its pattern (a stream in, a per-message reply stream with a TTL) is the model for commands.
- **Postgres:** reachable through the engine role (data read/write only). Migrations run in the one-shot `migrate` service from `engine/persistence/migrations`; `0001` is an empty baseline.
- **Market data:** `TickBus` delivers ticks, `MarketDataService.provider` handles owner-based subscriptions, and `InstrumentMaster` gives lot size, tick size and freeze quantity.

Motivation: see proposal.md. Requirements: the five spec deltas.

The owner's choices for this change are: Stage 2 is split, with the read-only Dhan adapter in `add-dhan-readonly`; and the developer CLI sends commands through the Redis stream from inside the engine container.

## Goals / Non-Goals

**Goals:**
- **One order path for every source:** command → validation → gate → broker → state machine → persistence → snapshot. S3–S5 then plug in without re-routing anything.
- **No interleaving:** a crash at any point either loses nothing or leaves an order `Unknown`, and never produces two orders.
- **Unit-testable without Postgres or Redis:** repositories and the command transport sit behind small interfaces. Integration tests run the real stores in CI.

**Non-Goals:**
- **Throughput.** Phase 1 has about 10 orders a day and at most ~10 concurrent positions.
- **Daily P&L, loss limits and the "carried positions" questions** (§12 Q12, Q13). These belong to S3.
- **Exactly mirroring Dhan's order states in paper.** Paper fills whole orders and never partially fills; the state machine still supports `PartiallyFilled` for Dhan.

## Decisions

### 1. Module layout

| Path | Contents |
|---|---|
| `contracts/orders.py` | `OrderSide`, `ProductType`, `OrderType`, `OrderState`, `Order`, `Fill`, `Position` values (pydantic v2, `schema_version`, `account_id`) |
| `contracts/commands.py` | `PlaceOrder`, `CancelOrder`, `CommandResult`, stream and key names |
| `engine/orders/validation.py` | Pure checks against an `Instrument` and held quantity |
| `engine/orders/gate.py` | `RiskGate` Protocol and `PaperOnlyGate` |
| `engine/orders/machine.py` | Pure transition table and `transition()` |
| `engine/orders/service.py` | `OrderService`: the single owner of order and position state |
| `engine/orders/commands.py` | Redis consumer-group reader and reply writer |
| `engine/broker/adapter.py` | `BrokerAdapter` Protocol and `BrokerUpdate` values |
| `engine/broker/paper.py`, `engine/broker/costs.py` | `PaperBroker`; trading-cost rate table and calculator |
| `engine/positions/book.py` | Pure `PositionBook` (apply fill, MTM) |
| `engine/persistence/` | SQLAlchemy tables, `TradingRepository` Protocol, `PostgresRepository`, `MemoryRepository` (tests) and migration `0002_trading` |

`contracts` imports nothing internal, and `api` never imports `engine` (import-linter, unchanged). `PaperBroker` lives in `engine.broker` beside the future `DhanBroker`.

### 2. One owner of trading state, serialised by a lock

`OrderService` owns all orders and positions in memory. Commands, broker updates (fills, cancels), ticks for limit triggering, and the expiry timer all call into it. Every *state-changing* operation runs under one `asyncio.Lock`, like a C# `SemaphoreSlim(1)` used with `await WaitAsync()`. The sequence inside is fixed:

1. Change in memory.
2. Await the Postgres write.
3. Publish.
4. Release the lock.

Ticks that only refresh MTM need no lock: they change no persisted state, and they recompute from immutable position values.

*Why:* asyncio is single-threaded, but every `await` inside an operation lets another operation start, for example a fill landing between "persist Requested" and "submit". The lock makes each operation atomic, with no cost that matters at these volumes.
*Alternative:* an actor or queue that funnels everything through one task. That's equivalent, but it routes ticks and commands through one queue and makes the order path harder to trace.

### 3. Write-through persistence, an outbox only on failure

Each operation writes its rows in **one transaction** before publishing. Placing an order writes the `Requested` order, its transition record and the command result together. A broker submission happens only after that commit.

If a write fails:
- the operation's rows go into an in-memory **pending-write queue**;
- `persistence_ok` is set to false, so new orders are rejected (spec: Fail closed);
- a retry loop with backoff drains the queue in order;
- fills and cancels for existing orders keep being applied in memory and queued.

Once the queue is empty, `persistence_ok` returns to true.

*Alternative:* event sourcing (store only transitions and fills, derive everything). It's attractive for audit, but positions are already rebuildable from fills (spec: Rebuild from fills), so a positions table plus transitions gives the same guarantees with simpler reads.

### 4. Idempotency and the command stream

- **Stream and group.** The stream is `sentinelx:commands`, read by consumer group `engine` with consumer `engine-1`.
- **Startup.** The engine first reads its pending entries (id `0`), which are messages delivered before a crash but never acknowledged, then new ones (`>`).
- **Per command.** `command_results` has a unique `(account_id, idempotency_key)`. If the key exists, the stored result is replied and the message acknowledged. Otherwise the command is processed, its result stored in the same transaction as the order, then replied and acknowledged (`XACK`). The reply goes to stream `sentinelx:reply:<command_id>` with a 60 s TTL, the diagnostics pattern.
- **Order ids** are engine-generated UUIDs. The broker's id is a separate column.

*Why both the idempotency key and the consumer-group acknowledgement:* the acknowledgement makes redelivery happen, and the key makes redelivery harmless. Either alone fails one crash window.

### 5. Broker adapter: request and response plus an update stream

`BrokerAdapter` is a `Protocol` with:
- `place(order) -> BrokerAck`
- `cancel(order) -> None`
- `order_book() -> list[BrokerOrder]`
- `updates() -> AsyncIterator[BrokerUpdate]`

`BrokerUpdate` covers acknowledged, filled (quantity, price, time, costs), cancelled and rejected. `place` is awaited with `ORDER_ACK_TIMEOUT_S` (default 5). On a timeout the order becomes `Unknown`. Reconciliation then calls `order_book()` after a short delay, and again at each later status change, and adopts the broker's state; if the broker has no such order, the order becomes `Rejected` (spec "Submission times out").

This is the same shape the Dhan adapter needs: a REST place and cancel, plus the order-update WebSocket, which `add-dhan-readonly` parses.

### 6. Paper fill rules (conservative)

`PaperBroker` subscribes to the `TickBus` (consumer `paper-broker`) and keeps the last price and its receive time per instrument.

- **Market orders** fill in full at last price ± `PAPER_SLIPPAGE_TICKS` × tick, against the trader and never below one tick. They're rejected if the price is missing or older than `PAPER_PRICE_MAX_AGE_S`.
- **Limit orders** fill in full at the limit price on the first tick that trades through it. If they're marketable on arrival, they fill at once at the better of the limit and the last price.

*Why fill at the limit and not the tick price:* a real limit order can never get a better price than its limit from a later trade.

*Accepted optimism:* whole-quantity fills, and no queue position at the limit price. Both are noted in the README so paper results aren't over-trusted.

### 7. Trading costs: a code table, owner-verified

`engine/broker/costs.py` holds one rate table, keyed by (segment, product, side). Each rate is a `Decimal` fraction of turnover (premium × quantity for options), except brokerage, which is "flat per order, capped by a percentage". GST applies to brokerage, exchange charges and SEBI fees. Every component is rounded to the paisa (ROUND_HALF_UP), the same as Dhan's contract notes.

The option and equity-intraday rates below were checked against Dhan's brokerage calculator on 2026-10-28 (task 4.4). The futures and equity-delivery rates are **not verified**.

| Component | Options | Futures | Equity intraday | Equity delivery |
|---|---|---|---|---|
| Brokerage per order | ₹20 | ₹20 or 0.03%, lower | ₹20 or 0.03%, lower | ₹0 |
| STT | 0.15% sell, on premium | 0.02% sell | 0.025% sell | 0.1% buy and sell |
| NSE transaction | 0.03553% of premium | 0.00173% | 0.00297% | 0.00297% |
| SEBI | 0.0001% | 0.0001% | 0.0001% | 0.0001% |
| Stamp duty (buy only) | 0.003% | 0.002% | 0.003% | 0.015% |
| GST | 18% of brokerage + transaction + SEBI | same | same | same |

Changing a rate means editing this table: one file, covered by the cost tests. A per-rate setting would add a dozen environment variables for values that change about once a year.

### 8. Positions

`PositionBook` is pure. `apply(position, fill) -> (position, realized)` covers increase, reduce and flip (spec), using `Decimal` throughout. Quantities are in units, so P&L needs no lot multiplier. MTM is computed on read from the latest price and never persisted. Positions are keyed by (account, instrument, product), so a `CNC` holding and an `INTRADAY` trade in the same stock stay separate, as they are at Dhan.

`OrderService` subscribes instruments with open positions under the owner `positions` and releases them when the position is flat.

### 9. Restart recovery order

Before the command consumer starts, recovery runs these steps in order:

1. Load open orders, today's fills, positions and command keys.
2. Rebuild the paper broker's resting orders from the open orders.
3. Reconcile: compare each open or `Unknown` order with `adapter.order_book()`.
4. Compare positions rebuilt from fills with the stored positions (spec: Rebuild from fills).

Any difference sets `reconciled = false`. New orders are then rejected with the difference logged, and existing orders are still managed (spec: Mismatch after restart). Clearing it is a manual step in this stage: the owner fixes the data and restarts. An automatic resolution flow is deferred.

### 10. Settings

| Variable | Default | Meaning |
|---|---|---|
| `ACCOUNT_ID` | `main` | Label for the owner's account in every record. Not the Dhan client ID, so no secret ends up in rows or logs |
| `PAPER_SLIPPAGE_TICKS` | 1 | Adverse ticks on paper market fills |
| `PAPER_PRICE_MAX_AGE_S` | 5 | Oldest price a paper market order may fill at |
| `ORDER_ACK_TIMEOUT_S` | 5 | Broker acknowledgement wait before `Unknown` |
| `SNAPSHOT_INTERVAL_S` | 1 | Minimum time between Redis snapshot writes |

All are optional. The owner adds them to `.env.example` because that file is agent-denied.

### 11. CLI through the container

`python -m cli order place KEY BUY QTY --product INTRADAY [--limit PRICE]`, plus `order cancel ID`, `orders` and `positions`. The CLI writes a command with a fresh UUID idempotency key, waits up to 10 s on its reply stream, and prints the result. On a timeout it prints "result unknown, check `orders`" and **does not resend**. The `orders` and `positions` commands read the Redis snapshot.

Redis isn't published to the host (it's a Stage 0 decision), so the owner runs these through `docker compose exec engine python -m cli …`. CI's Redis service runs the same code in tests.

### 12. Testing approach

- **Pure units:** validation, gate, transitions, cost calculator and `PositionBook`. `hypothesis` checks that applying fills in any split gives the same final position and P&L as one fill with the combined quantity and value.
- **Service tests:** `OrderService` with `MemoryRepository`, `PaperBroker`, `SimulatedFeed` and a fake clock cover every orders, paper-broker and positions scenario. A `FailingRepository` covers fail-closed behaviour.
- **Integration** (skipped without `TEST_REDIS_URL` / `TEST_DATABASE_URL`, like the existing Redis test; CI provides both):
  - `PostgresRepository` round trips;
  - the command stream with redelivery;
  - **kill -9 and restart**: the engine runs as a subprocess and is killed with `SIGKILL` (`TerminateProcess` on Windows), then state is compared.

## Risks / Trade-offs

- [A Postgres write that hangs, not fails, stalls the lock and therefore all orders] → Every repository call has a timeout (`asyncio.timeout`, 3 s). A timeout counts as a failure, so the operation goes to the pending-write queue (Decision 3).
- [The cost rates may be outdated (exchange and tax rates change)] → Task 4.4 has the owner verify them against Dhan's calculator. The table is one file, and changes are covered by the cost tests.
- [Paper fills are more generous than live: no queue position, no partial fills, no rejections for margin] → This is documented. S3 adds margin and risk checks, and live forward testing in S7 measures the real gap.
- [Positions keyed by product may surprise someone comparing with Dhan's combined view] → Dhan keys positions the same way (product type is part of a Dhan position), and `add-dhan-readonly` will confirm this against the recorded positions.
- [Redis is required for commands, so an outage stops new orders] → This is intended. Exits in S4 run inside the engine without Redis, and the kill path in S7 does not depend on Redis (safety rule 8).

## Migration Plan

Migration `0002_trading` adds tables and never changes `0001`. The migrate service applies it on `docker compose up`. Rollback means running `alembic downgrade` to `0001` with the stack stopped, which drops the new tables (paper data only at this stage). CI adds a Postgres service for the integration tests.

## Open Questions

- Whether Dhan's order book returns enough fields to reconcile partial fills of a paper-placed order type. Not relevant until `add-dhan-readonly` records real responses; the `BrokerOrder` value can gain fields then without changing this design.
