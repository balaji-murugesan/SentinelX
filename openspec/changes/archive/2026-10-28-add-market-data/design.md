# Design

## Context

Stage 0 left a skeleton engine. [`src/engine/main.py`](../../../src/engine/main.py) runs an `asyncio.TaskGroup` with a heartbeat and a diagnostic-ping consumer. `engine/broker/` is an empty package. Constraints that shape this design:

- **Import boundary:** only `engine.broker` may call Dhan (`quality-gates`). Market data must still be a separate interface from execution (D-023).
- **Engine is single-loop asyncio** (CLAUDE.md). Nothing may block the loop.
- **Latency budget:** tick → exit decision p99 < 5 ms (PRD §9.1). The tick path must be lean.
- **Tests can't reach Dhan:** `tests/conftest.py` blocks `*.dhan.co`, including `images.dhan.co` and `api-feed.dhan.co`.
- **Dhan facts used here** come from the v2 docs (live-market-feed, authentication, instruments, annexure), checked 2026-10-25:
  - Feed: `wss://api-feed.dhan.co?version=2&token=…&clientId=…&authType=2`; JSON requests, little-endian binary responses with an 8-byte header (response code, length, segment, security ID).
  - Subscribe codes: 15 Ticker, 17 Quote, 21 Full. At most 100 instruments per message, 5,000 per connection, 5 connections per user.
  - Server pings every 10 s and drops clients silent for over 40 s.
  - Data API error codes: 805 too many connections, 806 plan not subscribed, 807 token expired, 808–810 auth/token/client invalid.
  - Tokens from the web portal last 24 h. `GET /v2/profile` returns `tokenValidity` and data-plan status.
  - The instrument CSV (`api-scrip-master-detailed.csv`), checked against the real file on 2026-10-26, does contain freeze quantity (`SM_FREEZE_QTY`), although the docs page omits it. Its tick sizes are in **paise** for equity and F&O rows and in rupees for index rows.

## Goals / Non-Goals

**Goals:**
- Ticks flow from either source to any number of in-engine consumers through one interface.
- Dhan disconnects heal themselves; credential problems stop cleanly with a clear message.
- Instrument data that S2/S3 can trust, with an explicit freshness flag.

**Non-Goals:**
- Candles, indicators, historical data (`add-candles`).
- Publishing ticks to Redis or the dashboard (S5).
- Market holiday calendar. The silence watchdog uses plain weekday hours (see Risks).
- Market depth (Full packet), OI-driven logic, multiple feed connections.

## Decisions

### 1. Module layout: interface in `engine/market_data`, Dhan code in `engine/broker/dhan`

```
src/contracts/market.py          ExchangeSegment, InstrumentKey, Instrument (pydantic, shared with S2+)
src/engine/market_data/
    provider.py                  MarketDataProvider Protocol, Tick, FeedStatus
    bus.py                       TickBus (fan-out)
    instruments.py               InstrumentMaster (pure lookups over parsed rows)
    simulated.py                 SimulatedFeed (scripted + random walk)
    service.py                   wires source + master + bus; supervised task for main.py
src/engine/broker/dhan/
    session.py                   startup profile check, expiry info
    instruments_source.py        CSV download, cache, parse → rows
    feed.py                      DhanFeed (WebSocket client, reconnect, watchdog)
    packets.py                   binary packet decoder (pure functions)
```

`MarketDataProvider` is a `typing.Protocol`. That is structural typing, close to a C# interface, except that a class satisfies it just by having the right methods, without declaring `: IMarketDataProvider`. `DhanFeed` and `SimulatedFeed` both satisfy it. Consumers depend only on `TickBus` and the protocol, never on `engine.broker`.

*Alternative:* put the Dhan feed in `engine/market_data/dhan/` and widen the import rule. Rejected by the owner: the rule stays simple, and the separation D-023 asks for is at the interface level, not the folder level.

### 2. Own asyncio WebSocket client (`websockets` library), not the `dhanhq` SDK

The SDK's market feed runs its own blocking loop. We would need `asyncio.to_thread` plus a thread-to-loop hand-off for every tick, which would eat into the 5 ms budget and is awkward to fake in tests. The protocol is small (one header, about 8 packet types), so a decoder of pure functions using `struct.unpack_from("<…")` (`<` = little-endian) is short and fully unit-testable. `websockets` is asyncio-native and answers the server's pings automatically.

