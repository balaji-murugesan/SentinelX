"""MtmTracker (add-risk-engine task 3.3; stale-data-guard and equity-floor specs).

Scenarios: "Spike that reverts" and "Genuine gap". Also: an unconfirmed outlier still feeds the
loss side (which is never debounced), the peak starts at zero each day, the peak only moves on
fresh accepted prices, and MTM is *unknown* (None) while an open position has no price.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from contracts.orders import OrderSide, ProductType
from engine.market_data.provider import Tick
from engine.positions.book import position_id
from engine.risk.mtm import MtmTracker, TickKind
from tests.engine.risk_builders import NOW, OPTION, STOCK, fill, instrument_of, position

D = Decimal
OUTLIER_PCT = D("20")  # a jump of more than 20% from the last accepted price is an outlier
STALE = timedelta(seconds=5)


def tracker() -> MtmTracker:
    return MtmTracker(outlier_pct=OUTLIER_PCT, stale_after=STALE)


def tick(price: str, seconds: float = 0, key=OPTION) -> Tick:  # type: ignore[no-untyped-def]
    at = NOW + timedelta(seconds=seconds)
    return Tick(key, D(price), exchange_time=at, received_at=at)


def evaluate(t: MtmTracker, seconds: float = 0):  # type: ignore[no-untyped-def]
    """The day's reading for a long of 65 opened today at 100.00 (so there is no reference)."""
    held = position(OPTION, 65, "100.00")
    return t.evaluate(
        [held],
        [fill(OrderSide.BUY, 65, "100.00")],
        {},
        instrument_of,
        NOW + timedelta(seconds=seconds),
    )


# ---- the outlier filter ---------------------------------------------------------------------


def test_the_first_tick_of_an_instrument_is_accepted() -> None:
    assert tracker().on_tick(tick("100")) is TickKind.ACCEPTED


def test_ordinary_moves_inside_the_band_are_accepted() -> None:
    t = tracker()
    t.on_tick(tick("100"))

    assert t.on_tick(tick("119.99", 1)) is TickKind.ACCEPTED  # just under 20%
    assert t.on_tick(tick("100", 2)) is TickKind.ACCEPTED


def test_a_jump_beyond_the_band_is_an_outlier_and_not_accepted_yet() -> None:
    t = tracker()
    t.on_tick(tick("100"))

    assert t.on_tick(tick("130", 1)) is TickKind.OUTLIER
    assert t.latest_price(OPTION) == D("130")  # the latest is believed ...
    assert t.accepted_price(OPTION) == D("100")  # ... but the accepted price has not moved


def test_spike_that_reverts() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))

    assert t.on_tick(tick("100", 2)) is TickKind.ACCEPTED  # back at the old level: spike dropped

    assert t.accepted_price(OPTION) == D("100")
    reading = evaluate(t, 2)
    assert reading is not None and reading.peak == D(0)  # the spike never raised the peak


def test_genuine_gap() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))

    assert t.on_tick(tick("131", 2)) is TickKind.CONFIRMED  # stays near 130: the market moved

    assert t.accepted_price(OPTION) == D("131")
    reading = evaluate(t, 2)
    assert reading is not None
    assert reading.peak > D(1000)  # +31 x 65 = 2015 gross, less the estimated exit charge
    assert reading.peak == reading.mtm_day  # the new level is accepted, so the peak followed it


def test_two_different_outliers_in_a_row_each_wait_for_confirmation() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))

    assert t.on_tick(tick("170", 2)) is TickKind.OUTLIER  # not near 130, and not near 100
    assert t.accepted_price(OPTION) == D("100")


def test_a_drop_beyond_the_band_is_an_outlier_too() -> None:
    t = tracker()
    t.on_tick(tick("100"))

    assert t.on_tick(tick("60", 1)) is TickKind.OUTLIER


# ---- loss side vs peak ----------------------------------------------------------------------


def test_an_unconfirmed_outlier_still_feeds_the_loss_side() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("60", 1))  # a 40% drop, not yet confirmed

    reading = evaluate(t, 1)

    assert reading is not None
    assert reading.mtm_day < D(-2500)  # valued at 60: about -40 x 65, plus charges
    assert reading.peak == D(0)  # and it can never have raised the peak


