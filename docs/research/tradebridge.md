> **External research — unverified hypotheses.** Saved verbatim on 2026-11-11 as a reference for [`PRD.md`](../../PRD.md). Vendor facts come from vendor marketing pages; the SentinelX recommendations are the research author's suggestions. Per [`CLAUDE.md`](../../CLAUDE.md), never treat its numbers as defaults or as measured results.

# TradeBridge (MyAlgoMate): Product & Technical Spec, Plus a Blueprint for Sentinel

TradeBridge is a Windows desktop app that runs on a cloud machine. It turns webhook signals from Chartink, TradingView or your own code (JSON over HTTP) into broker orders through each broker's official API.\[1\] What makes it worth copying is not the webhook relay, which is a commodity. It is the execution layer behind it: strikes resolved live, up to 8 legs per side, risk limits at strategy, account and global level, limit-order chasing, paper mode, and multi-account multipliers.\[1\] Those are the ideas Sentinel should adopt.

## TL;DR

- **What it is:** TradeBridge is MyAlgoMate's execution-only bridge. It takes Chartink scanners, TradingView alerts or a Custom JSON API with five actions, and turns them into orders across every linked broker account, each sized by its own multiplier. It covers NSE/BSE index options (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX), single-stock F&O, equity and seven MCX commodities, and lists 149 supported brokers. The list price runs from ₹1,667/month per client ID on the annual plan up to ₹2,499/month on the monthly plan, ex-GST, with a 7-day trial.
- **What's verifiable vs not:** Every feature, setting name and price below comes from MyAlgoMate's own product page, dated 6 November 2026. Several things are not public: the exact JSON payload schemas, the webhook URL/port format, latency figures, and order-type support beyond limit-based orders with broker-side SL. I found no independent user reviews on Reddit, Traderji or TradingView. The 9 on-site reviews are vendor-moderated and similarly worded.
- **What to build in Sentinel:** Start with a Python (FastAPI) webhook receiver that acknowledges within three seconds, because TradingView's Help Center says "If a remote server takes longer than three seconds to process a request, the request will be cancelled." Add token-per-URL auth plus IP allowlisting, an idempotent signal queue, a live strike resolver (ATM±points, closest premium/delta), a three-scope risk engine with Exit Time and EXIT ALL, paper mode first, and Telegram notifications. Leave multi-account multipliers, multi-leg hedging and limit-chasing for phase 2.

---

## 1. Product overview

**What it is.** In MyAlgoMate's words, TradeBridge "is execution software: it turns trading signals from Chartink scanners, TradingView alerts or your own code into live orders at your broker. It picks the contract when the signal arrives, places the orders and manages the exits on every broker account you link, each sized by its own multiplier."\[1\]

**Problem solved.** Charting and scanner platforms can fire alerts, but they can't place orders at Indian brokers,\[2\] and turning an alert like "NIFTY crossed X" into "buy the 24,500 CE of the nearest expiry, 2 lots, hedged, with a trailing SL" is the hard part. TradeBridge sits in that gap.

**Positioning.** It is explicitly execution-only and non-advisory: "TradeBridge does not generate signals — what to trade is always your decision. MyAlgoMate provides technology only, not investment advice."\[1\] Historical backtesting is outside the product. AlgoTest's comparison article describes historical strategy research as "a separate step outside TradeBridge" and calls TradeBridge "Best for Detailed Execution Controls."\[3\]

**Target users.** No-code Chartink and TradingView users who paste a URL, coders who send JSON from "Python, C++, Java, Node, Excel VBA", and people running several accounts (family or multiple brokers).\[1\]

**Vendor.** Myalgomate Technologies LLP is based in Rajkot, Gujarat,\[4\] and says it has been live since 2020. It describes itself as "a technology provider and does not act as an investment adviser, research analyst or portfolio manager."\[5\] A business directory (IndiaMart) lists its GST registration date as 18 June 2022 and its turnover band as ₹40 L–1.5 Cr, so it is a small firm.\[4\] Sister products include TradeMate (option-chain trading), Copy Trading Software, an RMS Tool, a Cloud Server and a "Virtual Trading Environment" (static IP).\[6\]\[7\]

## 2. Full feature list (as published)

