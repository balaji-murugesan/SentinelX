# service-stack Specification

## Purpose

Defines the container topology and health behavior of the SentinelX stack, so the whole platform can be started, checked, and recovered with a single command.

## Requirements

### Requirement: Single-command stack
The repository SHALL provide a Compose definition that starts the engine, api, redis, postgres, caddy, and otel-collector services together, plus a one-shot migrate service that runs to completion.

#### Scenario: Stack start
- **WHEN** an operator runs `docker compose up -d` with a valid `.env`
- **THEN** the six long-running services start and the migrate service exits successfully

### Requirement: Health reporting
Every long-running service SHALL declare a health check, and the stack SHALL reach an all-healthy state without manual steps.

#### Scenario: All healthy
- **WHEN** the stack has been up long enough for start-up checks to complete
- **THEN** every long-running service reports healthy

#### Scenario: Engine health
- **WHEN** the engine process is running its event loop normally
- **THEN** its health check passes

#### Scenario: Dependency not ready
- **WHEN** postgres or redis is not yet healthy
- **THEN** engine and api wait for it rather than failing permanently

### Requirement: Automatic restart
All long-running services SHALL restart automatically after a crash or host reboot unless explicitly stopped. The one-shot migrate service SHALL NOT restart after it exits.

#### Scenario: Process crash
- **WHEN** a service process exits unexpectedly
- **THEN** the container is restarted automatically

### Requirement: Paper mode in the shipped stack
The shipped Compose definition and example environment SHALL NOT enable live trading.

#### Scenario: Default stack
- **WHEN** the stack is started with the example environment
- **THEN** the engine logs paper mode

### Requirement: Engine independent of api
The engine SHALL start and stay healthy when the api service is stopped or absent.

#### Scenario: Api stopped
- **WHEN** the api container is stopped
- **THEN** the engine remains healthy and keeps running

### Requirement: Private data services
Redis and postgres SHALL NOT be published on host network interfaces by default.

#### Scenario: Port exposure
- **WHEN** the stack is running
- **THEN** redis and postgres are reachable only from other services on the Compose network

### Requirement: Migrations run as a separate step
Database migrations SHALL be applied by the one-shot migrate service, and the engine SHALL start only after migrate has completed successfully.

#### Scenario: Successful migration
- **WHEN** the stack starts and migrate applies all migrations
- **THEN** migrate exits with status 0 and the engine then starts

#### Scenario: Failed migration
- **WHEN** migrate exits with a non-zero status
- **THEN** the engine does not start and is not restarted in a crash loop, and the failure is visible in the migrate logs

### Requirement: Least-privilege database users
The engine SHALL connect to postgres as a user with data read/write rights only, and the migrate service SHALL use a separate user that holds schema (DDL) rights.

#### Scenario: Engine attempts schema change
- **WHEN** the engine's database user attempts to create or alter a table
- **THEN** postgres rejects the statement

#### Scenario: Migrate applies schema change
- **WHEN** the migrate service runs a migration that creates a table
- **THEN** the statement succeeds and the engine user can read and write that table

#### Scenario: Table with generated identifiers
- **WHEN** the migrate service creates a table with an identity column
- **THEN** the engine user can insert rows into it

### Requirement: Api has no database access
The api service SHALL NOT be given any database URL or credentials, and SHALL communicate with the rest of the system only through Redis.

#### Scenario: Api configuration
- **WHEN** the api settings are loaded
- **THEN** they contain no database URL or database credentials

### Requirement: Database credentials come only from the environment
Database passwords SHALL be supplied only through environment variables read from the operator's local environment file. No password SHALL appear in the Compose file, any Dockerfile, the database initialisation script, logs, or the example environment file.

#### Scenario: Files in the repository
- **WHEN** the Compose file, Dockerfiles, initialisation script and example environment file are inspected
- **THEN** none contains a password value; the example environment file holds empty or visibly placeholder values only

#### Scenario: Password missing
- **WHEN** an operator runs `docker compose up` without one of the database passwords set
- **THEN** Compose stops with a message naming the missing variable

#### Scenario: Weak or duplicate passwords
- **WHEN** the database is initialised with a missing or short (under 16 characters) role password, or the same password for both roles
- **THEN** initialisation aborts with an error naming the variable and no roles are created

#### Scenario: Logs
- **WHEN** the stack has started and run
- **THEN** no container log contains a database password