def test_the_loss_side_uses_the_latest_price_and_the_peak_the_accepted_one() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))  # outlier: mtm_day sees 130, the peak does not

    reading = evaluate(t, 1)

    assert reading is not None
    assert reading.mtm_day > D(1500)
    assert reading.peak == D(0)


def test_the_peak_starts_at_zero_and_only_rises() -> None:
    t = tracker()
    assert t.peak == D(0)
    t.on_tick(tick("100"))
    first = evaluate(t)
    assert first is not None and first.peak == D(0)  # at 100 the day is only -charges

    t.on_tick(tick("110", 1))
    high = evaluate(t, 1)
    t.on_tick(tick("102", 2))
    lower = evaluate(t, 2)

    assert high is not None and high.peak > D(0)
    assert lower is not None and lower.peak == high.peak  # a fall never lowers the peak
    assert lower.mtm_day < high.mtm_day


def test_a_new_day_resets_the_peak_to_zero() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("110", 1))
    high = evaluate(t, 1)
    assert high is not None and high.peak > D(0)

    t.new_day()

    assert t.peak == D(0)
    assert t.latest_price(OPTION) == D("110")  # prices are kept: they are still current


def test_the_peak_does_not_move_on_a_price_that_is_no_longer_fresh() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("110", 1))

    reading = evaluate(t, 1 + 600)  # ten minutes later, with no tick since

    assert reading is not None
    assert reading.peak == D(0)  # the accepted price is stale, so the peak did not follow it
    assert reading.mtm_day > D(0)  # the loss side still values at the last known price


# ---- unknown MTM ----------------------------------------------------------------------------


def test_mtm_is_unknown_while_an_open_position_has_no_price() -> None:
    assert evaluate(tracker()) is None  # no tick at all: None, never a guessed zero


def test_a_flat_book_has_a_known_mtm_without_any_price() -> None:
    reading = tracker().evaluate([position(OPTION, 0, "0")], [], {}, instrument_of, NOW)

    assert reading is not None and reading.mtm_day == D(0) and reading.peak == D(0)


def test_other_instruments_do_not_disturb_each_other() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("500", 1, key=STOCK))  # first tick for STOCK: accepted, not an outlier
    t.on_tick(tick("130", 2))  # outlier for OPTION only

    assert t.accepted_price(STOCK) == D("500")
    assert t.accepted_price(OPTION) == D("100")


def test_the_age_of_the_latest_tick() -> None:
    t = tracker()
    assert t.age(OPTION, NOW) is None
    t.on_tick(tick("100", 3))

    assert t.age(OPTION, NOW + timedelta(seconds=10)) == timedelta(seconds=7)


@pytest.mark.parametrize("bad", ["0", "-5", "NaN", "Infinity"])
def test_the_outlier_percentage_must_be_a_positive_number(bad: str) -> None:
    with pytest.raises(ValueError, match="outlier_pct"):
        MtmTracker(outlier_pct=D(bad), stale_after=STALE)


def test_a_move_of_exactly_the_band_is_not_an_outlier() -> None:
    """ "More than" 20%: a jump of exactly 20% is an ordinary tick."""
    t = tracker()
    t.on_tick(tick("100"))

    assert t.on_tick(tick("120", 1)) is TickKind.ACCEPTED
    assert t.on_tick(tick("144", 2)) is TickKind.ACCEPTED  # exactly 20% above 120
    assert t.on_tick(tick("115.20", 3)) is TickKind.ACCEPTED  # exactly 20% below 144


# ---- review round 1 -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "-5"])
def test_a_garbage_tick_is_rejected_and_changes_nothing(bad: str) -> None:
    t = tracker()
    t.on_tick(tick("100", 0))

    assert t.on_tick(tick(bad, 3)) is TickKind.REJECTED

    assert t.latest_price(OPTION) == D("100") and t.accepted_price(OPTION) == D("100")
    # The garbage tick did not refresh the clock: a silent feed must not look alive.
    assert t.age(OPTION, NOW + timedelta(seconds=10)) == timedelta(seconds=10)
    assert evaluate(t, 3) is not None  # and the day's MTM is still computable


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0", "-5"])
def test_a_garbage_first_tick_is_rejected_and_leaves_the_instrument_unpriced(bad: str) -> None:
    t = tracker()

    assert t.on_tick(tick(bad)) is TickKind.REJECTED
    assert t.latest_price(OPTION) is None and t.age(OPTION, NOW) is None
    assert evaluate(t) is None  # still unknown, never a number built from garbage

    assert t.on_tick(tick("100", 1)) is TickKind.ACCEPTED  # a real tick works afterwards