Consequence: `dhanhq` stays un-imported. The import-linter exception `engine.broker -> dhanhq` still matches nothing, so its alerting stays `"none"`. The Stage 0 follow-up note about switching it to `"warn"` waits until something actually imports `dhanhq` (S7 `DhanBroker`, if ever).

### 3. Quote packets; prices converted float32 → `Decimal`

Subscribe with code 17 (Quote). It carries LTP, last traded quantity, last trade time, volume and day OHLC, which is what `add-candles` needs. Ticker lacks volume, and Full adds depth we don't use. Index instruments send Ticker/Index packets; the decoder handles codes 1, 2, 4, 5, 6, 7, 8 and 50, and counts and skips anything else.

Dhan sends prices as float32. Conversion: `Decimal(f"{value:.2f}")`, then, if the instrument is in the master, round to its tick size. Why both steps: float32 has 24 bits of precision, so above about ₹1,31,072 (e.g. MRF) adjacent float32 values are about 0.0156 apart. Two-decimal rounding alone could then be off by 0.01, but rounding to the 0.05 tick grid is still exact.

### 4. `Tick` is a frozen slotted dataclass, not a pydantic model

`@dataclass(frozen=True, slots=True)` is similar to a C# `readonly record struct`-style immutable record with no per-instance dictionary. Ticks never leave the engine process, so the CLAUDE.md rule that cross-process messages are pydantic models in `contracts` doesn't apply. Pydantic validation on every tick would cost microseconds for no benefit. `InstrumentKey` and `Instrument` *are* pydantic models in `contracts`, because S2 order commands and S5 snapshots will carry them across processes.

`InstrumentKey` = (`ExchangeSegment`, security ID). It is the common key D-023 asks for: Dhan's segment names (`NSE_FNO` etc.) already encode exchange + segment, and for NSE the security ID is the exchange token (*assumption, checked against the CSV in task 2.1*).

### 5. Tick bus: one bounded queue per consumer, drop-oldest

`bus.subscribe(name, maxsize)` returns an async iterator. Consumers write `async for tick in subscription:`, the async counterpart of `foreach` over `IAsyncEnumerable<T>`. Publishing is synchronous and non-blocking: for each consumer it calls `put_nowait`, and if that queue is full it drops the oldest item and counts the drop. The feed reader therefore never waits on a consumer, and one stuck consumer can't starve the others. Status events travel on the same queues, so ordering between "reconnecting" and later ticks holds.

*Alternative:* call consumers' handlers directly and synchronously. Lower latency (no queue hop), but one slow or buggy handler delays everyone, including the exit check. The queue hop costs tens of microseconds, well inside 5 ms.

*Trade-off:* drop-oldest suits LTP consumers (only the latest price matters). The candle builder in `add-candles` needs every tick, so it will use a large queue and monitor its drop counter.

### 6. Reconnect policy

- Back-off: 1 s, doubling, capped at `FEED_RECONNECT_MAX_S` (default 30 s), with ±20 % jitter. It resets after a connection stays up for 60 s.
- Code 805 (too many connections): back-off starts at the cap.
- Codes 806–810 (plan, token, client ID), or an HTTP 401/403 on the WebSocket upgrade: terminal. Status `down`, no more attempts until restart. Retrying with a dead token only produces noise and could trip Dhan's limits.
- Silence watchdog: during weekdays 09:15–15:30 IST, if no frame arrives for `FEED_SILENCE_S` (default 30 s), close and reconnect. This catches half-open TCP connections that the 40 s server timeout doesn't detect from our side.
- On reconnect, resubscribe the full current subscription set in batches of 100.
- Subscriptions are reference-counted per instrument. A second request for the same key sends nothing to Dhan. The set is capped at `FEED_MAX_SUBSCRIPTIONS` (default 200), so a bug can't bulk-subscribe the instrument master. The cap is checked before any message goes out.

### 7. Session check: one `GET /v2/profile` at startup

The check uses httpx `AsyncClient` with a 10 s timeout. It parses `tokenValidity` into a UTC expiry. The exact format must be confirmed from one real response; the owner runs `cli` in task 3.1 and a redacted fixture is saved. The check also reads the data-plan status. On failure the market-data service stays unavailable; the process does not exit, because Docker's restart policy would turn an expired token into a restart loop hitting Dhan.

