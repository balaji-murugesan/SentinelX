# Spec Delta

## Purpose

Simulates order execution against live prices in paper mode, conservatively and with realistic trading costs, so paper results are not better than live trading would be, and without ever contacting the broker.

## ADDED Requirements

### Requirement: Paper never reaches Dhan
In paper mode every order SHALL be executed by the paper broker. The paper broker SHALL NOT make any network call. Paper fills SHALL be marked as paper in every record.

#### Scenario: Paper market order
- **WHEN** a market order is placed in paper mode
- **THEN** it is filled by the paper broker, no request is sent to any broker, and the fill is recorded as a paper fill

### Requirement: Market orders fill at the latest price with slippage
A paper market order SHALL fill in full at the instrument's latest traded price, moved against the trader by `PAPER_SLIPPAGE_TICKS` ticks (default 1): up for a buy, down for a sell, never below one tick. If there is no price for the instrument, or the latest is older than `PAPER_PRICE_MAX_AGE_S` (default 5 s), the order SHALL be rejected.

#### Scenario: Buy with one tick of slippage
- **WHEN** a paper market buy arrives, the last price is 193.85 and the tick size is 0.05
- **THEN** the order fills in full at 193.90

#### Scenario: Stale price
- **WHEN** the latest price for the instrument is 30 seconds old
- **THEN** the market order is rejected with a reason saying the price is stale

### Requirement: Limit orders fill when price trades through
A paper limit buy SHALL fill in full at its limit price once a later tick trades at or below the limit; a limit sell, once a tick trades at or above it. A limit order that is already marketable on arrival SHALL fill at once, at the better of its limit and the latest price. Until then it SHALL rest as `Open`.

#### Scenario: Resting buy limit
- **WHEN** a buy limit at 190.00 rests and the price falls from 193.85 to 189.95
- **THEN** the order fills at 190.00 on that tick

#### Scenario: Marketable limit
- **WHEN** a buy limit at 195.00 arrives while the price is 193.85
- **THEN** it fills at once at 193.85

### Requirement: Trading costs on every fill
Every paper fill SHALL be charged trading costs from a configurable rate table per segment and product. The costs are brokerage, securities transaction tax, exchange transaction charges, SEBI fees, stamp duty, and GST on brokerage and charges. Each fill SHALL record the cost total and its breakdown as `Decimal` rupees, rounded to the paisa.

#### Scenario: Option sell costs
- **WHEN** a paper sell of 65 units of an option fills at 200.00
- **THEN** the fill records brokerage, STT on the sell premium, exchange and SEBI charges and GST per the rate table, no stamp duty (charged on buys only), and their total

#### Scenario: Costs reduce P&L
- **WHEN** a round trip buys and sells at the same price
- **THEN** the realized P&L is the negative of both fills' costs

### Requirement: Paper orders survive a restart
Resting paper orders SHALL be persisted, and after a restart the paper broker SHALL resume them from persisted state. Its order book SHALL then match the engine's open orders.

#### Scenario: Restart with a resting limit
- **WHEN** the engine is killed with a resting paper limit order and restarted
- **THEN** the order is still open and fills when the price later trades through it