| Area | Feature | Details (vendor-stated) |
|---|---|---|
| Signals | Chartink integration | Paste a URL into the Chartink alert's Webhook field. One alert carries every symbol the scan returned and all of them are entered. Each symbol is looked up on NSE and falls back to BSE. A symbol that already has an open position is skipped. Entry and exit come from separate scanners. Equity only.\[1\] |
| Signals | TradingView integration | Webhook URL plus a short JSON message using `{{ticker}}` and `{{exchange}}`. The bare `{{ticker}}` is required; `EXCHANGE:TICKER` will not resolve. Watchlist alerts send one call per symbol. "Once per bar close" is the recommended trigger, with open-ended expiry. Pine Script indicators are supported.\[1\] |
| Signals | Custom JSON API | Five actions: `STRATEGY_ENTRY`, `STRATEGY_EXIT`, `STRATEGY_EXIT_ALL`, `LEG_EXIT`, `LEG_PARTIAL_EXIT`. Entry states each leg outright: exchange, instrument type, symbol, expiry, strike, option type, side, quantity in lots, hedge config, SL and target. "Many legs, many strategies, one call."\[1\] |
| Signals | Google Sheets | Uses the Custom API, with the JSON copied from an in-app **SAMPLE PAYLOADS** button. Needs ngrok. Timer granularity is about 1 minute and Sheets prices are delayed.\[1\] |
| Signals | Entry/exit URLs | Two URLs per strategy, or four with long and short: LONG ENTRY/EXIT and SHORT ENTRY/EXIT. Each URL encodes the integration, strategy name and signal type, and ends in a unique token. Regenerating a URL kills the old one immediately. Public IP or Ngrok can be chosen per strategy.\[1\] |
| Signals | Reversal systems | "Stop and reverse" lets two entry URLs keep the strategy always in the market. A URL can be shared, so one strategy's entry can be another's exit. The close is fired before the open but is not waited for.\[1\] |
| Signals | Trading window | Start Time, Stop Entry Time and Exit Time. Defaults are 15:00/15:15 for equity and indices and 23:00/23:15 for MCX. Day/DTE filters restrict weekdays or days-to-expiry. Trade Type is intraday, positional or STBT.\[1\] |
| Accounts | Multi-account | One signal runs on all linked accounts in parallel. The grid shows login state, funds, margin, risk caps and multiplier.\[1\] |
| Accounts | Groups & multipliers | A per-account multiplier (e.g. 2 doubles every leg). Groups are managed in Group Account Manager, and portfolios can point at a group.\[1\] |
| Accounts | Auto Login & templates | Auto Login on startup, Login All Accounts, and Save/Load Account Template. Accounts "must be logged in **before** the session starts."\[1\] |
| Strategy | Multi-leg builder | Up to 8 legs per side. Leg types are CE, PE, FUT and EQ. Underlyings are indices, stock F&O or MCX. Pricing reference is spot, future or synthetic future. Expiry is nearest or next. Product is MIS or NRML.\[1\] |
| Strategy | Strike selection | Seven modes: ATM Points, ATM %, Direct Strike, Closest Premium, Closest Delta, CP Strikes based on SP, Straddle Width. Rounding can be towards OTM, towards ITM or closest. Premium/delta selection has a match tolerance with a fallback of Avoid Entry, Keep waiting, or Wait X seconds.\[1\] |
| Strategy | Hedging | Hedge strike set by ATM Points, Closest Premium, Closest Delta or Fixed Distance. "Hedge legs go in before the main leg."\[1\] |
| Strategy | Position sizing | Fixed lots; capital-based (capital plus min/max lots); or Risk Per Trade, where size comes from the leg's SL and min/max is ignored.\[1\] |
| Strategy | Per-leg exits | Target and SL in %, points or absolute price. Trailing SL in X/Y steps. Exits can reference the leg price or the underlying.\[1\] |
| Strategy | Portfolios | Strategies sit inside portfolios, which point at accounts or groups. A portfolio can be activated, exited, reset, fired manually, or exported/imported.\[1\] |
| Execution | Order profiles | Separate profiles for entry, exit and SL. Limit Price Ref can be LTP, bid/ask, an immediate-fill price or the bid/ask mid. Limit Buffer in % or points. If Not Filled revises the price at a set interval for N revisions. Order Rejection Retry. Place SL in Broker and Trail SL in Broker.\[1\] |
| Execution | Paper Trade | The full pipeline runs with no broker orders. The current mode is always shown on the command bar.\[1\] |
| Execution | Part booking | Whole strategy (proportional), single leg, or the API's `LEG_PARTIAL_EXIT`, in `LOTS` or `PERCENT`.\[1\] |
| Execution | Exit controls | Exit at leg, strategy, portfolio or account level; EXIT ALL on the command bar; and the exit webhook, which closes the whole strategy and allows re-entry later.\[1\] |
| Risk | Global / account / strategy | Max Loss, Max Profit, Frequency, Exit Mode (immediate, after a delay, or after N ticks), Trail Max Loss (X/Y), Protect Profit, and Time Based Exit. All three scopes run at once, "whichever triggers first." Settings can be edited live.\[1\] |
| Risk | Strategy caps | Max Trades per Stock; Max No of Stocks per day.\[1\] |
| Risk | Profit protection | Four modes: Ignore, Lock Min Profits, Trail Profits, and Lock & Trail. Parameters are If Profit Reaches, Lock Min Profit At, Increase In Profit, and Trail Min Profit By.\[1\] |
| Monitoring | Workspace | Seven tabs: Portfolio, Strategies, Legs Detail, Order Book, Position (with LTP, bid, ask, delta), A/C Management, Index Summary (spot, future, synthetic, ATM).\[1\] |
| Monitoring | Grids, MTM chart, logs | Configurable grids that persist between sessions. An MTM Visualizer at global, account, portfolio or strategy level. Application Logs and Execution Logs (which include the reason for anything skipped), with configurable retention.\[1\] |
| Monitoring | Telegram | Bot Token plus Channel Id, Send Telegram Alerts, and optional Live MTM Alerts on a chosen timeframe.\[1\] |
| Monitoring | Excel export | Any grid or all grids, with portfolio and account set-ups exported separately as a backup.\[1\] |
| Platform | Index/commodity settings | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX; GOLD, GOLD M, SILVER, SILVER M, Natural Gas, Crude Oil, Crude Oil M. "An underlying that is switched off will not accept an entry."\[1\] |

