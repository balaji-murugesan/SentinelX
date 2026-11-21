"""The continuous stale-data checks and the equity-floor breach (add-risk-engine tasks 7.1 and 7.2;
stale-data-guard spec "A silent feed halts new entries" and "Unknown MTM flattens the book";
equity-floor spec "Reaching the floor triggers the kill switch").

These run on a tick or on the monitor's timer, not on an order. Each is a pure function of the
``RiskContext`` and returns one decision; the monitor acts on the most severe one.

  * ``FeedDown``     - while the market is open and something is subscribed, no tick at all for
    ``feed_down_s`` halts new entries (``RISK_FEED_DOWN``). The halt clears by itself when ticks
    resume (the monitor recomputes it on every pass). Silence after the close raises nothing.
  * ``MtmBlind``     - during market hours, an open position with no fresh price for
    ``mtm_blind_s`` is a book the engine can no longer value. It returns ``RISK_MTM_BLIND``; the
    monitor then locks entries for the rest of the day (a persisted halt, no broker lock) and the
    book is flattened. The age of each position is taken from the price tracker, **not** from "the
    day's MTM is unknown": MTM is also unknown when a carried position was closed today with no
    previous-close reference, and that must refuse entries but must not flatten anything. Only
    price-blindness does that.

    Without the market-hours gate and a session anchor this would sell every carried holding each
    evening: yesterday's last tick is already hours old at the open, and ticks stop at 15:30. So
    age is measured from no earlier than today's open, and nothing fires outside 09:15-15:30 IST
    on a weekday. (No holiday calendar yet: a weekday exchange holiday looks like a silent feed.)
  * ``FloorBreach``  - ``MTM_day`` at or below the floor is a KILL, with no debounce on the loss
    side (the floor is a trigger, not a price guarantee). If the floor cannot be computed (the
    limits in force cannot make one) it compares with the last good floor the engine persisted,
    and halts new entries either way: fail closed, never "no floor".

Trading terms: blind - holding a position whose price the engine cannot see; flatten - exit every
position.
"""

from datetime import datetime, timedelta
from typing import Final

from engine.market_data.clock import IST, MARKET_OPEN, is_market_hours
from engine.risk.checks.common import money
from engine.risk.decision import ALLOW, Decision, RiskContext, RiskTrigger, halt_new_entries, kill
from engine.risk.floor import next_floor

FEED_DOWN: Final = "RISK_FEED_DOWN"
MTM_BLIND: Final = "RISK_MTM_BLIND"
FLOOR_BREACH: Final = "RISK_FLOOR_BREACH"
FLOOR_UNAVAILABLE: Final = "RISK_FLOOR_UNAVAILABLE"


class FeedDown:
    name = "stale_data"

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        if not is_market_hours(ctx.now) or not ctx.feed_subscribed or ctx.feed_silent_for is None:
            return ALLOW
        limit = timedelta(seconds=ctx.limits.feed_down_s)
        if ctx.feed_silent_for > limit:
            return halt_new_entries(
                FEED_DOWN,
                f"no tick for {ctx.feed_silent_for.total_seconds():.0f}s "
                f"(threshold {ctx.limits.feed_down_s}s)",
                silent_s=int(ctx.feed_silent_for.total_seconds()),
                threshold_s=ctx.limits.feed_down_s,
            )
        return ALLOW


class MtmBlind:
    name = "stale_data"

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        if not is_market_hours(ctx.now):
            return ALLOW  # no ticks are expected, so silence means nothing
        limit = timedelta(seconds=ctx.limits.mtm_blind_s)
        since_open = ctx.now - datetime.combine(
            ctx.now.astimezone(IST).date(), MARKET_OPEN, tzinfo=IST
        )
        for position in ctx.positions.values():
            if position.quantity == 0:
                continue
            quote = ctx.quote(position.key)
            # A priced position is as stale as its last tick. One that has never been priced is
            # as stale as the time the engine has been waiting for its first tick.
            age = quote.age if quote is not None else ctx.unpriced_for.get(position.key)
            if age is not None:
                age = min(age, since_open)  # yesterday's last tick is not today's silence
            if age is not None and age > limit:
                return halt_new_entries(
                    MTM_BLIND,
                    f"no fresh price for {position.key} for {age.total_seconds():.0f}s "
                    f"(threshold {ctx.limits.mtm_blind_s}s)",
                    instrument=str(position.key),
                    age_s=int(age.total_seconds()),
                    threshold_s=ctx.limits.mtm_blind_s,
                )
        return ALLOW


class FloorBreach:
    name = "equity_floor"

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        if ctx.mtm_day is None:
            return ALLOW  # unknown MTM refuses entries (FloorHeadroom); it is not a breach
        try:
            # The same floor the monitor ratchets: with the expiry giveback once the account has
            # had expiring exposure today (add-risk-guards task 8.2).
            floor = next_floor(
                ctx.day.floor,
                ctx.limits.floor_params(expiry=ctx.day.expiry_exposure),
                ctx.day.peak,
            )
        except (ValueError, TypeError, ArithmeticError) as error:
            # The limits in force cannot make a floor. Fall back to the last good one, so a
            # breach of it is still caught, and stop new entries in any case.
            last_good = ctx.day.floor
            if last_good is not None and ctx.mtm_day <= last_good:
                return kill(
                    FLOOR_BREACH,
                    f"MTM_day {money(ctx.mtm_day)} is at or below the last good floor "
                    f"{money(last_good)}",
                    mtm_day=money(ctx.mtm_day),
                    floor=money(last_good),
                )
            return halt_new_entries(
                FLOOR_UNAVAILABLE,
                f"the floor cannot be computed ({type(error).__name__}), so new entries stop",
                error=type(error).__name__,
            )
        if ctx.mtm_day <= floor:
            return kill(
                FLOOR_BREACH,
                f"MTM_day {money(ctx.mtm_day)} is at or below the floor {money(floor)}",
                mtm_day=money(ctx.mtm_day),
                floor=money(floor),
                peak=money(ctx.day.peak),
            )
        return ALLOW
