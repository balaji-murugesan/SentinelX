# Tasks

Owner pause points (commit-and-pause policy): after groups 2, 3, 4, 6 and 8 (orders, gate, broker, persistence of orders, recovery).

## 1. Contracts and settings

- [x] 1.1 Add `contracts/orders.py` (`OrderSide`, `ProductType` INTRADAY/CNC/MARGIN, `OrderType` MARKET/LIMIT, `OrderState`, `Order`, `Fill` with cost breakdown, `Position`) and `contracts/commands.py` (`PlaceOrder`, `CancelOrder`, `CommandResult`, stream and reply-key names), all pydantic v2 with `schema_version` and `account_id`, `Decimal` money, aware UTC times; comment the pydantic idioms with C# analogies; verify unit tests for JSON round trips, the "Limit price on a market order" scenario at model level, and `lint-imports` (contracts imports nothing internal)
- [ ] 1.2 Add `ACCOUNT_ID`, `PAPER_SLIPPAGE_TICKS`, `PAPER_PRICE_MAX_AGE_S`, `ORDER_ACK_TIMEOUT_S`, `SNAPSHOT_INTERVAL_S` to `EngineSettings` (design §10) with bounds, add them to the test `CONFIG_VARS`, and (**owner**) to `.env.example` with real values; verify settings tests for defaults and bounds, and `test_env_example_resolves_to_paper`

## 2. Persistence