def test_a_price_exactly_as_old_as_the_limit_is_still_fresh() -> None:
    fresh, stale = tracker(), tracker()
    fresh.on_tick(tick("110"))
    stale.on_tick(tick("110"))

    at_the_limit = evaluate(fresh, 5)  # exactly STALE (5 s) old
    just_past = evaluate(stale, 5.001)

    assert at_the_limit is not None and at_the_limit.peak > D(0)
    assert just_past is not None and just_past.peak == D(0)


def test_freshness_is_the_age_of_the_accepted_price_not_of_the_latest_tick() -> None:
    t = tracker()
    t.on_tick(tick("100", 0))
    t.on_tick(tick("110", 1))
    assert t.on_tick(tick("140", 5)) is TickKind.OUTLIER  # the feed is alive, but unconfirmed

    reading = evaluate(t, 8)  # the accepted price is 7 s old, the latest tick only 3 s

    assert reading is not None
    assert reading.peak == D(0)
    assert reading.mtm_day > D(2000)  # the loss side valued at 140 meanwhile


def test_a_dropped_spike_does_not_leave_a_stale_pending_outlier_behind() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))  # pending
    t.on_tick(tick("100", 2))  # not near 130: the spike is dropped

    assert t.on_tick(tick("131", 3)) is TickKind.OUTLIER  # judged against 100, not against 130


def test_a_second_outlier_becomes_the_pending_one() -> None:
    t = tracker()
    t.on_tick(tick("100"))
    t.on_tick(tick("130", 1))
    t.on_tick(tick("170", 2))  # replaces the pending 130

    assert t.on_tick(tick("171", 3)) is TickKind.CONFIRMED


def test_the_accepted_time_advances_on_an_ordinary_tick() -> None:
    t = tracker()
    t.on_tick(tick("100", 0))
    t.on_tick(tick("110", 100))  # accepted 100 s later

    reading = evaluate(t, 100)

    assert reading is not None and reading.peak > D(0)  # fresh: the clock moved with the tick


def test_the_accepted_time_advances_when_an_outlier_is_confirmed() -> None:
    t = tracker()
    t.on_tick(tick("100", 0))
    t.on_tick(tick("130", 50))
    assert t.on_tick(tick("131", 100)) is TickKind.CONFIRMED

    reading = evaluate(t, 100)

    assert reading is not None and reading.peak > D(0)


def test_evaluate_forwards_the_previous_closes_and_reports_the_missing_ones() -> None:
    t = tracker()
    t.on_tick(tick("495", key=STOCK))
    held = position(STOCK, 100, "480.00", ProductType.CNC)
    pid = position_id(held)

    with_reference = t.evaluate([held], [], {pid: D("500")}, instrument_of, NOW)
    without = t.evaluate([held], [], {}, instrument_of, NOW)

    assert with_reference is not None and without is not None
    assert with_reference.mtm_day == D("-551.29")  # -500 from the close, less 51.29 to close
    assert with_reference.reference_missing == ()
    assert with_reference.peak == D(0)  # the peak also measures from the close: -551, not +1449
    assert without.mtm_day == D("1448.71")  # measured from entry instead: +1500 - 51.29
    assert without.reference_missing == (pid,)


def test_evaluate_accepts_one_shot_iterables() -> None:
    held = position(OPTION, 65, "100.00")
    buy = fill(OrderSide.BUY, 65, "100.00")
    listed, streamed = tracker(), tracker()
    listed.on_tick(tick("110"))
    streamed.on_tick(tick("110"))

    from_lists = listed.evaluate([held], [buy], {}, instrument_of, NOW)
    from_generators = streamed.evaluate(iter([held]), (f for f in [buy]), {}, instrument_of, NOW)

    assert from_lists is not None and from_generators == from_lists  # each was read twice safely


