# Decision Log — SentinelX

Architecture decisions from the design discussions (Sep–Oct 2026), kept as references for later stages. Each entry records the context, the decision, alternatives rejected, and when to revisit.

**Rules:** never edit a decision's substance after it's accepted — add a new entry that supersedes it. Status values: `Accepted`, `Proposed`, `Superseded by D-xxx`.

## Index

| ID | Decision | Status |
|---|---|---|
| D-001 | .NET as the core platform with Python for quant work | Superseded by D-010 |
| D-002 | Backtesting local; execution, webhooks, tracing, validator on a cloud VPS | Accepted |
| D-003 | Strategy/signal logic in Python, same code for backtest and live | Accepted |
| D-004 | VPS: Linux + Docker Compose; no Windows, no Kubernetes yet | Accepted |
| D-005 | Backtester stays local even though it's containerized | Accepted |
| D-006 | Trailing SL tracked algo-side; no per-tick order modification | Accepted |
| D-007 | Catastrophic static SL at the exchange; exit-before-cancel | Accepted |
| D-008 | Trailing SL engine in C# inside the .NET service | Superseded by D-010 |
| D-009 | Rust deferred; engine swappable behind `BrokerAdapter` | Accepted |
| D-010 | Execution engine in Python | Accepted |
| D-011 | TradingView Strategy Tester as a quick filter only | Accepted |
| D-012 | Risk engine in the backend, inside the engine process; fail closed | Accepted |
| D-013 | Headless engine; separate processes connected via Redis | Accepted |
| D-014 | Frontend: React + TS + Vite + Tailwind + shadcn/ui; FastAPI bridge; no charts | Accepted |
| D-015 | Manual trading from the UI now; phased path to full auto | Accepted |
| D-016 | Dashboard private behind Tailscale + login | Accepted |
| D-017 | Kill-switch fallback independent of the dashboard | Accepted |
| D-018 | Monorepo, single Python project, top-level `src/`, `import-linter` | Accepted |
| D-019 | OpenSpec spec-driven delivery in stages; paper until Stage 7 | Accepted |
| D-020 | `account_id` in the data model from Stage 2; multi-account in Stage 8 | Accepted |
| D-021 | Telegram `/kill` and alerts handled inside the engine | Proposed |
| D-022 | Both algo accounts on Dhan; Zerodha stays manual (no integration) | Accepted |
| D-023 | Market data separated from execution (`MarketDataProvider` vs `BrokerAdapter`) | Accepted |

---

## D-001 — .NET core platform, Python for quant work
**Status:** Superseded by D-010
**Context:** Owner is fluent in C#/ASP.NET Core, newer to Python. Priorities: latency, performance, telemetry.
**Decision:** .NET 8 for execution, webhooks, risk, telemetry; Python microservice for indicators/backtests.
**Why superseded:** a neutral comparison (D-010), with owner preference set aside, favoured one Python engine.

## D-002 — Local backtesting, cloud execution
**Status:** Accepted
**Context:** Research needs fast iteration; execution needs to be always on.
**Decision:** Strategy backtester runs on the local machine. Order placement, webhooks, tracing, and the F&O Trade Validator run on an always-on cloud VPS. Strategies move to the VPS only once finalized, for forward testing (paper or live).
**Alternatives:** everything local (laptop as single point of failure during market hours); everything on VPS (see D-005).
**Revisit when:** strategies need scheduled re-validation on the server (run off-hours with resource limits).

## D-003 — Python for strategy/signal logic, shared by backtest and live
**Status:** Accepted
**Context:** Re-implementing a strategy in a second language risks subtle divergence (rounding, NaN handling, window off-by-ones) between backtested and live behaviour.
**Decision:** The exact Python code that was backtested produces live signals.
**Alternatives:** port to C# (parity risk); Pine Script (alert latency, weak F&O support, a third implementation).

## D-004 — Linux + Docker Compose on the VPS
**Status:** Accepted
**Context:** One VPS running a handful of services.
**Decision:** Ubuntu LTS, Docker Compose, Caddy reverse proxy, `restart: unless-stopped`.
**Alternatives:** Windows (licence cost, overhead, Linux-first observability tooling); Kubernetes (control-plane complexity with no scaling need yet).
**Revisit when:** multi-tenant or multi-node needs appear.

