# Spec Delta

## Purpose

Defines what an order is, which orders the engine accepts, the mandatory pre-trade gate, and how every order moves through its lifecycle without ever being blindly retried.

## ADDED Requirements

### Requirement: Order content
An order SHALL carry the account id, instrument key, side (`BUY` or `SELL`), quantity in units, product type (`INTRADAY`, `CNC` or `MARGIN`), order type (`MARKET` or `LIMIT`), a `Decimal` limit price for `LIMIT` orders only, the idempotency key and the source (e.g. `cli`). Orders SHALL be valid for the trading day only.

#### Scenario: Limit price on a market order
- **WHEN** a `MARKET` order carries a limit price, or a `LIMIT` order has none
- **THEN** the order is rejected with a reason naming the field

### Requirement: Product type allowed for the segment
`CNC` SHALL be accepted only for NSE equity and `MARGIN` only for NSE F&O; `INTRADAY` SHALL be accepted for both. Index instruments SHALL NOT be ordered.

#### Scenario: Delivery on an option
- **WHEN** an order for an NSE_FNO option has product `CNC`
- **THEN** it is rejected with a reason naming the product and segment

### Requirement: Validation against instrument facts
Before the gate, the engine SHALL reject an order whose instrument is not in a loaded instrument master; whose quantity is not a positive whole number of lots; whose quantity is at or above the instrument's freeze quantity; or whose limit price is not a positive multiple of the tick size. For NSE F&O, an unknown freeze quantity SHALL reject the order (fail closed).

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

### Requirement: Delivery sell only against holdings
A `CNC` `SELL` SHALL be accepted only up to the account's held `CNC` quantity of that instrument minus quantity already being sold by open `CNC` sell orders. Cash-equity short selling SHALL be possible only with `INTRADAY`.

#### Scenario: Selling more than held
- **WHEN** the account holds 10 shares under `CNC` and a `CNC` sell for 15 arrives
- **THEN** the order is rejected with a reason naming the held quantity

### Requirement: Mandatory pre-trade gate
Every order SHALL pass the `RiskGate` after validation and before reaching the broker, whatever its source. The order service SHALL NOT be constructible without a gate, and there SHALL be no flag to bypass it. Until the risk engine (S3) replaces it, the gate SHALL reject every order unless the trading mode is paper.

#### Scenario: Live mode before the risk engine
- **WHEN** the engine runs with `TRADING_MODE=live` and `LIVE_CONFIRM=yes` and an order arrives
- **THEN** the gate rejects it with a reason saying risk checks are not yet available, and no broker call is made

#### Scenario: Gate rejection is final
- **WHEN** the gate rejects an order
- **THEN** the order ends in `Rejected` with the gate's reason, and is never sent to the broker

### Requirement: Order state machine
Orders SHALL move only along the transitions of PRD §5. These are Requested → Submitted or Rejected; Submitted → Open, Rejected or Unknown; Unknown → Open or Rejected; Open → PartiallyFilled, Filled or Cancelled; PartiallyFilled → Filled or Cancelled. Filled, Rejected and Cancelled are final. Any other transition SHALL be refused, logged with the order id, and leave the order unchanged.

#### Scenario: Update after a final state
- **WHEN** a fill arrives for an order that is already `Cancelled`
- **THEN** the order stays `Cancelled`, the event is logged as an invalid transition, and the fill is recorded for reconciliation but changes no position

### Requirement: Unknown orders are reconciled, never retried
When the broker does not acknowledge a submission within the order timeout, the order SHALL become `Unknown`. The engine SHALL NOT resubmit it. It SHALL query the broker's order book and move the order to the state found there, or to `Rejected` if the broker has no such order.

#### Scenario: Submission times out
- **WHEN** a submission gets no acknowledgement within the timeout
- **THEN** the order becomes `Unknown`, exactly one submission was made, and reconciliation later sets it to the broker's state

### Requirement: Cancelling an order
A cancel command SHALL cancel an `Open` or `PartiallyFilled` order through the broker. A cancel for a final or unknown order id SHALL be rejected with a reason. A filled quantity is never undone by a cancel.

#### Scenario: Cancel a resting limit order
- **WHEN** a cancel arrives for an open, unfilled limit order
- **THEN** the order becomes `Cancelled` and no fill happens afterwards

### Requirement: End-of-day expiry
Every order still `Open` or `PartiallyFilled` at the 15:30 IST close SHALL be cancelled as expired, with the reason recorded.

#### Scenario: Limit order never reached
- **WHEN** a limit order is still open at 15:30 IST
- **THEN** it becomes `Cancelled` with reason "expired at close"

### Requirement: Traceable order path
Every order SHALL be logged and traced with its account id, order id and idempotency key from command to final state. Broker round-trip latency SHALL be recorded. Logs SHALL contain no credentials and no broker request or response bodies.

#### Scenario: One trace per order
- **WHEN** an order goes from command to fill
- **THEN** a single trace contains spans for validation, gate, submission and fill, each tagged with the order id
