# market-feed Specification

## Purpose

Delivers live price ticks for subscribed instruments to every engine component that needs them, from Dhan's feed or a simulated source, and keeps delivering them through disconnects.

## Requirements

### Requirement: Feed source selection
The engine SHALL take its tick source from the required `FEED_SOURCE` setting, which accepts exactly `dhan` or `simulated`. The startup banner SHALL state the feed source.

#### Scenario: Dhan source
- **WHEN** the engine starts with `FEED_SOURCE=dhan`
- **THEN** ticks come from Dhan's live feed and the banner states the feed source is Dhan

#### Scenario: Simulated source
- **WHEN** the engine starts with `FEED_SOURCE=simulated` in paper mode
- **THEN** ticks come from the simulated feed and the banner states the feed source is SIMULATED

#### Scenario: Missing or unknown source
- **WHEN** `FEED_SOURCE` is unset, empty, or any other value (for example `Dhan` or `sim`)
- **THEN** the engine refuses to start with an error naming `FEED_SOURCE` and its allowed values

### Requirement: Simulated feed refused in live mode
The engine SHALL refuse to start when live mode is combined with the simulated feed, so real orders can never be driven by simulated prices.

#### Scenario: Live with simulated feed
- **WHEN** `TRADING_MODE=live`, `LIVE_CONFIRM=yes` and `FEED_SOURCE=simulated`
- **THEN** the engine exits with a non-zero status and an error stating that the simulated feed is not allowed in live mode

### Requirement: Configured subscriptions
At startup the engine SHALL subscribe to the instruments listed in `FEED_INSTRUMENTS` (comma-separated `SEGMENT:SECURITY_ID` keys). It SHALL also allow components to add and remove subscriptions while running.

#### Scenario: Startup subscriptions
- **WHEN** the engine starts with `FEED_INSTRUMENTS=IDX_I:13,NSE_FNO:52175`
- **THEN** ticks for both instruments are delivered to consumers

#### Scenario: Malformed entry
- **WHEN** an entry in `FEED_INSTRUMENTS` is not a known segment name followed by a numeric security ID
- **THEN** the engine refuses to start with an error naming `FEED_INSTRUMENTS` and the bad entry

#### Scenario: Key not in instrument master
- **WHEN** a configured key is not found in an available instrument master
- **THEN** a warning names the key and the subscription is still attempted

#### Scenario: Subscribe while running
- **WHEN** a component subscribes to a new instrument while the feed is connected
- **THEN** ticks for that instrument start arriving without reconnecting the feed

