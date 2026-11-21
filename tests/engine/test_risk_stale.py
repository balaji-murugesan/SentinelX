"""The continuous stale-data checks and the floor breach (add-risk-engine tasks 7.1, 7.2).

Scenarios (stale-data-guard): "Feed goes quiet", "Feed returns", "After the close", "Blind too
long"; (equity-floor): "Breach". Plus the point of the design: an unknown MTM from a missing
reference is not blindness, and a floor that cannot be computed still stops entries.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.risk.checks.stale import (
    FEED_DOWN,
    FLOOR_BREACH,
    FLOOR_UNAVAILABLE,
    MTM_BLIND,
    FeedDown,
    FloorBreach,
    MtmBlind,
)
from engine.risk.decision import Decision, RiskTrigger, Severity, most_severe, run_continuous
from engine.risk.floor import LockMode
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import LIMITS, OPTION, STOCK, context, day_state, position

D = Decimal
TIMER = RiskTrigger.TIMER
# Limits: stale 5 s, feed down 15 s, MTM blind 60 s (see the BASE set in test_risk_limits).
AFTER_CLOSE = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)  # 16:00 IST


def seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def floors(**changes: object) -> RiskLimits:
    """Limits with only the starting loss (floor -2000) unless more terms are given."""
    base = dict(
        daily_max_loss=D(2000), lock_mode=LockMode.OFF, trail_loss_x=None, trail_loss_y=None,
        lock_trigger=None, lock_at=None, trail_step=None, trail_by=None, giveback_arm=None,
        giveback_abs=None, giveback_pct=None,
    )  # fmt: skip
    return replace(LIMITS, **{**base, **changes})  # type: ignore[arg-type]


# ---- FeedDown --------------------------------------------------------------------------------


def test_feed_goes_quiet() -> None:
    """Spec "Feed goes quiet": no tick for longer than the feed-down time at 10:30 IST."""
    ctx = context(feed_subscribed=True, feed_silent_for=seconds(16))

    decision = FeedDown().continuous(ctx, TIMER)

    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == FEED_DOWN
    assert decision.inputs == {"silent_s": 16, "threshold_s": 15}


@pytest.mark.parametrize(("silent", "halted"), [(0, False), (14, False), (15, False), (15.5, True)])
def test_the_feed_down_threshold_is_strict(silent: float, halted: bool) -> None:
    ctx = context(feed_subscribed=True, feed_silent_for=seconds(silent))

    assert (FeedDown().continuous(ctx, TIMER).severity is Severity.HALT_NEW_ENTRIES) is halted


def test_the_halt_clears_when_ticks_resume() -> None:
    """Spec "Feed returns": the check is recomputed on every pass, so nothing latches."""
    quiet = context(feed_subscribed=True, feed_silent_for=seconds(30))
    back = context(feed_subscribed=True, feed_silent_for=seconds(1))

    assert FeedDown().continuous(quiet, TIMER).code == FEED_DOWN
    assert FeedDown().continuous(back, TIMER).severity is Severity.ALLOW


def test_silence_after_the_close_raises_nothing() -> None:
    """Spec "After the close": ten minutes of silence at 16:00 IST."""
    ctx = context(now=AFTER_CLOSE, feed_subscribed=True, feed_silent_for=seconds(600))

    assert FeedDown().continuous(ctx, TIMER).severity is Severity.ALLOW


def test_silence_before_the_open_and_on_a_weekend_raises_nothing() -> None:
    before = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 08:30 IST
    weekend = datetime(2026, 11, 14, 5, 30, tzinfo=UTC)  # Saturday 11:00 IST

    for at in (before, weekend):
        ctx = context(now=at, feed_subscribed=True, feed_silent_for=seconds(600))
        assert FeedDown().continuous(ctx, TIMER).severity is Severity.ALLOW


def test_nothing_subscribed_or_nothing_known_raises_nothing() -> None:
    unsubscribed = context(feed_subscribed=False, feed_silent_for=seconds(600))
    unknown = context(feed_subscribed=True, feed_silent_for=None)

    assert FeedDown().continuous(unsubscribed, TIMER).severity is Severity.ALLOW
    assert FeedDown().continuous(unknown, TIMER).severity is Severity.ALLOW


# ---- MtmBlind --------------------------------------------------------------------------------


def test_blind_too_long() -> None:
    """Spec "Blind too long": the blind time is 60 s and a position has had no tick for 61 s."""
    ctx = context(positions=[position(quantity=65)], prices={OPTION: "100"}, age_s=61)

    decision = MtmBlind().continuous(ctx, TIMER)

    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == MTM_BLIND
    assert decision.inputs["age_s"] == 61 and decision.inputs["threshold_s"] == 60


@pytest.mark.parametrize(("age", "blind"), [(0, False), (60, False), (60.5, True), (600, True)])
def test_the_blind_threshold_is_strict(age: float, blind: bool) -> None:
    ctx = context(positions=[position(quantity=65)], prices={OPTION: "100"}, age_s=age)

    assert (MtmBlind().continuous(ctx, TIMER).code == MTM_BLIND) is blind


def test_a_position_never_priced_is_blind_after_the_wait_not_at_once() -> None:
    held = [position(quantity=65)]

    waiting = context(positions=held, unpriced_for={OPTION: seconds(30)})
    too_long = context(positions=held, unpriced_for={OPTION: seconds(61)})

    assert MtmBlind().continuous(waiting, TIMER).severity is Severity.ALLOW
    assert MtmBlind().continuous(too_long, TIMER).code == MTM_BLIND


def test_a_position_with_no_price_and_no_wait_recorded_is_not_called_blind() -> None:
    """Nothing says how long it has been: the monitor records the wait, so a missing record is
    "just started waiting", never an instant flatten."""
    assert MtmBlind().continuous(context(positions=[position(quantity=65)]), TIMER).code is None


def test_flat_positions_cannot_be_blind() -> None:
    flat = position(quantity=0, average="0")
    ctx = context(positions=[flat], prices={OPTION: "100"}, age_s=600)

    assert MtmBlind().continuous(ctx, TIMER).severity is Severity.ALLOW


def test_one_blind_position_among_priced_ones_is_enough() -> None:
    held = [position(OPTION, 65), position(STOCK, 10, "50")]
    ctx = context(
        positions=held, prices={OPTION: "100"}, age_s=1, unpriced_for={STOCK: seconds(90)}
    )

    decision = MtmBlind().continuous(ctx, TIMER)

    assert decision.code == MTM_BLIND and decision.inputs["instrument"] == str(STOCK)


def test_an_unknown_mtm_from_a_missing_reference_is_not_blindness() -> None:
    """Every open position has a fresh price; the day's MTM is unknown for another reason (a
    carried position closed today, no reference). That refuses entries but must not flatten."""
    ctx = context(positions=[position(quantity=65)], prices={OPTION: "100"}, age_s=1, mtm_day=None)

    assert MtmBlind().continuous(ctx, TIMER).severity is Severity.ALLOW


@pytest.mark.parametrize(
    "at",
    [
        AFTER_CLOSE,
        datetime(2026, 11, 16, 3, 0, tzinfo=UTC),  # 08:30 IST, before the open
        datetime(2026, 11, 14, 5, 30, tzinfo=UTC),  # Saturday 11:00 IST
    ],
    ids=["after the close", "before the open", "weekend"],
)
def test_no_book_is_blind_outside_market_hours(at: datetime) -> None:
    """Ticks stop at 15:30 and do not start before 09:15: a carried holding must not be sold
    every evening because its last tick is hours old."""
    ctx = context(
        now=at, positions=[position(quantity=65)], prices={OPTION: "100"}, age_s=6 * 3600,
        unpriced_for={STOCK: seconds(6 * 3600)},
    )  # fmt: skip

    assert MtmBlind().continuous(ctx, TIMER).severity is Severity.ALLOW


@pytest.mark.parametrize(
    ("at", "blind"),
    [
        (datetime(2026, 11, 16, 3, 45, 0, tzinfo=UTC), False),  # 09:15:00, the open itself
        (datetime(2026, 11, 16, 3, 45, 45, tzinfo=UTC), False),  # 45 s into the session
        (datetime(2026, 11, 16, 3, 46, 0, tzinfo=UTC), False),  # exactly the blind time
        (datetime(2026, 11, 16, 3, 46, 15, tzinfo=UTC), True),  # 75 s into the session
    ],
    ids=["at the open", "45 s in", "60 s in", "75 s in"],
)
def test_yesterdays_last_tick_is_not_todays_silence(at: datetime, blind: bool) -> None:
    """At the open the last tick is from yesterday (18 hours old). Age is measured from no earlier
    than today's open, so the position is blind only after a full blind time of THIS session."""
    held = [position(quantity=65)]
    priced = context(now=at, positions=held, prices={OPTION: "100"}, age_s=18 * 3600)
    never_priced = context(now=at, positions=held, unpriced_for={OPTION: seconds(18 * 3600)})

    for ctx in (priced, never_priced):
        assert (MtmBlind().continuous(ctx, TIMER).code == MTM_BLIND) is blind


