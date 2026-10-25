# Spec Delta

## Purpose

Covers how the engine holds Dhan credentials and decides, once at startup, whether its access token and Data API plan let it use Dhan market data, with warnings before the token expires.

## ADDED Requirements

### Requirement: Dhan credentials from environment
When the Dhan feed is selected, the engine SHALL read the Dhan client ID and access token from environment variables. Missing credentials SHALL stop the engine from starting, and the error SHALL name the missing variable.

#### Scenario: Credentials present
- **WHEN** the engine starts with `FEED_SOURCE=dhan`, `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` set
- **THEN** the credentials are loaded and configuration validation passes

#### Scenario: Token missing
- **WHEN** the engine starts with `FEED_SOURCE=dhan` and `DHAN_ACCESS_TOKEN` unset or empty
- **THEN** the engine exits with a non-zero status and an error naming `DHAN_ACCESS_TOKEN`

#### Scenario: Simulated feed needs no credentials
- **WHEN** the engine starts with `FEED_SOURCE=simulated` and no Dhan credentials
- **THEN** configuration validation passes

### Requirement: Credentials never exposed
The access token SHALL NOT appear in logs, error messages, trace attributes or settings output. The client ID SHALL appear only masked to its last four characters.

#### Scenario: Settings logged
- **WHEN** the engine settings are converted to a string or logged
- **THEN** the access token is masked and the client ID shows only its last four characters

#### Scenario: Dhan rejects the token
- **WHEN** Dhan returns an authentication error during the startup check
- **THEN** the logged error names the cause and contains neither the token nor the unmasked client ID

### Requirement: Startup session check
With the Dhan feed selected, the engine SHALL check the token and Data API plan once at startup, before connecting the feed. It SHALL connect only if the token is valid and the plan is active.

#### Scenario: Valid token and active plan
- **WHEN** Dhan reports the token valid and the Data API plan active
- **THEN** the engine logs the token expiry time and proceeds to connect the feed

#### Scenario: Token expired or invalid
- **WHEN** Dhan reports the token expired or invalid
- **THEN** the engine does not connect the feed, logs an error stating that a new token must be supplied, and marks market data unavailable

#### Scenario: Data API plan inactive
- **WHEN** Dhan reports the Data API plan not active
- **THEN** the engine does not connect the feed, logs an error stating that the Data API plan is inactive, and marks market data unavailable

### Requirement: Failed session check keeps engine running
A failed session check SHALL NOT stop the engine process and SHALL NOT be retried automatically against Dhan. Recovery SHALL require restarting the engine with new credentials.

#### Scenario: Engine stays up
- **WHEN** the session check fails for any reason
- **THEN** the engine process keeps running and its liveness heartbeat continues

#### Scenario: No retry loop
- **WHEN** the session check has failed
- **THEN** the engine makes no further session-check calls to Dhan until it is restarted

#### Scenario: Dhan unreachable
- **WHEN** the session check cannot reach Dhan (timeout or connection error)
- **THEN** the engine marks market data unavailable, logs the error, and keeps running

### Requirement: Token expiry warnings
The engine SHALL warn ahead of token expiry using the expiry time from the startup check, with a configurable lead time. It SHALL warn at startup if the token expires before today's market close (15:30 IST).

#### Scenario: Expiry inside the warning window
- **WHEN** the time remaining until token expiry falls below the configured lead time
- **THEN** the engine logs a warning stating the expiry time in IST, at most once per configured interval

#### Scenario: Expires during today's session
- **WHEN** at startup the token expiry falls before 15:30 IST today
- **THEN** the engine logs a warning that the token will expire during market hours

#### Scenario: Warnings need no Dhan calls
- **WHEN** expiry warnings are evaluated
- **THEN** they are computed from the stored expiry time without calling Dhan

### Requirement: Engine never mints tokens
The engine SHALL NOT generate, renew or store Dhan access tokens. Tokens SHALL only be supplied through configuration by the owner.

#### Scenario: Token near expiry
- **WHEN** the token is about to expire
- **THEN** the engine only warns and makes no call to generate or renew a token
