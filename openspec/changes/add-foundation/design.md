# Design

## Context

Greenfield: the repo holds only docs and OpenSpec config. Layout, tooling, import boundaries and safety defaults are fixed by CLAUDE.md, PRD §4 and §11, and D-010, D-013, D-018, D-019. See proposal.md for motivation. Development happens on Windows; the stack targets Linux containers.

## Goals / Non-Goals

**Goals:**
- A skeleton where `docker compose up` yields six healthy services, a completed one-shot `migrate` run, and one trace spanning api and engine.
- Safety rules (paper default, import boundaries, no real-Dhan tests) enforced mechanically from day one.
- Later stages add code into existing seams without changing tooling.

**Non-Goals:**
- Postgres tables (Stage 2): Alembic is initialised with an empty baseline migration only.
- Dhan, order, risk, or Telegram code. Real TLS and Tailscale (Stage 7).
- Dashboard (`src/web`) scaffolding (Stage 6).

## Decisions

**1. Settings via `pydantic-settings`, one class per service, shared base in `contracts`-independent module `engine.config` / `api.config`.**
`contracts` must not import internal packages and `api` must not import `engine`, so a shared settings module cannot live in either. Put the small trading-mode logic in `contracts.mode` (pure, no I/O) and let each service own its own `Settings` class using it. Secrets use `SecretStr` (masked in `repr`).
*Alternative:* a fourth shared `common` package. Rejected: it adds a package outside the layout fixed by D-018.

**2. Live gate is a model validator.** `trading_mode: Literal["paper","live"] = "paper"`, `live_confirm: str = ""`; validator raises when mode is `live` and confirm is not `yes`. Startup failure beats a silent downgrade, because a silent fallback to paper hides a misconfiguration in production.

**3. Logging with `structlog` + processors.** Order: contextvars merge → add level/timestamp (UTC ISO) → service name → OTel `trace_id` injector → masking processor → JSON renderer. Masking is key-based (token, secret, password, key, authorization) plus an `account_id`/`client_id` last-four rule. It runs last so no processor can reintroduce a raw value.
*Alternative:* mask in the formatter only. Rejected: key-based masking on the event dict is easy to unit test.

**4. OpenTelemetry via OTLP/gRPC to the collector; `BatchSpanProcessor`.** Batch export keeps the exporter off the event loop's critical path and tolerates an unreachable collector (spans dropped, warning logged). The collector config only logs spans (`debug` exporter) for now; Jaeger/Tempo is chosen later. FastAPI is instrumented with `opentelemetry-instrumentation-fastapi`.

**5. Diagnostic ping/pong round trip over Redis (owner-approved, with conditions).** Redis is the only channel between api and engine (D-013). The api's probe endpoint sends a `DiagnosticPing` (api → Redis Stream, W3C `traceparent` in a message field); the engine's consumer continues the trace, and replies with a `DiagnosticPong` (engine → Redis → api). Both models live in `contracts` with `schema_version`, so `api` never imports `engine` and import-linter stays green. The exchange is diagnostic only: no orders, no state changes, no broker calls. The api records the round-trip time as a span attribute (`diagnostics.rtt_ms`); this is intended to become the engine heartbeat behind the dashboard's connection status. The engine-side consumer handles only this message type in this change. Stage 5 reuses the pattern for real commands.

**6. Engine health = heartbeat file, not a port.** The engine updates a timestamp file every few seconds from inside its event loop. The container health check runs a tiny `python -m engine.healthcheck` that fails if the file is stale. This proves the loop is alive without opening a listening port or adding an HTTP server to the engine. The api exposes `/healthz` over HTTP; redis uses `redis-cli ping`; postgres `pg_isready`; caddy and otel-collector use their own health/ready endpoints.
*Alternative:* HTTP health server in the engine. Rejected: extra surface and dependency on a web framework in the trading process.

**7. Dependency ordering with `depends_on: condition: service_healthy`.** Engine and api wait for redis and postgres. Services also retry Redis connection with backoff at startup, since Compose ordering does not cover later restarts.

**8. Import-linter configuration.** Three contracts: `forbidden` (api → engine), `independence`/forbidden for `contracts` against `engine`, `api`, `cli`, and `forbidden` for `dhanhq` outside `engine.broker`. The last needs `engine.broker` to exist (empty package with `__init__.py`) and `dhanhq` added only as a dependency in a later stage; the contract uses `allow_indirect_imports` off. Include a deliberate-violation test that runs `lint-imports` on a temporary tree to prove each contract can fail.

