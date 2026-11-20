# Design

## Context

`add-market-data` delivers `Tick` values (exchange time in UTC, `Decimal` LTP, optional LTQ and cumulative day volume) and `FeedStatus` events on the `TickBus`. Each consumer reads from its own bounded queue that drops the oldest item when full and counts the drops. Feed subscriptions are owner-based and capped (`Subscriptions`). `MarketDataService` supervises the feed, the instrument master and their loops, and `engine/market_data/clock.py` holds the IST helpers. The market-hours smoke test (2026-10-28) showed that exchange time (LTT) is an IST epoch with **1-second resolution**, and ticks arrive 0.1–1.1 s after their LTT. Motivation: see proposal.md, Why. Requirements: `specs/candles/spec.md`.

## Goals / Non-Goals

**Goals:**
- One code path for live, historical and simulated candles, so warm-up and live candles cannot disagree on alignment or aggregation.
- Closing and publishing candles cost little per tick, so the S4 tick-to-exit budget (p99 < 5 ms, PRD §9.1) is unaffected.
- Every way a candle can be wrong (missed start, dropped ticks, outage) is visible to the consumer as the partial flag or as a refill, never silent.

**Non-Goals:**
- Indicators, trailing methods and any decision about which timeframe a trade uses (S4).
- Persisting candles or surviving a restart without reloading history.
- Repairing a partial candle that has no outage behind it (for example a startup mid-interval) from history later. It stays partial.

## Decisions

### 1. Build only 1-minute candles from ticks; aggregate everything else from 1-minute candles

The tick builder makes 1-minute candles only. 3-, 5- and 15-minute candles are aggregated from completed 1-minute candles, whether those came from ticks or from history. History is also fetched at 1 minute.

*Why:* one aggregation function serves live, warm-up and refill, so a 15-minute candle has exactly the same shape whether it came from history or from ticks. The partial flag propagates naturally: an aggregate is partial if any constituent is. Per tick there is one candle update, not three.
*Alternative:* a tick builder per timeframe. That means three times the per-tick work, and live candles could diverge from the history-derived ones.

### 2. Close by a shared boundary timer, not only by the next tick

1-minute boundaries are the same for every instrument, so one task sleeps until the next boundary plus `CANDLE_CLOSE_GRACE_S` and then closes every due 1-minute candle and every aggregate whose interval has ended. A tick for a later interval also closes its instrument's current candle early (spec: Completed candles only).

*Why the grace (default 2 s):* the smoke test measured ticks arriving up to ~1.1 s after their LTT second, so a trade at 10:05:59 can arrive at about 10:06:01. Two seconds covers that with margin. Ticks later than that are counted as late and dropped (spec scenario "Tick after publication").
*Alternative:* close only on the next tick. Illiquid options would then hold a candle open for minutes, which delays the stop.

### 3. Bucket by exchange time; the clock decides when to close

A tick goes into the interval of its `exchange_time` (IST epoch, confirmed). The engine clock (`utc_now`, injectable) decides when an interval is closed. Using exchange time keeps live candles consistent with Dhan's history and charts. Using the engine clock for closing means a silent instrument still closes on time.

### 4. Volume from cumulative day volume deltas

Each tick's volume contribution is `volume − previous volume` for that instrument. The first tick after start or reconnect has no baseline, so its candle is partial anyway (Decision 6) and its volume counts from that tick. A negative delta (a feed reset) counts as 0 and marks the candle partial. Ticks without volume (indices) add 0.

### 5. History source interface next to `MarketDataProvider`

A small `HistorySource` `Protocol` in `engine/market_data` has one method: fetch 1-minute candles for a key between two times. There are two implementations:

- `DhanHistory` in `engine/broker/dhan/history.py`: an `httpx.AsyncClient` call to the intraday chart endpoint.
- `SimulatedHistory`: a seeded random walk on the tick grid using the `SIM_*` settings.

Consumers never import `engine.broker` (import-linter, D-023).

**Lookback.** The fetch window ends at the last completed 1-minute boundary. It starts far enough back to give `CANDLE_HISTORY` candles of the largest timeframe:

1. sessions = ⌈history × max timeframe ÷ 375⌉
2. calendar days = sessions × 7 ÷ 5, plus 4 days of holiday margin

For the defaults that is about 16 calendar days, which is far below Dhan's 90-day per-request limit, so one request per instrument is enough. Candles beyond `CANDLE_HISTORY` are dropped after aggregation.

*Alternative:* a market-holiday calendar to compute the window exactly. That's out of scope; the margin costs only a slightly larger response.

### 6. Join, partial and refill rules

- **Warm-up order:**
  1. Subscribe the feed first, so ticks start building the live 1-minute candle.
  2. Then load history up to the last completed boundary.
  3. History fills every interval before the first live interval, and live candles continue from there.
  4. The first live interval is partial unless its first tick fell in the first second of the interval and the feed was connected for the whole interval. Treating a mid-start as partial is the conservative default.
- **Partial** is set when any of these holds: the builder started mid-interval; the feed was not `CONNECTED` for part of the interval; the builder's bus queue dropped ticks during the interval (the drop counter is read when the candle closes); or a negative volume delta occurred.
- **Refill:** on a `CONNECTED` status after `RECONNECTING`, the service waits until the interval containing the reconnect has completed, plus 5 seconds so Dhan's history includes it. It then reloads history for each candle-subscribed key from the start of the outage. Missing intervals are published, and partial ones are replaced by their history version. A replacement is published as a new completion event with a `replaced` marker, so S4 can recompute.

