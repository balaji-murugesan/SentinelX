# Spec Delta

## MODIFIED Requirements

### Requirement: Order content
An order SHALL carry the account id, instrument key, side (`BUY` or `SELL`), quantity in units, product type (`INTRADAY`, `CNC` or `MARGIN`), order type (`MARKET`, `LIMIT` or `STOP_MARKET`), a `Decimal` limit price for `LIMIT` orders only, a `Decimal` trigger price for `STOP_MARKET` orders only, the idempotency key and the source (e.g. `cli`). An order MAY also carry an initial stop, the `Decimal` price at which an entry is wrong, which the risk rules require on every entry (see risk-rules). Orders SHALL be valid for the trading day only.

#### Scenario: Limit price on a market order
- **WHEN** a `MARKET` order carries a limit price, or a `LIMIT` order has none
- **THEN** the order is rejected with a reason naming the field

#### Scenario: Stop-market without a trigger price
- **WHEN** a `STOP_MARKET` order has no trigger price
- **THEN** the order is rejected with a reason naming the field

#### Scenario: Limit price on a stop-market order
- **WHEN** a `STOP_MARKET` order carries a limit price
- **THEN** the order is rejected with a reason naming the field

#### Scenario: Trigger price on any other order type
- **WHEN** a `MARKET` or `LIMIT` order carries a trigger price
- **THEN** the order is rejected with a reason naming the field

### Requirement: Validation against instrument facts
Before the gate, the engine SHALL reject an order whose instrument is not in a loaded instrument master; whose quantity is not a positive whole number of lots; whose quantity is at or above the instrument's freeze quantity; or whose limit price or trigger price is not a positive multiple of the tick size. For NSE F&O, an unknown freeze quantity SHALL reject the order (fail closed).

#### Scenario: Not a whole number of lots
- **WHEN** an order for a contract with lot size 65 asks for 100 units
- **THEN** it is rejected with a reason naming the lot size

#### Scenario: At the freeze quantity
- **WHEN** an order's quantity equals the instrument's freeze quantity
- **THEN** it is rejected, because the exchange freezes orders at or above that quantity

#### Scenario: Unknown freeze quantity on F&O
- **WHEN** an NSE_FNO instrument has no freeze quantity in the master
- **THEN** every order for it is rejected with a reason saying the freeze quantity is unknown

#### Scenario: Instrument master unavailable
- **WHEN** no instrument master has loaded
- **THEN** every order is rejected with a reason saying instrument data is unavailable

#### Scenario: Trigger price off the tick grid
- **WHEN** a `STOP_MARKET` order's trigger price is not a multiple of the instrument's tick size
- **THEN** it is rejected with a reason naming the trigger price and the tick size

### Requirement: Delivery sell only against holdings
A `CNC` `SELL` SHALL be accepted only up to the account's held `CNC` quantity of that instrument minus quantity already being sold by open `CNC` sell orders, not counting the catastrophic stop that the risk engine rests for the holding (the order whose source is `risk-stop`), because that stop protects the same shares an exit would sell and an exit goes out while the stop rests. An open order from any other source, a stop-market order included, SHALL count like any other resting sell. Cash-equity short selling SHALL be possible only with `INTRADAY`.

#### Scenario: Selling more than held
- **WHEN** the account holds 10 shares under `CNC` and a `CNC` sell for 15 arrives
- **THEN** the order is rejected with a reason naming the held quantity

#### Scenario: Exit while the catastrophic stop rests
- **WHEN** the account holds 100 shares under `CNC`, the catastrophic stop for them is resting, and a `CNC` sell for 100 arrives
- **THEN** the sell is not refused for holdings

#### Scenario: Another resting sell still counts
- **WHEN** the account holds 100 shares under `CNC`, a stop-market sell for 100 from another source is open, and a `CNC` sell for 100 arrives
- **THEN** the order is rejected with a reason naming the held quantity and the quantity already being sold

### Requirement: Mandatory pre-trade gate
Every order SHALL pass the `RiskGate` after validation and before reaching the broker, whatever its source. The order service SHALL NOT be constructible without a gate, and there SHALL be no flag to bypass it. The gate is the risk engine (see risk-gate): while the engine has no live broker adapter it SHALL reject every order when the trading mode is live, with reason code `RISK_LIVE_NOT_ENABLED`, and it SHALL judge paper-mode orders by the risk rules. While persistence is unavailable or reconciliation is pending, an entry SHALL be refused, but that state alone SHALL NOT refuse an order that only reduces a position (an exit): the exit SHALL be validated and judged by the gate like any other exit, and sent if both allow it (see trading-state).

#### Scenario: Live mode before the risk engine
- **WHEN** the engine runs with `TRADING_MODE=live` and `LIVE_CONFIRM=yes` and an order arrives
- **THEN** the gate rejects it with `RISK_LIVE_NOT_ENABLED`, because the engine has no live broker adapter yet, and no broker call is made

#### Scenario: Paper order judged by the risk rules
- **WHEN** a paper-mode order arrives
- **THEN** the risk rules decide whether it is accepted or rejected, not its mode

#### Scenario: Gate rejection is final
- **WHEN** the gate rejects an order
- **THEN** the order ends in `Rejected` with the gate's reason, and is never sent to the broker

#### Scenario: Exit while the engine is degraded
- **WHEN** Postgres is unreachable or a recovery halt is in force and an order that only reduces an open position arrives
- **THEN** the order is not refused for that reason: it is validated and judged by the gate like any other exit and, if both allow it, sent to the broker, while an entry in the same state is rejected
