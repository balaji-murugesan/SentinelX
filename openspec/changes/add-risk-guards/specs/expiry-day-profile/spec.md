# Spec Delta

## Purpose

Applies stricter settings to contracts that expire today, when premiums move fastest and a day's peak is most easily inflated, without loosening anything on any other day or contract.

## ADDED Requirements

### Requirement: A contract expires today only by its master expiry date
A contract SHALL count as expiring today only when its expiry date in the instrument master equals today's IST date. The engine SHALL NOT infer an expiry from the weekday. A contract with no expiry date (equity, index) SHALL never count as expiring.

#### Scenario: Weekly option on its expiry date
- **GIVEN** an option whose master expiry date is today
- **WHEN** an entry for it is judged
- **THEN** the expiry-day overrides apply to it

#### Scenario: Expiry moved by a holiday
- **GIVEN** an option whose usual expiry weekday is today but whose master expiry date is tomorrow
- **WHEN** an entry for it is judged
- **THEN** the expiry-day overrides do not apply to it

### Requirement: Overrides are optional and at least as tight as their base
The expiry overrides SHALL be optional limits: `entry_cutoff_expiry`, `square_off_time_expiry`, `max_open_lots_per_underlying_expiry`, `max_open_lots_total_expiry`, `min_premium_expiry`, `giveback_arm_expiry`, `giveback_abs_expiry` and `giveback_pct_expiry`. An unset override SHALL mean the base value applies. An edit SHALL be rejected as inconsistent if a set override is looser than its set base value.

#### Scenario: Override looser than base
- **WHEN** an edit sets `entry_cutoff` to 14:00 and `entry_cutoff_expiry` to 14:30
- **THEN** the edit is rejected as inconsistent

#### Scenario: Override equal to base
- **WHEN** an edit sets `max_open_lots_total` and `max_open_lots_total_expiry` both to 8
- **THEN** the edit is accepted

#### Scenario: Expiry cutoff after the expiry square-off
- **WHEN** an edit sets `entry_cutoff_expiry` to 14:00 and `square_off_time_expiry` to 13:30
- **THEN** the edit is rejected as inconsistent

### Requirement: A giveback override cannot add a leg to the base giveback
The giveback is the larger of its legs, so a leg that the base term lacks would widen it. When the base giveback term is on, an edit SHALL be rejected as inconsistent if it sets a giveback leg override (`giveback_abs_expiry` or `giveback_pct_expiry`) for a leg the base term does not set. When the base term is off, the expiry overrides alone SHALL form a valid giveback term or be rejected.

#### Scenario: Override adds a missing leg
- **GIVEN** a base giveback with only `giveback_pct` set
- **WHEN** an edit sets `giveback_abs_expiry`
- **THEN** the edit is rejected as inconsistent

#### Scenario: Expiry-only giveback
- **GIVEN** no base giveback
- **WHEN** an edit sets `giveback_arm_expiry` and `giveback_abs_expiry`
- **THEN** the edit is accepted and the giveback term applies only once the account has expiring exposure

### Requirement: Entries in expiring contracts use the expiry cutoff
An entry in a contract expiring today SHALL be refused with `RISK_EXPIRY_CUTOFF` at or after `entry_cutoff_expiry` IST. Entries in other contracts SHALL keep the base cutoff.

#### Scenario: Earlier cutoff on expiry
- **GIVEN** `entry_cutoff` = 15:00 and `entry_cutoff_expiry` = 14:00
- **WHEN** at 14:10 IST an entry in a contract expiring today arrives
- **THEN** it is refused with `RISK_EXPIRY_CUTOFF`

#### Scenario: Other contracts unaffected
- **GIVEN** the same limits
- **WHEN** at 14:10 IST an entry in next week's contract arrives
- **THEN** it is not refused by any cutoff

### Requirement: Intraday positions in expiring contracts are squared off earlier
The engine's own square-off SHALL exit intraday positions in contracts expiring today at `square_off_time_expiry` IST. An intraday position whose instrument has no master record SHALL be squared off at the earlier of the two times. Other positions SHALL keep the base square-off time.

#### Scenario: Expiry square-off
- **GIVEN** `square_off_time` = 15:10 and `square_off_time_expiry` = 14:30
- **WHEN** it is 14:30 IST and an intraday position in a contract expiring today is open
- **THEN** the engine exits that position and leaves an intraday position in next week's contract open until 15:10

### Requirement: Entries in expiring contracts use the expiry lot caps
An entry in a contract expiring today SHALL be judged against `max_open_lots_per_underlying_expiry` and `max_open_lots_total_expiry` in place of the base open-lot caps, counting open and pending lots as the base caps do. The daily gross-lot cap SHALL be unchanged.

#### Scenario: Tighter cap on expiry
- **GIVEN** `max_open_lots_per_underlying` = 4, its expiry override = 2, and 1 open NIFTY lot
- **WHEN** a 2-lot entry in a NIFTY contract expiring today arrives
- **THEN** it is refused with `RISK_LOTS_UNDERLYING`

### Requirement: Option entries in expiring contracts use the higher minimum premium
An option entry in a contract expiring today SHALL be judged against the higher of `min_premium` and `min_premium_expiry`, whichever are set.

#### Scenario: Cheap expiring option
- **GIVEN** `min_premium` = ₹5 and `min_premium_expiry` = ₹15
- **WHEN** an entry at ₹9 in an option expiring today arrives
- **THEN** it is refused with `RISK_MIN_PREMIUM`

### Requirement: The expiry giveback applies once the account has expiring exposure
From the moment the gate allows an entry in a contract expiring today, or the account holds a position in one, the equity floor's giveback term SHALL use each set giveback override in place of its base value for the rest of that IST day. This state SHALL be persisted, only switch on during the day, and reset on the next date.

#### Scenario: Entering an expiring contract
- **GIVEN** a base giveback of ₹2,000 and `giveback_abs_expiry` = ₹1,000
- **WHEN** an entry in a contract expiring today is allowed and the day's peak then reaches its arming level
- **THEN** the floor allows at most ₹1,000 of giveback from the peak

#### Scenario: Expiring position closed
- **GIVEN** the expiry giveback has applied since 10:00
- **WHEN** at 11:00 the expiring position is closed
- **THEN** the expiry giveback still applies and the floor does not fall

#### Scenario: Restart keeps it
- **WHEN** the engine restarts on the same day after the expiry giveback started to apply
- **THEN** the expiry giveback still applies

### Requirement: An expiring entry must leave headroom under the expiry floor
An entry in a contract expiring today SHALL be judged by the floor-headroom rule against the floor computed with the expiry giveback, so that allowing it cannot by itself put `MTM_day` at or below the floor.

#### Scenario: Entry that would trip the tighter giveback
- **GIVEN** a peak of ₹5,000, `MTM_day` of ₹3,200 and `giveback_abs_expiry` = ₹1,500
- **WHEN** an entry in a contract expiring today arrives
- **THEN** it is refused with `RISK_FLOOR_HEADROOM`

### Requirement: Expiry overrides tighten in the same direction as their base
During market hours each expiry override SHALL tighten in its base limit's direction: earlier times, fewer lots, a higher minimum premium, and a smaller giveback or earlier giveback arming. Setting an override that was not set SHALL count as tightening, and removing one SHALL count as loosening.

#### Scenario: Removing an override mid-day
- **WHEN** at 11:00 IST an edit removes `entry_cutoff_expiry`
- **THEN** the edit is rejected with `TIGHTEN_ONLY`