## 3. Architecture & how it works

**Signal flow (vendor-described):**

```
Chartink scan / TradingView alert / own code (Python, Excel VBA, Sheets)
        │  HTTP POST (JSON) to strategy-specific URL ending in a unique token
        ▼
[Public IP + port-forward]  or  [ngrok tunnel]
        ▼
TradeBridge.exe on a Windows cloud machine (listener is in-process)
  → match URL → strategy(ies) → check window/day/DTE/underlying enabled
  → resolve contract live from option chain (spot/future/synthetic)
  → size legs (lots/capital/risk) × per-account multiplier
  → place hedge legs first, then main legs, via each broker's official API
  → manage limit revisions, retries, broker-side SL, per-leg & RMS exits
  → grids, logs, MTM chart, Telegram
```

- **Desktop vs cloud.** The app is Windows-only and must run on an always-on machine: "the signal listener lives inside the application." The minimum spec is 1 vCPU, 4 GB RAM and 50 GB HA NVMe. All state (strategies, accounts, history) stays on that machine and is not held on a shared service.\[1\]\[8\] MyAlgoMate also says on X that each user's algo runs on "your own desktop or your own cloud machine… never a machine shared by many users."\[9\]
- **Inconsistency to note.** The FAQ says a cloud server is required, yet the tutorial list includes "Webhook without a VPS: ngrok on your own PC."\[1\] The likely reading is that ngrok on a PC works but is not the supported production setup.
- **Ports (inference).** TradeBridge says "the port you publish is yours to choose."\[1\] TradingView's own documentation says webhooks are accepted only on ports 80 and 443, come from four fixed IPs (52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7) and do not support IPv6.\[10\]\[11\] In practice, then, the published port for TradingView must be 80 or 443, or you go through ngrok's HTTPS.
- **Latency.** No numeric claims are published on the product page. MyAlgoMate says that when orders land after candle close, "The delay is in the signal, not in the execution." TradingView confirms a bar on the first tick of the next one, which is near-instant on liquid contracts and "several seconds" on thin ones. The user guide reportedly has measured examples,\[1\] but I could not access it. The homepage's "Low latency" is marketing copy with no figure attached.\[5\]

## 4. Brokers, exchanges, segments, multi-account

- **Brokers.** The product page says "Works with 149 brokers". Elsewhere the same page says "Most of MyAlgoMate's 150+", the About page says "160+", and TradeMate lists 155. Named examples include Zerodha, Angel One, Upstox, Dhan, Fyers, Kotak Neo, Groww, Alice Blue, R Money and 5paisa,\[1\]\[12\]\[13\] plus a long tail of brokers on XTS, Greeks/GreekSoft, ODIN and Kambala back-ends.
- **Connection model.** Each user brings their own broker API credentials. Examples: Zerodha needs an API Key, API Secret and TOTP key, with the app mapped to the client ID.\[14\] Kotak Neo needs a Consumer Key, Client ID, MPIN and TOTP.\[15\] Dhan needs an API key/secret, and Groww needs a TOTP token and secret.\[16\]\[17\] A static IP must be whitelisted at the broker.\[14\]\[15\]
- **Segments.** Equity (signal-supplied symbols); index options and futures on NSE and BSE; single-stock F&O; and MCX options and futures on 7 commodities. Products are MIS and NRML, with intraday, positional and STBT trade types.\[1\]
- **Multi-account / copy.** Native fan-out to every linked account, with per-account multipliers and groups.\[1\] Separate master-to-child copy trading is a different MyAlgoMate product (Copy Trading Software, from ₹400/account/month annual).\[18\]

