# Spec Delta

## Purpose

Defines how SentinelX services load configuration and decide between paper and live trading, so that live trading can never be enabled by accident or by a missing setting.

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
The system SHALL run in paper mode unless live mode is explicitly enabled.

#### Scenario: Mode not specified
- **WHEN** `TRADING_MODE` is unset
- **THEN** the effective mode is paper

#### Scenario: Example environment file
- **WHEN** `.env.example` is used unchanged
- **THEN** the effective mode is paper

### Requirement: Live mode requires two explicit flags
The system SHALL enter live mode only when `TRADING_MODE=live` AND `LIVE_CONFIRM=yes` are both set.

#### Scenario: Both flags set
- **WHEN** `TRADING_MODE=live` and `LIVE_CONFIRM=yes`
- **THEN** the effective mode is live

#### Scenario: Live without confirmation
- **WHEN** `TRADING_MODE=live` and `LIVE_CONFIRM` is unset or not `yes`
- **THEN** the service refuses to start and states that live mode needs confirmation

#### Scenario: Confirmation without live
- **WHEN** `LIVE_CONFIRM=yes` and `TRADING_MODE` is unset or `paper`
- **THEN** the effective mode is paper

### Requirement: Mode visible at startup
Each service SHALL log its effective trading mode once at startup.

#### Scenario: Startup log
- **WHEN** a service finishes loading configuration
- **THEN** a log record states the effective trading mode (paper or live)

### Requirement: Secrets are never printed
Secret settings (broker credentials, tokens, passwords) SHALL NOT appear in logs, error messages, or string representations of settings.

#### Scenario: Settings printed
- **WHEN** the settings object is logged or converted to a string
- **THEN** secret fields are shown masked
