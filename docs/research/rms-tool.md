> **External research — unverified hypotheses.** Saved verbatim on 2026-11-11 as a reference for [`PRD.md`](../../PRD.md). Vendor facts come from vendor marketing pages; the SentinelX recommendations are the research author's suggestions. Per [`CLAUDE.md`](../../CLAUDE.md), never treat its numbers as defaults or as measured results.

# SentinelX Risk Engine: Lessons from MyAlgoMate's RMS Tool, a Gap Analysis and a Spec-Ready Upgrade Plan

SentinelX's current design is already stricter than MyAlgoMate's RMS Tool on the things that matter most: fail-closed checks, persisted state, a kill switch that stays latched, and pre-trade checks. What it lacks is MyAlgoMate's **profit-side ratchets**: Max Profit, Lock Minimum Profit, Trail Profits, Lock & Trail, and Trail Max Loss. It also needs three hardening items specific to trading on Dhan: **continuous detection of manual orders, an exit policy that copes with MPP (Market Price Protection), and an automatic daily sync of Dhan's own backstops**. Build these first.

*Process note: I couldn't run the final enrichment pass, which checks vague claims against sources, because the draft was over its size limit. Every claim below is based on the sources listed at the end.*

## TL;DR

- **What MyAlgoMate's RMS Tool is:** a monitor that runs on your Windows PC or your own cloud server. It exits positions when account MTM reaches a Max Profit, Max Loss, time-based or "Protect Profit" level you set.
  - You choose how often it checks (every tick, 1 min or 5 min), how it exits (immediately, after a delay, or after N consecutive ticks) and which positions it exits (9 selectors).
  - Its public docs say nothing about pre-trade limits (trades per day, lots, symbols, cooldowns), how it treats manual trades, or what happens when the PC goes down.