## 5. Order types & order management

| Capability | Status |
|---|---|
| Limit orders with smart pricing (LTP / bid-ask / mid / immediate-fill + buffer) | ✅ Published |
| Limit revision until filled, rejection retry | ✅ Published |
| Broker-side SL and broker-side trailing SL | ✅ Published ("Place SL in Broker", "Trail SL in Broker")\[1\] |
| Plain MARKET orders, SL-M, bracket/cover orders | ❓ Not stated on the product page. Do not assume |
| Strike selection ATM/ITM/OTM (via points/%/rounding), premium, delta, straddle width | ✅ 7 modes\[1\] |
| Expiry selection | ✅ Nearest or next (per-underlying expiry settings)\[1\] |
| Lot sizing / position sizing | ✅ Lots, capital, risk-per-trade; × multiplier\[1\] |
| Auto square-off | ✅ Exit Time at strategy, account and global level\[1\] |
| Target / SL / trailing | ✅ Per leg, in %, points or price; referenced to the leg or the underlying\[1\] |
| Re-entry | ⚠️ Partial. The exit webhook "leaves it able to re-enter on a later signal".\[1\] Max Trades per Stock limits repeats. No automatic re-entry-after-SL rule is published |
| Basket orders | ✅ Chartink multi-symbol alerts; multi-leg/multi-strategy JSON in one call\[1\] |

## 6. Signal syntax, payloads, endpoints, authentication

- **Published:** the action names (`STRATEGY_ENTRY`, `STRATEGY_EXIT`, `STRATEGY_EXIT_ALL`, `LEG_EXIT`, `LEG_PARTIAL_EXIT`), the partial-exit units (`LOTS`, `PERCENT`),\[1\] the TradingView placeholders (`{{ticker}}`, `{{exchange}}`) and the URL naming (LONG ENTRY etc.).\[1\]
- **Not published publicly (unverified):** literal JSON key names, a sample TradingView message body, the URL path/port format, and response codes. MyAlgoMate puts these in the in-app SAMPLE PAYLOADS button and the user guide,\[1\] neither of which I could access.\[1\]
- **Authentication model:** a per-URL secret token in the address. Regenerating it revokes the old URL.\[1\] No HMAC signature or IP allowlist is described.
- **Chartink's fixed payload** (useful for Sentinel; set by Chartink, not TradeBridge): `{"stocks": "SEPOWER,ASTEC,...", "trigger_prices": "3.75,541.8,...", "triggered_at": "2:34 pm", "scan_name": "...", "scan_url": "...", "alert_name": "..."}`.\[19\]\[20\] Chartink has since announced custom columns for alert webhooks, with JSON preview and testing.\[21\]

## 7. Risk management controls

- **Three concurrent scopes:** strategy, account and global (all accounts combined). The first to trigger acts.\[1\]
- **Controls:** Max Loss and Max Profit; check Frequency; Exit Mode (immediate, after a delay in seconds, or after N ticks, to avoid spike exits); Trail Max Loss X/Y; Protect Profit in four modes; and Time Based Exit.\[1\]
- **Kill switches:** EXIT ALL on the command bar, plus Exit All per account.\[1\]
- **Throttles:** Max Trades per Stock and Max No of Stocks per day ("A scan returning forty symbols will not open forty positions unless you allow it").\[1\]
- **Time gating:** Start, Stop Entry and Exit times, plus Day/DTE filters. The vendor warns that "Stop Entry Time is not an exit."\[1\]
- **Duplicate filtering:** only symbol-level dedup is described (a Chartink symbol with an open position is skipped).\[1\] No request-ID or idempotency-key scheme is described.
- **Error handling:** order rejection retry with count and interval, limit revision, and broker-resident SL for when the machine dies. Logs record the reason for anything skipped.\[1\]

## 8. UI/UX

The Windows workspace has seven live tabs, an MTM Visualizer window, terminal-style grids (column chooser, filters, freeze, pinned actions, density, light/dark theme), Application and Execution logs, and Excel export.\[1\] **Notifications:** Telegram only. Email, sound and SMS are not mentioned. **Mobile:** no app is mentioned. Remote access is presumably via RDP to the cloud machine, which is an inference, plus Telegram alerts.

