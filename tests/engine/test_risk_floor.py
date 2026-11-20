"""The equity floor (add-risk-engine task 3.1; equity-floor spec, PRD §7.1).

Scenarios: "No terms configured", "Trailing-loss term", "Lock reached", "Trail steps" and
"Giveback term", each with the spec's worked numbers. Property tests (``hypothesis``) check the
floor against an independent, exact re-statement of the PRD formulas written with ``Fraction``,
and that the floor never decreases as the peak rises or along any path of peaks.
"""

import math
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.risk.floor import FloorParams, LockMode, floor_of, next_floor

D = Decimal


def params(**overrides: object) -> FloorParams:
    values: dict[str, object] = {"daily_max_loss": D(3000)}
    return FloorParams(**{**values, **overrides})  # type: ignore[arg-type]


# ---- the spec's scenarios -------------------------------------------------------------------


def test_no_terms_configured_the_floor_stays_at_the_starting_value() -> None:
    assert floor_of(params(), D(0)) == D(-3000)
    assert floor_of(params(), D(5000)) == D(-3000)


def test_trailing_loss_term() -> None:
    trailing = params(trail_loss_x=D(1000), trail_loss_y=D(500))

    assert floor_of(trailing, D(2500)) == D(-2000)  # two whole steps: -3000 + 2 x 500
    assert floor_of(trailing, D(999)) == D(-3000)  # no whole step yet
    assert floor_of(trailing, D(0)) == D(-3000)


def test_lock_reached() -> None:
    locked = params(lock_mode=LockMode.LOCK, lock_trigger=D(1000), lock_at=D(200))

    assert floor_of(locked, D(1500)) == D(200)
    assert floor_of(locked, D(999)) == D(-3000)  # not armed below the trigger


def test_trail_steps() -> None:
    trailing = params(lock_mode=LockMode.TRAIL, trail_step=D(1000), trail_by=D(500))

    assert floor_of(trailing, D(2500)) == D(1000)  # two whole steps x 500


def test_trail_does_not_lift_the_floor_before_its_first_step() -> None:
    """Zero whole steps adds nothing: a literal floor(0 / step) x by = 0 would put the floor at
    zero from the first tick of the day and kill the account at once."""
    trailing = params(lock_mode=LockMode.TRAIL, trail_step=D(1000), trail_by=D(500))

    assert floor_of(trailing, D(0)) == D(-3000)
    assert floor_of(trailing, D(999)) == D(-3000)
    assert floor_of(trailing, D(1000)) == D(500)


def test_lock_and_trail_starts_at_the_lock_and_climbs_from_the_trigger() -> None:
    both = params(
        lock_mode=LockMode.LOCK_AND_TRAIL, lock_trigger=D(1000), lock_at=D(200),
        trail_step=D(500), trail_by=D(250),
    )  # fmt: skip

    assert floor_of(both, D(999)) == D(-3000)
    assert floor_of(both, D(1000)) == D(200)  # armed, no whole step past the trigger yet
    assert floor_of(both, D(1499)) == D(200)
    assert floor_of(both, D(2000)) == D(200 + 2 * 250)  # two steps past the trigger


def test_giveback_term() -> None:
    giveback = params(giveback_arm=D(2000), giveback_abs=D(1000), giveback_pct=D(30))

    assert floor_of(giveback, D(4000)) == D(2800)  # 4000 - max(1000, 30% x 4000 = 1200)
    assert floor_of(giveback, D(2500)) == D(1500)  # 2500 - max(1000, 750)
    assert floor_of(giveback, D(1999)) == D(-3000)  # not armed below the arm level


def test_giveback_with_only_a_percentage_or_only_an_amount() -> None:
    assert floor_of(params(giveback_arm=D(1000), giveback_pct=D(50)), D(3000)) == D(1500)
    assert floor_of(params(giveback_arm=D(1000), giveback_abs=D(400)), D(3000)) == D(2600)