Expiry warnings are computed locally every `DHAN_TOKEN_WARN_INTERVAL_MIN` (default 30) once inside `DHAN_TOKEN_WARN_BEFORE_H` (default 3 h). There is an extra startup warning if expiry falls before 15:30 IST today. Telegram delivery of these warnings comes in S7. For now they are structured log events (`dhan_token_expiring`).

### 8. Instrument master: detailed CSV, disk cache, immutable snapshot

- Download with httpx streaming to a temp file in `INSTRUMENT_CACHE_DIR`, then atomically rename it to `instruments-YYYY-MM-DD.csv`. Keep the last 3 files.
- Parse with the stdlib `csv` module inside `asyncio.to_thread` (runs on a worker thread, like `Task.Run`), keeping only NSE_EQ, NSE_FNO and IDX_I rows.
- Build an immutable snapshot with a dict keyed by `InstrumentKey` and a dict keyed by (underlying, expiry, strike, option type). Swap it in with a single reference assignment, so readers never see a half-built master.
- Text search is a linear scan, which is fine for CLI and future trade-panel use and is not on the tick path.
- The daily refresh runs at `INSTRUMENT_REFRESH_IST` (default `08:45`). Daily because weekly expiries, new strikes and lot-size revisions only appear in a fresh file. The file is static, served from Dhan's file host rather than an API endpoint: one download per day plus one per engine restart, and it contains no prices. MCX and BSE rows are discarded while parsing.

### 9. Freeze quantity from Dhan's file (`SM_FREEZE_QTY`)

*Revised 2026-10-26 after inspecting the real file; owner's choice.* The plan originally had an owner-maintained `config/freeze_qty.toml`, because the docs page doesn't list the column. The real file has it, refreshed daily alongside lot sizes, and its values follow NSE's convention: an order is frozen when quantity ≥ freeze quantity. For example:

| Underlying | Lot | `SM_FREEZE_QTY` | Largest single order |
|---|---|---|---|
| NIFTY options | 65 | 3511 | 3510 (54 lots) |
| BANKNIFTY options | 30 | 1441 | 1440 (48 lots) |
| RELIANCE options | 500 | 20001 | 20000 (40 lots) |

The parser stores the raw value as `freeze_qty`; 0, empty or non-numeric becomes `None` ("unknown"). S3 must treat `qty >= freeze_qty` as needing slicing, and must fail closed on `None`. The owner spot-checks one value against NSE's published freeze limits during task 2.2.

*Alternatives:* the TOML file (manual upkeep, drifts from lot-size changes); Dhan value plus a TOML override (more code for a problem not yet seen).

Tick sizes are converted at parse time: NSE_EQ and NSE_FNO rows ÷ 100 (paise → rupees), IDX_I rows as-is; a zero or invalid tick drops the row (counted). Index placeholders (`0001-01-01` expiry, `XX` option type, empty or `0` strike) become `None`.

### 10. Settings

New `EngineSettings` fields:

| Variable | Required | Notes |
|---|---|---|
| `FEED_SOURCE` | always | `dhan` \| `simulated` |
| `FEED_INSTRUMENTS` | no (default empty) | `SEGMENT:ID,…` |
| `DHAN_CLIENT_ID` | when `dhan` | masked to last four characters |
| `DHAN_ACCESS_TOKEN` | when `dhan` | `SecretStr` |
| `INSTRUMENT_CACHE_DIR` | no | default `/var/lib/sentinelx/instruments` (compose volume) |
| `INSTRUMENT_REFRESH_IST` | no | default `08:45` |
| `FEED_MAX_SUBSCRIPTIONS` | no | default 200, must be 1–5000 |
| `FEED_RECONNECT_MAX_S` | no | default 30 |
| `FEED_SILENCE_S` | no | default 30 |
| `DHAN_TOKEN_WARN_BEFORE_H` | no | default 3 |
| `DHAN_TOKEN_WARN_INTERVAL_MIN` | no | default 30 |
| `SIM_*` | no | random-walk start price, step and interval, seed |

`DHAN_FEED_URL`, `DHAN_API_BASE` and `DHAN_INSTRUMENTS_URL` default to Dhan's addresses. They exist only so tests can point at a local fake server.

A `model_validator(mode="after")` enforces two rules: credentials are required when `FEED_SOURCE=dhan`, and `simulated` is refused when the resolved mode is live. This mirrors the existing mode gate. `FEED_SOURCE` is required with no default, so a production paper run can't silently fall back to fake prices.

