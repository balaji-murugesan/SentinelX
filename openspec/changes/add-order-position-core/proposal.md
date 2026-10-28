# Proposal

## Why

Stage 1 gives the engine prices and candles, but it cannot yet hold an order or a position. Everything after this (risk limits in S3, trailing exits in S4, the API and dashboard in S5–S6, live trading in S7) needs one place where orders are accepted, validated, tracked through their lifecycle and turned into positions with P&L, and that state must survive a crash. This change is the first half of PRD Stage 2. It delivers paper trading end to end through the real command path. The second half, `add-dhan-readonly`, adds the read-only Dhan adapter and reconciliation against real Dhan responses.

## What Changes

- **Order contracts** in `contracts`: place and cancel commands, order and fill events, and position values. All are pydantic models with `schema_version` and `account_id` (D-020). Every order carries a **product type**: `INTRADAY`, `CNC` (equity delivery) or `MARGIN` (F&O carried overnight), chosen per trade or strategy (PRD §12 Q11).
- **Command intake over Redis**: the engine consumes order commands from a Redis Stream and writes each result to a per-command reply. This is the same path the API will use in S5. Every command carries an **idempotency key**, and a repeated key returns the original result without placing anything (safety rule 5).
- **Order validation** against instrument facts. Unlike the risk limits, these come from the instrument master:
  - quantity a whole number of lots;
  - quantity below the exchange freeze quantity; an unknown freeze quantity for F&O is rejected (fail closed);
  - limit price on the tick grid;
  - product type allowed for the segment;
  - a `CNC` sell only against held quantity, because cash-equity shorts are intraday only.
- **`RiskGate` seam**: the order service requires a pre-trade gate (a `Protocol`) in its constructor, so no order source can skip it. This change ships a stub gate that **rejects every order unless the trading mode is paper**. S3 replaces it with the real risk rules without touching call sites (PRD S2).
- **`BrokerAdapter` protocol and order state machine** (PRD §5):
  - States: Requested → Submitted → Open → PartiallyFilled → Filled, plus Rejected, Cancelled and Unknown.
  - An order that times out becomes `Unknown` and is reconciled against the broker's order book, **never retried** (safety rule 4).
  - Order types are `MARKET` and `LIMIT`, valid for the day. Stop orders (SL, SL-M) arrive with the catastrophic stop in S3.
- **PaperBroker**: fills against live prices from the tick bus and never calls Dhan.
  - **Market orders** fill at the latest price moved against the trader by a configurable slippage.
  - **Limit orders** rest until the price trades through them.
  - Every fill is charged **trading costs** (brokerage, STT, exchange and SEBI fees, stamp duty, GST) from a configurable rate table, so paper P&L is not overstated.
  - Unfilled orders expire at the 15:30 close.
- **Positions and P&L**: net quantity and average price per account, instrument and product. Realized P&L, costs and mark-to-market (MTM) P&L are updated on every tick. Open positions keep their instruments subscribed on the feed.
- **Persistence and recovery**: orders, order transitions (audit), fills, positions and command results go to Postgres in a new migration. If persistence is unavailable, **new orders are rejected** (fail closed, safety rule 9). On restart the engine reloads its state and reconciles open orders with the broker before accepting commands. Kill -9 then restart restores identical state.
- **State snapshot** in Redis: open orders and positions with MTM, refreshed on change, for the CLI now and the API later.
- **Developer CLI**: `order place`, `order cancel`, `orders` and `positions`. Commands go through the Redis stream, run inside the engine container (`docker compose exec engine python -m cli …`).

Out of scope:
- **Next change (`add-dhan-readonly`):** the read-only Dhan adapter, Dhan order-update parsing, and reconciliation against recorded Dhan responses.
- **S3:** risk rules, sizing, the catastrophic stop, the kill switch and the rate limiter.
- **S4:** trailing exits.
- **S5:** the API, WebSocket deltas and sequence numbers.
- **S7:** `DhanBroker` order placement.
- **Not planned yet:** slicing orders above the freeze quantity, partial fills in paper, multi-leg orders (§12 Q10), and multi-account routing (S8).

## Capabilities

### New Capabilities
- `order-commands`: command intake over the Redis stream, command validation, account check, idempotency and per-command replies.
- `orders`: order validation against instrument facts, the mandatory `RiskGate`, the `BrokerAdapter` interface, the order state machine, `Unknown` handling without retry, cancellation and end-of-day expiry.
- `paper-broker`: simulated fills against live prices with slippage and trading costs, never touching Dhan.
- `positions`: net positions per account, instrument and product, average price, realized and MTM P&L, costs, and feed subscriptions for open positions.
- `trading-state`: Postgres persistence of orders, fills, positions and command results; fail-closed when persistence is down; restart recovery and reconciliation; the Redis state snapshot.

### Modified Capabilities
None. This change relies on existing requirements unchanged:
- schema changes only through the one-shot migrate service, and the engine's read/write-only database role (`service-stack`);
- paper mode as the default and the live gate (`runtime-config`);
- masking and tracing (`observability`);
- import boundaries (`quality-gates`);
- owner-based, capped feed subscriptions (`market-feed`);
- lot size, tick size and freeze quantity from the instrument master (`instruments`).

## Impact

- **Code**:
  - New `src/contracts/orders.py` and `src/contracts/commands.py`.
  - New `src/engine/orders/` (validation, `RiskGate`, state machine, order service, command consumer).
  - New `src/engine/broker/adapter.py` (`BrokerAdapter`) and `src/engine/broker/paper.py`.
  - New `src/engine/positions/`.
  - New `src/engine/persistence/` repositories and a migration `0002`.
  - Wiring in `src/engine/main.py`.
  - CLI commands in `src/cli/`.
- **Config**:
  - `ACCOUNT_ID` (the owner's account label, not the Dhan client ID).
  - `PAPER_SLIPPAGE_TICKS`.
  - Trading-cost rate settings, with defaults from Dhan's published charges for the owner to confirm.
  - Order timeout and snapshot interval.
- **Database**: new tables `orders`, `order_events`, `fills`, `positions` and `command_results`, all carrying `account_id`.
- **Redis**: a new command stream, per-command reply streams with a TTL, and a snapshot key.
- **CI**: a Postgres service container for the persistence and recovery integration tests, following the existing Redis pattern (`TEST_DATABASE_URL`).
- **Dependencies**: none new (SQLAlchemy async, asyncpg, Alembic and redis are already present).
- **Downstream**:
  - `add-dhan-readonly` implements `BrokerAdapter`'s read side.
  - S3 replaces the stub `RiskGate`.
  - S5 reads the snapshot and sends the same commands.