def test_the_floor_is_the_highest_of_all_the_terms() -> None:
    everything = params(
        trail_loss_x=D(1000), trail_loss_y=D(500),
        lock_mode=LockMode.LOCK, lock_trigger=D(1000), lock_at=D(200),
        giveback_arm=D(2000), giveback_abs=D(1000),
    )  # fmt: skip

    # peak 2500: trailing-loss -2000, lock 200, giveback 1500
    assert floor_of(everything, D(2500)) == D(1500)


def test_a_negative_or_non_finite_peak_is_refused() -> None:
    for bad in (D(-1), D("NaN"), D("Infinity")):
        with pytest.raises(ValueError, match="peak"):
            floor_of(params(), bad)


# ---- the ratchet ----------------------------------------------------------------------------


def test_next_floor_never_falls_below_the_previous_floor() -> None:
    trailing = params(trail_loss_x=D(1000), trail_loss_y=D(500))

    assert next_floor(None, trailing, D(2500)) == D(-2000)  # no floor yet: the formula's
    assert next_floor(D(-1500), trailing, D(2500)) == D(-1500)  # the previous floor was higher
    assert next_floor(D(-2500), trailing, D(2500)) == D(-2000)  # the formula is higher


def test_a_looser_configuration_cannot_lower_a_floor_already_reached() -> None:
    tight = params(daily_max_loss=D(1000))
    loose = params(daily_max_loss=D(5000))

    reached = next_floor(None, tight, D(0))  # -1000
    assert next_floor(reached, loose, D(0)) == D(-1000)  # the loosened limit does not lower it


# ---- configuration the floor refuses --------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"daily_max_loss": D(0)}, "daily_max_loss"),
        ({"daily_max_loss": D("NaN")}, "finite"),
        ({"trail_loss_x": D(1000)}, "go together"),
        ({"trail_loss_x": D(0), "trail_loss_y": D(10)}, "trail_loss_y"),
        ({"trail_loss_x": D(100), "trail_loss_y": D(101)}, "trail_loss_y"),
        ({"trail_loss_x": D("0.001"), "trail_loss_y": D("0.0005")}, "trail_loss_x"),
        (
            {"lock_mode": LockMode.TRAIL, "trail_step": D("0.001"), "trail_by": D("0.0005")},
            "trail_step",
        ),
        ({"daily_max_loss": D("1e13")}, "at most"),
        ({"lock_mode": LockMode.LOCK}, "lock_trigger and lock_at"),
        ({"lock_mode": LockMode.LOCK, "lock_trigger": D(500), "lock_at": D(500)}, "below"),
        ({"lock_mode": LockMode.TRAIL}, "trail_step and trail_by"),
        ({"lock_mode": LockMode.TRAIL, "trail_step": D(100), "trail_by": D(100)}, "below"),
        ({"lock_mode": LockMode.TRAIL, "trail_step": D(0), "trail_by": D(0)}, "below"),
        ({"giveback_abs": D(10)}, "giveback_arm"),
        ({"giveback_arm": D(100)}, "giveback_abs or giveback_pct"),
        ({"giveback_arm": D(100), "giveback_pct": D(150)}, "below 100"),
        ({"giveback_arm": D(100), "giveback_pct": D(100)}, "below 100"),  # the whole peak
        ({"giveback_arm": D(100), "giveback_pct": D(0)}, "above 0"),
        ({"giveback_arm": D(100), "giveback_pct": D(-5)}, "above 0"),
        ({"giveback_arm": D(100), "giveback_abs": D(-1)}, "negative"),
        ({"giveback_arm": D(100), "giveback_abs": D(0)}, "above zero"),
    ],
)
def test_configurations_that_would_misbehave_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        params(**overrides)


# ---- property tests: an exact restatement of PRD §7.1 ---------------------------------------


def amounts(low: Decimal, high: Decimal) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=low, max_value=high, places=2, allow_nan=False)