def test_the_peak_cannot_be_assigned_and_only_ever_rises() -> None:
    t = tracker()
    with pytest.raises(AttributeError):
        t.peak = D(5)  # type: ignore[misc]

    t.raise_peak(D(100))
    t.raise_peak(D(40))  # lower: ignored
    t.raise_peak(D("NaN"))  # not a number: ignored
    t.raise_peak(D("Infinity"))

    assert t.peak == D(100)
    t.new_day()
    assert t.peak == D(0)  # the only way down is a new day


@pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1)])
def test_the_stale_limit_must_be_above_zero(bad: timedelta) -> None:
    with pytest.raises(ValueError, match="stale_after"):
        MtmTracker(outlier_pct=OUTLIER_PCT, stale_after=bad)


def test_an_absurdly_large_peak_is_ignored_so_the_floor_check_never_goes_blind() -> None:
    """A corrupt price must not set a peak that floor_of refuses for the rest of the day."""
    t = tracker()

    t.raise_peak(D("1e15"))
    assert t.peak == D(0)

    t.raise_peak(D("1e12"))  # the supported maximum itself is accepted
    assert t.peak == D("1e12")


# ---- a baseline for the first tick, and new thresholds (task 7.1) -----------------------------


def test_a_corrupt_first_tick_far_from_the_baseline_needs_confirmation() -> None:
    """Without a baseline the first tick is accepted whatever it says, so ten times the real price
    could raise the peak for good. With the position's previous close as a baseline it is an
    outlier like any other jump."""
    t = tracker()
    t.seed_baseline(OPTION, D("100"))

    assert t.on_tick(tick("1000")) is TickKind.OUTLIER
    assert t.latest_price(OPTION) == D("1000")  # believed for the loss side
    assert t.accepted_price(OPTION) == D("100")  # the peak keeps using the baseline
    reading = evaluate(t)
    assert reading is not None and reading.peak == D("0")  # the spike did not raise the peak
    assert t.on_tick(tick("100.5", 1)) is TickKind.ACCEPTED  # the spike reverted: dropped
    assert t.accepted_price(OPTION) == D("100.5")


def test_a_first_tick_that_is_confirmed_is_a_genuine_gap() -> None:
    t = tracker()
    t.seed_baseline(OPTION, D("100"))
    t.on_tick(tick("130"))

    assert t.on_tick(tick("131", 1)) is TickKind.CONFIRMED
    assert t.accepted_price(OPTION) == D("131")


def test_a_first_tick_near_the_baseline_is_accepted_at_once() -> None:
    t = tracker()
    t.seed_baseline(OPTION, D("100"))

    assert t.on_tick(tick("105")) is TickKind.ACCEPTED
    assert t.accepted_price(OPTION) == D("105")


def test_a_baseline_is_not_a_tick() -> None:
    t = tracker()
    t.seed_baseline(OPTION, D("100"))

    assert t.latest_price(OPTION) is None and t.age(OPTION, NOW) is None  # still unpriced


@pytest.mark.parametrize("bad", [D("0"), D("-5"), D("NaN"), D("Infinity")])
def test_a_bad_baseline_is_ignored(bad: Decimal) -> None:
    t = tracker()
    t.seed_baseline(OPTION, bad)

    assert t.on_tick(tick("1000")) is TickKind.ACCEPTED  # nothing to compare with


def test_new_thresholds_apply_without_losing_the_prices() -> None:
    t = tracker()
    t.on_tick(tick("100"))

    t.reconfigure(outlier_pct=D("5"), stale_after=timedelta(seconds=2))

    assert t.latest_price(OPTION) == D("100")  # the series survived
    assert t.on_tick(tick("110", 1)) is TickKind.OUTLIER  # 10% is now beyond the 5% band


@pytest.mark.parametrize("pct", [D("0"), D("-1"), D("NaN")])
def test_reconfigure_refuses_what_the_constructor_refuses(pct: Decimal) -> None:
    t = tracker()

    with pytest.raises(ValueError, match="outlier_pct"):
        t.reconfigure(outlier_pct=pct, stale_after=STALE)
    with pytest.raises(ValueError, match="stale_after"):
        t.reconfigure(outlier_pct=D("20"), stale_after=timedelta(0))