- [ ] 2.1 Add SQLAlchemy table definitions and migration `0002_trading` (`orders`, `order_events`, `fills`, `positions`, `command_results` with unique `(account_id, idempotency_key)`; `Numeric` money, `timestamptz`, `account_id` on every table); verify the migration applies and downgrades on a fresh database in the integration suite, and that the engine role can read and write but not create tables (existing `service-stack` scenarios)
- [ ] 2.2 Define the `TradingRepository` Protocol (save new order + command result in one transaction, save transition, save fill + position, load open orders / today's fills / positions / command keys) with `MemoryRepository` and `PostgresRepository` (async SQLAlchemy, 3 s timeout per call); verify one shared contract test suite runs against both (Postgres skipped without `TEST_DATABASE_URL`, loudly, like `TEST_REDIS_URL`)
- [ ] 2.3 Add a Postgres service and `TEST_DATABASE_URL` to `.github/workflows/ci.yml` (migrated by Alembic before the tests) and a README note on running it locally; verify CI runs the Postgres tests (not skipped) and they pass

## 3. Validation, gate and state machine

- [ ] 3.1 Implement `engine/orders/validation.py` (instrument known and master loaded, whole lots, below freeze, unknown F&O freeze rejects, tick grid, product per segment, no index orders, CNC sell ≤ held − open CNC sells); verify one test per `orders` validation scenario plus the "Delivery on an option" and "Selling more than held" scenarios
- [ ] 3.2 Implement the `RiskGate` Protocol and `PaperOnlyGate` (design §1), commenting `Protocol` with a C# analogy; verify the "Live mode before the risk engine" and "Gate rejection is final" scenarios, and a test that `OrderService` cannot be constructed without a gate
- [ ] 3.3 Implement the pure transition table (PRD §5) and `transition()`; verify every allowed transition, a parametrised test that every other pair is refused, and the "Update after a final state" scenario

## 4. Broker adapter, paper broker and costs

- [ ] 4.1 Define `BrokerAdapter` (place, cancel, order_book, updates) and `BrokerUpdate`/`BrokerAck`/`BrokerOrder` values in `engine/broker/adapter.py` (design §5); verify mypy accepts `PaperBroker` and a test fake as implementations
- [ ] 4.2 Implement `PaperBroker` market fills (last price ± slippage, stale/missing price rejects, no network); verify the "Paper market order", "Buy with one tick of slippage" and "Stale price" scenarios, and a test that patches `httpx` and sockets to fail if any network call happens
- [ ] 4.3 Implement resting limit orders (trade-through at the limit, marketable on arrival at the better price, cancel, rebuild from open orders); verify the "Resting buy limit", "Marketable limit", "Cancel a resting limit order" and "Restart with a resting limit" scenarios (the last at broker level)
- [ ] 4.4 Implement `engine/broker/costs.py` (rate table and calculator per design §7, paisa rounding); verify the "Option sell costs" and "Costs reduce P&L" scenarios with worked numbers in the tests; **owner step**: compare one option and one equity-intraday fill with Dhan's brokerage calculator and correct the table if needed, recording the date checked in `costs.py`

## 5. Positions

- [ ] 5.1 Implement the pure `PositionBook` (increase, reduce, flip, realized P&L, costs, MTM, net P&L); verify the "Scale in then partly out", "Long 65, sell 130" and "Short loses as price rises" scenarios, and `hypothesis` properties: splitting a fill gives the same position and P&L, and rebuilding from fills equals incremental application
- [ ] 5.2 Add feed subscriptions for open positions under owner `positions` (subscribe on first fill, release when flat); verify the "Position closed" scenario against `SimulatedFeed` with another owner holding the same key

## 6. Order service

- [ ] 6.1 Implement `OrderService.place` (validation → gate → persist Requested + command result in one transaction → submit with timeout → transitions) under the trading lock (design §2), with tracing spans and broker-latency recording; verify the "Place command answered", "Same key twice" (service level), and "One trace per order" scenarios with `MemoryRepository`, `PaperBroker` and an in-memory span exporter
- [ ] 6.2 Implement broker-update handling (fills → position, costs, transitions), `Unknown` on timeout with reconciliation via `order_book()` and no resubmission, cancel, and end-of-day expiry by the injectable clock; verify the "Submission times out" (fake adapter that never acks; exactly one `place` call), "Cancel a resting limit order", "Limit order never reached" and "Fills update positions exactly once" scenarios
- [ ] 6.3 Implement fail-closed persistence (pending-write queue, retry with backoff, `persistence_ok`, 3 s write timeout); verify the "Postgres down" and "Postgres back" scenarios with a `FailingRepository`, and that fills for existing orders keep applying while new orders are rejected

## 7. Command stream, snapshot and CLI

- [ ] 7.1 Implement the command consumer (consumer group, pending entries first, idempotency lookup, reply stream with TTL, ack after persist, malformed and other-account handling, Redis-down retry like `answer_pings`); verify the "Malformed command", "Other account" and "Crash between persist and acknowledge" scenarios against Redis (`TEST_REDIS_URL`)
- [ ] 7.2 Implement the Redis snapshot writer (open orders, positions with MTM/realized/net P&L, `schema_version`, taken-at time, at most every `SNAPSHOT_INTERVAL_S`, retries when Redis is down); verify the "Redis down" scenario and that the snapshot JSON round-trips through the contract models
- [ ] 7.3 Add CLI `order place`, `order cancel`, `orders`, `positions` (fresh idempotency key, 10 s reply wait, "result unknown" without resend, snapshot reads only); verify the "CLI order round trip" and "Engine not answering" scenarios against `TEST_REDIS_URL`, and that `--help` lists them

## 8. Recovery and engine wiring

- [ ] 8.1 Implement restart recovery (design §9: load → rebuild paper book → reconcile orders → rebuild-and-compare positions → set `reconciled`) before the command consumer starts; verify the "Duplicate after restart" and "Mismatch after restart" scenarios with `MemoryRepository` and a fake adapter
- [ ] 8.2 Wire `OrderService`, `PaperBroker`, the command consumer and the snapshot writer into `engine.main.run` with the existing supervision, paper broker only (no `DhanBroker` yet), banner showing the account label; verify a `run()` test placing a paper order through the command stream, and that a crash in the snapshot writer leaves orders and the heartbeat running
- [ ] 8.3 Add the kill -9 integration test (engine subprocess against `TEST_REDIS_URL` + `TEST_DATABASE_URL`, positions and a resting limit, `SIGKILL`/`TerminateProcess`, restart, compare persisted and snapshot state); verify the "Kill -9 and restart" and "Restart with a resting limit" scenarios pass in CI

## 9. Integration check

- [ ] 9.1 Run the full definition of done: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest` (with `TEST_REDIS_URL` and `TEST_DATABASE_URL` set), `openspec validate add-order-position-core --strict`; verify all pass
- [ ] 9.2 Owner smoke test (paper, market hours, `FEED_SOURCE=dhan` for real prices): `docker compose up -d --build`, then via `docker compose exec engine python -m cli`: place a market buy of one lot of a liquid option, check `positions` against Dhan's LTP, place a far limit sell and cancel it, place a near limit sell and watch it fill, `docker kill --signal=KILL` the engine, let it restart, and confirm `orders`/`positions` are unchanged; record the results in this task

## Workflow follow-up

- After merge, archive with `/opsx:archive add-order-position-core`, then propose `add-dhan-readonly` (read-only adapter, order-update parsing, reconciliation against recorded responses: the rest of PRD S2).
- Notes for S3: replace `PaperOnlyGate` behind the same `RiskGate` seam; add SL/SL-M order types with the catastrophic stop; decide §12 Q12/Q13 (carried positions); add the max-lots-per-order limit (default 10).
