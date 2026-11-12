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
| D-022 | Both algo accounts on Dhan; Zerodha stays manual (no integration) | Superseded by D-029 |
| D-023 | Market data separated from execution (`MarketDataProvider` vs `BrokerAdapter`) | Accepted |
| D-024 | Shared runtime telemetry lives in `observability`; `contracts` stays pure models | Accepted |
| D-025 | Trailing SL is a pluggable method chosen per trade, with a configured default | Proposed |
| D-026 | Risk rules are pluggable `RiskCheck`s with monotonic, engine-owned state; daily loss becomes an equity-floor ratchet | Proposed |
| D-027 | Exits are bounded limit-price chases, never MARKET orders | Proposed |
| D-028 | External (manual) orders detected continuously; orphan catastrophic SLs cancelled first | Proposed |
| D-029 | Spouse's Zerodha account integrated in Stage 8 (execution only); market data stays on Dhan | Proposed |
| D-030 | Automatic stops lock the account for the day at the broker; risk halts have no intraday resume | Proposed |
| D-031 | Webhooks: own scripts over Tailscale; TradingView through one public, IP-allowlisted route | Proposed |

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
**Status:** Superseded by D-029 (2026-11-12). Originally accepted 2026-10-25.
**Context:** Owner considered running his Dhan account and his spouse's Zerodha account from the platform on one static IP. Family IP sharing (spouse, dependent children, dependent parents) is documented within a broker, but sharing across two brokers isn't explicitly covered. Zerodha would also need a second broker adapter, instrument-ID mapping, and a fragile daily password + TOTP login.
**Decision:** The spouse opens a Dhan account for algo trading; her Zerodha account stays for manual long-term investing, outside the platform (no static IP needed, since the rule applies only to API orders). Family static IP mapping is requested by email from the registered address to apihelp@dhan.co, CC'ing her registered email.
**Trade-off:** broker concentration — a Dhan outage affects both algo accounts; exchange-side catastrophic SLs still protect open positions.
**Revisit when:** a second broker is genuinely needed (D-023 keeps that possible).

## D-023 — Market data separated from execution
**Status:** Accepted (2026-10-25)
**Context:** Prices are exchange prices, independent of broker, so one feed can serve positions at any broker.
**Decision:** `MarketDataProvider` (ticks, instrument master) is a separate interface from `BrokerAdapter` (orders, positions), with a common instrument key (exchange + segment + exchange token). Both are implemented by Dhan today.
**Why now:** cheap at Stage 1; keeps a second broker or the Rust execution engine swappable later without touching market data.

## D-024 — Shared telemetry in `observability`, `contracts` stays pure
**Status:** Accepted (2026-10-25)
**Context:** Engine and api both need structured logging, secret masking and tracing setup. `api` must not import `engine`, so the shared code needs a neutral home. Putting it in `contracts` would turn a package of pure message models into a grab bag.
**Decision:** Shared runtime telemetry (logging setup, masking, tracing, the startup banner) lives in a new top-level package `src/observability/`. `contracts` keeps only messages and shared types (including `DiagnosticPing` / `DiagnosticPong`). Import rules, enforced by `import-linter`: `observability` must not import `engine`, `api` or `cli`; `contracts` must not import `observability` (or any other internal package). Extends the layout in D-018.
**Alternatives:** keep it in `contracts` (blurs its purpose); duplicate it in `engine` and `api` (two masking implementations that can drift, which is a security risk).
**Revisit when:** a third kind of shared runtime code appears that is not telemetry; then consider a more general shared package.

## D-025 — Pluggable trailing-SL method, chosen per trade
**Status:** Proposed (2026-10-25); to be confirmed in the Stage 4 change `add-trailing-exits`.
**Context:** No single trailing method suits every situation: a steady trend wants a wide, volatility-based stop (ATR Chandelier, Supertrend), a quick intraday move wants a tight one (5-candle low, Parabolic SAR), a small target wants breakeven early (step trail). The PRD only planned "points / % / ATR-based later". The owner's catalogue and decision guide are in [`trailing-sl.md`](trailing-sl.md).
**Decision:** The trailing SL is a pluggable method behind one interface, with eight methods planned: ATR Chandelier, N-candle lowest low, Supertrend, step trail, Parabolic SAR, Nth-candle-back low, EMA close trail, fixed % / fixed points. A default method and parameters are configuration (stored and edited like risk limits); the trader may pick another method and its parameters per trade at entry (developer CLI until the Stage 6 trade panel); strategies (Stage 9+) send their own. Every method obeys the same rules: tighten only; never looser than the initial stop used for position sizing; the trail stays in the engine with no per-tick broker changes (D-006); exit before cancel (D-007); method, parameters, level and internal state are persisted. Changing the method on an open position is allowed only if the new stop is at least as tight. The engine never chooses a method by itself.
**Consequence:** seven of the eight methods need candles and indicators (ATR, EMA), so Stage 1 market data must build candles from ticks and load recent history to warm indicators up at entry.
**Alternatives:** one global method (cannot match the method to the situation); free-form per-strategy code for trailing (untestable, and bypasses the shared tighten-only and restart rules).
**Revisit when:** backtests show some methods add nothing for NSE F&O intraday; drop them rather than carry untested options. The scores in `trailing-sl.md` are hypotheses until then.