## D-005 — Backtester stays local
**Status:** Accepted
**Context:** Containers make it trivially movable to the VPS.
**Decision:** Keep it local. Heavy backtests would compete for CPU/RAM with live execution during market hours, widen the blast radius, add storage cost, and sit near live credentials.
**Revisit when:** scheduled walk-forward checks are needed — then run on the VPS outside market hours with `mem_limit`/`cpus` caps.

## D-006 — Trailing SL tracked algo-side
**Status:** Accepted
**Context:** Dhan caps modifications at 25 per order and order APIs at 10/sec, 250/min, 1,000/hour, 7,000/day.
**Decision:** The trailing level lives in the engine. The broker only sees one entry, one catastrophic SL, and one exit when the trail is hit.
**Alternatives:** modify a live SL order every tick (exceeds limits within seconds; poor practice that can trigger broker RMS flags).

## D-007 — Catastrophic SL at the exchange; exit before cancel
**Status:** Accepted
**Context:** With the trail algo-side, an engine crash would leave positions with no exchange-side protection.
**Decision:** On entry, place one wide static SL at Dhan as a worst-case backstop. When the trail triggers, place the exit **first**, then cancel the static SL. Two sequential calls cost roughly 150–300 ms (mostly network); exit-first keeps the position covered during that window. Persist trail state so restarts resume correctly. Measure real latency from day one.
**Alternatives:** cancel then place (unprotected window); modify the static SL into the exit (one call, if the order types allow — evaluate in Stage 4).

## D-008 — Trailing SL in C#
**Status:** Superseded by D-010
**Context:** Tick-driven logic tightly coupled to positions and orders, which at the time lived in the .NET service.
**Why superseded:** the execution engine moved to Python (D-010), so the trailing SL lives there too, in one process with position state.

## D-009 — Rust deferred
**Status:** Accepted
**Context:** Rust gives the most predictable timing, but network round-trips (~100–150 ms) dwarf in-process processing (microseconds) for a retail API. Tauri is a desktop UI framework and irrelevant on a headless VPS.
**Decision:** No Rust now. Keep Dhan access behind the `BrokerAdapter` protocol so the execution engine alone can later be replaced.
**Revisit when:** strategies become latency-critical beyond what the REST API allows (e.g. colocation).

## D-010 — Python execution engine
**Status:** Accepted (supersedes D-001, D-008)
**Context:** Neutral comparison of Python vs .NET for ~10 concurrent orders with SL strategies, owner preference set aside. Speed is a tie: language overhead is <0.1% of order round-trip time.
**Decision:** Python. Decisive factors: Dhan's official SDK (including the binary live-feed parser) is Python; most reference implementations (OpenAlgo, community code) are Python; one language across backtest, signals, trailing SL, and execution removes parity risk.
**Mitigations for .NET's advantages:** `asyncio` (+ `uvloop`), `mypy --strict`, pydantic models, OpenTelemetry from day one.
**Alternatives:** .NET (compile-time safety, mature async, tunable GC, first-class telemetry — but a self-built Dhan client and a cross-language port).

## D-011 — TradingView Strategy Tester as a quick filter only
**Status:** Accepted
**Context:** Owner has a paid TradingView subscription and existing buy/sell indicators.
**Decision:** Use Pine's Strategy Tester for a fast first-pass viability check. Ideas that pass are ported to Python and backtested properly; Python is the source of truth.
**Why:** Pine has bar-close fill assumptions, simplified costs, repainting risk, and no reuse for live execution.

## D-012 — Risk engine inside the engine process
**Status:** Accepted
**Context:** Need per-trade and per-day risk limits.
**Decision:** Risk checks run inside the engine, on every order from every source. Fail closed. Persist counters. Limits editable from the UI but **tighten-only during market hours**. Dhan's own P&L exit / kill switch as a wider backstop.
**Alternatives:** risk in the UI (bypassed by any non-UI order); separate risk service (extra hop and failure mode).

## D-013 — Headless engine, Redis between processes
**Status:** Accepted
**Context:** The engine must keep trading with no browser open; the UI is for human interaction only.
**Decision:** Separate processes — engine, api, web. Engine publishes a state snapshot plus deltas to Redis and consumes commands from a Redis Stream. Only the engine talks to Dhan.
**Alternatives:** FastAPI embedded in the engine process (simpler, but an API bug or restart affects trading).