### 11. Engine wiring and containment

`main.run` adds one task: `market_data_service.run(stop)`. Inside, each sub-task (session check, master refresh loop, feed loop, expiry-warning loop) runs under a small supervisor. The supervisor catches `Exception`, logs it with the component name, marks the component down and returns. It never re-raises into the engine `TaskGroup`, which would cancel the heartbeat and restart the container. `asyncio.CancelledError` (shutdown) is a `BaseException`, so it passes through untouched.

### 12. Keeping the token out of logs

Only `feed.py` builds the URL, and nothing else stores it. The `websockets` library's own logger is set to WARNING. Connection errors are logged as exception class plus `observability.masking.scrub_text(str(exc))`, which already masks JWT-looking strings. A test forces a failing connection at DEBUG level and asserts that neither the token nor `token=` appears in the captured output.

### 13. Testing approach

- **Decoder:** packets built in tests with `struct.pack` per the documented layouts, covering every handled code, plus truncated and unknown packets.
- **DhanFeed:** a real `websockets.serve` server on `127.0.0.1` (allowed by the guard). It checks the subscribe JSON, streams packets, drops the connection, sends code 807, and stays silent, so it exercises reconnect, resubscribe, terminal stop and the watchdog. Time-based behaviour uses small configured delays and an injectable clock for "is it market hours".
- **Session and instrument source:** `httpx.MockTransport` (an in-process fake HTTP handler, no sockets) with a trimmed CSV fixture and a redacted profile fixture.
- **SimulatedFeed:** determinism (same seed → same path), tick grid, scripted order.
- **Guard:** a test scans `src/` for `dhan.co` outside `src/engine/broker/`.
- **Manual smoke (owner, never CI):** `uv run --env-file .env python -m cli feed watch IDX_I:13` during market hours. Compare the printed LTPs with Dhan's web terminal, then turn Wi-Fi off and on and watch it reconnect.

## Risks / Trade-offs

- [Dhan changes the packet layout or codes] → The decoder is isolated and table-driven. Unknown codes are counted and skipped, not fatal. The owner's smoke check is the early warning.
- [Exchange time (LTT) epoch may be IST-based rather than true UTC] → Verify against a live packet in the smoke task by comparing with the receive time. The conversion lives in one function. Engine receive time is always recorded independently.
- [Silence watchdog fires on market holidays (no ticks at 11:00)] → Reconnects back off to the 30 s cap and are logged at info level after the first. A holiday calendar is a later addition.
- [Drop-oldest hides a slow consumer] → The per-consumer drop counter is logged (rate-limited) and exposed for S5 health display.
- [Instrument CSV is large (tens of MB) and parsing takes seconds] → It runs on a worker thread at startup and 08:45. Ticks don't depend on it. Tick-size rounding falls back to 2 dp until it loads.
- [Token pasted daily is a manual step that can be forgotten] → Startup check plus warnings. The engine keeps running so exits keep working once S4 exists. Automated renewal can be proposed later as its own change.
- [Dhan's freeze quantity is wrong or its meaning changes] → Spot-checked against NSE in task 2.2. Unknown (0/empty) is never treated as unlimited, and S3 must fail closed on unknown. That is noted for the S3 proposal.

## Migration Plan

1. Add the new variables to `.env` and `.env.example` (`FEED_SOURCE=simulated` in the example, so `docker compose up` and CI keep working with no Dhan credentials). The `.env.example` edit needs owner approval because it is read-denied to the agent.
2. `docker-compose.yml`: pass the new variables to `engine`, using `FEED_SOURCE: ${FEED_SOURCE:?set FEED_SOURCE in .env}`, and add a named volume for `INSTRUMENT_CACHE_DIR`.
3. No database migration.
4. Rollback: revert the commit. The engine returns to Stage 0 behaviour, and the extra env vars are ignored (`extra="ignore"`).

## Open Questions

- Exact `tokenValidity` format in `/v2/profile`. It is resolved by one real call in task 3.1 and affects only the parser.
- Whether the LTT epoch is UTC or IST-shifted. Resolved in the smoke task; it affects one conversion function.
- Exact column names in the detailed CSV (security ID, underlying). Resolved by inspecting the real header in task 2.1; affects only the parser mapping.
