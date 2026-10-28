# Spec Delta

## Purpose

Turns the live tick stream into completed OHLCV candles per instrument and timeframe, warmed up from recent history, so that candle-based trailing stops and indicators have enough data from the moment a trade is entered.

## ADDED Requirements

### Requirement: Configured intervals
The engine SHALL build candles for the intervals in `CANDLE_INTERVALS`, given in minutes (default `1,3,5,15`), all anchored to the 09:15 IST open. Allowed values are 1, 3, 5 and 15: the intervals up to 15 minutes that divide the 375-minute session exactly, so the day never ends with a short candle. Any other or empty value SHALL stop the engine at startup.

#### Scenario: Default intervals
- **WHEN** `CANDLE_INTERVALS` is not set
- **THEN** candles are built for 1-, 3-, 5- and 15-minute intervals

#### Scenario: Interval that does not divide the session
- **WHEN** `CANDLE_INTERVALS` contains `7` or a value that is not a positive whole number
- **THEN** the engine refuses to start with an error naming the setting and the bad value

### Requirement: Session-aligned intervals
Candle intervals SHALL start at 09:15 IST and repeat at the interval length until 15:30 IST. A tick SHALL be placed in an interval by its exchange trade time. Ticks whose exchange time falls outside 09:15–15:30 IST SHALL NOT be used for candles.

#### Scenario: 15-minute alignment
- **WHEN** a tick has exchange time 09:31:05 IST
- **THEN** it belongs to the 15-minute candle that starts at 09:30 IST and the 5-minute candle that starts at 09:30 IST

#### Scenario: Received late, traded on time
- **WHEN** a tick with exchange time 09:29:59 IST is received at 09:30:00.8 IST
- **THEN** it updates the 09:29 one-minute candle, not the 09:30 one

#### Scenario: Outside the session
- **WHEN** a tick arrives with exchange time 09:08 IST (pre-open) or 15:40 IST
- **THEN** no candle is created or changed

### Requirement: Candle content
Each candle SHALL carry the instrument key, timeframe, start time (aware UTC), open, high, low and close as `Decimal` prices, volume as a whole number, and a partial flag. Open is the first trade in the interval, close the last, and high and low the extremes. Volume SHALL be the traded quantity within the interval, taken from the feed's cumulative day volume, and zero when the source has no volume (indices).

#### Scenario: OHLC from ticks
- **WHEN** an interval receives trades at 100.00, 101.50, 99.80 and 100.40 in that order
- **THEN** the candle has open 100.00, high 101.50, low 99.80 and close 100.40

#### Scenario: Volume from cumulative day volume
- **WHEN** the day volume is 1,000 at the last tick before an interval and 1,650 at its last tick
- **THEN** the candle's volume is 650

#### Scenario: Index candle
- **WHEN** candles are built for an index whose ticks carry no volume
- **THEN** every candle has volume 0

### Requirement: Completed candles only
A candle SHALL be published to consumers only after it is complete: when its interval end plus `CANDLE_CLOSE_GRACE_S` (default 2 seconds) has passed by the engine clock. A candle is also complete earlier when a tick for a later interval of the same instrument arrives. A forming candle SHALL NOT be readable through the consumer interface. A published candle SHALL change only by refill after a reconnect.

#### Scenario: Close on time with no further ticks
- **WHEN** the last trade in the 10:05 one-minute interval arrives at 10:05:40 and no tick follows
- **THEN** the 10:05 candle is published shortly after 10:06:02 by the engine clock

#### Scenario: Tick after publication
- **WHEN** a tick with exchange time inside an already-published interval arrives
- **THEN** the published candle is unchanged, and the late tick is counted and reported in a rate-limited warning

### Requirement: Intervals without trades
An interval in which no trade occurred SHALL produce no candle. Consumers SHALL see the next published candle start later than one timeframe after the previous one.

#### Scenario: Illiquid option
- **WHEN** an option has trades at 11:01 and 11:04 and none in between
- **THEN** one-minute candles are published for 11:01 and 11:04 only

### Requirement: Historical warm-up
When candles are first requested for an instrument, the engine SHALL always fetch one-minute history, whatever intervals are configured, and SHALL complete the request only after the warm-up is installed. It SHALL keep up to `CANDLE_HISTORY` (default 200) completed candles per instrument and interval. Every longer interval SHALL be derived from one-minute candles by the same aggregation the live builder uses: open first, high max, low min, close last, volume sum.

#### Scenario: Enough history at entry
- **WHEN** candles are requested at 10:30 IST for an instrument that traded on previous days
- **THEN** each interval immediately holds up to 200 completed candles ending with the last completed interval, including candles from previous sessions where today's are not enough

#### Scenario: Warm-up before first use
- **WHEN** a component requests candles and then reads the last 22 candles of an interval
- **THEN** the read happens only after the request has completed, and returns 22 history candles (where Dhan has that much history) before any live candle is used

#### Scenario: Derived 15-minute candle
- **WHEN** history holds the fifteen one-minute candles from 09:15 to 09:29 IST
- **THEN** the 09:15 fifteen-minute candle has the first one-minute open, the highest high, the lowest low, the last close and the summed volume