@st.composite
def floor_params(draw: st.DrawFn) -> FloorParams:
    kwargs: dict[str, object] = {"daily_max_loss": draw(amounts(D(100), D(10_000)))}
    if draw(st.booleans()):
        x = draw(amounts(D(100), D(5000)))
        kwargs["trail_loss_x"] = x
        kwargs["trail_loss_y"] = draw(amounts(D("0.01"), x))
    mode = draw(st.sampled_from(list(LockMode)))
    kwargs["lock_mode"] = mode
    if mode in (LockMode.LOCK, LockMode.LOCK_AND_TRAIL):
        trigger = draw(amounts(D(100), D(5000)))
        kwargs["lock_trigger"] = trigger
        kwargs["lock_at"] = draw(amounts(D(-1000), trigger - D("0.01")))
    if mode in (LockMode.TRAIL, LockMode.LOCK_AND_TRAIL):
        step = draw(amounts(D(100), D(2000)))
        kwargs["trail_step"] = step
        kwargs["trail_by"] = draw(amounts(D("0.01"), step - D("0.01")))
    if draw(st.booleans()):
        kwargs["giveback_arm"] = draw(amounts(D(100), D(5000)))
        which = draw(st.sampled_from(["abs", "pct", "both"]))
        if which in ("abs", "both"):
            kwargs["giveback_abs"] = draw(amounts(D("0.01"), D(2000)))
        if which in ("pct", "both"):
            kwargs["giveback_pct"] = draw(amounts(D("0.01"), D("99.99")))
    return FloorParams(**kwargs)  # type: ignore[arg-type]


peaks = amounts(D(0), D(20_000))


def reference_floor(p: FloorParams, peak: Decimal) -> Fraction:
    """PRD §7.1, written straight from the text with exact rational arithmetic."""
    P = Fraction(peak)
    terms = [-Fraction(p.daily_max_loss)]
    if p.trail_loss_x is not None and p.trail_loss_y is not None and P > 0:
        steps = math.floor(P / Fraction(p.trail_loss_x))
        terms.append(-Fraction(p.daily_max_loss) + steps * Fraction(p.trail_loss_y))
    lock_on = p.lock_mode in (LockMode.LOCK, LockMode.LOCK_AND_TRAIL)
    if lock_on and P >= Fraction(p.lock_trigger or 0):
        terms.append(Fraction(p.lock_at or 0))
    if p.lock_mode is LockMode.TRAIL:
        steps = math.floor(P / Fraction(p.trail_step or 1))
        if steps >= 1:
            terms.append(steps * Fraction(p.trail_by or 0))
    if p.lock_mode is LockMode.LOCK_AND_TRAIL and P >= Fraction(p.lock_trigger or 0):
        steps = math.floor((P - Fraction(p.lock_trigger or 0)) / Fraction(p.trail_step or 1))
        terms.append(Fraction(p.lock_at or 0) + steps * Fraction(p.trail_by or 0))
    if p.giveback_arm is not None and P >= Fraction(p.giveback_arm):
        absolute = Fraction(p.giveback_abs) if p.giveback_abs is not None else Fraction(0)
        percent = Fraction(p.giveback_pct) * P / 100 if p.giveback_pct is not None else Fraction(0)
        terms.append(P - max(absolute, percent))
    return max(terms)


@given(floor_params(), peaks)
def test_the_floor_equals_the_prd_formulas(p: FloorParams, peak: Decimal) -> None:
    assert Fraction(floor_of(p, peak)) == reference_floor(p, peak)


@given(floor_params(), peaks, peaks)
def test_the_floor_never_decreases_as_the_peak_rises(
    p: FloorParams, a: Decimal, b: Decimal
) -> None:
    low, high = sorted((a, b))

    assert floor_of(p, low) <= floor_of(p, high)


@given(floor_params(), st.lists(peaks, min_size=1, max_size=30))
def test_the_ratchet_never_falls_along_any_path_of_peaks(
    p: FloorParams, path: list[Decimal]
) -> None:
    floor: Decimal | None = None
    for peak in path:
        raised = next_floor(floor, p, peak)
        assert floor is None or raised >= floor
        assert raised >= -p.daily_max_loss
        floor = raised