## 9. Security, compliance & regulation

- **The rules.** SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, dated 4 February 2025, and NSE's implementation standards of 5 May 2025 require that API orders come from a broker-whitelisted static IP using a unique API key.\[6\] A self-built algo needs exchange registration only above 10 orders per second.\[22\]\[23\]\[24\] SEBI's follow-up circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132 of 30 September 2025 states that "W.e.f. April 01, 2026, algo framework specified in circular dated February 04, 2025 ... will be applicable for all stock brokers." It set broker milestones of 31 October 2025 (apply), 30 November 2025 (register) and a mock session on 3 January 2026. Brokers who missed them were "barred from onboarding new retail clients for API based algo trading framework w.e.f. January 05, 2026." MyAlgoMate's broker pages state compliance has been mandatory "since 1 April 2026." Its Zerodha page adds that the static IP can be changed "only once in 7 days."\[14\] One primary and one backup IP are allowed per app.\[6\]
- **How TradeBridge addresses it.** It uses each broker's official API with your own keys; the static IP is the cloud machine's (MyAlgoMate sells a cloud server and a "Virtual Trading Environment" static IP); and it is positioned as non-advisory.\[1\]\[25\] An older MyAlgoMate blog claims "MyAlgoMate handles empanelment."\[6\] I could not verify MyAlgoMate's NSE empanelment status or any algo-ID tagging, so treat both as unconfirmed.
- **Security gaps (inference).** Credentials (API secret, TOTP seed) are stored on the user's Windows machine. Webhook auth is a bearer-style URL token. Securing the VPS (RDP hardening, firewall) is left to the user.

## 10. Setup & onboarding

1. Provision a Windows cloud machine (min 1 vCPU/4 GB/50 GB) and register its static IP with each broker.\[8\]\[26\]
2. Install TradeBridge and sign in with your MyAlgoMate account.\[26\]
3. Add broker accounts using each broker's API credentials and TOTP.\[26\]
4. Create a portfolio, then create strategies (legs, strikes, exits, window).
5. Copy entry/exit URLs into Chartink or TradingView, or call them from code.\[26\]
6. Run in Paper Trade mode.\[26\]
7. Set risk limits at strategy, account and global level, switch to live, and press START.\[26\]

Prerequisites: a paid TradingView plan (webhooks plus 2FA) or Chartink premium, which the vendor states.\[8\]\[27\]\[28\] The vendor publishes 18 tutorial videos (A–E playlists); its tutorials index says 17.\[1\]\[29\]

## 11. Pricing (product page, 6 Nov 2026; INR, ex-GST, per client ID)

| Plan | List | Diwali offer (code DIWALI2026) |
|---|---|---|
| Annual | ₹1,667/mo | ₹1,333/mo, billed ₹15,999/yr (20% off)\[1\] |
| Quarterly | ₹2,100/mo | ₹1,785/mo, billed ₹5,354/quarter (15% off)\[1\] |
| Monthly | ₹2,499/mo | ₹2,249/mo (10% off)\[1\] |

There is a 7-day free trial, and "one subscription is needed for each trading account (client ID)."\[1\] There are no feature tiers; all plans appear to include everything. Hidden extras: a Windows VPS with static IP, a TradingView or Chartink paid plan, and any broker API fees (e.g., Kite Connect).\[3\]

## 12. Strengths, limitations, complaints

**Strengths (documented):** deep execution controls; live strike resolution; three-scope RMS; broker-side SL survival; paper mode; broad broker coverage; per-account multipliers; per-strategy URL tokens; detailed public docs.

**Limitations:**
- Windows-only, single machine, so the setup is a single point of failure.
- Per-client-ID pricing makes multi-account use expensive (5 accounts ≈ ₹1 L/yr at list).
- Chartink is equity-only and direction-less, so you need one scanner per direction.\[1\]
- Reversal close is not awaited, which creates margin risk.\[1\]
- Accounts can't log in after the session starts.\[1\]
- There is no backtesting.\[3\]
- No published payload schema, latency figures or MARKET/SL-M support.

**Reviews:** 4.9★ from 9 reviews (8 five-star, 1 four-star), all on MyAlgoMate's own moderated site. Most were posted September–November 2026 and share near-identical phrasing ("convenient to connect TradingView or Chartink signals with broker execution"),\[1\] so treat them as low-evidence. I found no independent Reddit, Traderji or TradingView discussion of TradeBridge. Beware name collisions: "TradeBridge" at tradebridge.live is an unrelated TradingView-to-MT5 product.\[30\]

## 13. Competitor comparison

