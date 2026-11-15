# Spec Delta

## Purpose

Defines how the risk engine behaves when market data is old, missing or wrong. Risk decisions made on stale prices are worse than none, so entries stop first, then protection steps in, and a bad tick can never raise the day's peak.

## ADDED Requirements

### Requirement: Stale quotes block entries
An entry in an instrument whose latest tick is older than the configured stale threshold, or that has no tick, SHALL be rejected with `RISK_STALE_QUOTE`. Exits are not blocked by this rule.

#### Scenario: Old quote
- **WHEN** the threshold is 5 s and the instrument's last tick is 8 s old
- **THEN** an entry in it is rejected with `RISK_STALE_QUOTE`

### Requirement: A silent feed halts new entries
While the market is open and at least one instrument is subscribed, if no tick at all has arrived for the configured feed-down time, the account SHALL be HALT_NEW_ENTRIES with reason `RISK_FEED_DOWN` and an alert SHALL be logged. The halt SHALL clear on its own when fresh ticks resume. Exits and existing protection continue. Silence outside market hours SHALL NOT raise it.

#### Scenario: Feed goes quiet
- **WHEN** the feed-down time is 10 s and no tick arrives for 12 s at 11:00 IST
- **THEN** entries are rejected with `RISK_FEED_DOWN` and an alert is logged

#### Scenario: Feed returns
- **WHEN** ticks resume after a feed-down halt
- **THEN** the halt clears and entries are judged by the other rules again

#### Scenario: After the close
- **WHEN** no tick arrives for ten minutes at 16:00 IST
- **THEN** no feed-down halt is raised

### Requirement: Unknown MTM flattens the book
If an open position has had no fresh price for the configured MTM-blind time, the engine SHALL record `RISK_MTM_BLIND`, flatten all open positions with the kill switch's flatten, and keep the account HALT_NEW_ENTRIES for the rest of the trading day. It SHALL NOT request the broker lock. The engine SHALL NOT estimate MTM from a price it does not have.

#### Scenario: Blind too long
- **WHEN** the MTM-blind time is 60 s and an open position has had no tick for 61 s
- **THEN** the engine flattens the book and rejects entries until the next trading day

### Requirement: Unconfirmed outlier ticks never raise the peak
A tick that moves more than the configured outlier percentage from the last accepted price SHALL be recorded as `RISK_OUTLIER_TICK` and SHALL NOT raise the day's peak unless the next tick of that instrument confirms it by staying within the same percentage of it. An unconfirmed outlier SHALL still be used for the loss side, which is not debounced.

#### Scenario: Spike that reverts
- **WHEN** a position's price jumps 30% on one tick and the next tick returns to the old level
- **THEN** the peak is unchanged

#### Scenario: Genuine gap
- **WHEN** a price jumps 30% and the next tick stays within the outlier percentage of the new level
- **THEN** the new level is accepted and the peak may rise
