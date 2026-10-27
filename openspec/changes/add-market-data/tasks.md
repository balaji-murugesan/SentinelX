# Tasks

## 1. Contracts, settings and dependencies

- [x] 1.1 Add `websockets` to `pyproject.toml` dependencies and create the empty packages `src/engine/market_data/` and `src/engine/broker/dhan/`; verify `uv sync` succeeds and `uv run python -c "import websockets, engine.market_data, engine.broker.dhan"` exits 0
- [x] 1.2 Add `src/contracts/market.py` with `ExchangeSegment` (the annexure names and their numeric feed codes), `InstrumentKey` (segment + security ID, parsed from and rendered as `SEGMENT:ID`) and `Instrument` (fields per the `instruments` spec, `Decimal` tick size and strike, optional freeze quantity), each with `schema_version`; verify unit tests cover key round-trip parsing, rejection of bad keys, and Decimal fields, and `lint-imports` passes
- [x] 1.3 Extend `EngineSettings` with the variables in design §10, including a `model_validator` that requires Dhan credentials when `FEED_SOURCE=dhan` and refuses `simulated` in live mode; add the new names to the test `CONFIG_VARS` list; verify tests for every `broker-session` "credentials" scenario and the `market-feed` "Feed source selection", "Simulated feed refused in live mode" and "Malformed entry" scenarios, plus a masked-repr test (token hidden, client ID last four only)

## 2. Instrument master

- [x] 2.1 Download the real `api-scrip-master-detailed.csv` once on the dev machine (public file, outside tests), record its header and segment/instrument codes in a code comment, and save a trimmed fixture (a few NSE_EQ, NSE_FNO option/future and IDX_I rows, plus one BSE row) under `tests/fixtures/`; verify the fixture parses with the stdlib `csv` module and contains the columns the parser maps
- [x] 2.2 Implement the CSV parser in `engine/broker/dhan/instruments_source.py` (keep NSE_EQ/NSE_FNO/IDX_I, map rows to `Instrument`, reject files missing required columns or with no kept rows); verify unit tests against the fixture, including the BSE-row exclusion and the corrupt-file rejection
- [x] 2.3 Implement download-to-cache (httpx streaming, temp file + atomic rename, keep the last 3 files) and the fallback order download → latest cache → empty and unavailable; verify tests using `httpx.MockTransport` and a temp directory for each `instruments` "Cache and fallback" scenario
- [x] 2.4 Map `SM_FREEZE_QTY` to `freeze_qty` (0, empty or non-numeric → unknown) and convert tick sizes to rupees (÷100 for NSE_EQ/NSE_FNO, as-is for IDX_I, zero/invalid rows dropped and counted), documenting NSE's "frozen at qty ≥ freeze qty" meaning in code; verify tests for every "Freeze quantity source" and "Tick size in rupees" scenario, and owner spot-checks one freeze value against NSE's published limits
- [x] 2.5 Implement `InstrumentMaster` in `engine/market_data/instruments.py` (immutable snapshot swap, lookups by key, by contract and by text search with limit, freshness by IST date) and the daily refresh loop with parsing in `asyncio.to_thread`; verify tests for every "Instrument lookups" and "Freshness reporting" scenario, plus a test that a refresh with a deliberately slow parse doesn't stop a concurrent heartbeat-style task from running

## 3. Dhan session check

- [x] 3.1 Owner step: run a one-off `GET /v2/profile` with their token (via a small `cli` helper added here that prints only field names, `tokenValidity` and the data-plan fields), then save a redacted fixture under `tests/fixtures/`; verify the fixture contains no token, client ID or personal data, and note the `tokenValidity` format in `session.py`
- [x] 3.2 Implement `engine/broker/dhan/session.py` (one profile call, 10 s timeout, result: valid with expiry / expired-or-invalid / plan inactive / unreachable), never retried automatically; verify `httpx.MockTransport` tests for each `broker-session` "Startup session check" and "Failed session check keeps engine running" scenario, and that logged errors contain neither the token nor the unmasked client ID
- [x] 3.3 Implement the expiry-warning loop (lead time, repeat interval, "expires before 15:30 IST today" warning at startup) using an injectable clock and no Dhan calls; verify tests for each "Token expiry warnings" scenario and the "Engine never mints tokens" scenario

## 4. Provider interface, tick bus and SimulatedFeed

