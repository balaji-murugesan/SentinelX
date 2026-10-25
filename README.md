# SentinelX

Self-hosted trading platform for Dhan: a headless Python engine that manages orders, trailing stop-losses, and risk limits, with a React dashboard for one-click trading and an emergency kill switch.

> **Paper trading is the default.** Live trading is enabled only from Stage 7 and requires explicit configuration.

- What and why: [`PRD.md`](PRD.md)
- Working rules for Claude Code: [`CLAUDE.md`](CLAUDE.md)
- Design decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

| Stage | | Status |
|---|---|---|
| S0 | Foundation | ⬜ Not started |
| S1 | Market data | ⬜ |
| S2 | Orders & positions | ⬜ |
| S3 | Risk engine | ⬜ |
| S4 | Trailing SL & exits | ⬜ |
| S5 | API bridge | ⬜ |
| S6 | Dashboard | ⬜ |
| S7 | Go-live hardening | ⬜ |
| S8 | Multi-account | ⬜ |
| S9 | Semi-auto | ⬜ |
| S10 | Full auto | ⬜ |

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

`.env` keys (full list in `.env.example` once Stage 0 lands):

| Key | Purpose |
|---|---|
| `TRADING_MODE` | `paper` (default) or `live` |
| `LIVE_CONFIRM` | Must be `yes` for live mode to start |
| `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | Broker credentials (from web.dhan.co → DhanHQ Trading APIs) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS` | Alerts and `/kill` fallback |
| `POSTGRES_*`, `REDIS_URL` | Local infrastructure |

## Starting development (Stage 0 onward)

The repo starts with only these docs. Stage 0 scaffolds everything below. In Claude Code:

```
/opsx:propose add-foundation
```

Review the generated `openspec/changes/add-foundation/` (proposal, tasks, spec deltas), approve it, then ask Claude Code to implement it.

## Running locally (after Stage 0)

```bash
uv sync                               # create .venv and install dependencies
docker compose up -d                  # redis, postgres, otel, engine, api, caddy
docker compose logs -f engine         # follow the engine

uv run pytest                         # tests
uv run ruff check . && uv run mypy src && uv run lint-imports

cd src/web && npm install && npm run dev   # dashboard (Stage 6+)
```

Outside market hours, set `MARKET_FEED=simulated` to drive the engine with a simulated tick feed.

## Repository layout

```
src/
  contracts/   shared pydantic models
  engine/      headless trading engine (only code that calls Dhan)
  api/         FastAPI bridge
  cli/         developer CLI
  web/         React dashboard
tests/         mirrors src/
openspec/      specs and changes
docs/          decisions, architecture, runbook
docker/  infra/  scripts/
```

## Development workflow

1. Pick the next stage in `PRD.md` §11.
2. `/opsx:propose <change-id>` → review → `openspec validate <change-id> --strict` → approve.
3. Implement; keep `tasks.md` checkboxes current.
4. All checks green → merge → `openspec archive <change-id> --yes`.
5. If a design decision changes, add a superseding entry to `docs/DECISIONS.md`.

## Emergency

- **Dashboard:** hold the kill switch for 1 second.
- **Dashboard down:** send `/kill` to the Telegram bot (Stage 7+).
- **Engine down:** use Dhan's own kill switch at web.dhan.co; static catastrophic SLs remain at the exchange.

Full procedures: `docs/runbook.md` (Stage 7).