- **What SentinelX should add:**
  - A single **"equity floor" ratchet** that combines Max Loss, Trail Max Loss, Lock Min Profit and Trail Profit into one number that only ever rises.
  - A daily profit target and a giveback limit (maximum drawdown from the day's peak).
  - Lot caps, re-entry caps and an instrument allow/deny policy.
  - Continuous detection of external orders, expiry-day rules, a stale-feed breaker, and an exit chaser that copes with MPP.
  - Every rule plugs into one interface for pre-trade or continuous checks, and every parameter declares which direction counts as tightening.
- **How to layer on Dhan:**
  - Each morning, re-arm Dhan's P&L-based exit (`POST /pnlExit`, which resets every session) as a wider backstop than the engine's own limit.
  - Use `DELETE /positions` (Exit All) only as a last-resort flatten.
  - After an automatic daily-loss halt, activate Dhan's Kill Switch once the book is flat (Dhan requires no open positions or pending orders).
  - Budget for Dhan's limits (10 orders/s, 25 modifications per order)\[1\] and SEBI's static-IP and 10-orders-per-second rules, mandatory since 1 April 2026.

---

## How to read this document
- **"MyAlgoMate docs state"** marks content quoted from MyAlgoMate's public pages; the page is named. **"Recommendation"** marks my own engineering proposals for SentinelX.
- Some RMS behaviour is documented only on **sibling MyAlgoMate products**: TradeMate, the StockMock/AlgoTest tool, Copy Trading, 5 EMA and ORB. They appear to share one RMS module, since the RMS Tool's settings screenshot is captioned "Trademate Rms Setting".\[2\] Facts from these pages are labelled as sibling-product facts.

### Glossary
| Term | Meaning |
|---|---|
| MTM (mark-to-market) | The day's running P&L: realized P&L plus unrealized P&L on open positions at current prices |
| F&O / lot | NSE futures and options, traded in **lots** (a fixed contract multiplier)\[3\] |
| Square-off / auto square-off | Closing a position with an opposite order / the broker force-closing intraday positions near its cut-off time\[4\] |
| ITM / OTM / ATM | Where an option's strike sits relative to the underlying: in-, out-of- or at-the-money |
| Short / hedge | A sold option (high margin) / a bought option further OTM that reduces risk |
| MPP | Market Price Protection: a market order is converted into a limit order capped at X% from the LTP (last traded price) |

---

## Part A — MyAlgoMate RMS Tool: what the docs state

### A.1 Pages crawled and coverage
| Page | Status | Notes |
|---|---|---|
| `myalgomate.com/products/rms-tool` (also served at `www.myalgomate.com/product/rms-tool/`) | **Fetched in full** | The only RMS-specific documentation ("Last updated: 8 November 2026"). Contains the FAQ, pricing and "Every setting, explained"\[2\] |
| `/products/stockmock-algotest-strategy` | Fetched | Sibling. Covers strategy, account and global RMS levels; the Ticks exit mode; Trail Max Loss\[5\] |
| `/product/trademate-options-trading` (canonical URL `/products/trademate`) | Fetched | Sibling. Covers stop-losses held at the broker, behaviour on restart, Telegram, Paper mode |
| `/products/copy-trading-software`, `/5-ema-strategy-power-of-stocks`, `/orb-opening-range-breakout`, `/tradebridge`, `/getting-started`, `/tutorials`, `/brokers/dhan` | Search snippets only | Used for corroboration only |
| `myalgomate.com/llms/rms-tool.txt` (the "complete user guide" the product page refers to) | **Not reachable** | Blocked in this session |
| `/glossary` (kill-switch, max-loss and protect-profit anchors), `/glossary/auto-square-off`, `/products/rms-tool/brokers`, `/pricing`, `/faq` | **Not reachable** | |
| help.myalgomate.com, docs, knowledge base, changelog | **Not found** | `/tutorials` lists "36 videos · 2 products" (Copy Trading and TradeBridge), so there are no RMS Tool videos.\[6\] A Telegram channel serves as informal release notes for the straddle product\[7\] |

### A.2 Product facts
- **What it does:** "watches your open positions and exits them when the Max Profit, Max Loss, time-based or Protect Profit rules you set are reached, per account or across all. It runs on your own Windows PC or your own cloud server."\[2\]
- **How rules are enforced:** orders go "through your broker's official API".\[2\] Enforcement is **software-side only**; the page describes no order resting at the broker.
- **Brokers:** "Works with 155 brokers", including Dhan.\[2\] MyAlgoMate's Dhan page lists the RMS Tool as one of 7 products that work with Dhan.\[8\]
- **Pricing (per client ID, excluding GST):**
  - List price: ₹1,999/month, ₹4,999/quarter or ₹11,999/year.\[2\]\[9\]
  - With the Diwali 2026 code DIWALI2026: ₹1,799/month, ₹4,249/quarter or ₹9,599/year.\[2\] This is a promotion and will change.
  - "One subscription is needed for each trading account (client ID)."\[2\]

### A.3 Every documented setting (RMS Tool page)
| Setting (exact name) | Units | Trigger | Action | Edge cases and notes in the docs |
|---|---|---|---|---|
| **Max Profit** | ₹ of MTM | "If the MTM crosses the 'Max Profit' threshold" | "it will exit all positions" (subject to Positions to exit) | Example given: 10,000\[2\] |
| **Max Loss** | ₹ of MTM, **signed** | MTM crosses the threshold | Exit all positions | "if you are just starting trading, the 'Max Loss' value should be negative." It can be set **positive** once well in profit: with MTM at 50,000, setting +30,000 exits at 30,000.\[2\] That makes it a manual profit floor\[2\] |
| **Time-Based Exit** | Clock time | The set time is reached | Exit open positions (subject to Positions to exit) | Separate from the broker's own cut-off\[2\] |
| **Don't Sell Exit Position Above** | Option premium (₹) | Enabled with a price, e.g. 500 | On a Max Profit, Max Loss or Lock & Trail exit, it "won't exit any options with a sell position and a price above 500" | "does not apply to any other type of exit, including master maximum profit and maximum loss"\[2\] |
| **Max Profit/Loss check interval** (set separately for profit and loss) | Every Tick / 1 Minute / 5 Minute | Checked at that cadence | – | Checks align to the clock: set at 09:20:50, the first check is 09:21:00; set at 09:22:30 on the 5-minute interval, the first check is 09:25:00\[2\] |
| **Exit Modes** (set separately for profit and loss) | Immediate / Delayed Exit (seconds) / Ticks (count) | Threshold crossed | Exit immediately, after N seconds, or after N **consecutive** ticks | Ticks mode: "If you don't get consecutive 5 ticks, then it will reset the counter."\[2\] The docs don't say whether Delayed mode re-checks the threshold before exiting |
| **Positions to exit** (set separately for Max Profit, Max Loss and Time-Based Exit) | 9 options | – | All; All Loss Making; All Profit Making; All Sell; Sell Profit Making; Sell Loss Making; All Buy; Buy Profit Making; Buy Loss Making\[2\] | What happens to positions left open after a partial exit is not documented |
| **Lock Minimum Profit** | ₹ ("If Profit Reaches", "Lock Min Profit At") | Profit reaches 10,000 | Locks a floor at 5,000; "All positions will be exited when the MTM decreases to 5000" | "This action is performed only once."\[2\] |
| **Trail Profits** | ₹ ("Increase in Profit", "Trail Min Profit by") | Every 5,000 of additional profit | Floor rises by 3,000 each step: profit 5k → floor 3k; 10k → 6k\[2\] | Step function\[2\] |
| **Lock & Trail Profits** | ₹ (all four parameters) | Lock first, then trail | Profit 10k → floor 5k; 15k → 8k; 20k → 11k\[2\] | – |
| **Trail Max Loss** | ₹ (X, Y) | Every X of MTM gained | With Max Loss −10,000, X=1000, Y=500: MTM 1000 → −9,500; MTM 1500 → −9,000\[2\] | **The example is internally inconsistent** (the second step should come at 2000, not 1500). A sibling page has a correct example\[5\] |
| **Scope** | Per account, or "across all strategies together" | – | – | A "master maximum profit and maximum loss" is mentioned but never defined\[2\] |

### A.4 Behaviour documented on sibling products
- **Three scope levels** (StockMock/AlgoTest page):
  - "Strategy Level: MTM of individual strategy; Account Level: total MTM across all strategies of that account; Global Level: total MTM across all accounts."\[5\]
  - Example: "If Global Level Max Loss = -50000… all positions in entire system will be exited."\[5\]
- **A correct Trail Max Loss example** (same page): Max Loss −5000, X=1000, Y=500. MTM 1000 → −4500; 2000 → −4000; 3000 → −3500.\[5\]
- **Units by level** (Telegram release notes): strategy-level limits are set as "value per lot"; global limits are "in rupees terms".\[7\] The same notes say Global Max Loss can be "both positive and negative", and that global protect profit can be enabled "after the algo has started".\[10\]
- **Broker-side vs software-side** (TradeMate page):
  - "A stop loss with **At broker** turned on rests as a real order with your broker, so it still works when TradeMate is closed. Everything else… runs inside TradeMate and stops when TradeMate stops."\[11\]
  - With At broker off, TradeMate "watches the price itself, ten times a second".\[11\]
  - This is the clearest statement in MyAlgoMate's docs of what happens when the PC goes down: **software-side rules stop**.
- **Restart** (TradeMate page): "Do my rules stay set after a restart? No, and that is on purpose… all start fresh each time. The values you typed as defaults are kept."\[11\]
- **Trailing done in software** (5 EMA page): "We do not modify the SL price in the broker when trailing happens… Whenever trailing SL is hit, it will modify the SL order to market to exit."\[12\] This is the same design as SentinelX.
- **Session lockout** (Copy Trading page): an account that hits Max Loss or Max Profit "is squared off and taken out of copying for the rest of the session. Every other account carries on untouched."\[13\]
- **Alerts and logs:**
  - Telegram alerts go to the user's own bot, and TradeMate "sends a test message to check the details" when you save.\[11\]
  - Copy Trading adds pop-ups, a sound on rejection, and a searchable live log.\[13\]
- **Pacing and static IP** (StockMock/AlgoTest page): "Orders pace to each broker's API rate limit." Static IP: brokers "accept API orders only from a static IP registered with them — one for each trading account."\[5\]
- **Max trades:** "Max. no. of Trades" appears only in the ORB and 5 EMA settings, not on the RMS Tool page.\[12\]\[14\]

### A.5 What the RMS docs don't cover
The public RMS docs are silent on all of the following:
- Limits on trades or orders per day
- Lot or quantity caps
- Symbol allow/deny lists
- Entry cutoffs, cooldowns and re-entry limits
- Whether new orders are **blocked** after a Max Loss exit (the docs only describe the exit itself)
- **How manual trades are treated** (an account-wide view is implied but not stated)
- Latency
- Telegram alerts for the RMS Tool itself
- Behaviour when the PC or internet connection fails

Treat it as an **exit automaton, not a pre-trade gate**.

### A.6 UX patterns worth copying
- A worked numeric example next to every parameter.
- A per-trigger choice of which positions to exit.
- A signed Max Loss shown on one number line: loss floor → profit floor → target.
- A test message when the alert channel is saved.
- Refusing state changes while risk is live. TradeMate "refuses to switch one [index] off while it still holds an open position or an armed rule".\[11\]
- Per-account isolation.

---

## Part B — SentinelX gap analysis
| # | MyAlgoMate RMS feature | SentinelX | Recommendation | Priority | Stage |
|---|---|---|---|---|---|
| 1 | Max Loss on MTM → exit all | **Have** | Keep the loss check immediate. Never copy Delayed or Ticks mode for losses | – | – |
| 2 | Max Profit (daily target) | **Missing** | R1 | P1 | S1 |
| 3 | Signed Max Loss (manual profit floor) | **Partial** | Model as an equity floor that can sit above zero (R2) | P1 | S1 |
| 4–7 | Lock Min Profit / Trail Profits / Lock & Trail / Trail Max Loss | **Missing** | One equity-floor ratchet (R2) | P1 | S1 |
| 8 | Check interval | **Partial** | Loss checks on every MTM recompute; a bar-close cadence only for profit locks | P2 | S2 |
| 9 | Exit modes | **Missing** | Debounce only the profit target; filter bad prints with R9 | P2 | S2 |
| 10 | Positions to exit | **Missing** | Kill and loss exits always close everything. Exit **shorts before hedges** (R10) | P0/P2 | S0/S2 |
| 11 | Don't Sell Exit Position Above | **Missing** | **Don't copy for loss exits.** Use R10 plus R4 instead | P2 | S2 |
| 12 | Time-Based Exit | **Have** (15:10) | Earlier square-off on expiry day (R8) | P1 | S1 |
| 13 | Account vs global scope | **Partial** | Household scope (R11) | P1 | S3 |
| 14 | Strategy-level limits | **Missing** | Strategy scope with per-strategy loss budget and max trades | P1 | S2 |
| 15 | Per-lot vs ₹ units | **Missing** | Support INR / INR_PER_LOT / PCT_CAPITAL | P2 | S2 |
| 16 | SL resting at broker | **Have** | Verify the SL is live after every fill | – | – |
| 17 | Trail in software, then SL → market | **Have** | Market orders now become MPP limits, so use R10 | P0 | S0 |
| 18 | Rules reset on restart | **Better** (state is persisted) | Re-validate persisted state against the broker | – | – |
| 19 | Telegram + test on save | **Partial** | Test on save, heartbeat, external dead-man monitor | P1 | S1 |
| 20 | Logs and exports | **Partial** | `risk_event` audit table; daily CSV | P1 | S1 |
| 21 | Paper mode | **Have** | Shadow mode for each new rule before it enforces | P1 | S1 |
| 22 | Pacing to rate limits | **Have** | Rejection breaker and modification cap (R6) | P1 | S1 |
| 23 | Session lockout after a limit | **Have** | Optionally activate Dhan's Kill Switch once flat | P1 | S1 |
| 24 | Max trades | **Have** | Lot caps (R3) and re-entry caps (R5) | P1 | S1–S2 |
| 25 | Manual trades outside the tool | Not documented by MyAlgoMate; SentinelX **Partial** (startup check only) | **Continuous** detection (R7) | **P0** | S0 |
| 26 | Days-from-expiry selection | **Missing** | Expiry-day profile (R8) | P1 | S1 |
| 27 | Smart limit order + retry | **Partial** | Bounded exit-only price chase (R10) | P0 | S0 |
| 28 | Stale-data handling | **Missing** | R9 | **P0** | S0 |

---

## Part C — New risk rules (spec-delta ready)

### C.0 Conventions
- **MTM_day** = realized P&L for the day + Σ unrealized P&L at LTP, in ₹, preferably net of estimated charges.
- **Tighten direction:** every parameter declares `tighten: up | down | enable_only | enum_order`.
  - Between 09:15 and 15:30 IST, a config change is accepted only if every changed parameter moves in its tighten direction.
  - Ratchet state (the floor and the day's peak MTM) belongs to the engine and can never be edited from the UI.
- **Scopes:** account (default), strategy, underlying, instrument, household.
- **Fail-closed:** if an input is missing or stale, pre-trade checks reject the order, and continuous checks raise the account to at least HALT_NEW_ENTRIES.

### R1 — Daily profit target
- **Definition:** when `MTM_day ≥ daily_profit_target` for `target_confirm_ticks` consecutive updates, flatten and stop trading for the day.
- **Parameters:**
  - `daily_profit_target` (₹; tightening = lowering it)
  - `target_confirm_ticks` (tightening = lowering it)
  - `target_exit_selector` (ALL | PROFIT_MAKING)
- **Action:** set HALTED_TARGET (persisted), cancel pending entries, exit through R10, send an alert. Entries stay blocked until the reset after the close.
- **Edge cases:**
  - A price spike can carry MTM past the target and then reverse; that is accepted.
  - Stay halted even if MTM falls back below the target.
  - On expiry day, short premium decaying toward zero can inflate MTM.

```
### Requirement: Daily profit target
The risk engine SHALL flatten all positions and block new entries for the rest of the trading day when MTM_day is at or above daily_profit_target for target_confirm_ticks consecutive MTM updates.

#### Scenario: Target reached
- GIVEN daily_profit_target = 4000 and target_confirm_ticks = 2
- WHEN MTM_day updates are 3950, 4010, 4020
- THEN after 4020 the state is HALTED_TARGET and persisted
- AND entries are cancelled, positions exited, new entries rejected with RISK_TARGET_HALT

#### Scenario: Loosening blocked intraday
- GIVEN 11:00 IST and daily_profit_target = 4000
- WHEN a config change sets 6000
- THEN it is rejected with TIGHTEN_ONLY
```

### R2 — Equity-floor ratchet
This single rule replaces Max Loss, Trail Max Loss, Lock Min Profit, Trail Profits and Lock & Trail.

**Definition:** the engine keeps one `floor`, in ₹ of MTM_day, and exits everything when `MTM_day ≤ floor`. The floor only ever rises:

```
peak  = max(peak, MTM_day)                          # fresh, non-outlier data only (R9)
f_static     = -daily_max_loss
f_trail_loss = -daily_max_loss + floor(peak / trail_loss_x) * trail_loss_y        # peak > 0
f_lock       = lock_at                       if peak >= lock_trigger (LOCK / LOCK_AND_TRAIL)
f_trail      = floor(peak / trail_step) * trail_by                                 (TRAIL)
             = lock_at + floor((peak - lock_trigger) / trail_step) * trail_by      (LOCK_AND_TRAIL)
f_giveback   = peak - max(giveback_abs, giveback_pct * peak)  if peak >= giveback_arm
floor = max(floor_prev, f_static, f_trail_loss, f_lock, f_trail, f_giveback)
```

This reproduces MyAlgoMate's correct examples:
- Trail Max Loss: −5000 → −4500 at 1000 → −4000 at 2000.\[5\]
- Lock & Trail: 10k → 5k, 15k → 8k, 20k → 11k.\[2\]
- Trail Profits: 5k → 3k, 10k → 6k.\[2\]

`f_giveback` (a drawdown-from-peak limit) is my own addition; MyAlgoMate has no equivalent.

- **Parameters:** each is listed with the direction that counts as tightening.
  - `daily_max_loss` ↓, `trail_loss_x` ↓, `trail_loss_y` ↑
  - `lock_trigger` ↓, `lock_at` ↑, `trail_step` ↓, `trail_by` ↑
  - `lock_mode` (OFF / LOCK / TRAIL / LOCK_AND_TRAIL)
  - `giveback_pct` ↓, `giveback_abs` ↓, `giveback_arm` ↓
- **Action:** the kill switch fires: halt, cancel entries, flatten through R10, then cancel catastrophic SLs once exits have filled.
- **Tighten-only:** on save, simulate the new parameters and reject the change if the floor would fall.
- **Edge cases:**
  - A price gap through the floor fills worse than the floor; the floor is a trigger, not a price guarantee.\[15\]
  - On restart, use max(persisted floor, recomputed floor).
  - Partial exit fills keep the account halted.
  - On expiry day the peak gets inflated, so apply R8's tighter giveback.
  - Bad ticks could raise the peak permanently, so only fresh, validated data (R9) may update it.

```
### Requirement: Equity floor ratchet
The risk engine SHALL maintain a per-account, per-day equity floor that never decreases during the trading day, and SHALL trigger the kill switch when MTM_day is at or below the floor.

#### Scenario: Lock and trail
- GIVEN daily_max_loss = 3000, lock_mode = LOCK_AND_TRAIL, lock_trigger = 2000, lock_at = 500, trail_step = 1000, trail_by = 500
- WHEN MTM_day rises to 3100 and then falls to 990
- THEN the floor is 1000 once MTM has reached 3000
- AND the kill switch triggers at or below 1000

#### Scenario: Floor survives restart
- GIVEN persisted floor = 1000 and peak = 3100
- WHEN the engine restarts at 13:00 with MTM_day = 1800
- THEN floor remains 1000 and peak remains 3100

#### Scenario: Config cannot lower an active floor
- GIVEN floor = 1000 at 12:00 IST
- WHEN a config change sets lock_mode = OFF
- THEN it is rejected with TIGHTEN_ONLY
```

### R3 — Lot caps
- **Definition:** an entry is allowed only if all of these hold:
  - `open_lots(underlying) + reserved_lots + order_lots ≤ max_open_lots_per_underlying`
  - the same check against `max_open_lots_total`
  - `gross_entry_lots_today + order_lots ≤ max_gross_lots_per_day`
- All limits tighten downward. Exits never count toward the caps.
- **Edge cases:** read the lot size per contract from the instrument master, because lot sizes get revised. A pending entry reserves its lots until the order reaches a terminal state.

```
### Requirement: Lot caps
The risk engine MUST reject any entry order whose lots would exceed max_open_lots_per_underlying, max_open_lots_total or max_gross_lots_per_day, counting pending entries as reserved capacity.

#### Scenario: Pending order reserves capacity
- GIVEN max_open_lots_per_underlying = 4 for NIFTY and a pending 3-lot NIFTY entry
- WHEN a 2-lot NIFTY entry is submitted
- THEN it is rejected with RISK_LOTS_UNDERLYING
```

### R4 — Instrument policy
- **Definition:** an entry is allowed only if all of these hold:
  - it matches the allow list (when the list is non-empty);
  - it matches no deny entry;
  - `|strike − ATM| ≤ max_strike_distance_steps × strike_step`;
  - `premium ≥ min_premium`;
  - `spread_pct ≤ max_spread_pct`;
  - it is not on the F&O ban list.
- Exits are always allowed.
- Load the ban list before the open. If it can't be loaded, fail closed.
- Because MPP limit orders can sit unfilled on illiquid strikes, the spread filter at entry is the real defence.

```
### Requirement: Instrument policy
The risk engine SHALL reject entry orders for denied, not-allowed or illiquid instruments, and SHALL never apply instrument policy to exit orders.

#### Scenario: Wide spread rejected
- GIVEN max_spread_pct = 3 and an option quoted 10.0 / 10.8
- WHEN an entry is submitted
- THEN it is rejected with RISK_ILLIQUID
```

### R5 — Re-entry limits
- **Key:** `(account, strategy, underlying, side)`.
- **Definition:** `entries_today < max_entries_per_key` and `now − last_exit ≥ reentry_cooldown_s`. Setting `reentry_after_sl_allowed=false` blocks the key for the rest of the day after a stop-loss exit.
- **Counting rules:**
  - A partially filled entry that is then cancelled still counts as an entry.
  - A position reversal counts as an exit plus an entry.

```
### Requirement: Re-entry limit
The risk engine MUST reject an entry on a key that has reached max_entries_per_key or whose last exit was less than reentry_cooldown_s ago.

#### Scenario: Cooldown after exit
- GIVEN reentry_cooldown_s = 600 and last NIFTY long exit at 10:00:00
- WHEN a NIFTY long entry is submitted at 10:05:00
- THEN it is rejected with RISK_REENTRY_COOLDOWN
```

### R6 — Overtrading and order-flow guards
- **Guards:**
  - `max_entries_per_min`
  - `min_seconds_between_entries`
  - A **rejection breaker:** if `reject_breaker_n` broker rejections occur within a rolling 5 minutes, set HALT_NEW_ENTRIES and send an alert.
  - `max_mods_per_order` (default 20, below Dhan's cap of 25).\[1\]
- Exits are exempt from the entry-pacing guards but still go through the token bucket.

```
### Requirement: Rejection circuit breaker
The risk engine SHALL halt new entries and alert when broker rejections within a rolling 5-minute window reach reject_breaker_n.

#### Scenario: Margin rejection storm
- GIVEN reject_breaker_n = 3
- WHEN three entries are rejected within 2 minutes
- THEN state becomes HALT_NEW_ENTRIES and an alert is sent
```

### R7 — Detecting and handling external (manual) orders
**Why:** MyAlgoMate's docs don't say how manual trades are handled, and SentinelX currently checks only at startup. Dhan's Live Order Update WebSocket delivers updates "for all orders placed via your account, irrespective of the platform", with a `Source` field ("P for API Orders").\[16\] That makes continuous detection possible.

- **Definitions:**
  - An order is **external** if its ID isn't in SentinelX's order table, or if its source isn't the API.
  - **Drift** means the broker's `netQty` differs from the quantity the engine expects.
- **Parameters:**
  - `external_order_policy`: ALERT_ONLY < ADOPT < HALT_AND_ALERT < FLATTEN_EXTERNAL
  - `position_poll_s`: 5–15 s
  - `drift_grace_s`: 10 s
- **Default policy: ADOPT.**
  - The external fill becomes a position with `origin=EXTERNAL`.
  - It counts against every limit and gets a catastrophic SL.
  - If adopting it would breach a limit, escalate to HALT_AND_ALERT.
  - In Full-auto mode, default to HALT_AND_ALERT.
- **Edge cases:**
  - After a WebSocket reconnect, backfill from the order book.
  - If an engine position is closed from outside SentinelX, **cancel its catastrophic SL immediately**. A resting SL on a flat book is a naked entry order.

```
### Requirement: External order handling
The risk engine SHALL detect, within drift_grace_s, any order or fill not originated by SentinelX, SHALL include its exposure in all risk limits, and SHALL apply external_order_policy.

#### Scenario: Manual exit leaves an orphan SL
- GIVEN an engine-managed long with a catastrophic SL at Dhan
- WHEN the user exits from the Dhan app
- THEN the engine detects netQty = 0 and cancels the SL before any other action
- AND records risk_event EXTERNAL_EXIT

#### Scenario: Manual entry breaches the floor
- GIVEN external_order_policy = ADOPT and floor = -3000
- WHEN a manual fill takes MTM_day to -3100
- THEN the kill switch triggers as for an engine position
```

### R8 — Expiry-day profile
- **Definition:** for contracts expiring today, apply override values, for example:
  - an entry cutoff of 14:00;
  - a square-off at 15:00;
  - tighter lot caps, a higher minimum premium and a tighter giveback.
- Each override must be at least as tight as its base value.
- **Rules:**
  - Take the expiry date from the instrument master, never infer it from the weekday.
  - By default, block holding stock options into expiry, because they are physically settled.

```
### Requirement: Expiry-day overrides
The risk engine SHALL apply expiry-day overrides to instruments expiring today, and override values MUST be equal to or tighter than base values.

#### Scenario: Earlier cutoff on expiry
- GIVEN entry_cutoff = 15:00 and entry_cutoff_expiry = 14:00
- WHEN an entry on a contract expiring today is submitted at 14:10
- THEN it is rejected with RISK_EXPIRY_CUTOFF
```

### R9 — Stale-data and outlier breaker
- **Definitions:**
  - A quote is **stale** when no tick has arrived for `stale_s` (default 5 s).
  - The feed is **down** when no ticks have arrived for `feed_down_s` (default 10 s).
  - A tick is an **outlier** when it jumps more than `outlier_pct` and isn't confirmed by the next tick or a REST quote.
- **Actions:**
  - Reject entries priced from a stale quote.
  - If the feed is down, set HALT_NEW_ENTRIES and compute MTM from REST quotes.
  - If MTM can't be computed for longer than `mtm_blind_s`, flatten.
  - Outliers never update the day's peak.

```
### Requirement: Fail closed on stale data
The risk engine MUST NOT accept entries priced from quotes older than stale_s, and MUST halt new entries when no ticks arrive for feed_down_s during market hours.

#### Scenario: Feed outage
- GIVEN feed_down_s = 10
- WHEN no ticks arrive for 12 s at 11:30 IST
- THEN state becomes HALT_NEW_ENTRIES and MTM uses REST quotes
```

### R10 — Exit execution that copes with MPP
- **Definition:**
  - Place exits as aggressive LIMIT orders at LTP ± `exit_buffer_pct`.
  - Re-price every `chase_interval_ms`, up to `max_chase_steps` times (no more than `max_mods_per_order`), and never beyond `max_exit_slippage_pct`.
  - If the book isn't flat within `exit_escalate_s`, call Dhan's Exit All (`DELETE /positions`) and send a critical alert.
- **Ordering:** close **short options before their hedges**. Dhan support says hedged positions require that "you need to square-off the parent leg first or can maintain the sufficient funds".\[17\]
- **Why:**
  - Dhan converts API market orders into MPP limit orders;\[18\] its support article cites 10% protection on options.\[19\]
  - A forum user reports converted orders being placed at LTP with no buffer.\[20\]
  - **Never use MARKET orders for exits.**

```
### Requirement: Bounded exit chase
The risk engine SHALL place exits as limit orders re-priced toward the market at most max_chase_steps times, SHALL close short legs before hedge legs, and SHALL call Exit All if the account is not flat within exit_escalate_s of a kill.

#### Scenario: Illiquid exit escalates
- GIVEN a kill at 14:00:00 and exit_escalate_s = 30
- WHEN one leg is still open at 14:00:31
- THEN the engine calls Exit All and sends a critical alert
```

### R11 — Household floor (Stage 3)
- **Definition:** `MTM_household = Σ MTM_day` across linked accounts, with an R2-style floor applied to that total.
- **Behaviour:**
  - A household breach kills every linked account.
  - A per-account breach kills only that account (the isolation pattern from MyAlgoMate's Copy Trading).\[21\]
  - An outage on one account must never block exits on the other.

```
### Requirement: Household floor
When household linking is enabled, the risk engine SHALL kill every linked account when household MTM is at or below the household floor, and SHALL halt only the affected account on a per-account breach.

#### Scenario: Isolation
- GIVEN accounts A and B linked and A breaches its own floor
- WHEN B and the household are above their floors
- THEN only A becomes HALTED
```

### R12 — Syncing Dhan's backstop
- **When:** before 09:15 (after the daily login) and after every tightening of `daily_max_loss`.
- **Call:** `POST /pnlExit` with:
  - `lossValue = daily_max_loss × backstop_multiplier` (e.g. 1.25)
  - `productType=["INTRADAY"]`
  - `enableKillSwitch=true`
- Then confirm the result with `GET /pnlExit`.
- **Why the multiplier:** it ensures the engine acts first. Dhan's exit is only for when the engine is dead.
- **Caution:** a `lossValue` already beyond the current loss "will be triggered immediately".\[22\]

```
### Requirement: Broker backstop armed
In Live mode the risk engine MUST confirm Dhan's P&L-based exit is ACTIVE with a loss value no greater than daily_max_loss × backstop_multiplier before accepting the day's first entry.

#### Scenario: Backstop not confirmed
- GIVEN Live mode at 09:16 and GET /pnlExit returns INACTIVE
- WHEN an entry is submitted
- THEN it is rejected with RISK_BACKSTOP_UNARMED and an alert is sent
```

---

## Part C.2 — Postgres schema additions
```sql
CREATE TABLE risk_rule_config (
  id BIGSERIAL PRIMARY KEY, account_id TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('account','strategy','underlying','instrument','household')),
  scope_key TEXT NOT NULL DEFAULT '*', rule_key TEXT NOT NULL,
  params JSONB NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
  mode TEXT NOT NULL DEFAULT 'enforce' CHECK (mode IN ('shadow','enforce')),
  version INT NOT NULL, effective_from TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT NOT NULL,
  UNIQUE (account_id, scope_type, scope_key, rule_key, version)
);
CREATE TABLE risk_param_meta (
  rule_key TEXT NOT NULL, param TEXT NOT NULL,
  unit TEXT NOT NULL,   -- INR|INR_PER_LOT|PCT|LOTS|SECONDS|COUNT|ENUM|BOOL
  tighten TEXT NOT NULL CHECK (tighten IN ('up','down','enable_only','enum_order')),
  enum_order TEXT[], min_value NUMERIC, max_value NUMERIC, PRIMARY KEY (rule_key, param)
);
CREATE TABLE risk_day_state (   -- engine-owned; never UI-editable
  account_id TEXT NOT NULL, trade_date DATE NOT NULL, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
  mtm_peak NUMERIC(14,2) NOT NULL DEFAULT 0, equity_floor NUMERIC(14,2) NOT NULL,
  halt_state TEXT NOT NULL DEFAULT 'ACTIVE', halt_reason TEXT,
  trades_count INT NOT NULL DEFAULT 0, gross_lots INT NOT NULL DEFAULT 0, consec_losses INT NOT NULL DEFAULT 0,
  counters JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, trade_date, scope_type, scope_key)
);
CREATE TABLE instrument_policy (
  id BIGSERIAL PRIMARY KEY, account_id TEXT NOT NULL,
  list_type TEXT NOT NULL CHECK (list_type IN ('allow','deny')),
  pattern JSONB NOT NULL, reason TEXT, valid_from DATE, valid_to DATE
);
CREATE TABLE external_order (
  account_id TEXT NOT NULL, broker_order_id TEXT NOT NULL, source TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), payload JSONB NOT NULL, policy_applied TEXT NOT NULL,
  PRIMARY KEY (account_id, broker_order_id)
);
CREATE TABLE risk_event (   -- append-only audit
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), account_id TEXT NOT NULL,
  rule_key TEXT NOT NULL, phase TEXT NOT NULL CHECK (phase IN ('pre_trade','continuous','config','backstop')),
  decision TEXT NOT NULL, reason_code TEXT, inputs JSONB NOT NULL, latency_us INT, order_key TEXT
);
CREATE TABLE broker_backstop (
  account_id TEXT NOT NULL, trade_date DATE NOT NULL, pnl_exit JSONB, kill_switch TEXT,
  kill_activations_today INT NOT NULL DEFAULT 0, verified_at TIMESTAMPTZ, PRIMARY KEY (account_id, trade_date)
);
```

## Part C.3 — Rule interface (Python-style pseudocode)
```python
class Phase(Enum):
    PRE_TRADE = 1
    CONTINUOUS = 2


class Action(IntEnum):  # ordered by severity
    ALLOW = 0
    DOWNSIZE = 1
    REJECT = 2
    HALT_NEW_ENTRIES = 3
    TARGET_HALT = 4
    KILL = 5


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_key: str
    reason: str = ""
    new_lots: int | None = None
    state_patch: dict | None = None


class RiskCheck(Protocol):
    rule_key: str
    phases: frozenset[Phase]

    def pre_trade(self, ctx, intent) -> Decision: ...
    def continuous(self, ctx, event) -> Decision: ...


class EquityFloorCheck:
    rule_key, phases = "equity_floor", frozenset({Phase.CONTINUOUS, Phase.PRE_TRADE})

    def continuous(self, ctx, event):
        if ctx.mtm_day is None:
            return Decision(Action.HALT_NEW_ENTRIES, self.rule_key, "MTM_UNKNOWN")
        p, s = ctx.params, ctx.day_state
        peak = max(s.mtm_peak, ctx.mtm_day) if event.fresh else s.mtm_peak
        c = [s.equity_floor, -p["daily_max_loss"]]
        if p.get("trail_loss_x") and peak > 0:
            c.append(-p["daily_max_loss"] + (peak // p["trail_loss_x"]) * p["trail_loss_y"])
        m = p.get("lock_mode", "OFF")
        if m in ("LOCK", "LOCK_AND_TRAIL") and peak >= p["lock_trigger"]:
            c.append(p["lock_at"])
        if m == "TRAIL" and peak >= p["trail_step"]:
            c.append((peak // p["trail_step"]) * p["trail_by"])
        if m == "LOCK_AND_TRAIL" and peak >= p["lock_trigger"]:
            c.append(p["lock_at"] + ((peak - p["lock_trigger"]) // p["trail_step"]) * p["trail_by"])
        if p.get("giveback_pct") and peak >= p.get("giveback_arm", float("inf")):
            c.append(peak - max(p.get("giveback_abs", 0), p["giveback_pct"] * peak))
        floor = max(c)
        patch = {"mtm_peak": peak, "equity_floor": floor}
        act = Action.KILL if ctx.mtm_day <= floor else Action.ALLOW
        return Decision(act, self.rule_key, "FLOOR_BREACH" if act else "", state_patch=patch)

    def pre_trade(self, ctx, intent):
        if intent.is_exit:
            return Decision(Action.ALLOW, self.rule_key)
        if ctx.mtm_day is None or ctx.mtm_day - intent.risk_inr <= ctx.day_state.equity_floor:
            return Decision(Action.REJECT, self.rule_key, "ENTRY_WOULD_BREACH_FLOOR")
        return Decision(Action.ALLOW, self.rule_key)


class RiskEngine:
    async def gate(self, ctx, intent) -> Decision:  # every order, every source
        if ctx.day_state.halt_state != "ACTIVE" and not intent.is_exit:
            return Decision(Action.REJECT, "halt", ctx.day_state.halt_state)
        worst = Decision(Action.ALLOW, "all")
        for c in self.pre:
            try:
                d = c.pre_trade(ctx, intent)
            except Exception:
                d = Decision(Action.REJECT, c.rule_key, "CHECK_ERROR")  # fail closed
            await self.store.log_event(ctx, d, "pre_trade", intent)
            worst = max(worst, d, key=lambda x: x.action)
        return worst

    async def on_event(self, ctx, event):  # tick / MTM / order update / timer
        ds = []
        for c in self.cont:
            try:
                ds.append(c.continuous(ctx, event))
            except Exception:
                ds.append(Decision(Action.HALT_NEW_ENTRIES, c.rule_key, "CHECK_ERROR"))
        async with self.store.tx() as tx:  # persist ratchets BEFORE acting
            for d in ds:
                if d.state_patch:
                    await tx.apply_monotonic(ctx.account_id, d.state_patch)
        worst = max(ds, key=lambda d: d.action)
        if worst.action == Action.KILL:
            await self.exec.kill(ctx.account_id, worst.reason)  # idempotent
        elif worst.action == Action.TARGET_HALT:
            await self.exec.target_halt(ctx.account_id)
        elif worst.action == Action.HALT_NEW_ENTRIES:
            await self.store.set_halt(ctx.account_id, worst.reason)
```

**Implementation notes:**
- The config validator checks each changed parameter against `risk_param_meta.tighten`, simulates the floor with the new values, and rejects the change if the floor would fall.
- `apply_monotonic` writes `GREATEST(existing, new)` in SQL, so a stale or out-of-order write can never lower the floor or the peak.
- The kill is idempotent, keyed on `(account_id, trade_date, 'KILL')`.

---

## Part D — Dhan-specific layering (DhanHQ API v2)

### D.1 What Dhan provides
| Feature | Endpoint / behaviour | Constraints | Source |
|---|---|---|---|
| **Kill Switch** | `POST /killswitch?killSwitchStatus=ACTIVATE` or `DEACTIVATE`; `GET /killswitch` | Disables trading "for current trading day". To activate: "ensure that all your positions are closed and there are no pending orders".\[22\] Dhan support: if activated twice in a day, "it cannot be deactivated manually… You will be able to place orders again from the next working day."\[23\] A forum post says it can be deactivated "only once a day" and resets at midnight\[24\] | dhanhq.co Trader's Control; dhan.co support; madefortrade |
| **P&L Based Exit** | `POST`/`GET`/`DELETE /pnlExit` with `profitValue`, `lossValue`, `productType`, `enableKillSwitch` | "all applicable positions are exited" on breach. Active only for the current day and "reset at the end of the trading session". Values beyond the current P&L trigger immediately.\[22\] Runs on Dhan's infrastructure\[25\] | dhanhq.co; madefortrade launch post (4 May 2026)\[22\] |
| **Exit All** | `DELETE /positions` | "Exit all active positions and cancel all open orders for the current trading day" | dhanhq.co Portfolio\[26\] |
| **Live Order Update** | `wss://api-order-update.dhan.co` \[18\] | Covers all orders "irrespective of the platform"; includes a `Source` field | dhanhq.co |
| **Rate limits** | Order APIs: 10/s, 250/min, 1000/hr, 7000/day\[27\] | 25 modifications per order | Dhan support\[28\] |
| **MPP** | API market orders → limit orders with MPP\[18\] (10% on options) | Unfilled orders stay pending until end of day | Dhan freshdesk; madefortrade\[19\] |
| **Auto square-off** | Intraday positions not squared off by **3:20 PM** are closed; another support page says the process "starts by 3:18 pm" | — | dhan.co support\[29\]\[30\] |

**Conflicts in Dhan's documentation:**
- The v2.5 release notes (9 Feb 2026) describe the P&L exit as a "percentage", but the API docs and Python SDK v2.3.0 say "absolute value thresholds".\[28\]\[31\] Trust the API docs, but test it.\[28\]
- Treat 15:18 as the hard square-off deadline. SentinelX's 15:10 square-off leaves an 8-minute buffer.

**Missing from Dhan:** a profit-preserving (trailing) P&L exit. A user asked for one, and Dhan said it "will evaluate this with our team".\[25\] R2 therefore has to live inside SentinelX.

### D.2 Layering sequence
1. **Pre-open:** arm and verify `/pnlExit` (R12). In Live mode, allow no entries until it is verified.
2. **Intraday tightening:** re-send the backstop, keeping `lossValue` beyond the *current* loss.
3. **Automatic kill:** R10 exit chase → Exit All if needed → once the book is flat, **activate Dhan's Kill Switch**. This also blocks manual revenge trades in the Dhan app. Track `kill_activations_today`.
4. **Manual kill from the dashboard or Telegram:** require a separate confirmation before activating Dhan's Kill Switch, so a mistaken kill doesn't lock the account for the day.
5. **Engine down:** catastrophic SLs plus Dhan's P&L exit act as backstops. Run an external heartbeat monitor on a second host. Reserve about 4 of the 10 per-second tokens for exits and cancels.

---

## Part E — Lessons, pitfalls and regulatory context
1. **Software-side rules die with the process.** In TradeMate, rules "stop when TradeMate stops"; only SLs held at the broker survive. Keep the catastrophic SL and Dhan's P&L exit mandatory in Live mode.
2. **Resetting rules on restart is dangerous for automation.** TradeMate resets them on purpose. SentinelX's persisted ratchets are the right choice; add re-validation against the broker.
3. **Delayed or tick-confirmed exits help profit-taking, not loss-cutting.** A latched kill switch "caps the bleeding near the limit but cannot perfectly pin it". OpenAlgo's ₹50,000 example tripped at ₹-56,468.\[15\]
4. **Vendor examples can be wrong.** MyAlgoMate's own Trail Max Loss example is inconsistent, so write property tests from your own formulas.
5. **Skipping exits on expensive shorts is a hole on the loss side.** The "Don't Sell Exit Position Above" behaviour should not be copied for loss exits.
6. **SEBI framework:** circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025), fully applicable from **1 April 2026**\[32\] under the extension circular of 30 Sep 2025.\[33\]
   - **Rate:** algos you build yourself that stay below **10 orders per second per exchange** don't need exchange registration.\[34\] Hard-cap SentinelX well below that.\[35\]
   - **Static IP:** a whitelisted static IP is mandatory for order APIs.\[35\]\[36\] FYERS says each IP "cannot be shared across multiple users",\[37\] and MyAlgoMate says one static IP "for each trading account". **Plan a second static IP for your spouse's account in Stage 3**, and confirm Dhan's policy.
   - **Family use:** the circular lets a self-built algo be used by your family (self, spouse, dependent children and dependent parents).\[38\]
   - **Daily login:** daily 2FA and short-lived tokens\[39\] mean the morning sequence (login → arm backstop → reconcile) must be automated, with alerts on failure.
   - **Market orders:** these now become MPP limit orders (Dhan, from 21 March 2026).\[18\]\[35\] R10 is the mitigation.
7. **Static-IP egress is a single point of failure, and orphaned stop orders become entries.** Monitor order-API health separately from data health. Treat R7's orphan-SL cancellation as P0.

---

## Part F — Prioritised roadmap
| Stage | Items | Exit criteria |
|---|---|---|
| **S0: before any Live trading (P0)** | R7 (WebSocket + position poll + orphan-SL cancel); R9; R10 (chase, shorts first, Exit All escalation); R12; `risk_event` audit; monotonic SQL; idempotent kill | 20 paper sessions with zero unreconciled drift; simulated outages and rejections end fail-closed; time from kill to flat is measured |
| **S1: Live, Manual mode, one account (P1)** | R2 (shadow mode for 10 sessions first); R1; R3; R6; R8; Dhan Kill Switch after an automatic flatten; Telegram test on save, heartbeat and dead-man monitor; tighten-only validator | Property tests show the floor never decreases; no false kills |
| **S2: Semi-auto / Full auto (P1/P2)** | Strategy scope and budgets; R5; R4; unit conversion; optional profit debounce and exit selectors | Each strategy runs at least 20 paper sessions within its budget |
| **S3: Multi-account (P1)** | R11; per-account static IP and backstop; dashboard number line (floor / peak / MTM) per account | A kill on one account never blocks the other's exits |

---

## Caveats
- I couldn't retrieve MyAlgoMate's full RMS user guide (`/llms/rms-tool.txt`) or its glossary. Where I've inferred behaviour from sibling products, it is labelled. I could not verify the RMS Tool's runtime behaviour or latency.
- Dhan's documentation contradicts itself in places (P&L exit as % vs absolute; 3:18 vs 3:20 square-off), and forum posts are secondary sources. Verify each behaviour on a low-risk live session.
- SEBI details beyond the circulars come from broker notices. Confirm the static-IP and family-use rules with Dhan.
- All parameters and defaults are engineering suggestions, not trading advice.

## Sources
- MyAlgoMate RMS Tool: https://myalgomate.com/products/rms-tool
- MyAlgoMate StockMock/AlgoTest: https://myalgomate.com/products/stockmock-algotest-strategy
- MyAlgoMate TradeMate: https://myalgomate.com/product/trademate-options-trading
- MyAlgoMate Copy Trading: https://myalgomate.com/products/copy-trading-software
- MyAlgoMate 5 EMA: https://myalgomate.com/products/5-ema-strategy-power-of-stocks
- MyAlgoMate Dhan page: https://myalgomate.com/brokers/dhan
- MyAlgoMate Telegram: https://t.me/s/myalgomatestraddle?before=237
- DhanHQ Trader's Control: https://dhanhq.co/docs/v2/traders-control/
- DhanHQ Portfolio: https://dhanhq.co/docs/v2/portfolio/
- DhanHQ Live Order Update: https://dhanhq.co/docs/v2/order-update/
- DhanHQ releases: https://dhanhq.co/docs/v2/releases/
- dhanhq PyPI: https://pypi.org/project/dhanhq/
- Dhan P&L exit launch: https://madefortrade.in/t/now-live-p-l-based-exit-and-exit-all-on-apis/64092
- Dhan Kill Switch: https://dhan.co/support/account-related/traders-controls/how-can-i-activate-the-kill-switch/
- Dhan rate limits: https://dhan.co/support/platforms/dhanhq-api/how-many-orders-can-i-placed-using-dhan-api/
- Dhan MPP: https://dhan.freshdesk.com/support/solutions/articles/82000911330-what-is-the-mpp-market-price-protection-
- Dhan MPP forum: https://madefortrade.in/t/market-price-protection/61897
- Dhan square-off: https://dhan.co/support/orders-and-positions/order-placement-and-modifications/what-is-intraday-square-off/ ; https://dhan.co/support/orders-and-positions/positions/under-what-circumstances-will-dhan-automatically-close-my-positions-trades/
- Dhan hedged-leg exit: https://madefortrade.in/t/square-of-time-and-order-execution-time/8008
- SEBI extension circular: https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf
- SEBI circular (4 Feb 2025): https://www.cse-india.com/upload/upload/Feb_042025.pdf
- SEBI family-use summary: https://www.finseclaw.com/article/finsec-tracker-on-sebi-issues-guidelines-on-retail-participation-in-algorithmic-trading
- FYERS static IP: https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026
- OpenAlgo kill switch: https://openalgo.in/quant/kill-switches-risk-controls

## Sources

1. [dhan-api-v2-docs/01-introduction.md at main · Kalaiviswa/dhan-api-v2-docs · GitHub](https://github.com/Kalaiviswa/dhan-api-v2-docs/blob/main/01-introduction.md)
2. [RMS Tool: Auto Square Off & Max Loss Software · MyAlgoMate](https://myalgomate.com/products/rms-tool)
3. [NSE F&O Lot Sizes (Nov 2026): Nifty, Bank Nifty · MyAlgoMate](https://myalgomate.com/tools/fno-lot-sizes)
4. [Square Off in Share Market: Meaning, Tyoes, Tax Implications](https://www.bajajfinserv.in/what-is-square-off-in-stock-market)
5. [Stockmock and Algotest strategy automation tool (Time Based Straddle Strangle) · MyAlgoMate](https://myalgomate.com/products/stockmock-algotest-strategy)
6. [Video tutorials for MyAlgoMate's algo trading software.](https://myalgomate.com/tutorials)
7. [Myalgomate Time based Straddle/Strangle](https://t.me/s/myalgomatestraddle?before=237)
8. [Algo Trading with Dhan · MyAlgoMate](https://myalgomate.com/brokers/dhan)
9. [RMS Tool - Risk Management Tool - Myalgomate](https://www.myalgomate.com/product/rms-tool/)
10. [Myalgomate Time based Straddle/Strangle](https://t.me/s/myalgomatestraddle?before=252)
11. [TradeMate: Multi-Expiry Option Chain Software · MyAlgoMate](https://myalgomate.com/product/trademate-options-trading)
12. [5 EMA Strategy Automation (Power of Stocks) Software · MyAlgoMate](https://myalgomate.com/products/5-ema-strategy-power-of-stocks)
13. [Copy Trading Software in India — One Master, Many Accounts](https://myalgomate.com/products/copy-trading-software)
14. [ORB Algo: Opening Range Breakout Automation Software · MyAlgoMate](https://myalgomate.com/products/orb-opening-range-breakout)
15. [Kill Switches, Risk Controls and Algo Surveillance - Quantitative Trading](https://openalgo.in/quant/kill-switches-risk-controls)
16. [Live Order Update - DhanHQ Ver 2.0 / API Document](https://dhanhq.co/docs/v2/order-update/)
17. [Square of time and order execution time - MadeForTrade](https://madefortrade.in/t/square-of-time-and-order-execution-time/8008)
18. [GitHub - Kalaiviswa/dhan-api-v2-docs: DhanHQ API v2.0 — subdivision-wise markdown documentation · GitHub](https://github.com/Kalaiviswa/dhan-api-v2-docs)
19. [What is the MPP (Market Price Protection)? : . - Dhan](https://dhan.freshdesk.com/support/solutions/articles/82000911330-what-is-the-mpp-market-price-protection-)
20. [Market Order via API Converted to Limit Order Without Slippage Buffer - APIs, Automation, Algos & Code - MadeForTrade](https://madefortrade.in/t/market-order-via-api-converted-to-limit-order-without-slippage-buffer/94274)
21. [Master and Child Accounts in Copy Trading, Explained · MyAlgoMate](https://myalgomate.com/blog/master-child-copy-trading-explained)
22. [Trader's Control - DhanHQ Ver 2.0 / API Document](https://dhanhq.co/docs/v2/traders-control/)
23. [How can I activate the Kill Switch?](https://dhan.co/support/account-related/traders-controls/how-can-i-activate-the-kill-switch/)
24. [Introducing: Kill Switch API on DhanHQ Trading APIs for Managing Risk - APIs, Automation, Algos & Code - MadeForTrade](https://madefortrade.in/t/introducing-kill-switch-api-on-dhanhq-trading-apis-for-managing-risk/25002)
25. [Now Live: P&L based Exit and Exit All on APIs](https://madefortrade.in/t/now-live-p-l-based-exit-and-exit-all-on-apis/64092)
26. [Portfolio and Positions - DhanHQ Ver 2.0 / API Document](https://dhanhq.co/docs/v2/portfolio/)
27. [How many orders can I placed using Dhan API?](https://dhan.co/support/platforms/dhanhq-api/how-many-orders-can-i-placed-using-dhan-api/)
28. [Releases - DhanHQ Ver 2.0 / API Document](https://dhanhq.co/docs/v2/releases/)
29. [What is intraday square-off?](https://dhan.co/support/orders-and-positions/order-placement-and-modifications/what-is-intraday-square-off/)
30. [Under what circumstances will Dhan automatically close my positions / trades?](https://dhan.co/support/orders-and-positions/positions/under-what-circumstances-will-dhan-automatically-close-my-positions-trades/)
31. [dhanhq · PyPI](https://pypi.org/project/dhanhq/)
32. [SEBI's Algo Trading Framework: New Rules for Retail Algorithmic Trading](https://www.equityresearchindia.com/post/sebi-s-algo-trading-framework-new-rules-for-retail-algorithmic-trading)
33. [SEBI Algo Trading Rules 2026 for Retail Investors · RTI Wiki](https://righttoinformation.wiki/sebi-algo-trading-rules-retail-investors-2026)
34. [SEBI's New Algo Trading Rules From April 2026: What Every Retail Trader Must Know](https://www.tradejini.com/blogs/what-sebis-new-algo-trading-rules-mean-for-you)
35. [DhanHQ Update : Safer participation of retail investors in Algo Trading - Static IP, MPP & Rate Limits - APIs, Automation, Algos & Code - MadeForTrade](https://madefortrade.in/t/dhanhq-update-safer-participation-of-retail-investors-in-algo-trading-static-ip-mpp-rate-limits/61689)
36. [dhanhq-skills/skills/dhanhq/references/orders.md at main · dhan-oss/dhanhq-skills · GitHub](https://github.com/dhan-oss/dhanhq-skills/blob/main/skills/dhanhq/references/orders.md)
37. [What are the new SEBI rules for retail algo trading from April 01, 2026?](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)
38. [SEBI Issues Guidelines on Retail Participation in Algorithmic Trading](https://www.finseclaw.com/article/finsec-tracker-on-sebi-issues-guidelines-on-retail-participation-in-algorithmic-trading)
39. [New SEBI Framework for Retail Algo Trading from April 01, 2026 - FYERS](https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026)