| Tool | Model | Signal sources | Notable | Price (published) |
|---|---|---|---|---|
| **MyAlgoMate TradeBridge** | Windows app on your VPS | Chartink, TradingView, JSON API, Sheets | 7 strike modes, 3-scope RMS, multipliers, 149 brokers | ₹1,667–2,499/mo per client ID + GST\[1\] |
| **AlgoTest Signals** | Hosted web | TradingView, Chartink, Signals AI | Forward-testing, backtest ecosystem; "Different multipliers per broker under one strategy are not currently supported" | ₹1,599 + GST/30 days; ₹6,999/180 days\[31\] |
| **Tradetron** | Hosted web, no-code builder | Own builder + TradingView webhook (key/value via api.tradetron.tech) | Marketplace, backtesting credits | Retail ₹1,200/mo, Retail+ ₹2,500/mo (help center)\[32\] |
| **Algomojo** | Hosted bridge | TradingView, AmiBroker, MetaTrader, Excel, Python | Free for partner-broker accounts | ₹999/mo or ₹5,999/yr otherwise\[33\]\[34\] |
| **OpenAlgo** | Open-source, self-hosted (Flask + React) | TradingView, AmiBroker, Chartink, MT5, Excel, Sheets, N8N, SDKs | AGPL, 35+ broker plugins, Chartink action from scan-name keyword, webhook rate limit default 100/min\[35\] | Free |
| **Quantman** | Hosted | Builder | Listed as empanelled in an OpenAlgo industry survey (secondhand) | Basic ₹1,300/mo billed monthly (₹1,234/mo quarterly, ₹1,084/mo annual; 4 live deployment credits, 250 backtest credits); Premium ₹3,300/mo; Pro ₹5,434/mo quarterly; ex-GST (Quantman "Plans & Pricing Explained," Jan 2026) |

**Takeaway:** TradeBridge's edge is options-specific execution depth (strike modes, hedge-first, multi-scope RMS, multipliers). Its weaknesses are cost per account and Windows lock-in. For a Python developer, OpenAlgo is the closest open reference architecture, and TradeBridge is the better feature checklist.

---

## Ideas to adopt in Sentinel (my recommendations — not MyAlgoMate facts)

### Prioritized feature list

1. **A fast-ack webhook receiver.** Return 200 immediately and process asynchronously. TradingView's Help Center states that "If a remote server takes longer than three seconds to process a request, the request will be cancelled," and that "only ports 80 and 443 are accepted."
2. **Per-URL secret tokens plus an IP allowlist.** Copy TradeBridge's regenerate-to-revoke tokens. Add TradingView's 4 IPs, an optional HMAC for your own code, and a request-ID for idempotency, which TradeBridge doesn't describe.
3. **Paper mode as a first-class switch,** with the same pipeline and a mode badge everywhere, including the Telegram prefix.
4. **A trading window guard:** Start, Stop Entry and Exit Time; Day/DTE filters; and an "underlying enabled" check.
5. **A live strike resolver:** ATM±N strikes, ATM %, closest premium, and closest delta with tolerance plus a fallback (avoid / wait N s).
6. **A three-scope risk engine** (strategy, account, global) with max loss/profit, trail-max-loss X/Y, lock-and-trail profit, exit-after-N-ticks debounce, EXIT ALL, and max trades per symbol and max symbols per day.
7. **A broker-side SL as a safety net,** so the position survives if Sentinel crashes.
8. **Telegram notifier and command bot.** You already have a Telegram watcher, so reuse that stack. Add `/status`, `/exitall` and MTM pushes.
9. **An execution log that includes the reason for skips**, which is TradeBridge's most underrated feature, plus a SQLite journal.
10. **Phase 2:** multi-leg with hedge-first ordering; limit-price chasing with N revisions; part-booking (LOTS/PERCENT); stop-and-reverse; multi-account multipliers; and an MTM chart.

### Suggested module breakdown (Python)

| Module | Responsibility | Reuse from your projects |
|---|---|---|
| `sentinel.ingress` | FastAPI app; `/hook/{source}/{strategy}/{token}`; auth, IP allowlist, ack and enqueue | — |
| `sentinel.normalize` | Chartink/TradingView/Telegram-tip → canonical `Signal` model (pydantic) | Telegram tip watcher becomes a signal source (manual-confirm by default) |
| `sentinel.gate` | Window, DTE, dedup (idempotency key + open-position check), caps | **F&O trade validator** becomes the pre-trade validation step |
| `sentinel.resolver` | Instrument master, expiry, strike modes, lot size | One-click app's symbol logic |
| `sentinel.sizing` | Lots / capital / risk-per-trade × multiplier | — |
| `sentinel.exec` | Broker adapter interface (`place`, `modify`, `cancel`, `positions`), limit chasing, retries, paper broker | One-click execution app |
| `sentinel.risk` | Strategy/account/global loops, profit lock, EXIT ALL | — |
| `sentinel.notify` | Telegram alerts and commands | Telegram watcher |
| `sentinel.store` | SQLite: signals, orders, legs, MTM snapshots, logs | — |
| `sentinel.ui` | Phase 2: simple dashboard (Streamlit/HTMX), or a .NET WPF front-end over REST if preferred | — |

