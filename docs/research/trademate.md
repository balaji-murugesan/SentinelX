> **External research — unverified hypotheses.** Saved verbatim on 2026-11-11 as a reference for [`PRD.md`](../../PRD.md). Vendor facts come from vendor marketing pages; the SentinelX recommendations are the research author's suggestions. Per [`CLAUDE.md`](../../CLAUDE.md), never treat its numbers as defaults or as measured results.

# TradeMate Reference Spec for Sentinel

Nov 11, 2026 · @Balaji

> Scope: a teardown of MyAlgoMate's TradeMate, built from its product page and user guide (updated 11 Nov 2026), plus the SEBI retail-algo rules in force since 1 Apr 2026. It ends with a spec for Sentinel. Assumption: Sentinel v1 is manual one-click execution with automated risk controls. Strategy automation comes later. Not verified: user reviews, community feedback, and real-world latency or reliability.

## 1. Executive summary

TradeMate is a Windows desktop app for **manual, discretionary options trading** on index options (NSE and BSE) and MCX commodity options. It isn't a strategy engine. The trader chooses every entry. TradeMate makes that fast (a one-click option chain, keyboard trading, hedged sells) and enforces exits automatically. Exits work at the position level (SL, target, trailing SL), the portfolio level (MTM stop and target) and through rule-based adjustments (rolling bands, price checks, and stops on the ATM straddle premium).

What Sentinel should take from it: a clean split between **discretionary entry** and **mechanical risk and adjustment**, linked hedge legs, smart limit orders with automatic revision, and an "Active Rules" registry that shows everything armed in one place.

What Sentinel should do better: TradeMate keeps rules in memory only, so they are lost on restart. A timed exit is dropped if the app was closed when its time passed. There's no TradingView or webhook input. Sentinel can fix all three.

## 2. Product overview

