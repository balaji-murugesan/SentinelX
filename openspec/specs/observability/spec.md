# observability Specification

## Purpose

Defines the logging and tracing every SentinelX service must emit, so any order or request can later be followed end to end and latency measured from the first stage.

## Requirements

### Requirement: Structured JSON logs
Each service SHALL write logs to standard output as one JSON object per line, including timestamp (UTC), level, service name, and message.

#### Scenario: Log line format
- **WHEN** a service logs an event
- **THEN** the output line parses as JSON with timestamp, level, service and message fields

### Requirement: Correlation fields in logs
Log records emitted inside an active trace SHALL include the `trace_id`, and SHALL include `account_id` and `order_id` whenever those are bound to the context.

#### Scenario: Log inside a trace
- **WHEN** a log record is emitted while a trace span is active
- **THEN** the record contains that span's `trace_id`

#### Scenario: Bound account
- **WHEN** `account_id` has been bound to the logging context
- **THEN** subsequent records in that context include it

### Requirement: Sensitive data masking
Logs SHALL mask secrets and account identifiers before output.

#### Scenario: Token in log fields
- **WHEN** a log event contains a field known to hold a secret (token, password, secret, key)
- **THEN** the emitted value is masked and the original does not appear

#### Scenario: Account ID in log fields
- **WHEN** a log event contains an `account_id` or broker client ID
- **THEN** only a masked form is emitted (for example the last four characters)

### Requirement: Distributed tracing across services
The engine and api SHALL export traces to the OpenTelemetry collector and propagate trace context between them.

#### Scenario: Cross-service trace
- **WHEN** a request enters the api that results in work in the engine
- **THEN** the spans from both services appear under one trace ID

#### Scenario: Collector unavailable
- **WHEN** the collector cannot be reached
- **THEN** the services keep running and log a warning, without blocking request or engine processing

### Requirement: Telemetry never carries secrets
Trace attributes SHALL NOT contain secrets or unmasked account identifiers.

#### Scenario: Span attributes
- **WHEN** a span is exported
- **THEN** none of its attributes contain secret values