### Requirement: Live candles agree with history
A complete (non-partial) live candle SHALL have the same start time as the history candle for the same interval, built from the same one-minute aggregation. Quote-mode ticks are snapshots that can miss individual trades, so the live high SHALL NOT exceed the history high, and the live low SHALL NOT be below the history low. Open, close and volume MAY differ only by such missed trades.

#### Scenario: 3-minute live vs history
- **WHEN** 3-minute candles built from live ticks over a session are compared with 3-minute candles aggregated from Dhan's 1-minute history for the same period
- **THEN** every complete live candle has a matching history candle with the same start, live high ≤ history high, live low ≥ history low, and open, close and volume within the small differences that missed trades explain

### Requirement: Seamless join of history and live candles
After warm-up, live candles SHALL continue from the last historical candle with no duplicate and no overlap. If the live interval that was forming when warm-up began was not seen from its start, it SHALL be marked partial.

#### Scenario: Started mid-candle
- **WHEN** candles are requested at 10:32:20 IST for the 5-minute timeframe
- **THEN** history supplies candles up to the 10:25 candle, and the live 10:30 candle is published as partial

### Requirement: Partial candles
A live candle SHALL be marked partial when the builder did not observe its whole interval. This covers starting mid-interval, a feed outage during the interval, or ticks dropped from the candle builder's queue. Complete candles SHALL have the partial flag unset.

#### Scenario: Ticks dropped
- **WHEN** the candle builder's queue drops ticks during the 11:20 one-minute interval
- **THEN** the 11:20 candle and every higher-timeframe candle containing it are published as partial

### Requirement: Refill after reconnect
After the feed reconnects, the engine SHALL reload history for every instrument with candle subscribers. It SHALL publish any completed interval missed during the outage, and replace candles that were published as partial during the outage with the history version. The history versions are not partial.

#### Scenario: Two-minute outage
- **WHEN** the feed is down from 12:00:30 to 12:02:40 IST and then reconnects
- **THEN** the 12:01 one-minute candle is published from history, and the 12:00 and 12:02 candles are taken from history instead of the partial live versions

### Requirement: Candle subscriptions on demand
Candles SHALL be built only for instruments a component has requested, using the same owner-based rules as feed subscriptions. Requesting candles SHALL subscribe the instrument on the feed under the same owner, and releasing the last owner SHALL stop candle building and release the feed subscription for that owner. A request that would exceed `FEED_MAX_SUBSCRIPTIONS` SHALL be refused unchanged.

#### Scenario: Two owners, one releases
- **WHEN** two components request candles for the same instrument and one of them releases it
- **THEN** candles keep being built for the other component, and history is not reloaded

#### Scenario: Over the feed cap
- **WHEN** a candle request would take feed subscriptions above `FEED_MAX_SUBSCRIPTIONS`
- **THEN** the request fails with the feed's cap error and no candle building or history load starts

### Requirement: Reading candles and completion events
Consumers SHALL be able to read the last N completed candles for an instrument and timeframe, oldest first, and SHALL receive an event each time a candle for a subscribed instrument completes or is replaced by refill. Every event SHALL carry the candle.

#### Scenario: Read the last five
- **WHEN** a consumer asks for the last 5 completed 5-minute candles
- **THEN** it receives at most 5 candles in time order, the newest being the last completed interval, and never the forming one

#### Scenario: Fewer candles than asked
- **WHEN** a consumer asks for 50 candles and only 30 are held
- **THEN** it receives the 30 candles, so it can decide that history is too short

### Requirement: History failures are contained
A failed or rejected history load SHALL NOT stop the engine, the feed or live candle building. The engine SHALL log the failure without the token or request and response bodies. It SHALL continue with live candles only, report the instrument's history as unavailable, and retry the load at the next reconnect or new request.

#### Scenario: History endpoint down
- **WHEN** the history request for an instrument times out or returns an error
- **THEN** live candles are still published for it, an error is logged once with the instrument key and reason, and reading its candles returns only the live ones

#### Scenario: Data plan inactive
- **WHEN** Dhan rejects the history request because of the Data API plan or token
- **THEN** no further history requests are sent until restart, and the reason is logged

### Requirement: History request rate
History requests to Dhan SHALL be spaced so that no more than `DHAN_HISTORY_RATE_PER_S` requests start in any one second. A burst of candle requests SHALL be queued, not refused.

#### Scenario: Twenty instruments at once
- **WHEN** candles are requested for 20 instruments at the same moment
- **THEN** all 20 history loads complete, and no second contains more history requests than the configured rate

### Requirement: Simulated history
With `FEED_SOURCE=simulated`, history SHALL come from a seeded simulated source using the simulated-feed price settings, on the tick grid. The warm-up, join and refill rules then run through the same code path as with Dhan.

#### Scenario: Repeatable simulated warm-up
- **WHEN** the engine runs twice with `FEED_SOURCE=simulated`, the same `SIM_SEED` and the same clock
- **THEN** both runs load identical warm-up candles whose prices are all positive multiples of the tick size