## D-026 — Pluggable risk checks with monotonic state; equity-floor ratchet
**Status:** Proposed (2026-11-11); to be confirmed in the Stage 3 change `add-risk-engine`.
**Context:** The PRD's §7 listed rules but gave no shape for adding more, and "daily max loss" was a single static number. The RMS research ([`research/rms-tool.md`](research/rms-tool.md) R2) shows that five vendor settings, Max Loss, Trail Max Loss, Lock Minimum Profit, Trail Profits and Lock & Trail, are all versions of one number that only rises. It also shows that vendor tools lose that state on restart.
**Decision:**
- **One interface.** Every risk rule implements one `RiskCheck` protocol with a pre-trade phase, a continuous phase, or both. Each returns a decision ranked by severity (ALLOW < REJECT < HALT_NEW_ENTRIES < TARGET_HALT < KILL), and the gate acts on the most severe. A check that raises fails closed.
- **Equity floor.** The daily max loss becomes a per-account, per-day **equity floor** that never decreases, defined in PRD §7.1.
- **Monotonic state.** Ratchet state (floor, peak MTM, halt state, day counters) is engine-owned and never editable from the UI. It is persisted *before* the engine acts, with `GREATEST(existing, new)` writes, and restored on restart as `max(persisted, recomputed)`.
- **Declared tightening direction.** Every parameter declares its tightening direction, which makes D-012's "tighten-only during market hours" mechanical. A change is also rejected if it would lower the floor.
- **No loss debounce.** The kill switch on the loss side is never debounced. Only the profit target may require consecutive confirmations.
**Alternatives:** keep separate max-loss / trail / lock rules (five interacting thresholds, hard to prove monotonic); vendor-style in-memory rules that reset on restart (TradeMate does this on purpose; unsafe for automation).
**Revisit when:** strategy- or household-scope floors (S8–S10) need a scope model beyond per account.

## D-027 — Exits as bounded limit-price chases, never MARKET
**Status:** Proposed (2026-11-11); to be confirmed in the Stage 4 change `add-trailing-exits` and verified against Dhan in Stage 7.
**Context:** Since SEBI's retail-algo framework, Dhan converts API market orders into limit orders with Market Price Protection (MPP), a cap near LTP. These can sit unfilled on a fast or illiquid strike (research: [`research/rms-tool.md`](research/rms-tool.md) R10, Part D — *verify*). The PRD's exit flow assumed "market / marketable limit". This refines D-007 (exit before cancel still holds) and stays within D-006 (no per-tick modification).
**Decision:**
- **Limit orders only.** Exits are placed as aggressive LIMIT orders at LTP ± a buffer.
- **Bounded chase.** The engine re-prices an exit every `chase_interval_ms`, at most `max_chase_steps` times. That is always fewer than the per-order modification cap, which is below Dhan's 25. It never re-prices beyond `max_exit_slippage_pct`.
- **Escalation.** If a kill leaves the book unflattened after `exit_escalate_s`, the engine calls Dhan's Exit All and sends a critical alert.
- **Leg order.** Short option legs are exited before their hedges.
- **No MARKET orders.** The engine never sends a MARKET order.
- **Parameters.** All of the above are configuration; the research's values are examples only.
**Open:** whether the catastrophic SL can stay an SL-M under MPP (PRD §12 Q17). If not, a further entry refines D-007.
**Alternatives:** MARKET exits (converted to MPP limits anyway, with no control over the price); unbounded chasing (breaches the modification cap and rate limits).

## D-028 — Continuous detection of external orders
**Status:** Proposed (2026-11-11); groundwork in the Stage 2 change `add-dhan-readonly`, policy in Stage 7 `add-live-trading`.
**Context:** Reconciliation (PRD §6.5) catches drift only at startup. A manual order or exit from the Dhan app during the day goes unseen. In particular, a manual exit leaves the engine's catastrophic SL resting on a flat book, where it would *open* a new position if it triggered. Dhan's order-update WebSocket reports orders from every platform with a `Source` field (research: [`research/rms-tool.md`](research/rms-tool.md) R7 — *verify*).
**Decision:**
- **What counts as external.** An order is external if its broker order ID is unknown to the engine or its source isn't the API. Detection uses the order-update feed plus a position poll.
- **Drift.** Net-quantity drift that lasts beyond a grace period raises the account to HALT_NEW_ENTRIES.
- **Orphan SL first.** When an engine position turns flat because of an external exit, its catastrophic SL is cancelled **before any other action**.
- **Policy.** Other handling follows a configured `external_order_policy` (PRD §12 Q14).
- **Audit.** Every external order is audited.
**Alternatives:** detect at startup only (the current state; leaves orphan SLs live all day); forbid manual trading by policy alone (unenforceable).