### Proposed webhook payload schema (Sentinel's own; inspired by TradeBridge's five actions)

```json
{
  "version": "1",
  "request_id": "tv-2026-11-11T09:20:00Z-NIFTY-LE",
  "action": "STRATEGY_ENTRY",
  "strategy": "nifty_orb",
  "side": "LONG",
  "mode": "PAPER",
  "source": "tradingview",
  "underlying": {"symbol": "NIFTY", "exchange": "NSE", "ref": "FUTURE"},
  "legs": [
    {
      "id": "L1",
      "instrument": "OPT",
      "option_type": "CE",
      "expiry": "NEAREST",
      "strike": {"mode": "ATM_POINTS", "value": 0, "rounding": "OTM"},
      "txn": "BUY",
      "qty": {"mode": "LOTS", "value": 1},
      "product": "MIS",
      "order": {"type": "LIMIT", "price_ref": "ASK", "buffer_pct": 0.5, "max_revisions": 3},
      "exits": {
        "target": {"unit": "PCT", "value": 30},
        "stoploss": {"unit": "PCT", "value": 20, "in_broker": true},
        "trail": {"x": 10, "y": 5, "unit": "PCT"}
      },
      "hedge": null
    }
  ],
  "risk_override": {"max_loss": 3000},
  "ts": "2026-11-11T09:20:00+05:30"
}
```

Other actions follow the same envelope: `STRATEGY_EXIT {strategy}`, `EXIT_ALL {}`, `LEG_EXIT {strategy, leg_id}`, `LEG_PARTIAL_EXIT {strategy, leg_id, unit: "LOTS"|"PERCENT", value}`. For TradingView, keep the alert message minimal (`{"request_id":"{{timenow}}-{{ticker}}","ticker":"{{ticker}}","exchange":"{{exchange}}","action":"STRATEGY_ENTRY","side":"LONG"}`) and let the strategy config on the Sentinel side supply the legs, which is the same split TradeBridge uses. For Chartink, accept its fixed payload and get direction from the URL (`/long-entry`), as TradeBridge does, or from the scan-name keyword, as OpenAlgo does.\[35\]

### Roadmap

**MVP (2–4 weeks, single account, single broker):**
- Ingress, normalization and the paper broker
- TradingView and Chartink adapters
- Window guard and idempotency
- ATM-points and closest-premium strike modes
- Fixed-lot sizing and MARKET/LIMIT orders with broker SL
- Strategy and account max loss/profit, Exit Time and EXIT ALL
- Telegram alerts and SQLite logs with skip reasons
- Run on a Linux VPS with a static IP registered at your broker; stay well under 10 orders/sec

**Phase 2:**
- Multi-leg with hedge-first ordering
- Closest-delta mode (needs an IV/greeks calc)
- Limit chasing and rejection retry
- Trail SL and profit lock
- Part booking and stop-and-reverse
- Risk-per-trade sizing
- Dashboard and MTM chart

**Phase 3:**
- Multi-account fan-out with multipliers and groups
- Global RMS
- Telegram-tip ingestion with a human-in-the-loop approval button
- Excel/CSV journal export
- A backup static IP and a failover runbook

---

## Caveats

- **Primary-source dependence.** Almost all specs come from MyAlgoMate's marketing and product page as of 6 Nov 2026. The full user guide and the in-app sample payloads were inaccessible, so the payload keys, URL format, latency figures and MARKET/SL-M support are **unverified**.
- **Inconsistent numbers on the vendor site:** brokers (149 / 150+ / 155 / 160+), review count (8 vs 9), and tutorial count (17 vs 18).
- **Regulatory specifics** (OPS threshold, static-IP rules, dates) come from the SEBI circulars, including the primary text of SEBI circular CIR/2025/132, plus vendor pages. CIR/2025/132 records that the start date was deferred from 1 August 2025 to 1 October 2025 (via a 29 July 2025 circular) and then to full applicability on 1 April 2026; NSE relayed it as circular NSE/INVG/70541 on 30 September 2025. Check your own broker's current API terms before going live.
- **Competitor prices** come from each vendor's site or docs and change often. Quantman's figures come from its own January 2026 "Plans & Pricing Explained" blog post.
- **The Sentinel recommendations are my design suggestions,** not features MyAlgoMate has confirmed.

