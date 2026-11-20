# Proposal

## Why

Seven of the eight planned trailing-stop methods (D-025, [`docs/trailing-sl.md`](../../../docs/trailing-sl.md)) work from candles, and some need indicators such as ATR(22) or EMA(50) that require dozens of completed candles at the moment a trade is entered. Today the engine has ticks only. This change is the second half of PRD Stage 1 (the first, `add-market-data`, was archived 2026-10-28). It builds candles from ticks and loads recent history, so that Stage 4 can trail on candles from the first minute of a trade.

## What Changes

- **Candle builder**: turns the tick stream into OHLCV candles (open, high, low, close, volume) per instrument for the configured intervals (default 1, 3, 5 and 15 minutes). Candles align to NSE's 09:15 IST session start and cover 09:15–15:30 IST only. Each candle is bucketed by the exchange's trade time, not the receive time. Volume comes from the feed's cumulative day volume; it is zero for indices. A candle is published only once it is **complete**, when its interval ends, plus a short grace period for late ticks. Forming candles are never offered to consumers, so indicators never jitter (`trailing-sl.md`, "completed candles only").
- **Historical warm-up**: when a component asks for candles on an instrument, the engine first loads recent 1-minute history from Dhan's intraday chart API (or from a simulated source) and derives the higher timeframes from it. It then continues seamlessly with live candles. The amount kept per instrument and timeframe is configurable (default 200 candles).
- **Gap handling**: after a feed reconnect, completed candles missed during the outage are refilled from history. A live candle the builder did not see from start to end is marked **partial**, so later consumers can decide whether to trust it. Partial candles can arise when the engine starts mid-candle or when ticks were dropped.
- **Candle subscriptions on demand**: candles are built only for instruments a component asks for, with the same owner-based rules as feed subscriptions. Asking for candles also subscribes the feed. Consumers can read the last N completed candles and receive an event each time a candle completes.
- **Dhan historical client**: a new client in `engine/broker/dhan/` (still the only package that talks to Dhan) with its own simple rate limiter for the data API. The central limiter arrives in S3.
- **Developer CLI**: a `candles watch <KEY> --tf <minutes>` command prints the warm-up candles and then each completed live candle. On exit it compares the live candles with a fresh history fetch for the same period, which is the owner's acceptance check that live and historical candles agree.

Out of scope: indicators (ATR, EMA, Supertrend, …) and trailing methods (S4), persisting candles to Postgres (they are rebuilt from history on restart), candles outside the regular session, timeframes above 15 minutes, a market-holiday calendar, expired-contract history, and candles for the dashboard (S6, "no charts").

## Capabilities

### New Capabilities
- `candles`: building completed OHLCV candles from ticks, timeframe alignment to the NSE session, historical warm-up and gap refill, partial-candle marking, on-demand candle subscriptions, and the read and completion-event interface for consumers.

### Modified Capabilities
None. The candle service relies on existing requirements unchanged: owner-based, capped feed subscriptions (`market-feed`, "Subscriptions only on demand"); fan-out with a bounded, drop-counted queue per consumer (`market-feed`, "Fan-out to consumers"); the startup session check and the Data API plan (`broker-session`); and the rule that only `engine.broker` calls Dhan (`quality-gates`).

## Impact

- **Code**:
  - New `src/engine/market_data/candles.py` (builder, store, subscriptions) and a history-source interface beside `MarketDataProvider`.
  - New `src/engine/broker/dhan/history.py` (intraday chart client with a rate limiter).
  - A simulated history source next to `SimulatedFeed`.
  - Wiring in `src/engine/market_data/service.py`.
  - New settings in `src/engine/config.py`.
  - A new CLI command in `src/cli/`.
- **Config**: new optional env vars `CANDLE_INTERVALS` (default `1,3,5,15`), `CANDLE_HISTORY` (default 200), `CANDLE_CLOSE_GRACE_S` (default 2) and `DHAN_HISTORY_RATE_PER_S`.
- **Dhan**: new calls to the intraday chart endpoint, under the existing Data API plan. A redacted response is recorded once by the owner as a test fixture. No test calls Dhan.
- **Dependencies**: none new.
- **Downstream**: S4 (`add-trailing-exits`) consumes completed candles and the partial flag. S2 and S3 are unaffected.