| Item | TradeMate (confirmed) |
| --- | --- |
| Form factor | Windows desktop app, run on your own PC or a cloud server |
| Instruments | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX, plus MCX options on Gold, Gold Mini, Silver, Silver Mini, Crude Oil, Crude Oil Mini and Natural Gas |
| Multi-expiry | Several expiries per index at once, each with its own rules, up to the plan's chain limit |
| Brokers | "155 brokers" on the brokers page, "more than 130" in the FAQ. Includes Zerodha, Angel One, Upstox, Dhan, Fyers, Kotak Neo, Groww, AliceBlue, Shoonya and XTS brokers (IIFL, Jainam and others) |
| Pricing (excl. GST, per client ID) | ₹23,999/yr (₹2,000/mo), ₹8,499/quarter, ₹3,599/mo. 7-day free trial |
| Licensing | One subscription per trading account (client ID) |
| Modes | Paper trade (simulated in the app, live prices from the broker) and Live (red window border) |
| Prerequisites | Machine that stays on, static IP per account (via the vendor's VTE proxy or cloud server), broker API access |

## 3. Feature inventory (all confirmed from the guide)

### 3.1 Option chain

- Modes: **Straddle** (CE and PE at one strike), **Strangle** (CE and PE equidistant from ATM), **Custom** (up to 20 user-built rows, saved per index).
- Strike window: 0–200 strikes either side of ATM, default 10. ATM is fixed until the user presses ATM ⟳.
- ATM source: spot, index future, or synthetic future (ATM strike + CE − PE).
- Columns per leg: PNL, Qty, Buy, RB, Sell, Exit, Move ½, Move 1, Delta. A Combined band covers straddle and strangle pairs.
- Pills show open long and short quantity on each strike.

### 3.2 Order entry

- **One-click B, S and EXIT** on every leg and on combined pairs. No second confirmation.
- **RB box (command entry):** `50.` buys 50 now, `-50.` sells 50 now, `50@120` arms a buy at 120. An armed RB order is a pending rule.
- **Hedge on sell:** set a distance of 1–99 strikes (default 5). A sell becomes buy-hedge-first, then sell. The hedge stays **linked**: exiting the short exits the hedge, and adding to the short buys more hedge first.
- **Move 1 / Move ½:** shifts all, or half, of the other shorts on that side (same index, product and expiry) to the clicked strike by closing and reopening them.
- **Keyboard trading:** arrow navigation along a user-defined path, with B, S, E and Enter or Space. Off by default and needs confirmation to turn on.
- Quantity is in lots, with a toolbar default per index and per-row overrides. Max qty per order is set per index, so large orders are sliced.

### 3.3 Smart order handling

- Entry and exit orders are **limit orders**. Stop losses are **SL-limit** orders.
- Limit price source: LTP, BestBidAsk, IMFill or AvgBidAsk, plus a buffer (% or points) with a minimum buffer.
- If unfilled: revise the limit every N seconds up to a maximum (default 15 revisions), then wait at the last price. The alternative is "Do nothing and wait".
- Retry rejected orders: on by default, 3 retries, 1 s apart. Optional retry with reduced quantity.
- Product: MIS or NRML on entry. Exits inherit the position's product.

### 3.4 Positions and exits

- Separate Sell, Buy and Closed books. Group, filter, sort, pin columns, export CSV.
- Exit or add by size: 100, 75, 50 or 25%, custom % or custom quantity, rounded to whole lots. Legs smaller than one lot are skipped.
- Quick exit buttons and **timed exits** (24-hour time, today only).
- Roll buttons: Away, Away 2, Close, Close 2, Move To. Each shows the new strike and price before you click.
- Cancel All cancels open orders at the broker and clears timed exits.

### 3.5 Risk management

| Layer | Trigger | Actions | Notes |
| --- | --- | --- | --- |
| Per-position SL and target | Points or % from entry | Exit, Add, Away 1–3, Close 1–3 | Quantity: Full, 90, 75, 50 or 25%, or custom. Auto-arm for new Buy or Sell positions |
| Broker-side SL | Same as above | Rests at the broker | The **only** protection that survives the app closing |
| Trailing SL | Every X move, step the SL by Y | Same as above | Ratchets one way only |
| Portfolio MTM (Risk tab) | MTM ≤ stop or ≥ target, across all indices | Exit all, add to all, exit buys, exit sells, exit losers, exit winners | Quantity: Full, 75, 67, 50, 33 or 25%. Optional trailing (e.g. −5000, every 1000, move 500). Stop and target form a pair, so when one fires both are removed |
| ATM straddle premium | Premium ≥ stop or ≤ target | Exit all or add to all for **that index and expiry only** | Example: premium 212, stop 250, target 170 |
| Rolling bands | Index crosses an upper or lower band (spot or synthetic) | Upper cross: roll the CE away and the PE closer (lower cross is the reverse). In All mode, move N strikes | The band re-centres after each roll |
| Price rules | Price Check CE/PE (highest short entry plus offset); Price Difference (CE–PE gap) | Roll to a premium ≤ X, or move N strikes | Maximum adjustments per index (default 10). A failed move removes the rule; other errors pause it for 30 s |
| Validation | A stop must be at least one tick from price, above price for shorts and below for longs | — | An armed stop survives partial exits or adds and is recalculated only after a full close |

**Active Rules panel:** one list of every armed rule across indices (MTM, ATM, rolling, price, timed exits and armed RB orders), with per-rule delete and Delete All.

### 3.6 Monitoring

- Session bar: Spot, Syn Fut, ATM Straddle, Position Value, CE–PE, VIX, MTM (with the day's high and low).
- Index Summary: an always-on-top window with one row per index plus a total (MTM, decay, delta, net quantity).
- MTM history: intervals from 1 s to 1 h, with SMA, EMA, WMA or Bollinger overlays and CSV export.
- Payoff graph: payoff at expiry and today, break-evens, ±1σ and ±2σ bands, chance of profit, Delta, Theta and Vega, what-if sliders for date and IV (±20%), and per-leg exclusion. Refreshes every 2 s.
- Telegram alerts through the user's own bot, with optional live MTM every N minutes (skipped when the market is closed or MTM is zero).
- Status bar: BROKER and FEED health lights, with a "FEED RECONNECTING" state.

### 3.7 Safety and UX guardrails

- A risk agreement must be accepted at every login before any order can go out. There's no Start button.
- Nine destructive actions ask for confirmation by default (quick exits, cancel all, turning on keyboard orders and others).
- Live mode shows a red border. Switching modes needs a restart and a new sign-in.
- An index can't be turned off while it has an open position or an armed rule.
- Some updates are mandatory ("Update Required") and block use until installed.

## 4. Core user workflow

1. Sign in with the vendor account (licence check).
2. Add the broker account and log in with credentials plus the TOTP secret. Contracts download and the feed connects.
3. Configure the static IP proxy and register that IP with the broker.
4. Turn on indices, choose expiries, set strike gap and max quantity per order.
5. Accept the risk agreement. Each expiry becomes ready on its own.
6. Pick a strike on the chain and press B or S (optionally with a hedge).
7. SL and target auto-arm, and Risk, ATM, Rolling and Price rules are added as needed.
8. Monitor through the session bar, payoff graph and Telegram.
9. Exit manually, by timed exit, or when a rule fires.

## 5. Broker integration and market data (confirmed vs inferred)

- **Confirmed:** broker credentials and TOTP secrets are stored locally on the PC. Logins are automated through the TOTP secret. Per-broker field sets differ (Dhan: mobile, PIN, password, TOTP, API key and secret; Kotak Neo: mobile, TOTP, API key, MPIN; XTS: separate Market and Interactive API keys). The client ID can be changed at most once a day.
- **Confirmed:** live prices come from the broker's feed, even in paper mode. An expiry slot frees only after a restart on most brokers, which suggests feed subscriptions aren't cleanly torn down.
- **Inferred:** a broker-adapter layer that normalises 130+ broker APIs into one order, position and feed interface. This is the main source of TradeMate's moat and complexity. Sentinel doesn't need it.
- **Inferred:** a .NET or WPF-style Windows client (installer, admin rights, always-on-top windows, a theme toggle). Not verified.

## 6. Regulatory context (SEBI retail algo framework)

- SEBI's circular of 4 Feb 2025 ("Safer participation of retail investors in algorithmic trading") became mandatory for all brokers on **1 April 2026**, after phased extensions.
- **Static IP:** orders are accepted only from an API key or app mapped to a whitelisted static IP. A home dynamic IP won't work. NSE allows the IP to change at most once a calendar week. A static IP can be shared within the immediate family with consent.
- **10 OPS threshold:** self-developed algos need exchange registration only above **10 orders per second per exchange per client**. Below that, orders are still tagged as algo orders with a generic Algo ID.
- **Market orders via API:** some brokers (e.g. Fyers) convert them to market-price-protection orders. Plan on limit orders, as TradeMate already does.
- **Other:** OAuth or 2FA authentication is mandatory, retail algos must be hosted on Indian servers, and a kill switch is expected.

**What this means for Sentinel:** a personal tool for your own account, kept under 10 OPS, needs no exchange registration. You still need a static IP registered with your broker (a Mumbai-region VPS covers this and Indian hosting together), a hard OPS throttle in code, and a kill switch. Confirm the details with your broker's current API terms.

## 7. Gaps and limitations in TradeMate (from its own guide)

1. **Rules are session-only.** Restarting wipes every armed rule, auto-arm setting and adjustment counter. Only broker-side SLs survive.
2. **Timed exits are dropped** if the app was closed when their time passed.
3. Targets and trailing logic run only inside the app, so a PC or network failure leaves positions unmanaged.
4. No TradingView or webhook signal input, and no strategy automation (the guide doesn't mention either).
5. Windows only. No web or mobile monitoring.
6. Expiry slots need a restart to free up.
7. Per-client-ID pricing makes multiple accounts expensive.
8. **Unverified:** user complaints, latency and fill quality. The deeper research run can cover these.

## 8. Sentinel functional requirements (MoSCoW)

Items marked **\[TM\]** are adapted from TradeMate. Items marked **\[+\]** improve on it.

### Must have (MVP)

- F1 Broker adapter interface with **one** broker first (e.g. Dhan, Zerodha or Fyers), including an automated TOTP login. \[TM\]
- F2 Instrument master download and an expiry and strike resolver (ATM from spot, future or synthetic). \[TM\]
- F3 Websocket feed with reconnects and a health indicator (BROKER and FEED lights). \[TM\]
- F4 One-click and hotkey order entry: buy, sell or exit by strike offset (ATM ± N), lots and expiry. \[TM\]
- F5 Smart limit orders: price source, buffer, auto-revise every N s up to a maximum, then wait. \[TM\]
- F6 Order slicing by max lots per order, and retrying rejections with backoff. \[TM\]
- F7 Per-position SL, target and trailing SL, with an option to place the SL at the broker. \[TM\]
- F8 Portfolio MTM stop and target with trailing, plus actions (exit all, exit losers and the like). \[TM\]
- F9 **Kill switch:** cancel all orders, flatten all positions, block new orders for the day. Also triggered by a daily max loss. \[+\] (SEBI expectation)
- F10 **Persistent rule store** (SQLite) that reloads armed rules and timed exits on restart and reconciles with broker positions. \[+\]
- F11 Timed exits that **catch up on missed times** at startup (exit immediately if the time has passed and the position is still open). \[+\]
- F12 Paper-trading mode using the same engine, a simulated fill model and a clear LIVE banner. \[TM\]
- F13 OPS throttle (token bucket capped well below 10/s, e.g. 5/s) and a static-IP check at startup. \[+\]
- F14 Append-only audit log of every signal, order, modification, fill and rule trigger. \[+\]
- F15 Telegram alerts for fills, rule triggers, errors and periodic MTM. \[TM\]

### Should have

- S1 Linked hedge-on-sell (buy N strikes out first, auto-exit the hedge with its short). \[TM\]
- S2 Roll actions: Away, Close, Move To, and Move all shorts on one side. \[TM\]
- S3 ATM straddle premium stop and target per index and expiry. \[TM\]
- S4 Active Rules view with per-rule disarm. \[TM\]
- S5 Pre-trade validator (margin check, max lots, max open legs, no-trade windows, expiry-day rules). Your existing F&O Trade Validator could plug in here. \[+\]
- S6 Mobile or web monitoring view (read-only first) so you don't depend on one desktop. \[+\]
- S7 Telegram commands: /mtm, /positions, /kill. \[+\]

### Could have

- C1 Rolling-band adjustments and price-check or price-difference rules. \[TM\]
- C2 Payoff graph with Greeks and what-if sliders. \[TM\]
- C3 MTM history chart with indicators. \[TM\]
- C4 TradingView webhook or Telegram-tip ingestion that **suggests** a trade for one-tap confirmation. \[+\]
- C5 Trade journal and P&L analytics (Bold BI or a local dashboard). \[+\]
- C6 A second broker adapter. \[TM\]

### Won't have (for now)

- Support for 100+ brokers, multi-user licensing, copy trading, and fully automated black-box strategies (these would need registration or an RA licence if ever offered to others).

## 9. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Latency | Keypress to order sent < 100 ms in-process. Rule evaluation within 1 tick of a price update |
| Reliability | Feed auto-reconnect with exponential backoff. On reconnect, reconcile orders and positions with the broker before rules resume. Broker-side SL as the backstop for every short |
| Persistence | Rules, timed exits and adjustment counts survive restarts. State is reconciled against broker positions at startup |
| Safety | Daily max loss, max lots per order, max open positions, OPS throttle, confirmation on destructive bulk actions, kill switch reachable from UI, hotkey and Telegram |
| Security | Secrets (API key and secret, TOTP seed) in the OS keyring or an encrypted file, never in the repo. The UI binds to localhost or is behind auth |
| Compliance | Static IP registered with the broker. Hosted in India (Mumbai region). < 10 OPS. Limit orders only |
| Observability | Structured JSON logs, an audit table, a /health endpoint, Telegram alerts on errors |
| Testability | A broker simulator plus replay of recorded ticks, used for rule engine tests and paper mode |

## 10. Suggested architecture (Python)

```
┌────────────── UI (web: FastAPI + HTMX/React, or desktop: PySide6) ──────────────┐
│ Option chain · Hotkeys · Positions · Active Rules · Kill switch · Paper/LIVE   │
└───────────────▲─────────────────────────────────────────────▲──────────────────┘
                │ REST/WebSocket                               │
┌───────────────┴──────────── Core service (asyncio) ──────────┴──────────────────┐
│ MarketData  → TickBus ─► RuleEngine (SL/TGT/TSL/MTM/ATM/Rolling/Timed)        │
│ OrderManager (slicing, smart-limit revise, retry, OPS throttle, hedge linking) │
│ RiskGate (pre-trade checks, daily loss, kill switch)                           │
│ PositionBook (reconcile w/ broker)  · Notifier (Telegram) · AuditLog           │
└───────────────▲─────────────────────────────────────────────▲──────────────────┘
                │ BrokerAdapter interface                       │ SQLite / Postgres
        ┌───────┴────────┐  ┌──────────────┐              rules, orders, fills, audit
        │ DhanAdapter    │  │ PaperAdapter │
        └────────────────┘  └──────────────┘
Deploy: Docker on a Mumbai VPS with a static IP (registered with broker)
```

Key design choices:

- **One engine, two adapters.** Paper and live share every code path except the adapter, which is why TradeMate's paper mode is trustworthy.
- **Rules are data.** Each rule is a persisted row (type, scope, params, state, armed\_at). The engine loads them at startup and checks them on every tick.
- **Every order goes through RiskGate**, including orders that rules generate, so a runaway adjustment loop can't exceed the limits.
- This builds on your existing one-click execution app and Mumbai hosting plan instead of starting over.

## 11. Data models (starter)

```python
Instrument(token, symbol, underlying, expiry, strike, opt_type, lot_size, tick_size, exchange)
Order(id, broker_order_id, instrument, side, qty, order_type, limit_price, trigger_price,
      product, status, revisions, retries, parent_id, link_group, source, created_at)
Fill(order_id, qty, price, ts)
Position(instrument, product, net_qty, avg_price, realized_pnl, mtm, link_group)
Rule(id, type, scope{index,expiry,position_id|ALL}, params{json}, state{json},
     action, qty_pct, armed, created_at, fired_at, max_adjustments, adj_count)
  # type ∈ SL, TARGET, TSL, MTM_SL, MTM_TGT, ATM_PREMIUM, ROLLING_BAND,
  #        PRICE_CHECK, PRICE_DIFF, TIMED_EXIT, ARMED_ENTRY
RiskLimits(daily_max_loss, max_lots_per_order, max_open_legs, max_ops, trading_window)
AuditEvent(ts, kind, ref_id, payload{json})
Session(date, mode{PAPER|LIVE}, kill_switch, static_ip, broker, started_at)
```

The `link_group` field ties a hedge to its short (TradeMate's linked hedge). `parent_id` tracks slices and revisions of one logical order.

## 12. Phased roadmap

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| 0. Foundations (1 wk) | Broker adapter (login, instruments, feed, orders), audit log, config and secrets | Paper and live sessions connect. Feed is stable for a full session |
| 1. MVP execution (2 wks) | F4–F6, F12, F13: chain view, hotkeys, smart limits, slicing, paper mode | 50 paper trades with no unhandled errors |
| 2. Risk core (2 wks) | F7–F11, F14, F15: SL, TSL, MTM rules, kill switch, persistence, timed-exit catch-up, Telegram | Restart mid-session restores all rules. The kill switch flattens everything in < 3 s on paper |
| 3. Live pilot (1 wk) | Mumbai VPS with static IP, 1-lot live trading, broker-side SL on every short | Two weeks live at 1 lot with zero rule misfires |
| 4. Option-seller tools | S1–S5: linked hedges, rolls, ATM premium rules, validator | — |
| 5. Extras | C1–C5: rolling and price rules, payoff and Greeks, signal ingestion, journal | — |

## 13. Sources

- [MyAlgoMate – TradeMate product page](https://myalgomate.com/products/trademate)
- [MyAlgoMate – TradeMate user guide](https://myalgomate.com/guide/trademate)
- [Fyers – SEBI rules for retail algo trading from 1 Apr 2026](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)
- [Kotak Neo – Static IP whitelisting](https://www.kotakneo.com/platform/kotak-neo-trade-api/static-ip-details/)
- [Angel One – SEBI algo trading rules](https://www.angelone.in/knowledge-center/online-share-trading/sebi-algo-trading-rules)
- [Business Standard – NSE algo compliance standards](https://www.business-standard.com/markets/news/nse-issues-algo-trading-compliance-standards-retail-safety-125050501327_1.html)
- [Finseclaw – NSE implementation standards analysis](https://www.finseclaw.com/article/nse-releases-implementation-standards-for-retail-participation-in-algorithmic-trading)
- [AlgoBulls – SEBI algo regulations 2025–26](https://algobulls.com/blog/industry-insights-and-updates/sebi-new-algotrading-regulations-for-retail-investors)
- [Alice Blue – What changed from April 2026](https://aliceblueonline.com/blog/sebi-algo-trading-rules-for-retail-traders-what-changed-from-april-2026)