## D-029 — Spouse's Zerodha account in Stage 8; market data stays on Dhan
**Status:** Proposed (2026-11-12); supersedes D-022. To be confirmed in the Stage 8 change.
**Context:** The owner decided that phase 1 (S0–S7) runs the owner's Dhan account only, and phase 2 (S8) adds the spouse's **existing Zerodha account**, not a new Dhan account as D-022 planned. SentinelX is for personal and family use only, not commercial.
**Decision:**
- **Phase 1 unchanged:** Dhan only; no Zerodha code before Stage 8.
- **S8 adds a `ZerodhaBroker` adapter (Kite Connect)** behind the existing `BrokerAdapter` protocol, used for **execution** on the spouse's account: orders, order updates, positions.
- **Market data stays on the Dhan feed** for both accounts (D-023). This needs a mapping from the common instrument key to Zerodha's instrument identifiers.
- **Broker-specific features become optional capabilities.** Dhan's P&L exit, kill switch and Exit All are modelled as optional capabilities of a broker adapter, not as core steps. Where a broker lacks one (Zerodha appears to have no API kill switch or P&L exit — *verify*), the engine's own lock still applies and the owner is alerted to use the broker's manual control.
- **Only `engine.broker`** may import `dhanhq` or `kiteconnect`.
**Static IP (PRD §12 Q7):** each broker registers the static IP for its own client, so the same VPS IP serves the owner's Dhan app and the spouse's Kite Connect app, with no family mapping needed. D-022's IP concern applied only to two accounts at one broker. If a second Dhan account is ever opened, Dhan's family static IP mapping is decided then.
**Open:** Zerodha's daily login (Kite Connect access tokens expire daily and need an interactive login) and its API subscription cost.
**Trade-off:** a second adapter, instrument mapping and a second daily login, in exchange for no broker concentration (a Dhan outage no longer affects both accounts) and no new account for the spouse.

## D-030 — Automatic stops lock the account for the day
**Status:** Proposed (2026-11-12); to be confirmed in Stage 3 (engine lock) and Stage 7 (broker lock).
**Context:** The owner wants the day's rules to act as an emotional circuit breaker: once a rule stops trading, nothing, including the owner, can trade that account until the next day. The engine's HALTED state only blocks orders that go *through SentinelX*; the owner could still trade from the broker's own app.
**Decision:**
- **Automatic stops** (equity-floor breach, daily profit target) halt the account until the post-close reset. **No intraday resume command exists for a risk halt.** The recovery-halt resume (reconciliation) is a separate command and never clears a risk halt.
- **Broker lock after flat:** once the book is flat and no orders are pending, the engine activates the broker's own kill switch (Dhan: `POST /killswitch`), so the broker app is locked too.
- **Manual kills:** a manual kill (dashboard or Telegram) activates the broker lock only after a second confirmation, so a mistaken kill doesn't lock the day.
- **Limits stay tighten-only** during market hours (D-012); they aren't fully frozen.
- **Brokers without a lock API:** for a broker with no lock API (Zerodha, *verify*), the engine lock applies and an alert asks the owner to activate the broker-side lock manually.
**Alternatives:** engine lock only (bypassable from the broker app); lock on loss stops only (the owner chose both).
**Risk:** Dhan's kill switch can't be manually undone after a second activation in a day (*verify*). That is accepted as the point of the rule.

## D-031 — Webhook ingress: private for own scripts, one narrow public route for TradingView
**Status:** Proposed (2026-11-12); refines D-016; to be confirmed in Stage 9.
**Context:** The owner wants both their own scripts and TradingView alerts in Stage 9, without exposing the server to random traffic. TradingView sends only from published public IPs to ports 80/443 and can't reach a Tailscale address.
**Decision:**
- **Own scripts** post over **Tailscale** to a private webhook route, authenticated with an HMAC signature, a per-strategy token and a `request_id` idempotency key.
- **TradingView** posts over HTTPS to **one public route**, `/hook/tv/<strategy>/<token>`. The VPS firewall accepts public 443 traffic **only from TradingView's published IPs** and drops everything else before Caddy. Caddy serves nothing else publicly, and rate-limits the route.
- **Tokens.** Each URL token is per strategy and revocable by regeneration. TradingView can't sign its requests, so the token, the IP allowlist and idempotency together authenticate it.
- **Fast acknowledgement.** Both routes acknowledge quickly and process asynchronously; TradingView cancels after 3 s.
- **Dashboard and API stay Tailscale-only** (D-016 unchanged for them).
**Alternatives:** Tailscale only (TradingView impossible); a third-party tunnel or relay (another dependency that sees the payloads); a fully public endpoint (random traffic and scanning).
**Revisit when:** TradingView changes its source IPs (the allowlist must be kept current), or a signal source can join the tailnet.