### 7. Candle events: generalise the bus fan-out

Candle completions go to consumers through the same per-consumer queue pattern as ticks. `bus.py`'s `Subscription` and `TickBus` become a generic `Bus[T]`, and `TickBus` becomes `Bus[Tick | FeedStatus]`. Tick behaviour and its tests stay unchanged. Candle events are rare (at most instruments × timeframes per minute), so the default queue size never drops in practice. Drops are still counted and warned about by the same code.
*Alternative:* a second hand-written fan-out class, which duplicates the drop and warning logic.

### 8. In-memory store, `deque` per key and timeframe

`CandleStore` keeps a `collections.deque(maxlen=CANDLE_HISTORY)` (a fixed-size ring buffer, like a bounded `Queue<T>` that discards from the front) per (instrument, timeframe). Reading the last N copies a slice of immutable candles. `Candle` is a frozen slotted dataclass, like `Tick`, because it never crosses a process boundary in this stage. A pydantic contract arrives with the first API use (S5/S6).

### 9. Candle subscriptions reuse feed ownership

`CandleService.request(keys, owner)` calls the feed's `subscribe(keys, owner)` first. A `SubscriptionLimitError` propagates with nothing started. A key's candle state lives while any owner holds it. `release` mirrors `unsubscribe`. History loads only for keys that are new to the candle service.

### 10. Rate limiting the history endpoint

`DhanHistory` spaces request starts with a simple async limiter: one shared `asyncio.Lock` and the time of the last start, with a minimum spacing of 1 ÷ `DHAN_HISTORY_RATE_PER_S`. The default is 4/s, kept under Dhan's documented data-API limit (verify in tasks). The S3 central rate limiter will absorb this limiter later. Requests queue rather than fail. An HTTP 401/403 or a Dhan auth/plan error code sets a sticky "history disabled" flag until restart (spec: History failures are contained). Timeouts and 5xx errors are not retried automatically; the next reconnect or request retries.

### 11. Settings

| Variable | Default | Meaning |
|---|---|---|
| `CANDLE_INTERVALS` | `1,3,5,15` | Candle intervals in minutes, from {1, 3, 5, 15}; all anchored to 09:15 IST |
| `CANDLE_HISTORY` | 200 | Completed candles kept per instrument and timeframe (also the warm-up target) |
| `CANDLE_CLOSE_GRACE_S` | 2 | Seconds after an interval ends before it closes |
| `DHAN_HISTORY_RATE_PER_S` | 4 | History request starts per second |

They are validated in `EngineSettings` like the feed settings. All are optional, so `.env.example` only gains commented lines.

### 12. Testing approach

- **Builder and aggregation:** pure functions over scripted ticks with an injected clock. `hypothesis` checks the aggregation property: aggregating 1-minute candles to N minutes gives the same OHLCV as building N-minute candles directly from the same ticks. It also checks that high ≥ max(open, close) and low ≤ min(open, close).
- **Join, partial and refill:** `SimulatedFeed` plus `SimulatedHistory` with a fake clock and a forced status sequence.
- **`DhanHistory`:** `httpx.MockTransport` against a redacted fixture recorded once by the owner, never the real API (CLAUDE.md rule 11).

## Risks / Trade-offs

- [Dhan's intraday history may lag real time by some seconds, or include the still-forming minute] → The fetch window ends at the last completed boundary, and any returned candle at or after it is discarded. The refill waits 5 seconds after the interval completes. The owner smoke test checks both.
- [History timestamps could be a different epoch convention from LTT] → A task records the real response and the owner compares candle times with Dhan's chart. The decoder for history timestamps sits in one function, like the LTT one.
- [Quote-mode ticks are snapshots, so the live builder can miss trades between them: a missed trade at a new extreme, or the first or last trade of an interval] → The "Live candles agree with history" requirement only asserts what snapshots guarantee: same start, live high ≤ history high, live low ≥ history low. Open, close and volume differences are reported, not failed. Volume comes from the cumulative day total, so it drifts only at interval edges.
- [Dhan history may emit zero-volume candles for minutes without trades, unlike the live builder (spec: Intervals without trades)] → The parser drops candles with zero volume and open = high = low = close equal to the previous close *only if* the fixture shows Dhan emits them. Otherwise nothing to do. The task decides this from the recorded data.
- [A large `FEED_MAX_SUBSCRIPTIONS` burst makes warm-up slow at 4/s, e.g. 200 instruments ≈ 50 s] → Loads are queued per request, and a requesting component can read candles as soon as its own key loads. Startup has no candle requests until S4, so nothing waits at boot.
- [A startup mid-interval leaves one partial candle per timeframe that is never repaired (non-goal)] → It is flagged. S4's open question on "history too short" decides whether to skip or accept partial candles.
- [The builder queue drops ticks under extreme bursts] → The default queue is 10,000 ticks, the drop counter marks affected candles partial, and a warning is logged. This is visible, not silent.

## Migration Plan

This change is additive: no existing behaviour, contract or setting changes. The `bus.py` generalisation keeps `TickBus` working the same, and existing tests guard it. Deploying is the usual compose rebuild. Rollback means reverting the commit. No data is migrated, because candles are not persisted.

## Open Questions

- Dhan's exact data-API rate limit and whether intraday history includes today's minutes up to now. Neither changes the design: the rate is a setting, and both behaviours are handled by the window rule. The fixture task confirms them before the default is fixed.
