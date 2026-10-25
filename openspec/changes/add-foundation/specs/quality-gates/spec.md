# Spec Delta

## Purpose

Defines the automated checks and architectural boundaries every change must satisfy, so safety rules and module separation are enforced by CI rather than by memory.

## ADDED Requirements

### Requirement: Required CI checks
CI SHALL run formatting check, lint, strict type check, import-boundary check, tests, and `openspec validate --strict` on every push and pull request, and report failure if any of them fail.

#### Scenario: All checks pass
- **WHEN** a change passes every check
- **THEN** the CI run is green

#### Scenario: One check fails
- **WHEN** any single check fails
- **THEN** the CI run is red and identifies the failing check

#### Scenario: Empty skeleton
- **WHEN** CI runs on the Stage 0 skeleton
- **THEN** it is green

### Requirement: API cannot depend on engine
The `api` package SHALL NOT import the `engine` package, directly or transitively.

#### Scenario: Violating import
- **WHEN** a module under `api` imports from `engine`
- **THEN** the import-boundary check fails

### Requirement: Contracts are self-contained
The `contracts` package SHALL NOT import any other internal package.

#### Scenario: Violating import
- **WHEN** a module under `contracts` imports from `engine`, `api`, `cli`, or `observability`
- **THEN** the import-boundary check fails

### Requirement: Telemetry package is independent
The `observability` package SHALL NOT import `engine`, `api`, or `cli`.

#### Scenario: Violating import
- **WHEN** a module under `observability` imports from `engine`, `api`, or `cli`
- **THEN** the import-boundary check fails

### Requirement: Broker SDK confined to broker module
Only the `engine.broker` package SHALL import the Dhan SDK or make HTTP calls to Dhan.

#### Scenario: SDK imported elsewhere
- **WHEN** a module outside `engine.broker` imports `dhanhq`
- **THEN** the import-boundary check fails

### Requirement: Tests never reach the real broker
The test suite SHALL NOT contact the real Dhan API.

#### Scenario: Network to Dhan attempted
- **WHEN** a test attempts a network call to a Dhan host
- **THEN** the test fails

### Requirement: Secrets kept out of the repository
Real credentials SHALL NOT be committed; only `.env.example` with placeholder values SHALL be tracked.

#### Scenario: Env file ignored
- **WHEN** a developer creates a local `.env`
- **THEN** version control ignores it
