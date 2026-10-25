# Proposal

## Why

SentinelX has a PRD and a decision log but no code. Every later stage (market data, orders, risk) needs a runnable skeleton, enforced boundaries, and observability in place first. Retrofitting import boundaries, tracing, or paper-by-default safety after real order logic exists is costly and risky. Stage 0 builds that base while there is nothing to break.

## What Changes

- Create the repo skeleton from CLAUDE.md: `src/{contracts,engine,api,cli}`, `tests/`, `docker/`, `infra/`, `scripts/`, with minimal runnable `engine` and `api` entry points (no trading logic).
- Add `pyproject.toml` managed by `uv` (Python 3.12) with ruff, mypy `--strict`, pytest, pytest-asyncio and import-linter configured.
- Add typed configuration loading from environment, defaulting to paper mode; live mode requires `TRADING_MODE=live` **and** `LIVE_CONFIRM=yes`. Provide `.env.example`.
- Add structured JSON logging (structlog) with secret/account-ID masking, and OpenTelemetry tracing wired through engine and api into an OTel Collector.
- Add Docker Compose stack: engine, api, redis, postgres, caddy, otel-collector, each with a healthcheck and `restart: unless-stopped`.
- Initialise Alembic with an empty baseline migration, run by a separate one-shot `migrate` service that the engine waits on (so a failed migration cannot crash-loop the engine), and add a Postgres healthcheck. Stage 2 then only adds migrations.
- Create two Postgres users in `infra/postgres/init.sql`: engine (data read/write only) and migrate (DDL rights).
- Pin Python 3.12 with `.python-version` and `requires-python = ">=3.12"`; all commands run through `uv run`.
- Add a diagnostic-only `DiagnosticPing`/`DiagnosticPong` round trip (api → Redis → engine → Redis → api) that proves the cross-service trace and records round-trip time.
- Add the CI pipeline: ruff format/check, mypy, import-linter, pytest, `openspec validate --strict`.
- Add the import-linter contracts: `api` ↛ `engine`, `contracts` ↛ internal packages, only `engine.broker` may import `dhanhq`.
- Out of scope: Dhan connectivity, `web` (React) scaffold, DB tables (Stage 2), Tailscale/VPS provisioning, any order or risk logic.

## Capabilities

### New Capabilities
- `runtime-config`: environment-driven settings, paper-by-default trading mode, and the dual-flag live-mode gate; fail-fast on invalid config.
- `observability`: structured JSON logging with secret masking, and distributed tracing across engine and api.
- `service-stack`: the Docker Compose topology, service health reporting, and restart policy.
- `quality-gates`: CI checks and architectural import boundaries that every change must pass.

### Modified Capabilities
<!-- None: no specs exist yet. -->

## Impact

- New top-level directories and files: `src/`, `tests/`, `docker/`, `infra/`, `scripts/`, `pyproject.toml`, `docker-compose.yml`, `.env.example`, CI workflow under `.github/workflows/`.
- New dependencies: pydantic-settings, structlog, opentelemetry SDK/OTLP exporter, FastAPI + uvicorn, redis, httpx; dev: ruff, mypy, pytest, pytest-asyncio, hypothesis, import-linter.
- No broker, database schema, or user-facing behavior yet; the engine and api start, report health, and emit telemetry only.
- CI host is GitHub Actions (confirmed by the owner): `.github/workflows/ci.yml`.
