# Spec Delta

## Purpose

Defines the kill switch: the one action that stops all trading for the day, cancels pending entries and flattens every position. A risk halt is an emotional circuit breaker, so it has no intraday undo. The broker-side lock and Telegram trigger arrive in a later stage.

## ADDED Requirements

### Requirement: What triggers a kill
The engine SHALL run the kill switch when it receives a kill command (from the developer CLI over the command stream) or when a risk check returns a KILL decision, which includes an equity-floor breach. A kill SHALL be idempotent for an account and trading day: a second trigger SHALL NOT start a second kill and SHALL return the first result. A kill from a risk check SHALL NOT depend on the API, the dashboard or the command consumer.

#### Scenario: Floor breach kills without any client
- **WHEN** the equity floor is breached while no CLI, API or dashboard is connected
- **THEN** the kill switch runs

#### Scenario: Kill twice
- **WHEN** a kill command arrives after a kill already ran today
- **THEN** it returns the first result and no new orders are placed

### Requirement: Halt is persisted before anything else
On a kill the engine SHALL first set the account's state to HALTED for the day and persist it, then cancel pending entry orders, then flatten. If the write fails the engine SHALL continue with cancel and flatten and keep retrying the write. After a restart on the same day the engine SHALL still be HALTED and SHALL continue flattening any position that remains.

#### Scenario: Crash mid-flatten
- **WHEN** the engine is killed with `kill -9` after a kill started and one position is still open, then restarted
- **THEN** the account is still HALTED and the open position is flattened after reconciliation

### Requirement: Pending entries are cancelled and every position is flattened
A kill SHALL cancel every open entry order and, while the market is open (09:15 to 15:30 IST), exit every open position, whatever its product: `INTRADAY`, `CNC` and `MARGIN` positions, including those carried from earlier days. An entry that fills while it is being cancelled SHALL be exited too. While the market is closed it SHALL send no exit, because the broker would refuse it, but it SHALL still lock the day and say so in its answer.

#### Scenario: Carried delivery position is flattened
- **WHEN** a kill runs with an open `INTRADAY` position and a carried `CNC` holding
- **THEN** both are exited

#### Scenario: Entry fills during cancel
- **WHEN** an entry fills before its cancel is confirmed
- **THEN** the new position is exited

#### Scenario: Kill with the market closed
- **WHEN** a kill runs at 16:00 IST with a position open
- **THEN** the day is locked, no exit order is sent, a critical alert is logged, and the answer to the kill command says the open positions cannot be exited now

#### Scenario: Kill before the open
- **WHEN** a kill runs at 09:10 IST with a position open and the market then opens the same day
- **THEN** the day stays locked and the position is exited on the first monitor pass inside market hours

### Requirement: Exits are bounded limit orders, replaced only after a confirmed cancel
A kill's exits SHALL be limit orders priced past the latest price by the configured buffer, and the engine SHALL NOT send market orders. If an exit has not filled within the configured chase interval, the engine SHALL cancel it, wait for the cancel to be confirmed, and place a new one at the current price, up to the configured number of steps.

#### Scenario: Exit not filling
- **WHEN** an exit rests unfilled for the chase interval
- **THEN** it is cancelled and replaced at the current price, and no more than the configured number of replacements are made

#### Scenario: Replacement waits for the cancel
- **WHEN** a cancel has not yet been confirmed
- **THEN** no replacement exit is placed

### Requirement: Failure to flatten is loud, never silent
If the book is not flat after the configured escalation time, the engine SHALL log a critical alert and SHALL stay HALTED. It SHALL NOT mark the kill complete until every position is flat and no entry order is pending. Calling the broker's Exit All is not part of this change.

#### Scenario: Not flat in time
- **WHEN** a position is still open after the escalation time
- **THEN** a critical alert is logged, the account stays HALTED and flattening continues

### Requirement: Catastrophic stops are cancelled after the exits
During a kill the engine SHALL place each exit first and cancel the position's catastrophic stop only after the position is flat. It SHALL never leave a stop resting on a flat book.

#### Scenario: Stop after exit
- **WHEN** the exit for a position fills during a kill
- **THEN** its catastrophic stop is cancelled right after

### Requirement: The day is locked with no intraday resume
After a kill the engine SHALL reject every entry with `RISK_HALTED_DAY` until the start of the next trading day, and SHALL keep accepting exits. No command SHALL clear it. The recovery resume command, which clears only a reconciliation halt, SHALL NOT clear it, and a restart SHALL NOT clear it. The halt SHALL clear at the start of the next trading day.

#### Scenario: Resume does not help
- **WHEN** the recovery resume command is sent after a kill
- **THEN** entries are still rejected with `RISK_HALTED_DAY`

#### Scenario: Next day
- **WHEN** the first order of the next trading day arrives
- **THEN** the halt no longer applies and the day's counters are fresh

### Requirement: Engine lock only in this stage
After a kill the engine SHALL NOT request any broker-side lock, and SHALL NOT call the broker's Exit All. Both belong to the live-trading stage.

#### Scenario: No broker lock
- **WHEN** a kill completes and the book is flat
- **THEN** no broker kill-switch call is made