## D-014 — Frontend stack
**Status:** Accepted
**Context:** No charts needed; needs fast live position values and a decent design; owner will build with AI assistance.
**Decision:** React + TypeScript + Vite + Tailwind + shadcn/ui + Zustand, served by Caddy. FastAPI as the API layer, with TypeScript types generated from its OpenAPI spec. WebSocket push, server-computed P&L, throttled to 4–5 updates/sec, deltas only.
**Alternatives:** Svelte (marginally lighter, smaller ecosystem); Streamlit/NiceGUI (weak for live dashboards); Blazor (no link to a Python backend).

## D-015 — Manual UI trading now, phased automation
**Status:** Accepted
**Decision:** Trade button + kill switch now (manual entry, automated exits); then semi-auto (strategy proposes, human approves); then full auto. All modes share one order path through the risk engine, so progression is a mode switch.

## D-016 — Dashboard behind Tailscale
**Status:** Accepted
**Context:** The dashboard can place real orders.
**Decision:** Not exposed publicly. Tailscale private network plus login; audit every command.

## D-017 — Kill-switch fallback independent of the dashboard
**Status:** Accepted
**Decision:** Dashboard kill switch (hold-to-confirm) plus a path that works when the UI is down (Telegram `/kill` — see D-021) plus Dhan-side backstops when the engine itself is down.

## D-018 — Repo structure
**Status:** Accepted
**Decision:** Monorepo with a single Python project (one `pyproject.toml`, `uv`), all sources under top-level `src/` (`contracts`, `engine`, `api`, `cli`, `web`), tests mirrored under `tests/`, Dockerfiles in `docker/`. Boundaries enforced by `import-linter`: `api` ↛ `engine`; `contracts` ↛ internal; only `engine.broker` → `dhanhq`.
**Alternatives:** uv workspace with separate packages (boundaries enforced by dependencies, but more setup and nested `src/` folders).

## D-019 — OpenSpec, staged delivery
**Status:** Accepted
**Decision:** Spec-driven development with OpenSpec; stages S0–S10 per `PRD.md` §11; paper mode only until Stage 7 (go-live hardening).

## D-020 — Multi-account readiness
**Status:** Accepted (2026-10-25)
**Context:** Owner will later add his spouse's Dhan account. Building multi-account behaviour before the core is proven would double the failure surface, and early live bugs should only touch the owner's own money.
**Phase 1:** single account; no multi-account routing, loops, or account switcher before Stage 8.
**Decision:** Every contract, order, position, and risk record carries `account_id` from Stage 2; the multi-account feature ships in Stage 8. Cheap now, costly to retrofit.

## D-021 — Telegram inside the engine
**Status:** Proposed
**Decision:** The Telegram bot (alerts + `/kill`) runs inside the engine process, so the fallback works when the API, Redis consumer, or dashboard is down. Only whitelisted chat IDs are accepted.

## D-022 — Both algo accounts on Dhan; Zerodha stays manual
**Status:** Accepted (2026-10-25)
**Context:** Owner considered running his Dhan account and his spouse's Zerodha account from the platform on one static IP. Family IP sharing (spouse, dependent children, dependent parents) is documented within a broker, but sharing across two brokers isn't explicitly covered. Zerodha would also need a second broker adapter, instrument-ID mapping, and a fragile daily password + TOTP login.
**Decision:** The spouse opens a Dhan account for algo trading; her Zerodha account stays for manual long-term investing, outside the platform (no static IP needed, since the rule applies only to API orders). Family static IP mapping is requested by email from the registered address to apihelp@dhan.co, CC'ing her registered email.
**Trade-off:** broker concentration — a Dhan outage affects both algo accounts; exchange-side catastrophic SLs still protect open positions.
**Revisit when:** a second broker is genuinely needed (D-023 keeps that possible).

## D-023 — Market data separated from execution
**Status:** Accepted (2026-10-25)
**Context:** Prices are exchange prices, independent of broker, so one feed can serve positions at any broker.
**Decision:** `MarketDataProvider` (ticks, instrument master) is a separate interface from `BrokerAdapter` (orders, positions), with a common instrument key (exchange + segment + exchange token). Both are implemented by Dhan today.
**Why now:** cheap at Stage 1; keeps a second broker or the Rust execution engine swappable later without touching market data.