- [x] 4.1 Define `MarketDataProvider` (Protocol), `Tick` (frozen slotted dataclass) and `FeedStatus` in `engine/market_data/provider.py`, with comments explaining `Protocol` and `dataclass(frozen=True, slots=True)` with C# analogies; verify mypy passes and a test asserts `Tick` is immutable
- [x] 4.2 Implement `TickBus` (per-consumer bounded queue, drop-oldest with counter and rate-limited warning, async-iterator subscription, status events on the same queues, last-tick-time per instrument, error isolation); verify tests for every "Fan-out to consumers" and "Feed status events" scenario
- [x] 4.3 Implement `SimulatedFeed` scripted mode and runtime subscribe/unsubscribe through a shared, reference-counted subscription registry with the `FEED_MAX_SUBSCRIPTIONS` cap (used by both feeds); verify tests for "Simulated scripted paths", the "Subscribe while running" scenario, and every "Subscriptions only on demand" scenario against the simulated feed
- [x] 4.4 Implement the random-walk mode (seeded, tick-grid prices, never below one tick, `SIM_*` settings); verify tests for "Simulated random walk", including a `hypothesis` property test that every generated price is a positive multiple of the tick size

## 5. Dhan live feed

- [x] 5.1 Implement the binary decoder `engine/broker/dhan/packets.py` (header plus codes 1, 2, 4, 5, 6, 7, 8 and 50; float32 → `Decimal` at 2 dp, then tick-size rounding when the instrument is known; LTT → UTC in one function), with comments explaining `struct` format strings and the trading meaning of each field (LTP, LTQ, ATP, OI); verify unit tests with packets built by `struct.pack`, covering every code, unknown codes, truncated packets, and the 24500.05 and >₹1,31,072 price cases
- [x] 5.2 Implement `DhanFeed` connect/subscribe (Quote mode, batches of 100, URL built in one place, `websockets` logger at WARNING, errors logged through `scrub_text`); verify tests against a local `websockets.serve` fake server that the subscribe JSON is correct, ticks reach a bus consumer, and a duplicate or over-cap request sends no message to the server, and a DEBUG-level capture test that a failing connection logs neither the token nor `token=`
- [x] 5.3 Implement reconnect with back-off and jitter, reset after a stable connection, the 805 long back-off, resubscription of the full set, and terminal stop on 806–810 or HTTP 401/403; verify fake-server tests for "Forced disconnect", "Repeated failures" and "Token expired mid-session"
- [x] 5.4 Implement the silence watchdog with an injectable market-hours clock; verify fake-server tests for the "Silent during market hours" and "Silent after hours" scenarios

## 6. Engine wiring, CLI and stack

- [ ] 6.1 Implement `engine/market_data/service.py` (source selection, session check → master → feed, supervised sub-tasks per design §11) and add it to `engine.main.run`, including the feed source in the startup banner; verify tests that with `FEED_SOURCE=simulated` ticks reach a consumer through `run()`, that a feed component raising an exception leaves the heartbeat task running, that the banner names the feed source, and (scenarios only partly verified in groups 2–3) that a failed session check leaves the engine and heartbeat running (broker-session "Engine stays up"), and that an instrument refresh with a slow parse doesn't interrupt tick delivery (instruments "Refresh during market hours")
- [ ] 6.2 Add CLI commands `instruments search <text>` and `feed watch <KEY>...` (prints ticks and status changes until Ctrl+C), commenting the async entry idiom; verify `uv run python -m cli --help` lists both and `feed watch` runs against `FEED_SOURCE=simulated`
- [ ] 6.3 Update `docker-compose.yml` (pass the new engine variables, `FEED_SOURCE` required, named volume for the instrument cache) and, **after owner approval** (the file is read-denied to the agent), add the new placeholders to `.env.example` with `FEED_SOURCE=simulated`; verify `docker compose config` is valid, `tests/test_stack_files.py` passes, and `docker compose up -d` brings the engine up healthy with simulated ticks in its logs
- [ ] 6.4 Add a test that fails if the string `dhan.co` appears in any file under `src/` outside `src/engine/broker/`; verify it passes, and fails when a temporary offending file is added

## 7. Integration check

- [ ] 7.1 Run the full definition of done: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest`, `openspec validate add-market-data --strict`; verify all pass
- [ ] 7.2 Owner smoke test during market hours (never in CI): `uv run --env-file .env python -m cli feed watch IDX_I:13 <one NSE_FNO option key>`. Compare LTPs with Dhan's web terminal, confirm exchange time vs receive time (resolves the LTT time-zone question), toggle the network off and on, and confirm automatic reconnect and resumed ticks; record the result in this task

## Workflow follow-up

- Propose `add-candles` (candle builder + historical warm-up, the rest of PRD S1) after this change is merged.
- Update the Stage 1 import-linter memory note: `dhanhq` is still not imported, so `unmatched_ignore_imports_alerting` stays `"none"`.
- Note for the S2/S3 proposals: an order is frozen at `qty >= freeze_qty` (largest single order `freeze_qty - 1`; slice below it); `freeze_qty` is read from the instrument master at order time and never hardcoded (NSE revised index limits seven times in 2026); "unknown" must fail closed.
- Note for the S3 proposal: owner wants a configurable **max lots per order** risk limit, default 10. It is a separate risk rule (reject), not the exchange freeze quantity.
- Archive with `openspec archive add-market-data --yes` after review and merge.
