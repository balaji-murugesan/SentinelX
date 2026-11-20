# SentinelX

Self-hosted trading platform for personal and family use. Phase 1 trades the owner's Dhan account; phase 2 (Stage 8) adds the spouse's Zerodha account. It is a headless Python engine that manages orders, trailing stop-losses, and risk limits, with a React dashboard for one-click trading and an emergency kill switch.

> **Paper trading is the default.** Live trading is enabled only from Stage 7 and requires explicit configuration.

- What and why: [`PRD.md`](PRD.md)
- Working rules for Claude Code: [`CLAUDE.md`](CLAUDE.md)
- Design decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md)
- Field notes (external, unverified): [`docs/field-notes-option-buying.md`](docs/field-notes-option-buying.md)
- Product teardowns behind PRD v3 (external, unverified): [`docs/research/`](docs/research/). Covers TradeBridge (webhook signals), TradeMate (manual options execution) and the RMS Tool (risk rules and Dhan backstops)

## Status

What each stage builds and its acceptance criteria: [`PRD.md` §11](PRD.md#11-delivery-stages).

| Stage | | OpenSpec change | Status |
|---|---|---|---|
| S0 | Foundation | `add-foundation` | ✅ Done (archived 2026-10-25) |
| S1 | Market data | `add-market-data`, `add-candles` | ✅ Done (`add-market-data` archived 2026-10-28, `add-candles` archived 2026-11-20) |
| S2 | Orders & positions | `add-order-position-core`, `add-dhan-readonly` | 🟡 `add-order-position-core` ✅ (archived 2026-11-20); `add-dhan-readonly` after |
| S3 | Risk engine | `add-risk-engine`, `add-risk-guards` | ⬜ |
| S4 | Trailing SL & exits | `add-trailing-exits` | ⬜ |
| S5 | API bridge | `add-api-bridge` | ⬜ |
| S6 | Dashboard | `add-dashboard` | ⬜ |
| S7 | Go-live hardening | `add-live-trading` | ⬜ |
| S8 | Multi-account (spouse's Zerodha) | `add-multi-account` | ⬜ |
| S9 | Semi-auto | `add-semi-auto-signals` | ⬜ |
| S10 | Full auto | `add-full-auto` | ⬜ |

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| Git | any | |
| Docker + Docker Compose | recent | Runs Redis, Postgres, Caddy, OTel, and the services |
| [uv](https://docs.astral.sh/uv/) | latest | Python version + dependency manager (like `dotnet` + NuGet) |
| Python | 3.12 (installed via uv) | Engine and API |
| Node.js | ≥ 20.19 | Dashboard build and the OpenSpec CLI |
| OpenSpec CLI | latest | Spec-driven workflow |
| Claude Code | latest | Implementation |

```bash
# uv (Linux/macOS)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# OpenSpec
npm install -g @fission-ai/openspec@latest
```

## First-time setup

```bash
git clone <repo-url> sentinelx && cd sentinelx

# 1. OpenSpec — already initialised in this repo (openspec/config.yaml and
#    .claude/skills/). Only run this on a fresh clone that lacks them:
openspec init --tools claude
#    Optional: add project constraints to the `context:` block of openspec/config.yaml.

# 2. Secrets — copy the template and fill in values (never commit .env)
cp .env.example .env
```

Then put three different passwords into `.env` (each at least 16 characters). From Git Bash, this
writes them without printing them:

```bash
for v in POSTGRES_PASSWORD ENGINE_DB_PASSWORD MIGRATE_DB_PASSWORD; do
  sed -i "s/^$v=.*/$v=$(openssl rand -hex 24)/" .env
done
```

`.env` keys (the full, commented list is in `.env.example`):

| Key | Purpose | From |
|---|---|---|
| `TRADING_MODE` | `paper` (default) or `live`; anything else refuses to start | S0 |
| `LIVE_CONFIRM` | Must be exactly `yes` for live mode to start | S0 |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | S0 |
| `POSTGRES_PASSWORD` | Database bootstrap superuser (initialisation only) | S0 |
| `ENGINE_DB_PASSWORD` | Engine's database role: read/write data, no schema changes | S0 |
| `MIGRATE_DB_PASSWORD` | Migration role: schema changes; used only by the one-shot `migrate` service | S0 |
| `CADDY_PORT` | Optional; host port for the stack (default `8080`, always bound to 127.0.0.1) | S0 |
| `REDIS_URL`, `DATABASE_URL`, `MIGRATE_DATABASE_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Only for running a service outside Docker; Compose sets its own | S0 |
| `FEED_SOURCE` | Required: `dhan` (real prices) or `simulated` (dev/CI; refused in live mode) | S1 |
| `FEED_INSTRUMENTS` / `FEED_MAX_SUBSCRIPTIONS` | Instruments streamed at startup (`SEGMENT:ID,…`) and the subscription cap (default 200) | S1 |
| `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | Broker credentials (from web.dhan.co → DhanHQ Trading APIs); the token lasts ~24 h, paste a fresh one daily | S1 |
| `CANDLE_INTERVALS` | Optional; candle intervals in minutes, anchored to 09:15 IST, from `1,3,5,15` (default all four) | S1 |
| `CANDLE_HISTORY` / `CANDLE_CLOSE_GRACE_S` | Optional; completed candles kept per instrument and interval (default 200) and seconds after an interval ends before it closes (default 2) | S1 |
| `DHAN_HISTORY_RATE_PER_S` | Optional; Dhan intraday-history requests per second for candle warm-up (default 4; Dhan's limit is 5) | S1 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS` | Alerts and `/kill` fallback | S7 |

The database creates its roles once, when its volume is first made. To change a password later,
either `docker compose down -v` (deletes the database) or `ALTER ROLE` inside Postgres.

## Running locally

```bash
uv sync                                      # create .venv and install dependencies
docker compose up -d --build --wait          # start everything; returns when all are healthy
docker compose ps -a                         # 6 services healthy, migrate "exited (0)"
docker compose logs -f engine                # follow the engine (JSON lines)
uv run python scripts/smoke_test.py          # end-to-end checks against the running stack
docker compose down                          # stop (add -v to also delete the database)
```

The stack listens on `http://127.0.0.1:8080` only. In Stage 0 it serves a diagnostic round trip,
`POST /api/diagnostics/ping` (api → Redis → engine → Redis → api), which also shows up in
`docker compose logs otel-collector` as one trace spanning both services.

Checks (the same ones CI runs, in `.github/workflows/ci.yml`):

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run lint-imports
uv run pytest -ra                            # Redis round-trip tests skip unless TEST_REDIS_URL is set
openspec validate --all --strict
```

To include the Redis round-trip tests, start a throwaway Redis and point the tests at database 15
(they wipe it):

```bash
docker run -d --rm --name sx-redis -p 127.0.0.1:6380:6379 redis:7-alpine
TEST_REDIS_URL=redis://127.0.0.1:6380/15 uv run pytest     # Bash
docker stop sx-redis
```

In PowerShell, set the variable with `$env:TEST_REDIS_URL = "redis://127.0.0.1:6380/15"` and
remove it afterwards with `Remove-Item Env:TEST_REDIS_URL`.

The trading-state tests (orders, fills, positions; Stage 2) also run against a real Postgres
when `TEST_DATABASE_URL` names a throwaway database ending in `_test` (they migrate and
truncate it):

```bash
docker run -d --rm --name sx-pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=sentinelx_test -p 127.0.0.1:5433:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:5433/sentinelx_test uv run pytest
docker stop sx-pg-test
```

`FEED_SOURCE=simulated` drives the engine with simulated ticks outside market hours (refused in
live mode); `cd src/web && npm install && npm run dev` runs the dashboard (Stage 6).

To see candles for one instrument, with history warm-up first and then live candles; on Ctrl+C
it compares the live candles with a fresh history fetch:

```bash
uv run --env-file .env python -m cli candles watch IDX_I:13 --tf 5
```

## Setting risk limits

Every order passes the risk engine, and a fresh install has **no limits**, so it refuses every entry (an exit is never refused by an entry limit; price sanity and the gate still apply) until you set some. There are no defaults on purpose: the numbers are yours to decide from your own backtests.

```bash
# The engine image has no docs/ folder and Redis is not published to the host, so copy your
# limits file into the container, then send it from there:
cp docs/risk-limits.example.json my-limits.json             # then edit my-limits.json
docker compose cp my-limits.json engine:/tmp/limits.json
docker compose exec engine python -m cli risk set-limits --file /tmp/limits.json
docker compose exec engine python -m cli risk status        # today's P&L, floor, halt, entries
docker compose exec engine python -m cli risk size NSE_FNO:53720 BUY --entry 100 --stop 90
```

- [`docs/risk-limits.example.json`](docs/risk-limits.example.json) is a complete file with **example values, not a recommendation**. Copy it and edit your copy. Money and percentages are strings (`"3000"`), counts and seconds are numbers, and keys starting with `_` are comments.
- **While the market is open (09:15-15:30 IST) an edit may only tighten** (a lower loss limit, an earlier cut-off, fewer trades); loosening is refused with `TIGHTEN_ONLY`, and the answer names the keys. Outside market hours any valid edit is accepted. An edit is applied whole or not at all.
- An entry needs `--stop PRICE` (where the trade is wrong): the engine sizes the risk from it and places a wide catastrophic stop at the broker once the entry fills.
- **`kill --yes`** cancels pending entries and exits every position, and refuses entries until the next trading day. There is no intraday undo, and `recovery resume` does not clear it.

## Each morning: fresh Dhan token

Dhan access tokens last about 24 hours and the engine never creates or renews them.

1. On web.dhan.co → My Profile → Access DhanHQ APIs, generate a token.
2. Paste it into `.env` as `DHAN_ACCESS_TOKEN=…`.
3. Check it (read-only, one `GET /v2/profile`; prints only field names, token expiry in IST and the
   data-plan status, never the token, client ID or personal details):

   ```bash
   uv run --env-file .env python -m cli dhan profile
   ```

   Expect `token expiry : <date> IST (in 23h …)` and `data plan : Active`. If it says
   `EXPIRED` or the check fails, generate a new token before market open (09:15 IST).
4. Restart the engine so it picks up the new token: `docker compose up -d engine`.

## Repository layout

```
src/
  contracts/      shared pydantic models (messages, trading-mode gate, settings base)
  observability/  logging, secret masking, tracing (D-024)
  engine/         headless trading engine (only code that calls Dhan); persistence/ = migrations
  api/            FastAPI bridge
  cli/            developer CLI
  web/            React dashboard (Stage 6)
tests/            mirrors src/; integration/ needs Redis or Docker
openspec/         specs and changes
docs/             decisions, architecture, runbook; research/ = external teardowns
docker/           Dockerfiles        infra/   postgres, caddy, otel config
scripts/          smoke_test.py      docker-compose.yml, alembic.ini at the root
```

## Development workflow

1. Pick the next stage in `PRD.md` §11.
2. `/openspec-propose <change-id>` → review → `openspec validate <change-id> --strict` → approve.
3. Implement; keep `tasks.md` checkboxes current.
4. All checks green → merge → `openspec archive <change-id> --yes`.
5. If a design decision changes, add a superseding entry to `docs/DECISIONS.md`.

## Emergency

- **Dashboard:** hold the kill switch for 1 second.
- **CLI:** `docker compose exec engine python -m cli kill --yes` (needs only Redis and the engine, not the API or the dashboard). The engine also kills by itself when the day's loss reaches the equity floor.
- **Dashboard down:** send `/kill` to the Telegram bot (Stage 7+).
- **Engine down:** use Dhan's own kill switch at web.dhan.co; static catastrophic SLs remain at the exchange. From Stage 7, Dhan's P&L-based exit is armed every morning as a wider backstop.
- **Positions won't flatten:** the engine escalates to Dhan's Exit All on its own (Stage 7+). Manually, use Exit All in the Dhan app; it also cancels the catastrophic SLs.
- **You closed a position in the Dhan app:** check that its catastrophic SL was cancelled too. A resting SL on a flat book opens a new position if it triggers. The engine does this automatically from Stage 7.
- **Dhan's kill switch locks the account for the rest of the day** and needs a flat book first. Use it after flattening, not instead of it. From Stage 7 the engine presses it for you after every automatic stop (loss floor or profit target). A risk halt can't be resumed the same day; that's deliberate.

Full procedures: `docs/runbook.md` (Stage 7).
