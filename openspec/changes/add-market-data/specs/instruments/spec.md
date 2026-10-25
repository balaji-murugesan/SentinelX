# Spec Delta

## Purpose

Gives the engine an up-to-date master list of tradable NSE instruments and looks up each one's identifiers and trading constraints: security ID, lot size, tick size and freeze quantity.

## ADDED Requirements

### Requirement: Instrument master download
The engine SHALL download Dhan's instrument list at startup and once each trading day before the market opens. It SHALL keep only NSE equity, NSE F&O and index instruments.

#### Scenario: Startup download
- **WHEN** the engine starts and the instrument list download succeeds
- **THEN** the master contains the NSE equity, NSE F&O and index instruments from the file and no instruments from other segments

#### Scenario: Daily refresh
- **WHEN** the engine is running and the configured daily refresh time (default 08:45 IST) passes
- **THEN** the master is downloaded again and replaces the previous one

### Requirement: Instrument fields
Each instrument SHALL carry an instrument key (exchange segment + security ID), trading symbol, display name, underlying symbol, instrument type, lot size, tick size and freeze quantity. Derivatives SHALL also carry expiry date, and options SHALL also carry strike and option type (CE/PE).

#### Scenario: Option contract
- **WHEN** an NSE index option is looked up
- **THEN** it has expiry date, strike, option type, lot size and tick size

#### Scenario: Decimal tick size and strike
- **WHEN** an instrument is loaded
- **THEN** tick size and strike are exact decimal values, not binary floating point

### Requirement: Freeze quantity source
Freeze quantity SHALL come from an owner-maintained configuration file mapping underlying symbol to freeze quantity. An instrument with no entry SHALL report freeze quantity as unknown rather than zero or unlimited.

#### Scenario: Configured underlying
- **WHEN** the configuration lists a freeze quantity for an option contract's underlying
- **THEN** the contract reports that freeze quantity

#### Scenario: Unconfigured underlying
- **WHEN** the configuration has no entry for an instrument's underlying
- **THEN** the instrument reports freeze quantity as unknown

#### Scenario: Malformed configuration
- **WHEN** the freeze-quantity file cannot be parsed or contains a non-positive quantity
- **THEN** the engine refuses to start with an error naming the file and the offending entry

### Requirement: Cache and fallback
The engine SHALL cache each successful download on disk. If a download fails, it SHALL load the most recent cached file instead and log a warning. If neither exists, the master SHALL be empty and marked unavailable, and the engine SHALL keep running.

#### Scenario: Download fails, cache exists
- **WHEN** the download fails and a cached file exists
- **THEN** the master is loaded from the cache and a warning names the cache date

#### Scenario: Download fails, no cache
- **WHEN** the download fails and no cached file exists
- **THEN** the master is empty, reported unavailable, and the engine keeps running

#### Scenario: Corrupt download
- **WHEN** a downloaded file is missing required columns or has no instruments for the kept segments
- **THEN** it is rejected, the previous master (or cache) stays in use, and an error is logged

### Requirement: Freshness reporting
The master SHALL report the IST trading date of the data it holds and whether that date is today, so later stages can refuse new entries on stale data.

#### Scenario: Fresh master
- **WHEN** the master was downloaded today (IST)
- **THEN** it reports itself fresh with today's date

#### Scenario: Stale master
- **WHEN** the master was loaded from a cache dated before today (IST)
- **THEN** it reports itself not fresh, with the cache date

### Requirement: Instrument lookups
The master SHALL support lookup by instrument key, lookup of a single option or future by underlying, expiry, strike and option type, and case-insensitive text search over symbol and display name with a result limit.

#### Scenario: Lookup by key
- **WHEN** a key present in the master is looked up
- **THEN** that instrument is returned

#### Scenario: Unknown key
- **WHEN** a key not present in the master is looked up
- **THEN** the lookup reports not found without raising an unhandled error

#### Scenario: Contract lookup
- **WHEN** an underlying, expiry, strike and option type matching one listed option are given
- **THEN** exactly that option is returned

#### Scenario: Text search
- **WHEN** a search for `nifty` with limit 20 is made
- **THEN** at most 20 instruments whose symbol or display name contains `NIFTY` are returned

### Requirement: Loading does not stall the engine
Downloading and parsing the instrument list SHALL NOT block the engine's event loop. Ticks and heartbeats SHALL keep flowing while it loads.

#### Scenario: Refresh during market hours
- **WHEN** a refresh runs while the feed is delivering ticks
- **THEN** the heartbeat keeps updating and tick delivery continues during the refresh