def test_a_fresh_tick_after_the_open_keeps_its_own_smaller_age() -> None:
    """The session anchor only shortens an age; it never lengthens one."""
    at = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)  # 10:30 IST, 75 minutes into the session
    ctx = context(now=at, positions=[position(quantity=65)], prices={OPTION: "100"}, age_s=10)

    assert MtmBlind().continuous(ctx, TIMER).severity is Severity.ALLOW


# ---- FloorBreach -----------------------------------------------------------------------------


def test_breach() -> None:
    """Spec "Breach": the floor is -2,000 and an update puts MTM_day at -2,000."""
    decision = FloorBreach().continuous(context(limits=floors(), mtm_day=D(-2000)), TIMER)

    assert decision.severity is Severity.KILL and decision.code == FLOOR_BREACH
    assert decision.inputs == {"mtm_day": "-2000", "floor": "-2000", "peak": "0"}


@pytest.mark.parametrize(
    ("mtm", "kill"), [(0, False), (-1999, False), (-2000, True), (-2500, True)]
)
def test_the_floor_is_inclusive_and_never_debounced(mtm: int, kill: bool) -> None:
    ctx = context(limits=floors(), mtm_day=D(mtm))

    assert (FloorBreach().continuous(ctx, TIMER).severity is Severity.KILL) is kill


