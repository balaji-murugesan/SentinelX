# Proposal

## Why

Stage 2 left every order passing through a stub `RiskGate` that lets paper orders through and refuses every live order, because no risk rule exists yet. Nothing real can be traded until rules protect the account, and the rules must be correct before the exits, API and dashboard of later stages are built on top of them. This change is the first half of PRD Stage 3. It replaces the stub with the real gate, the equity-floor ratchet, the kill switch, the catastrophic stop-loss and the order rate limiter, all running on the paper broker. A second change, `add-risk-guards`, then adds the remaining rules at the same seam.

## What Changes

- **Real `RiskGate`** behind the unchanged `check(order) -> str | None` seam, so no call site changes. Each rule is a `RiskCheck` (a `Protocol`) with a pre-trade phase, a continuous phase, or both. Decisions are ranked ALLOW < REJECT < HALT_NEW_ENTRIES < TARGET_HALT < KILL and the gate acts on the most severe (PRD §7.2, D-026).
- **Entry or exit, by position effect.** An order that only reduces a position is an exit; anything that opens or grows a position is an entry. Exits are never blocked by entry rules (PRD §7).
- **Exits keep working when the engine is degraded.** Today the order service refuses *every* order when persistence is down or reconciliation is pending. Safety rule 9 says only entries are refused, so exits are accepted in both states (the order is held in memory with its write queued).
- **Limits as configuration** stored in Postgres. Every parameter declares a unit and a tightening direction. During 09:15–15:30 IST an edit is accepted only if every changed parameter tightens and the re-simulated floor does not fall. No limit has a built-in default: until the owner sets limits, every entry is rejected.
- **Pre-trade rules (PRD §7, tier A):** per-trade max risk, position sizing, max trades per day, consecutive-loss cooldown, max open positions (total and per underlying, carried positions included), entry cutoff, price sanity, own square-off of intraday positions. Quantity sanity is already enforced by S2 order validation.
- **Equity floor** replaces the flat daily max loss (PRD §7.1). `MTM_day` is **net of estimated charges**, and carried positions are measured from the **previous close**, so the floor tracks today's change only. The floor and the day's peak only ever rise: they are persisted before the engine acts, written with `GREATEST`, and restored on restart as `max(persisted, recomputed)`.
- **Stale-data guard:** stale quotes, a silent feed, an unknown MTM and outlier ticks (PRD §7, "Stale data / outliers").
- **Kill switch:** a persisted HALTED state, cancel of pending entries, flatten of every position including carried CNC, no intraday resume, reset only at the next trading day. Reachable from the CLI and the command stream; the equity-floor breach triggers it. Telegram `/kill` stays in S7 (PRD §12 Q8).
- **Catastrophic stop-loss:** one wide SL-M per open position, placed after the entry fills, resized by place-new-then-cancel-old, cancelled when the position goes flat. Carried CNC and MARGIN positions get a fresh SL each morning after reconciliation (PRD §12 Q12). The order type lives in one place, and verifying it against recorded Dhan responses is a gate before S7 (PRD §12 Q17).
- **Order rate limiter** in front of the broker adapter: per-second and per-minute token buckets with a share reserved for exits and cancels, and a hard ceiling below SEBI's 10 orders/s.
- **Risk audit:** an append-only `risk_event` table with stable reason codes. Each rule runs in `enforce` or `shadow` mode.
- **Contract additions** (all optional or new, `schema_version` stays 1): an initial stop on the place command and order, a `STOP_MARKET` order type with a trigger price, and new commands for the kill switch, risk-limit edits and sizing. The CLI gains matching commands. The snapshot gains a risk block.

Out of scope:
- **`add-risk-guards`:** daily profit target, lot caps, entry pacing, rejection breaker, modification cap, expiry-day profile, instrument policy.
- **S4:** the exit manager, trailing methods and partial exits. The kill switch ships a simple flattener behind a seam that S4 replaces.
- **S7:** Telegram `/kill`, the Dhan broker lock, Exit All and P&L-exit backstops, morning sequence, `DhanBroker` order placement.
- **Not planned yet:** a REST-quote fallback for MTM when the feed is silent (no REST quote client exists), multi-leg ordering (§12 Q10), multi-account (S8).

## Capabilities

### New Capabilities
- `risk-gate`: the real `RiskGate`, the `RiskCheck` interface and severity ranking, entry-versus-exit classification, fail-closed behaviour, shadow and enforce modes, and the append-only `risk_event` audit.
- `risk-limits`: limit storage, declared units and tightening directions, tighten-only edits in market hours, the no-defaults rule, and the limit-edit command.
- `risk-rules`: the pre-trade rules marked A in PRD §7 (per-trade risk, sizing, trades per day, cooldown, open positions, entry cutoff, price sanity) and the own square-off.
- `equity-floor`: net `MTM_day` with the previous-close basis, the floor and peak ratchet, persistence and restart behaviour, the pre-trade floor check and the breach trigger.
- `stale-data-guard`: stale quotes, silent feed, unknown MTM and outlier ticks.
- `kill-switch`: the kill command, the persisted day lock, cancel and flatten, and the reset rule.
- `catastrophic-stop`: SL placement and upkeep for open positions, morning re-placement for carried positions, and the Q17 live gate.
- `order-rate-limiter`: client-side token buckets with exit priority and a hard ceiling.

### Modified Capabilities
None can be written yet. The `orders`, `order-commands` and `trading-state` specs exist only inside the unarchived `add-order-position-core` change (its task 9.2, the market-hours smoke test, is still open), so `openspec/specs/` has nothing to modify. Once that change is archived, this change adds `MODIFIED` deltas for:
- `orders`: "Mandatory pre-trade gate" (the paper-only stub sentence is replaced by the real gate), the order-type list (adds `STOP_MARKET`), and the exit-while-degraded rule;
- `order-commands`: the new commands and fields;
- `trading-state`: persistence-outage and recovery-halt wording for exits.

This is task 1.1 and must be done before this change is archived.

## Impact

- **Code**:
  - New `src/engine/risk/` (gate, checks, limits, equity floor, MTM tracker, monitor, kill switch, catastrophic stops, rate limiter).
  - New `src/engine/orders/intent.py` (entry-or-exit classification, shared by the gate, the service and the limiter).
  - `src/engine/orders/service.py`: exits accepted when degraded, and a fill listener hook.
  - `src/engine/orders/gate.py`: `PaperOnlyGate` removed.
  - `src/engine/broker/paper.py`: `STOP_MARKET` orders.
  - `src/contracts/orders.py`, `commands.py`, `snapshot.py`: the additions above.
  - `src/engine/main.py` wiring and `src/cli/` commands.
- **Database**: migration `0004` adds `risk_limits`, `risk_day_state` and `risk_event`, plus an `initial_stop` / `trigger_price` column pair on `orders`. The engine role is denied UPDATE and DELETE on `risk_event`.
- **Config**: environment settings only for the rate-limiter ceiling and monitor intervals. Every risk limit lives in Postgres.
- **PRD / docs**: §7.2 gains a time-of-day unit; `docs/DECISIONS.md` confirms D-026 and D-030 (engine lock) and records the Q12, Q13, Q17 and Q18 outcomes; the PRD §12 rows for those questions are marked resolved.
- **Dependencies**: none new.
- **Downstream**: `add-risk-guards` adds its rules at the same seam. S4's exit manager replaces the kill switch's flattener. S7 adds the broker lock, Exit All and Telegram `/kill`.