**9. Blocking real Dhan in tests.** A `conftest.py` autouse fixture patches socket connection so hosts matching `*.dhan.co` raise; a test proves it. This backs CLAUDE.md rule 11 independent of future test authors' discipline.

**10. CI on GitHub Actions, confirmed by the owner (repo will live on GitHub)** (`.github/workflows/ci.yml`): `astral-sh/setup-uv`, `uv sync --frozen`, then ruff format `--check`, ruff check, mypy, lint-imports, pytest, and `openspec validate --all --strict`. Compose-level checks (health, trace) are a separate job using the docker daemon on the runner.

**11. Caddy is HTTP-only in Stage 0, bound to localhost (owner-approved for local dev).** The Compose port mapping is `127.0.0.1:<port>`, so nothing is reachable from the LAN. It proxies `/api` to the api and serves a placeholder for web. The Caddyfile carries a `TODO` that TLS and Tailscale arrive with the VPS setup in Stage 7; PRD §9.3 keeps access private regardless.

**13. Postgres healthcheck, Alembic baseline, and a one-shot `migrate` service (owner-approved).** Postgres has a `pg_isready` healthcheck. Alembic is initialised (async SQLAlchemy 2.0 env, `alembic.ini` at the repo root, migrations under `src/engine/persistence/migrations/`) with one empty baseline revision, so Stage 2 only adds migrations and never edits this one. A separate `migrate` service runs `alembic upgrade head` and exits. It `depends_on` postgres `service_healthy`, has `restart: "no"`, and uses the same engine image with a different command. The engine `depends_on` `migrate` with `condition: service_completed_successfully`. No tables are created in this change.
The reason for a separate service: a failed migration must not crash-loop the engine under `restart: unless-stopped`, and migrations need privileges the engine should not hold.
*Alternative:* `alembic upgrade head` in engine startup. Rejected on the two grounds above, not because of any multi-engine race (see Risks).

**14. Two database users (least privilege).** `infra/postgres/init.sql` creates two roles: a migrate role with DDL rights (owns the schema) and an engine role with data read/write only. Default privileges are set so tables created later by the migrate role are automatically readable and writable by the engine role, and sequences (identity/serial columns) are usable by it: `ALTER DEFAULT PRIVILEGES FOR ROLE <migrate_role> GRANT USAGE, SELECT ON SEQUENCES TO <engine_role>`, alongside the table grants. Credentials come from `.env` and are secrets (masked in logs). Only the engine (engine role) and migrate (migrate role) see database URLs. The **api has no database URL**: per D-013 it talks only to Redis, and a test asserts the api settings contain no DB URL. If Stage 5 auth needs storage, it gets its own scoped role then.
*Alternative:* one superuser shared by all. Rejected: a bug in trading code could then alter or drop tables.

**15. Python pinned to 3.12.** `.python-version` contains `3.12` and `pyproject.toml` sets `requires-python = ">=3.12"`. All local commands go through `uv run`, never the system Python (3.11 on the owner's machine). Task 1.1 confirms `uv run python --version` shows 3.12.x.

**12. Images.** One `python:3.12-slim` base per service with `uv` copied in; separate `docker/engine.Dockerfile` and `docker/api.Dockerfile` installing the same project with different entry points. Run as non-root.

## Risks / Trade-offs

- **Engine consuming a ping from Redis in Stage 0 is test scaffolding** → Keep it minimal and name the streams `diagnostics` so it can be removed or kept as a diagnostics tool later. It must not touch orders, state or the broker.
- **A failed migration blocks engine start** → Intended: the engine stays down (visible in `migrate` logs) instead of crash-looping or running against a half-migrated schema. Existing positions are still protected by exchange-side SLs (D-007). Stage 8 adds a second account inside the same engine process (D-020), not a second engine instance, so there is no migration race to guard against.
- **Engine role cannot run DDL** → Stage 2 tables must be created only via migrations; a test that creates a table as the engine role confirms it is rejected.
- **Heartbeat file proves loop liveness, not broker connectivity** → Acceptable now; richer health arrives with Stage 1/2.
- **Key-based masking can miss secrets under unexpected key names** → Mask by key list plus a unit test for each configured key; never log whole request/response bodies from broker code.
- **Windows dev vs Linux containers (path, line endings)** → Add `.gitattributes` forcing LF for shell/Docker files; run containers for the integration checks.
- **Dependency pinning drift** → Commit `uv.lock`; CI uses `--frozen`.