## Sources

1. [TradeBridge: TradingView & Chartink Auto Trading · MyAlgoMate](https://myalgomate.com/products/tradebridge)
2. [TradingView vs Tradetron: Features, Pricing & Automation](https://algotest.in/blog/tradetron-vs-tradingview-comprehensive-comparison/)
3. [5 Best MCX Algo Trading Software Platforms in India (2026)](https://algotest.in/blog/best-mcx-algo-trading-software-india/)
4. [Myalgomate Technologies LLP](https://m.indiamart.com/myalgomate-technologies)
5. [Algo Trading Software in India — MyAlgoMate](https://myalgomate.com/)
6. [NSE & SEBI Algo Trading Regulations for Retail Traders · MyAlgoMate](https://www.myalgomate.com/nse-sebi-algo-trading-regulations-2025/)
7. [Algo Trading Tools & Software Products · MyAlgoMate](https://myalgomate.com/products)
8. [MyAlgoMate FAQ — Copy Trading, Brokers, Static IP & Pricing · MyAlgoMate](https://myalgomate.com/faq)
9. [MyAlgoMate (@myalgomate) on X](https://x.com/myalgomate)
10. [How to configure webhook alerts — TradingView](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
11. [I cannot send webhook to a URL with a port number — TradingView India](https://in.tradingview.com/support/solutions/43000529314-i-cannot-send-webhook-to-a-url-with-a-port-number/)
12. [About — Algo trading technology, made in India · MyAlgoMate](https://www.myalgomate.com/about)
13. [TradeMate: Multi-Expiry Option Chain Software · MyAlgoMate](https://myalgomate.com/products/trademate)
14. [Algo Trading with Zerodha · MyAlgoMate](https://myalgomate.com/brokers/zerodha)
15. [Algo Trading with Kotak Neo · MyAlgoMate](https://myalgomate.com/brokers/kotak-neo)
16. [Algo Trading with Dhan · MyAlgoMate](https://myalgomate.com/brokers/dhan)
17. [Algo Trading with Groww Invest Tech Pvt Ltd · MyAlgoMate](https://myalgomate.com/brokers/groww-invest-tech-pvt-ltd)
18. [Copy Trading Software in India — One Master, Many Accounts](https://myalgomate.com/products/copy-trading-software)
19. [GitHub - marketcalls/openalgo-flow: OpenAlgo Flow - No Code Trading Workflow Automation · GitHub](https://github.com/marketcalls/openalgo-flow)
20. [Webhook support for Alerts](https://chartink.com/articles/2020/03)
21. [Chartink.com (@ChartinkConnect) / ...](https://x.com/ChartinkConnect?lang=en)
22. [AlgoIP.in](https://algoip.in/compliance)
23. [NSE Releases Implementation Standards for Retail Participation in Algorithmic Trading](https://www.finseclaw.com/article/nse-releases-implementation-standards-for-retail-participation-in-algorithmic-trading)
24. [Page 1 of 6 CIRCULAR SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013](https://www.cse-india.com/upload/upload/Feb_042025.pdf)
25. [Algo Trading with Zebu (MYNT API) · MyAlgoMate](https://myalgomate.com/brokers/zebu)
26. [Getting Started — Step-by-Step Setup Guide · MyAlgoMate](https://myalgomate.com/getting-started)
27. [Which TradingView Plan Do You Need for Webhook Alerts?](https://crosstrade.io/blog/tradingview-plan-for-webhook-alerts)
28. [ChartInk](https://docs.openalgo.in/trading-platform/chartink)
29. [Video tutorials for MyAlgoMate's algo trading software.](https://myalgomate.com/tutorials)
30. [TradeBridge — Automate TradingView signals on MetaTrader 5](https://tradebridge.live/)
31. [Pricing & Plans](https://docs.algotest.in/Signals-AI/Pricing/)
32. [Tradetron Pricing Plans, Upgrade & Refund Policy](https://help.tradetron.tech/en/article/tradetron-pricing-plans-upgrade-refund-policy-7oisc0/)
33. [Algomojo Platform Now Open to Fyers Users](https://algomojo.com/blog/algomojo-platform-now-open-to-fyers-users/)
34. [Algomojo Platform Now Open to Samco Users](https://algomojo.com/blog/algomojo-platform-now-open-to-samco-users/)
35. [Chartink Integration](https://docs.openalgo.in/new-features/chartink-integration)
