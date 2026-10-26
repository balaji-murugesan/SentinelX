# Proposal

## Why

Every later stage needs live prices and instrument facts. Paper fills (S2) fill at the live LTP (last traded price). The risk engine (S3) sizes positions from lot size and checks freeze quantity. Trailing stops (S4) react to each tick. Today the engine has no market data at all. This change is the first half of PRD Stage 1. The second half, candle building and historical warm-up, follows in a separate change, `add-candles`, to keep each review to one domain.

## What Changes

- **Dhan session check**: the engine reads `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` from the environment (the owner pastes a fresh token each day). At startup it checks the token and the Data API plan once against Dhan's profile endpoint. It refuses to start the live feed on an expired or invalid token or an inactive plan, and logs warnings ahead of expiry, with an explicit warning when the token will expire before today's market close. The engine never generates or renews tokens.
- **Instrument master**: downloads Dhan's daily instrument file and keeps only NSE equity, NSE F&O and index rows. It caches the file on disk and serves lookups: by instrument key, by contract (underlying, expiry, strike, CE/PE), and by text search. Each instrument carries security ID, lot size, tick size (as `Decimal`, converted to rupees) and freeze quantity, all taken from Dhan's file. The master reports whether it is fresh for today.
- **`MarketDataProvider` interface** (D-023), separate from the future `BrokerAdapter`, with two implementations:
  - **Dhan live feed**: an asyncio WebSocket client written for this project (no blocking `dhanhq` SDK). It subscribes in Quote mode in batches of at most 100, and decodes binary packets into ticks with `Decimal` prices and UTC timestamps. It reconnects with backoff and resubscribes automatically. It stops retrying on authentication or plan errors. A watchdog forces a reconnect when the feed goes silent during market hours. It lives in `engine/broker/dhan/`, the only package allowed to talk to Dhan.
  - **SimulatedFeed**: scripted price paths for tests, plus a seeded random walk for after-hours development. It is chosen with `FEED_SOURCE=simulated` and **refused at startup in live mode**.
- **Tick bus**: in-process fan-out from the provider to any number of engine consumers. Each consumer gets its own bounded queue, so a slow consumer never delays the feed or the other consumers. Feed status changes (connected, reconnecting, down) go out on the same bus.
- **Engine wiring**: the engine starts the selected feed with the configured instruments (`FEED_INSTRUMENTS`). Feed and instrument failures are contained: they never crash the engine process. The startup banner states the feed source.
- **Developer CLI**: `instruments search` to find security IDs, and `feed watch` to print live ticks for a manual smoke check against the real feed.
- **Guard**: a test fails if a Dhan hostname appears in source code outside `engine/broker`, which backs up the existing "only `engine.broker` calls Dhan" rule.

Out of scope: candle builder and historical candles (`add-candles`), orders, positions, P&L, persistence of ticks, Telegram alerts (S7), automated token generation or renewal, and multi-account (S8).

## Capabilities

### New Capabilities
- `broker-session`: Dhan credentials, the startup token and Data-plan check, and expiry warnings.
- `instruments`: instrument master download, caching, freshness, freeze-quantity source, and lookups.
- `market-feed`: the `MarketDataProvider` interface, the Dhan live feed with reconnect, SimulatedFeed and its live-mode refusal, the tick bus, and feed status.

### Modified Capabilities
None. Existing requirements already cover the rules this change relies on: only `engine.broker` calls Dhan (`quality-gates`), tests never reach Dhan (`quality-gates`), and secrets are never printed (`runtime-config`).

## Impact

- **Code**:
  - New `src/engine/market_data/` (interface, tick bus, instrument master lookups, SimulatedFeed).
  - New `src/engine/broker/dhan/` (session check, instrument download/parse, live feed client and packet decoder).
  - New `src/contracts/market.py` (exchange segment, instrument key, instrument model).
  - Changes to `src/engine/config.py` and `src/engine/main.py`.
  - New CLI commands in `src/cli/`.
- **Config**:
  - New env vars: `FEED_SOURCE`, `FEED_INSTRUMENTS`, `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `INSTRUMENT_CACHE_DIR`, plus optional tuning variables.
  - `docker-compose.yml` passes the new variables and mounts a cache volume.
  - `.env.example` gains placeholders. The agent can't read this file (deny rule), so the owner must approve that edit.
- **Dependencies**: adds `websockets` (asyncio WebSocket client). Does **not** add `dhanhq`, so the import-linter "only `engine.broker` may import dhanhq" exception still matches nothing, and its alerting stays `"none"`.
- **Tests**: unit tests for the decoder, bus, master and SimulatedFeed. Integration tests run the Dhan feed client against a local fake WebSocket server, including a forced disconnect. No test contacts Dhan. The existing `*.dhan.co` guard stays in place.
- **Dhan limits**: one profile call per engine start, one feed connection (Dhan allows 5), well under 5,000 instruments.