### Requirement: Subscriptions only on demand
The feed SHALL stream only instruments in the prefill list or explicitly requested by a component. It SHALL NOT subscribe from the instrument master in bulk. Total subscriptions SHALL be capped by `FEED_MAX_SUBSCRIPTIONS` (default 200, at most Dhan's 5,000 per connection).

#### Scenario: Master loaded
- **WHEN** the instrument master loads thousands of option contracts
- **THEN** no subscription is made for any of them unless it is in the prefill list or requested

#### Scenario: Cap reached
- **WHEN** a subscribe request would take the total above `FEED_MAX_SUBSCRIPTIONS`
- **THEN** the request is refused with an error naming the cap, existing subscriptions are unchanged, and nothing is sent to Dhan

#### Scenario: Prefill over the cap
- **WHEN** `FEED_INSTRUMENTS` lists more keys than `FEED_MAX_SUBSCRIPTIONS`
- **THEN** the engine refuses to start with an error naming both settings

#### Scenario: Duplicate request
- **WHEN** a component subscribes to an instrument that is already subscribed
- **THEN** no new subscribe message is sent to Dhan

### Requirement: Tick content
Each tick SHALL carry the instrument key, last traded price, last traded quantity where provided, cumulative volume where provided, exchange trade time and engine receive time. Prices SHALL be exact decimals with two decimal places, and times SHALL be timezone-aware UTC.

#### Scenario: Decimal prices
- **WHEN** Dhan sends an LTP that is not exactly representable in binary (for example 24500.05)
- **THEN** the tick's price equals `Decimal("24500.05")` exactly

#### Scenario: Timezone-aware times
- **WHEN** a tick is delivered
- **THEN** both its exchange time and receive time are timezone-aware UTC datetimes

#### Scenario: Unknown packet type
- **WHEN** the feed receives a packet type it does not handle
- **THEN** the packet is skipped, counted, and the connection continues

### Requirement: Fan-out to consumers
Every tick SHALL be delivered to every current consumer in arrival order per instrument. A slow or failing consumer SHALL NOT delay the feed or other consumers.

#### Scenario: Multiple consumers
- **WHEN** three consumers are subscribed and a tick arrives
- **THEN** each of the three receives the tick

#### Scenario: Slow consumer
- **WHEN** one consumer stops reading and its buffer fills
- **THEN** its oldest unread ticks are dropped, a rate-limited warning reports the drop count, and other consumers keep receiving every tick

#### Scenario: Consumer error
- **WHEN** a consumer raises an error while handling a tick
- **THEN** the error is logged and the feed and other consumers continue

### Requirement: Feed status events
The feed SHALL publish status changes (connecting, connected, reconnecting, down) to consumers, each with a reason and timestamp. Consumers SHALL be able to read the current status and the last tick time per instrument.

#### Scenario: Disconnect reported
- **WHEN** the connection to Dhan drops
- **THEN** consumers receive a `reconnecting` status with the reason before any further ticks

#### Scenario: Last tick time
- **WHEN** a consumer asks for an instrument's last tick time
- **THEN** it receives the receive time of the latest tick for that instrument, or none if no tick has arrived

### Requirement: Automatic reconnect
After an unexpected disconnect, the Dhan feed SHALL reconnect with increasing back-off up to a configured maximum delay, and SHALL resubscribe to every instrument subscribed before the drop.

#### Scenario: Forced disconnect
- **WHEN** the feed connection is closed by the server or network while subscribed to two instruments
- **THEN** the feed reconnects, resubscribes to both instruments, publishes `connected`, and ticks resume without restarting the engine

#### Scenario: Repeated failures
- **WHEN** reconnect attempts keep failing
- **THEN** the delay between attempts grows until the configured maximum and the attempts continue

### Requirement: No reconnect on credential errors
When Dhan disconnects or rejects the feed for an expired or invalid token, an invalid client ID, or an inactive Data API plan, the feed SHALL stop reconnecting and report `down` with the reason.

#### Scenario: Token expired mid-session
- **WHEN** Dhan disconnects the feed with the "access token expired" code
- **THEN** the feed publishes `down` with that reason, logs an error asking for a new token, and makes no further connection attempts until the engine restarts

### Requirement: Silent feed detection
During NSE market hours (09:15–15:30 IST), if no data arrives on a connected feed for a configured time, the feed SHALL treat the connection as dead and reconnect. Outside market hours, silence SHALL NOT trigger a reconnect.

#### Scenario: Silent during market hours
- **WHEN** a connected feed receives nothing for longer than the configured silence limit at 11:00 IST
- **THEN** the feed closes the connection, publishes `reconnecting` with reason "silent feed", and reconnects

#### Scenario: Silent after hours
- **WHEN** a connected feed receives nothing for longer than the silence limit at 20:00 IST
- **THEN** no reconnect is triggered

### Requirement: Feed failures are contained
Feed connection or decoding failures SHALL NOT stop the engine process or its other tasks.

#### Scenario: Feed down
- **WHEN** the feed is `down`
- **THEN** the engine process keeps running and its liveness heartbeat continues

### Requirement: Feed address never logged
The feed connection address SHALL NOT appear in logs, errors or traces, because it carries the access token.

#### Scenario: Connection error logged
- **WHEN** a connection attempt fails and is logged, including at debug level
- **THEN** the log record contains no access token and no full feed URL

### Requirement: Simulated scripted paths
The simulated feed SHALL replay a scripted price path (a sequence of instrument, price and optional volume steps with time offsets) through the same consumer interface as the Dhan feed.

#### Scenario: Scripted path delivered in order
- **WHEN** a script of five price steps for one instrument is run
- **THEN** consumers receive exactly five ticks with those prices, in order

#### Scenario: Same consumer code path
- **WHEN** a consumer runs against the simulated feed and then against the Dhan feed
- **THEN** it receives ticks of the same type through the same subscription interface

### Requirement: Simulated random walk
The simulated feed SHALL offer a random-walk mode for subscribed instruments. It SHALL use a configurable start price, step size and interval, produce prices on the instrument's tick grid that never go below one tick, and be reproducible from a seed.

#### Scenario: Reproducible walk
- **WHEN** two random walks run with the same seed and settings
- **THEN** they produce identical price sequences

#### Scenario: Prices on the tick grid
- **WHEN** a random walk runs for an instrument with tick size 0.05
- **THEN** every price is a positive multiple of 0.05