def test_the_ratchet_holds_the_floor_up() -> None:
    state = day_state(floor=D(-1000), peak=D(0))  # the formula says -2000; the engine's is -1000

    at_floor = context(limits=floors(), day=state, mtm_day=D(-1000))
    above = context(limits=floors(), day=state, mtm_day=D(-999))

    assert FloorBreach().continuous(at_floor, TIMER).severity is Severity.KILL
    assert FloorBreach().continuous(above, TIMER).severity is Severity.ALLOW


def test_a_fall_after_a_peak_breaches_the_floor_set_at_the_peak() -> None:
    """Spec "Floor after a fall": max loss 3,000, trailing-loss 500 per 1,000 of peak, peak 3,000:
    the floor is -1,500 and stays there when MTM_day falls back."""
    limits = floors(daily_max_loss=D(3000), trail_loss_x=D(1000), trail_loss_y=D(500))
    state = day_state(peak=D(3000), floor=D(-1500))

    fell_back = context(limits=limits, day=state, mtm_day=D(500))
    at_floor = context(limits=limits, day=state, mtm_day=D(-1500))

    assert FloorBreach().continuous(fell_back, TIMER).severity is Severity.ALLOW
    assert FloorBreach().continuous(at_floor, TIMER).code == FLOOR_BREACH


def test_with_expiring_exposure_the_breach_is_judged_against_the_expiry_floor() -> None:
    """add-risk-guards task 8.2: the same floor the monitor ratchets. Peak 5,000, a base giveback
    of 2,000 (floor 3,000) and an expiry giveback of 1,500 (floor 3,500): MTM_day 3,400 breaches
    only once the day has had expiring exposure."""
    limits = floors(giveback_arm=D(2000), giveback_abs=D(2000), giveback_abs_expiry=D(1500))

    def judged(exposure: bool) -> Decision:
        state = day_state(peak=D(5000), expiry_exposure=exposure)
        return FloorBreach().continuous(context(limits=limits, day=state, mtm_day=D(3400)), TIMER)

    assert judged(True).severity is Severity.KILL
    assert judged(False).severity is Severity.ALLOW


def test_an_unknown_mtm_is_not_a_breach() -> None:
    assert FloorBreach().continuous(context(limits=floors(), mtm_day=None), TIMER).code is None


def test_a_floor_that_cannot_be_computed_still_catches_a_breach_of_the_last_good_floor() -> None:
    broken = floors(trail_loss_x=D(500), trail_loss_y=D(600))  # y > x: the floor outruns the peak
    state = day_state(floor=D(-1500))

    breach = FloorBreach().continuous(context(limits=broken, day=state, mtm_day=D(-1600)), TIMER)
    above = FloorBreach().continuous(context(limits=broken, day=state, mtm_day=D(-100)), TIMER)

    assert breach.severity is Severity.KILL and breach.code == FLOOR_BREACH
    assert above.severity is Severity.HALT_NEW_ENTRIES and above.code == FLOOR_UNAVAILABLE


def test_with_no_floor_at_all_and_unusable_limits_new_entries_stop() -> None:
    broken = floors(trail_loss_x=D(500), trail_loss_y=D(600))

    decision = FloorBreach().continuous(context(limits=broken, mtm_day=D(-100)), TIMER)

    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == FLOOR_UNAVAILABLE


# ---- through the runner ----------------------------------------------------------------------


def test_the_most_severe_continuous_decision_is_the_one_acted_on() -> None:
    ctx = context(
        limits=floors(), mtm_day=D(-2500), feed_subscribed=True, feed_silent_for=seconds(60)
    )

    results = run_continuous([FeedDown(), MtmBlind(), FloorBreach()], ctx, TIMER)
    winner = most_severe(results)

    assert winner is not None and winner.decision.code == FLOOR_BREACH  # a KILL beats a halt