@given(floor_params(), peaks)
def test_the_floor_is_always_strictly_below_the_peak_that_set_it(
    p: FloorParams, peak: Decimal
) -> None:
    """Otherwise a term would put the account in breach the moment it switched on."""
    assert floor_of(p, peak) < peak


def test_the_giveback_arms_at_exactly_the_arm_level() -> None:
    giveback = params(giveback_arm=D(2000), giveback_abs=D(1000), giveback_pct=D(30))

    assert floor_of(giveback, D(2000)) == D(1000)  # 2000 - max(1000, 600): armed at the boundary


# ---- review round 1: gating, the spec's "floor after a fall", types and bounds -----------------


def test_trail_below_its_first_step_leaves_the_floor_at_the_starting_value() -> None:
    """Scenario "Trail below its first step": no whole step, no raise."""
    trailing = params(lock_mode=LockMode.TRAIL, trail_step=D(1000), trail_by=D(500))

    assert floor_of(trailing, D(999)) == D(-3000)


def test_floor_after_a_fall_stays_at_the_level_set_at_the_peak() -> None:
    """Scenario "Floor after a fall": peak 3,000 sets -1,500, and a fall never lowers it."""
    trailing = params(trail_loss_x=D(1000), trail_loss_y=D(500))

    at_peak = next_floor(None, trailing, D(3000))
    after_the_fall = next_floor(at_peak, trailing, D(500))  # a stale, lower peak arrives

    assert at_peak == D(-1500)
    assert after_the_fall == D(-1500)


def test_lock_and_trail_settings_are_ignored_while_the_mode_is_off() -> None:
    ignored = params(
        lock_mode=LockMode.OFF,
        lock_trigger=D(100),
        lock_at=D(50),
        trail_step=D(100),
        trail_by=D(50),
    )

    assert floor_of(ignored, D(5000)) == D(-3000)


def test_trail_settings_are_ignored_in_lock_mode_and_lock_settings_in_trail_mode() -> None:
    lock_only = params(
        lock_mode=LockMode.LOCK,
        lock_trigger=D(1000),
        lock_at=D(200),
        trail_step=D(500),
        trail_by=D(250),
    )
    trail_only = params(
        lock_mode=LockMode.TRAIL,
        lock_trigger=D(500),
        lock_at=D(400),
        trail_step=D(1000),
        trail_by=D(500),
    )

    assert floor_of(lock_only, D(3000)) == D(200)  # the trail would have said 1500
    assert floor_of(trail_only, D(1000)) == D(500)  # the trail's first step; the lock said 400
    # At 600 the lock (trigger 500, at 400) would be armed, but the first trail step (1000) is not
    # reached: only a lock wrongly armed in TRAIL mode could lift the floor off its start.
    assert floor_of(trail_only, D(600)) == D(-3000)


def test_a_trailing_loss_equal_to_its_step_trails_the_peak_at_a_fixed_distance() -> None:
    fixed = params(trail_loss_x=D(500), trail_loss_y=D(500))

    assert floor_of(fixed, D(2750)) == D(-3000 + 5 * 500)  # always below the peak
    assert floor_of(fixed, D(2750)) < D(2750)


def test_the_largest_allowed_giveback_percent_leaves_the_floor_just_above_zero() -> None:
    nearly_all = params(giveback_arm=D(100), giveback_pct=D("99.99"))

    # 99.99% of a 1000 peak is 999.9, so the floor is 1000 - 999.9; 100% itself is refused above.
    assert floor_of(nearly_all, D(1000)) == D("0.1")


@pytest.mark.parametrize("bad", [1000, 1000.5, "1000"])
def test_a_number_that_is_not_a_decimal_is_refused_as_a_type_error(bad: object) -> None:
    with pytest.raises(TypeError, match="Decimal"):
        params(daily_max_loss=bad)
    with pytest.raises(TypeError, match="Decimal"):
        params(giveback_arm=D(100), giveback_abs=bad)


def test_a_peak_beyond_the_supported_range_is_refused() -> None:
    with pytest.raises(ValueError, match="peak"):
        floor_of(params(), D("1e13"))
