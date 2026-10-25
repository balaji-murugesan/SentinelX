# Spec Delta

## Purpose

Defines how SentinelX services load configuration and decide between paper and live trading, so that live trading can never be enabled by accident, by a typo, or by a missing setting.

## ADDED Requirements

### Requirement: Configuration from environment
Each service SHALL load its settings from environment variables at startup and expose them as typed, validated values.

#### Scenario: Valid configuration
- **WHEN** a service starts with all required variables set to valid values
- **THEN** it starts normally and its settings are available with the correct types

#### Scenario: Required variable missing
- **WHEN** a service starts without a required variable
- **THEN** it exits with a non-zero status and an error naming the missing variable

#### Scenario: Invalid value
- **WHEN** a variable has a value of the wrong type or outside its allowed set
- **THEN** the service exits with a non-zero status and an error naming the variable, without echoing secret values

### Requirement: Paper mode is the default
The system SHALL run in paper mode when `TRADING_MODE` is unset or empty and no live request has been made.

#### Scenario: Mode not specified
- **WHEN** `TRADING_MODE` is unset
- **THEN** the effective mode is paper

#### Scenario: Mode empty
- **WHEN** `TRADING_MODE` is set to an empty string
- **THEN** the effective mode is paper

#### Scenario: Example environment file
- **WHEN** `.env.example` is used unchanged
- **THEN** the effective mode is paper

### Requirement: Live mode requires two explicit flags
The system SHALL enter live mode only when `TRADING_MODE` is exactly `live` AND `LIVE_CONFIRM` is exactly `yes`.

#### Scenario: Both flags set
- **WHEN** `TRADING_MODE=live` and `LIVE_CONFIRM=yes`
- **THEN** the effective mode is live

#### Scenario: Live without confirmation
- **WHEN** `TRADING_MODE=live` and `LIVE_CONFIRM` is unset or empty
- **THEN** the service refuses to start and the error states that live mode needs `LIVE_CONFIRM=yes`

#### Scenario: Confirmation without live
- **WHEN** `LIVE_CONFIRM=yes` and `TRADING_MODE` is unset, empty, or `paper`
- **THEN** the effective mode is paper

### Requirement: Mode values are matched exactly
`TRADING_MODE` SHALL accept only `paper` or `live` (lower case, no surrounding whitespace), and `LIVE_CONFIRM` SHALL accept only `yes` or an empty or unset value. Any other value SHALL stop the service from starting, in every mode, and SHALL NOT fall back to paper.

#### Scenario: Wrong case or whitespace in mode
- **WHEN** `TRADING_MODE` is `Live`, `LIVE`, `Paper`, ` live`, or `live `
- **THEN** the service refuses to start with an error naming `TRADING_MODE` and its allowed values

#### Scenario: Unknown mode
- **WHEN** `TRADING_MODE` is any value other than `paper`, `live`, or empty (for example `prod` or `1`)
- **THEN** the service refuses to start and does not run in paper mode

#### Scenario: Near-miss confirmation
- **WHEN** `LIVE_CONFIRM` is `true`, `Yes`, `YES`, `y`, `1`, `no`, or contains whitespace
- **THEN** the service refuses to start with an error naming `LIVE_CONFIRM`, even when `TRADING_MODE` is `paper` or unset

### Requirement: Mode banner at startup
Each service SHALL log one prominent startup banner stating its name and effective trading mode, and a live-mode banner SHALL be logged at warning level and say that real orders can be sent.

#### Scenario: Paper banner
- **WHEN** a service starts in paper mode
- **THEN** a log record states the service name and that the mode is PAPER

#### Scenario: Live banner
- **WHEN** a service starts in live mode
- **THEN** a warning-level log record states the service name and that the mode is LIVE with real orders

#### Scenario: No secrets in banner
- **WHEN** the banner is logged
- **THEN** it contains no secret or credential values

### Requirement: Secrets are never printed
Secret settings (broker credentials, tokens, passwords, connection strings with credentials) SHALL NOT appear in logs, error messages, or string representations of settings.

#### Scenario: Settings printed
- **WHEN** the settings object is logged or converted to a string
- **THEN** secret fields are shown masked

#### Scenario: Validation error
- **WHEN** configuration validation fails
- **THEN** the error message lists variable names and reasons only, never the supplied values of secret variables

### Requirement: Api has no database settings
The api service's settings SHALL NOT include any database URL or credentials.

#### Scenario: Api settings fields
- **WHEN** the api settings model is inspected
- **THEN** it defines no field for a database URL or database credentials
