# Proposal

## Why

`add-risk-engine` built the gate, the equity floor, the kill switch and the first set of rules (PRD §7, tier A). Several guards in the PRD are still missing. Nothing stops a good morning from being given back after a target, nothing caps the size of the book in lots, and nothing slows a burst of entries or reacts to a storm of broker rejections. Expiring contracts are treated like any others, and nothing keeps entries out of banned or unwanted instruments. This change is the second half of PRD Stage 3. It adds the tier-B rules at the seam `add-risk-engine` left for them, so S4's exits and S5's API are built on the full rule set.

## What Changes

- **Daily profit target** (PRD §7 R1): once `MTM_day ≥ daily_profit_target` for `target_confirm_ticks` consecutive monitor passes, the day is locked at a new halt level `TARGET_HALT` (persisted, between MTM-blind and kill). The engine flattens through the existing flattener and refuses entries for the rest of the day, even if MTM falls back. It is optional: off unless set. Dhan's broker lock after the stop remains S7 (D-030).
- **Lot caps** (R3): open lots plus the lots of pending entries plus this order must stay within a per-underlying cap and a total cap. Gross entry lots today must stay within a daily cap. Lots come from the instrument master's lot size. The day's gross lots are counted and persisted before the entry is sent, like the day's entry count.
- **Entry pacing** (R6): a maximum number of entries per rolling minute and a minimum number of seconds between entries.
- **Rejection breaker** (R6): `reject_breaker_n` broker rejections within a rolling 5 minutes halt new entries and raise a critical alert. The halt clears once the window falls below the threshold. Refusals by the engine's own gate or rate limiter are not broker rejections.
- **Expiry-day profile** (R8): optional overrides for contracts whose expiry date (from the instrument master) is today: an earlier entry cutoff, an earlier own square-off for intraday positions, tighter lot caps, a higher minimum premium and a tighter giveback. Each override must be at least as tight as its base value. The tighter giveback applies from the moment the account holds or enters a contract expiring today, for the rest of that day.
- **Instrument policy** (R4): allow and deny lists of underlyings, the F&O ban list, a maximum strike distance from the underlying's price for options, and a minimum option premium. The ban list is loaded by the owner each day through a new command and CLI file. With no list for today, every F&O entry is refused (fail closed).
- **Limits**: about fifteen new limits, each with a unit and tightening direction. A new `SYMBOLS` unit, with `subset` and `superset` directions, covers the allow and deny lists. The lot caps, pacing and breaker limits are **required**, so limits stored before this change become unusable until the owner loads a new limits file. That can only happen after the close, which is existing behaviour. This is the one **BREAKING** step: entries stay refused until it is done.
- **Scope moved out**, agreed with the owner on 2026-11-21:
  - **Bid-ask spread check:** deferred, because the feed carries no bid or ask (Quote mode). The PRD row is marked as waiting on market depth.
  - **Modification cap:** moved to S4 `add-trailing-exits`, because nothing modifies an order before S4's exit manager (D-027). The S3 flattener cancels and replaces instead.
- **Contract additions** (all optional or new, `schema_version` stays 1): `HaltLevel.TARGET_HALT = 20` (reserved for this by `add-risk-engine`), a `SetBanList` command, and new fields in the snapshot's risk block (gross lots today, whether today's ban list is loaded, recent broker rejections). The CLI gains `risk set-ban-list`.

Out of scope:
- **S4:** the modification cap, the exit manager and trailing.
- **S7:** Dhan's broker lock after a profit-target stop, Telegram alerts, fetching the ban list from NSE automatically.
- **Not planned:** the bid-ask spread check (needs a depth feed), re-entry caps (RMS R5, not in the PRD), blocking stock options from being held into expiry (RMS R8 default, not in the PRD), and a holiday calendar.

## Capabilities

### New Capabilities
- `profit-target`: the daily profit target, its confirmation count, the `TARGET_HALT` day lock and flatten, and its visibility.
- `lot-caps`: per-underlying and total open-lot caps with pending-entry reservations, and the daily gross-lot cap with its persisted counter.
- `order-flow-guards`: entry pacing (per-minute count and minimum gap) and the broker-rejection breaker. S4's modification cap will join this capability.
- `expiry-day-profile`: how a contract is judged to expire today, the override limits and their at-least-as-tight rule, and how each override applies to the cutoff, square-off, lot caps, minimum premium and giveback.
- `instrument-policy`: allow and deny lists, the F&O ban list with its load command and fail-closed rule, the strike-distance and minimum-premium checks, and the rule that the policy never applies to exits.

### Modified Capabilities
- `risk-limits` (introduced by `add-risk-engine`, not yet archived): one `ADDED` requirement for set-valued limits (the `SYMBOLS` unit with `subset` and `superset` tightening directions). Its existing requirements are not modified, so this change does not copy text from an unarchived spec. It must be archived after `add-risk-engine`.

## Impact

- **Code**:
  - New checks in `src/engine/risk/checks/` (profit target, lot caps, pacing, rejection breaker, instrument policy), appended to `registry.py`.
  - `engine/risk/limits.py`: new limits and the `SYMBOLS` unit.
  - `engine/risk/floor.py` and the monitor: the expiry giveback.
  - `engine/risk/monitor.py`: acting on `TARGET_HALT`, the target confirmation count, and expiry square-off.
  - `engine/risk/daybook.py` and `state.py`: gross lots, last entry time, expiry exposure.
  - New ban-list store and service.
  - `engine/orders/service.py`: a rejection listener hook, alongside the existing fill listener.
  - `engine/market_data/instruments.py`: a lookup from an underlying symbol to its spot instrument.
  - `contracts/commands.py` and `contracts/snapshot.py`; `cli`.
- **Database**: migration `0005` adds three columns to `risk_day_state` (gross entry lots, last entry time, expiry exposure, all merged one-way) and a `risk_ban_list` table keyed by `(account_id, trade_date)`.
- **Config**: none new in the environment. Strike-distance checks need the underlying's index or stock in `FEED_INSTRUMENTS`; without a fresh price for it, option entries are refused.
- **Docs**: PRD §7 table and §11 (S3 and S4 scope, the stage table) for the two moved items; `docs/risk-limits.example.json` and the README's limits section for the new limits and the ban-list command.
- **Dependencies**: none new.
- **Ordering**: implement after PR #9 (`add-risk-engine`) is reviewed and merged, and archive after `add-risk-engine`.
