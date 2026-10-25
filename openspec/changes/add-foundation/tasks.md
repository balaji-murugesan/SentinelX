# Tasks

## 1. Project scaffold and tooling

- [x] 1.1 Create `.python-version` (`3.12`) and `pyproject.toml` (`requires-python = ">=3.12"`, uv) with runtime and dev dependencies, plus the `src/{contracts,engine,api,cli}` and `tests/` package skeleton including an empty `engine/broker` package; verify `uv run python --version` prints 3.12.x, `uv sync` succeeds, and `uv run python -c "import contracts, engine, api, cli"` exits 0
- [x] 1.2 Configure ruff, mypy (`strict`), pytest (`asyncio_mode`) in `pyproject.toml`, add `.gitignore` (`.env`, `.venv`) and `.gitattributes` (LF for shell/Docker files); verify `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, and `uv run pytest` all pass on the skeleton
- [x] 1.3 Add import-linter contracts (api ↛ engine, contracts ↛ internal packages, `dhanhq` only inside `engine.broker`); verify `uv run lint-imports` passes
- [x] 1.4 Add tests that create a temporary violating module for each contract and assert `lint-imports` fails; verify the tests pass
- [x] 1.5 Add a pytest autouse fixture that fails any socket connection to `*.dhan.co`, with a test proving it; verify the test passes

## 2. Runtime configuration

- [x] 2.1 Implement trading-mode resolution in `contracts` (paper default, live needs `TRADING_MODE=live` and `LIVE_CONFIRM=yes`) with unit tests for every scenario in `runtime-config`; verify the tests pass
- [x] 2.2 Implement typed `Settings` for engine and api (pydantic-settings, `SecretStr` for secrets, fail-fast on missing or invalid values without echoing secrets), with tests for missing, invalid, and masked-repr cases, and a test asserting the api settings contain no database URL (only engine and migrate see DB URLs); verify the tests pass
- [x] 2.3 Add `.env.example` with placeholders and paper mode; verify a test loads it and resolves paper mode
- [x] 2.4 Log the effective mode once at engine and api startup; verify via a test capturing log output

## 3. Observability

- [x] 3.1 Implement structlog JSON configuration (UTC timestamp, level, service, message, bound `account_id`/`order_id`); verify a test parses a log line as JSON with required fields
- [x] 3.2 Implement the masking processor (secret-named keys, last-four for `account_id`/client IDs) with one test per configured key; verify the tests pass
- [x] 3.3 Implement OpenTelemetry setup (OTLP exporter, batch processor, FastAPI instrumentation, `trace_id` injected into logs); verify tests using an in-memory exporter show `trace_id` in log records and no secret attributes on spans
- [x] 3.4 Verify that an unreachable collector produces a warning and does not block startup or request handling (test with a closed port)
- [x] 3.5 Add `DiagnosticPing` and `DiagnosticPong` models to `contracts` (with `schema_version`), the api probe endpoint that sends the ping with `traceparent` and awaits the pong, and the engine consumer that continues the trace and replies; record round-trip time as the `diagnostics.rtt_ms` span attribute; verify an integration test against a real Redis shows both spans share one trace ID, the attribute is set, and `lint-imports` still passes

## 4. Service stack

- [ ] 4.1 Write the api (`/healthz`, probe endpoint) and engine (asyncio entry, heartbeat file, `engine.healthcheck`, graceful shutdown on SIGTERM) entry points; verify unit tests for the heartbeat staleness check and `/healthz` pass
- [ ] 4.2 Write `docker/engine.Dockerfile` and `docker/api.Dockerfile` (python:3.12-slim, uv, non-root); verify both images build
- [ ] 4.3 Write `infra/otel/` collector config, `infra/caddy/Caddyfile` (HTTP only, proxy `/api`, web placeholder, TODO comment that TLS + Tailscale come with the VPS setup in Stage 7), and `infra/postgres/init.sql` creating two roles (engine: data read/write only with default privileges on future tables and `GRANT USAGE, SELECT ON SEQUENCES`; migrate: DDL rights); verify the collector and caddy containers start with their configs and, against a throwaway Postgres, that the engine role is rejected when creating a table while the migrate role succeeds, and the engine role can read, write, and insert a row into a table with an identity column
- [ ] 4.4 Write `docker-compose.yml` with six long-running services plus a one-shot `migrate` service (`restart: "no"`, waits on postgres `service_healthy`; engine waits on `migrate` with `service_completed_successfully`), health checks (postgres `pg_isready`), separate engine and migrate database URLs, `restart: unless-stopped` on the long-running services, caddy published on `127.0.0.1` only, no host-published redis/postgres ports, paper mode; verify `docker compose config` is valid and shows the localhost-only binding
- [ ] 4.4a Initialise Alembic (async env, `alembic.ini`, `src/engine/persistence/migrations/`) with one empty baseline revision, and the `migrate` entry point (`alembic upgrade head`, exits 0 on success and non-zero on failure, using the migrate role's URL only); verify `uv run alembic upgrade head` against a local Postgres succeeds, creates no tables except `alembic_version`, `alembic downgrade base` works, and a deliberately broken migration makes the migrate command exit non-zero
- [ ] 4.5 Verify `docker compose up -d` makes all six long-running services healthy and `migrate` exit 0, the engine logs paper mode, stopping the api leaves the engine healthy, and with a deliberately failing migration the engine never starts and is not restarted
- [ ] 4.6 Verify killing the engine container causes Docker to restart it and it becomes healthy again

## 5. CI and documentation

- [ ] 5.1 Add `.github/workflows/ci.yml` running ruff format check, ruff check, mypy, lint-imports, pytest, and `openspec validate --all --strict` with `uv sync --frozen`; verify each command passes locally in the same order
- [ ] 5.2 Add a CI job (or documented script) that runs the compose health and cross-service trace checks, and a CI Redis service with `TEST_REDIS_URL=redis://localhost:6379/15` so the integration tests run instead of skipping; verify it passes locally
- [ ] 5.3 Create `scripts/` entry points referenced by README (for example a compose smoke-test script) and update README quick-start to match the real commands; verify each documented command runs as written
- [ ] 5.4 Comment Python-specific idioms on first use (settings class, `Protocol` if used, async entry point, context managers) with C# analogies per CLAUDE.md; verify by review

## 6. Integration check

- [ ] 6.1 Run the full definition of done on a clean checkout: `uv sync`, ruff format, ruff check, mypy, lint-imports, pytest, `openspec validate add-foundation --strict`, then `docker compose up -d` all healthy with one trace across api and engine

## Workflow follow-up

- Archive the change with `openspec archive add-foundation --yes` after the owner's review and merge.
