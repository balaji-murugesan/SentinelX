# Tasks

## 1. Settings, candle type and generic bus

- [x] 1.1 Add `CANDLE_INTERVALS`, `CANDLE_HISTORY`, `CANDLE_CLOSE_GRACE_S` and `DHAN_HISTORY_RATE_PER_S` to `EngineSettings` (design §11), with intervals parsed to a sorted tuple from {1, 3, 5, 15}; add the names to the test `CONFIG_VARS` list and as commented lines in `.env.example` (**after owner approval**, the file is agent-denied); verify tests for the "Default intervals" and "Interval that does not divide the session" scenarios and for the bounds of the other three settings
- [x] 1.2 Add `Candle` (frozen slotted dataclass: key, timeframe minutes, start UTC, Decimal OHLC, int volume, `partial`, `replaced`) in `engine/market_data/candles.py` and IST interval helpers in `clock.py` (interval start for a time and timeframe, session bounds, last completed 1-minute boundary); verify unit tests for the "15-minute alignment", "Received late, traded on time" and "Outside the session" scenarios, including 09:15 and 15:29:59 edges
- [x] 1.3 Generalise `bus.py` into `Bus[T]` with `TickBus = Bus[Tick | FeedStatus]` (design §7), commenting the Python generics syntax with a C# analogy; verify every existing `test_market_bus.py` test passes unchanged and mypy is clean

## 2. One-minute builder and aggregation

- [x] 2.1 Implement the 1-minute tick builder (design §1, §3, §4): OHLC by exchange time, volume from cumulative day-volume deltas (first tick has no baseline, a negative delta counts 0 and marks partial, missing volume adds 0), early close when a later-interval tick arrives, late ticks counted and rate-limit warned; verify tests for "OHLC from ticks", "Volume from cumulative day volume", "Index candle", "Tick after publication" and "Illiquid option"
- [x] 2.2 Implement aggregation from 1-minute candles to N minutes with partial propagation; verify the "Derived 15-minute candle" scenario and `hypothesis` properties: aggregating equals building N-minute candles directly from the same ticks, high ≥ max(open, close), low ≤ min(open, close), and an aggregate is partial iff any constituent is

## 3. Store, closing timer and candle subscriptions

- [x] 3.1 Implement `CandleStore` (`deque(maxlen=CANDLE_HISTORY)` per key and timeframe, last-N read oldest first, replace-by-start for refill), commenting `deque(maxlen=…)`; verify the "Read the last five" and "Fewer candles than asked" scenarios and that the forming candle is never returned
- [x] 3.2 Implement the shared boundary timer (sleep to next minute boundary + grace by the injectable clock, close due 1-minute candles, then due aggregates, publish completion events on a `Bus[Candle]`); verify the "Close on time with no further ticks" scenario with a fake clock, and that a tick for a later interval closes the candle before the timer does
- [x] 3.3 Implement `CandleService.request` / `release` on top of the feed's owner-based subscriptions (design §9); verify the "Two owners, one releases" and "Over the feed cap" scenarios against `SimulatedFeed`

## 4. History sources

- [x] 4.1 Owner step: run a one-off intraday-chart request (via a small `cli` helper added here that prints only field names, row count, first/last timestamps and whether any zero-volume or forming-minute rows are present) for `IDX_I:13` and one liquid NSE_FNO option during market hours; save a redacted fixture under `tests/fixtures/dhan/`; record in `history.py` the request shape, the timestamp epoch convention, the data-API rate limit from Dhan's docs, and the zero-volume and forming-minute findings (design Risks); verify the fixture holds no token or client ID
- [x] 4.2 Implement the `HistorySource` Protocol and `DhanHistory` (`httpx.AsyncClient`, lookback window per design §5, discard rows at or after the last completed boundary, timestamp conversion in one function, URL built from `endpoints.py`); verify `httpx.MockTransport` tests against the fixture, including window bounds and the discard rule, and that logged errors contain neither the token nor request or response bodies
- [x] 4.3 Add the request-start limiter and the sticky "history disabled" flag (design §10); verify the "Twenty instruments at once" scenario with a fake clock, and the "Data plan inactive" scenario (401/403 or an auth/plan error code stops further requests)
- [x] 4.4 Implement `SimulatedHistory` (seeded random walk on the tick grid from the `SIM_*` settings); verify the "Repeatable simulated warm-up" scenario, including a `hypothesis` check that prices are positive tick multiples

## 5. Warm-up, join, partial and refill

- [ ] 5.1 Implement warm-up and join in `CandleService` (feed subscribe → history load → install history before the first live interval → live continues; design §6); verify the "Enough history at entry", "Warm-up before first use" and "Started mid-candle" scenarios with `SimulatedFeed` + `SimulatedHistory` and a fake clock, including no duplicate or overlapping intervals
- [ ] 5.2 Implement partial marking from feed status and the builder's bus drop counter; verify the "Ticks dropped" scenario (1-minute and containing 5/15-minute candles partial) and that a candle seen whole while `CONNECTED` is not partial
- [ ] 5.3 Implement refill after reconnect (wait for the reconnect interval to complete + 5 s, reload from the outage start, publish missed intervals, replace partial ones with `replaced` events); verify the "Two-minute outage" scenario with a forced status sequence
- [ ] 5.4 Implement history-failure containment; verify the "History endpoint down" scenario (live candles continue, one error logged with key and reason, reads return live candles only) and that the next reconnect retries the load

## 6. Engine wiring and CLI

- [ ] 6.1 Wire `CandleService` into `MarketDataService` under `_supervise` (choosing `DhanHistory` or `SimulatedHistory` by `FEED_SOURCE`), with no candle requests at startup; verify a `run()` test that a candle request in simulated mode yields warm-up and live candles, and that a crash inside the candle service leaves the feed and heartbeat running
- [ ] 6.2 Add CLI `candles watch <KEY> --tf <minutes>` (prints the last few warm-up candles, then each completed or replaced candle with its partial flag, until Ctrl+C; on Ctrl+C it re-fetches history for the watched period and prints, per complete live candle, the "Live candles agree with history" check: same start, live high ≤ history high, live low ≥ history low, and the open/close/volume differences); verify `uv run python -m cli --help` lists it, it runs against `FEED_SOURCE=simulated`, and a unit test of the comparison flags a live high above the history high
- [ ] 6.3 Add the new variables to the README configuration table; verify the documented `candles watch` command runs as written in simulated mode

## 7. Integration check

- [ ] 7.1 Run the full definition of done: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports`, `uv run pytest`, `openspec validate add-candles --strict`; verify all pass
- [ ] 7.2 Owner smoke test during market hours (never in CI), `FEED_SOURCE=dhan`, one liquid NSE_FNO option: `uv run --env-file .env python -m cli candles watch <KEY> --tf 3` for at least 30 minutes. Acceptance: (a) the Ctrl+C comparison passes for every complete 3-minute candle (3m from ticks vs 3m aggregated from Dhan's 1m history); (b) candles appear only after their boundary, including an interval with no tick at its end; (c) the warm-up printed before the first live candle holds at least 22 candles per interval; (d) the first live candle is partial and later ones are not; (e) Wi-Fi off for ~2 minutes → the refill replaces the affected candles. Record the result in this task

## Workflow follow-up

- After merge, archive with `openspec archive add-candles --yes` (or `/opsx:archive add-candles`) and update the README Status table: S1 done.
- Note for the S4 proposal: candles carry `partial` and `replaced`; decide whether trailing methods skip partial candles and how a `replaced` candle recomputes the stop (tighten-only still applies).
