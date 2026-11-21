"""Risk limits: parsing, the tightening registry and ``validate_edit`` (add-risk-engine tasks 4.1,
4.2; risk-limits spec; equity-floor "A limits edit may not lower the floor"; risk-gate "Shadow and
enforce modes per rule").

The registry is the single table behind the tighten-only rule, so every row is tested in both
directions: a move the way the registry says is tighter is accepted, the opposite move is refused.
Adding a row without a case here fails ``test_every_registry_row_has_a_direction_case``.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from contracts.commands import LimitValue
from engine.risk.floor import LockMode, floor_of
from engine.risk.limits import (
    LIMITS_INVALID,
    MODE_PREFIX,
    REGISTRY,
    RULES,
    TIGHTEN_ONLY,
    Direction,
    EditRejected,
    InvalidLimits,
    RiskLimits,
    RuleMode,
    loosened_keys,
    parse_limits,
    trail_move_tightens,
    validate_edit,
)

D = Decimal

# Monday 2026-11-16, IST = UTC + 5:30.
MID_DAY = datetime(2026, 11, 16, 5, 30, tzinfo=UTC)  # 11:00 IST, market hours
AFTER_CLOSE = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)  # 16:00 IST
WEEKEND = datetime(2026, 11, 14, 5, 30, tzinfo=UTC)  # Saturday 11:00 IST

# A complete, valid set with every floor term configured, so each can be moved on its own.
BASE: dict[str, LimitValue] = {
    "per_trade_max_risk": "1000", "daily_max_loss": "3000",
    "catastrophic_sl_pct": "40", "price_band_pct": "10", "outlier_pct": "20",
    "exit_buffer_pct": "2",
    "max_trades_per_day": 5, "loss_streak_limit": 3, "max_open_total": 4,
    "max_open_per_underlying": 2, "max_chase_steps": 5,
    "cooldown_seconds": 900, "stale_s": 5, "feed_down_s": 15, "mtm_blind_s": 60,
    "chase_interval_s": 2, "exit_escalate_s": 30,
    "entry_cutoff": "15:00", "square_off_time": "15:15",
    "trail_loss_x": "1000", "trail_loss_y": "500",
    "lock_mode": "LOCK", "lock_trigger": "1000", "lock_at": "200",
    "trail_step": "500", "trail_by": "250",
    "giveback_arm": "2000", "giveback_abs": "1000", "giveback_pct": "30",
    # add-risk-guards' required limits (test values).
    "max_open_lots_per_underlying": 10, "max_open_lots_total": 20, "max_gross_lots_per_day": 50,
    "max_entries_per_minute": 5, "reject_breaker_n": 3, "min_entry_gap_s": 5,
}  # fmt: skip

# Every optional limit that add-risk-guards added, set, so each can be moved both ways. Kept out of
# BASE (which the gate, monitor and kill tests share) so a later rule cannot change those tests.
GUARDS: dict[str, LimitValue] = {
    "daily_profit_target": "4000", "target_confirm_ticks": 2,
    "min_premium": "5", "max_strike_distance_pct": "3",
    "allowed_underlyings": ["BANKNIFTY", "NIFTY"], "denied_underlyings": ["ABC", "XYZ"],
    "entry_cutoff_expiry": "14:00", "square_off_time_expiry": "14:30",
    "max_open_lots_per_underlying_expiry": 2, "max_open_lots_total_expiry": 4,
    "min_premium_expiry": "15",
    "giveback_arm_expiry": "1200", "giveback_abs_expiry": "600", "giveback_pct_expiry": "20",
}  # fmt: skip
FULL: dict[str, LimitValue] = {**BASE, **GUARDS}


def edit(**changes: LimitValue) -> dict[str, LimitValue]:
    return {**BASE, **changes}


def limits(**changes: LimitValue) -> RiskLimits:
    return parse_limits(edit(**changes))


def full(**changes: LimitValue) -> dict[str, LimitValue]:
    return {**FULL, **changes}


# ---- parsing (task 4.1) ---------------------------------------------------------------------


def test_a_complete_set_parses_and_round_trips_through_its_flat_form() -> None:
    parsed = parse_limits(BASE)

    assert parsed.per_trade_max_risk == D(1000)
    assert parsed.entry_cutoff.hour == 15 and parsed.entry_cutoff.minute == 0
    assert parsed.lock_mode is LockMode.LOCK
    assert parse_limits(parsed.to_values()) == parsed


def test_every_new_limit_round_trips_including_symbol_lists() -> None:
    parsed = parse_limits(FULL)

    assert parsed.max_open_lots_total == 20 and parsed.min_entry_gap_s == 5
    assert parsed.allowed_underlyings == frozenset({"NIFTY", "BANKNIFTY"})
    assert parsed.giveback_pct_expiry == D(20)
    assert parse_limits(parsed.to_values()) == parsed
    assert parsed.to_values()["denied_underlyings"] == ["ABC", "XYZ"]  # a sorted list


def test_symbols_are_upper_cased_deduplicated_and_stored_sorted() -> None:
    parsed = parse_limits(full(allowed_underlyings=["nifty", " BankNifty ", "NIFTY"]))

    assert parsed.allowed_underlyings == frozenset({"NIFTY", "BANKNIFTY"})
    assert parsed.to_values()["allowed_underlyings"] == ["BANKNIFTY", "NIFTY"]


@pytest.mark.parametrize(
    "value",
    [[], [""], ["NIFTY", "  "], "NIFTY", ["NIFTY", 5], 5, True],
)
def test_a_malformed_symbol_list_rejects_the_whole_edit(value: object) -> None:
    """risk-limits "Empty or malformed symbol": an empty list, an empty or blank symbol, a bare
    string or a non-text member."""
    raw: dict[str, LimitValue] = full(allowed_underlyings=value)  # type: ignore[arg-type]

    with pytest.raises(InvalidLimits, match="allowed_underlyings"):
        parse_limits(raw)


@pytest.mark.parametrize(
    "key",
    [
        "max_open_lots_per_underlying", "max_open_lots_total", "max_gross_lots_per_day",
        "max_entries_per_minute", "reject_breaker_n", "min_entry_gap_s",
    ],
)  # fmt: skip
def test_a_document_without_a_new_required_limit_no_longer_parses(key: str) -> None:
    """The BREAKING step of add-risk-guards: limits stored before it lack these keys, so they are
    unusable (entries refused) until the owner loads a new file after the close."""
    raw = dict(BASE)
    del raw[key]

    with pytest.raises(InvalidLimits, match=f"{key} is required"):
        parse_limits(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_open_lots_total", 0), ("max_open_lots_total", "8"), ("max_open_lots_total", True),
     ("max_gross_lots_per_day", -1), ("min_entry_gap_s", 0)],
)  # fmt: skip
def test_a_lot_count_or_gap_is_a_whole_number_of_at_least_one(key: str, value: LimitValue) -> None:
    with pytest.raises(InvalidLimits, match=key):
        parse_limits(edit(**{key: value}))


def test_to_values_gives_text_for_money_and_never_a_float() -> None:
    values = parse_limits(BASE).to_values()

    assert values["per_trade_max_risk"] == "1000"
    assert values["entry_cutoff"] == "15:00"
    assert values["max_trades_per_day"] == 5
    assert not any(isinstance(v, float) for v in values.values())


def test_every_registry_row_has_exactly_one_field_on_the_model() -> None:
    fields = set(RiskLimits.__dataclass_fields__) - {"modes"}
    rows = {key for key in REGISTRY if not key.startswith(MODE_PREFIX)}

    assert fields == rows
    assert {key for key in REGISTRY if key.startswith(MODE_PREFIX)} == {
        MODE_PREFIX + rule for rule in RULES
    }


def test_a_percentage_is_a_percent_number_and_reaches_the_floor_unconverted() -> None:
    """``giveback_pct = 30`` means 30%: nothing in limits.py divides it by 100."""
    parsed = limits(giveback_pct="30")

    assert parsed.giveback_pct == D(30)
    assert parsed.floor_params().giveback_pct == D(30)
    # peak 2000: the giveback is the larger of 1000 and 30% of 2000 (600), so the floor is 1000
    assert floor_of(parsed.floor_params(), D(2000)) == D(1000)


def test_out_of_range_value_rejects_the_whole_edit_and_names_every_problem() -> None:
    """Spec "Out-of-range value": a negative percentage. Every other problem is named too."""
    with pytest.raises(InvalidLimits) as raised:
        parse_limits(edit(price_band_pct="-5", per_trade_max_risk="0", max_trades_per_day=0))

    problems = " | ".join(raised.value.problems)
    assert "price_band_pct" in problems
    assert "per_trade_max_risk" in problems
    assert "max_trades_per_day" in problems


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("catastrophic_sl_pct", "0"),  # a percentage is above 0 ...
        ("catastrophic_sl_pct", "100.01"),  # ... and at most 100
        ("per_trade_max_risk", "NaN"),  # Decimal parses it: it must still be refused
        ("per_trade_max_risk", "Infinity"),
        ("per_trade_max_risk", "1e13"),  # above the 1e12 ceiling
        ("per_trade_max_risk", "abc"),
        ("per_trade_max_risk", True),  # bool is an int subclass in Python
        ("per_trade_max_risk", None),  # required
        ("max_trades_per_day", True),
        ("max_trades_per_day", "5"),  # a count is a number, not text
        ("max_trades_per_day", -1),
        ("entry_cutoff", "25:00"),
        ("entry_cutoff", "9:15"),  # HH:MM, two digits
        ("entry_cutoff", 900),
        ("lock_mode", "SIDEWAYS"),
        ("lock_mode", 1),
        (MODE_PREFIX + "per_trade_risk", "off"),
    ],
)
def test_a_bad_value_is_refused(key: str, value: LimitValue) -> None:
    with pytest.raises(InvalidLimits):
        parse_limits(edit(**{key: value}))


def test_a_float_is_refused_because_money_never_travels_as_one() -> None:
    raw: dict[str, LimitValue] = {**BASE, "per_trade_max_risk": 12.5}  # type: ignore[dict-item]

    with pytest.raises(InvalidLimits, match="per_trade_max_risk"):
        parse_limits(raw)


@pytest.mark.parametrize("key", ["daily_max_loss", "max_trades_per_day", "entry_cutoff"])
def test_a_list_value_is_refused_for_an_amount_a_count_and_a_time(key: str) -> None:
    """An INR, a COUNT and a TIME_OF_DAY key: a one-item list is not its value."""
    raw: dict[str, LimitValue] = {**BASE, key: ["3000"]}  # type: ignore[dict-item]

    with pytest.raises(InvalidLimits, match=key):
        parse_limits(raw)


@pytest.mark.parametrize("amount", ["1E-999999999", "0.0000001", "1E+13"])
def test_an_amount_with_an_absurd_exponent_is_refused_without_being_expanded(amount: str) -> None:
    with pytest.raises(InvalidLimits, match="per_trade_max_risk"):
        parse_limits(edit(per_trade_max_risk=amount))


def test_an_amount_with_six_decimal_places_is_accepted() -> None:
    assert limits(per_trade_max_risk="0.000001").per_trade_max_risk == D("0.000001")


def test_a_missing_required_limit_is_named() -> None:
    raw = dict(BASE)
    del raw["per_trade_max_risk"]

    with pytest.raises(InvalidLimits, match="per_trade_max_risk is required"):
        parse_limits(raw)


def test_engine_owned_state_is_an_unknown_limit() -> None:
    """Spec "Attempt to set the floor": the floor, peak, halt state and counters are not limits."""
    for owned in ("floor", "peak", "halt_level", "entries_today", "loss_streak", "cooldown_until"):
        with pytest.raises(InvalidLimits, match=f"unknown limit '{owned}'"):
            parse_limits(edit(**{owned: "1"}))


def test_buffer_wider_than_the_band_is_rejected() -> None:
    """Spec "Buffer wider than the band": the kill switch's own exits would fail price sanity."""
    with pytest.raises(InvalidLimits, match="exit_buffer_pct"):
        parse_limits(edit(exit_buffer_pct="3", price_band_pct="2"))
    assert limits(exit_buffer_pct="2", price_band_pct="2")  # equal is fine


def test_the_entry_cutoff_must_be_earlier_than_the_square_off() -> None:
    with pytest.raises(InvalidLimits, match="entry_cutoff must be earlier"):
        parse_limits(edit(entry_cutoff="15:15", square_off_time="15:15"))
    with pytest.raises(InvalidLimits, match="entry_cutoff must be earlier"):
        parse_limits(edit(entry_cutoff="15:20", square_off_time="15:15"))
    assert limits(entry_cutoff="15:14", square_off_time="15:15")


@pytest.mark.parametrize(
    ("stale", "down", "blind"),
    [(15, 15, 60), (20, 15, 60), (5, 60, 60), (5, 70, 60), (60, 15, 5)],
)
def test_stale_then_feed_down_then_mtm_blind_must_strictly_increase(
    stale: int, down: int, blind: int
) -> None:
    with pytest.raises(InvalidLimits, match="increasing order"):
        parse_limits(edit(stale_s=stale, feed_down_s=down, mtm_blind_s=blind))


def test_a_shadow_mode_is_refused_for_a_rule_that_must_always_bind() -> None:
    for rule, may_shadow in RULES.items():
        raw = edit(**{MODE_PREFIX + rule: "shadow"})
        if may_shadow:
            assert parse_limits(raw).mode_of(rule) is RuleMode.SHADOW
        else:
            with pytest.raises(InvalidLimits, match="must always bind"):
                parse_limits(raw)


@pytest.mark.parametrize("rule", ["equity_floor", "stale_data", "initial_stop"])
def test_these_rules_can_never_be_shadowed(rule: str) -> None:
    """Hard-coded on purpose: the loop above reads ``RULES``, so flipping a flag there would
    change what it expects. Spec "Shadow and enforce modes per rule": the equity floor and the
    stale-data guard must always bind (the initial stop too: without it risk cannot be measured)."""
    with pytest.raises(InvalidLimits, match="must always bind"):
        parse_limits(edit(**{MODE_PREFIX + rule: "shadow"}))


def test_a_rule_without_a_mode_enforces() -> None:
    assert parse_limits(BASE).mode_of("per_trade_risk") is RuleMode.ENFORCE


def test_floor_terms_that_would_breach_on_arming_make_the_set_invalid() -> None:
    """``FloorParams`` polices the floor's own consistency; its ValueError is a refusal here."""
    with pytest.raises(InvalidLimits, match="equity floor settings"):
        parse_limits(edit(trail_loss_x="500", trail_loss_y="600"))
    with pytest.raises(InvalidLimits, match="equity floor settings"):
        parse_limits(edit(giveback_pct="100"))
    with pytest.raises(InvalidLimits, match="equity floor settings"):
        parse_limits(edit(lock_mode="LOCK", lock_trigger=None, lock_at=None))


def test_trail_loss_y_may_equal_trail_loss_x() -> None:
    assert limits(trail_loss_x="500", trail_loss_y="500")


# ---- add-risk-guards consistency rules (task 3.2) -------------------------------------------


def test_per_underlying_above_total_is_rejected() -> None:
    """lot-caps "Per-underlying above total"."""
    with pytest.raises(InvalidLimits, match="max_open_lots_per_underlying must not exceed"):
        parse_limits(edit(max_open_lots_per_underlying=10, max_open_lots_total=8))
    with pytest.raises(InvalidLimits, match="max_open_lots_per_underlying must not exceed"):
        parse_limits(edit(max_open_lots_per_underlying=9, max_open_lots_total=8))  # one over
    assert limits(max_open_lots_per_underlying=8, max_open_lots_total=8)  # equal is fine


def test_the_expiry_lot_caps_must_keep_per_underlying_within_total() -> None:
    """lot-caps "Per-underlying above total", for the expiry pair: both overrides, and an
    override total below the base per-underlying cap that would still apply on an expiry day."""
    with pytest.raises(InvalidLimits, match="on an expiry day the per-underlying lot cap"):
        parse_limits(edit(max_open_lots_per_underlying_expiry=4, max_open_lots_total_expiry=3))
    with pytest.raises(InvalidLimits, match=r"per-underlying lot cap \(10\).*total lot cap \(6\)"):
        parse_limits(edit(max_open_lots_total_expiry=6))  # base per-underlying is 10
    assert limits(max_open_lots_per_underlying_expiry=6, max_open_lots_total_expiry=6)


@pytest.mark.parametrize(
    ("override", "base", "looser", "equal"),
    [
        ("entry_cutoff_expiry", "entry_cutoff", "15:30", "15:00"),
        ("square_off_time_expiry", "square_off_time", "15:20", "15:15"),
        ("max_open_lots_per_underlying_expiry", "max_open_lots_per_underlying", 11, 10),
        ("max_open_lots_total_expiry", "max_open_lots_total", 21, 20),
        ("min_premium_expiry", "min_premium", "4", "5"),  # UP: a lower minimum is looser
        ("giveback_arm_expiry", "giveback_arm", "2100", "2000"),
        ("giveback_abs_expiry", "giveback_abs", "1100", "1000"),
        ("giveback_pct_expiry", "giveback_pct", "35", "30"),
    ],
)
def test_an_expiry_override_looser_than_its_base_is_rejected(
    override: str, base: str, looser: LimitValue, equal: LimitValue
) -> None:
    """expiry-day-profile "Override looser than base" and "Override equal to base"."""
    settings: dict[str, LimitValue] = {"min_premium": "5"}
    with pytest.raises(InvalidLimits, match=f"{override} must be no looser than {base}"):
        parse_limits(edit(**settings, **{override: looser}))
    assert parse_limits(edit(**settings, **{override: equal}))


def test_the_spec_cutoff_example_of_a_looser_override_is_rejected() -> None:
    """expiry-day-profile "Override looser than base": cutoff 14:00, expiry cutoff 14:30."""
    with pytest.raises(InvalidLimits, match="entry_cutoff_expiry must be no looser"):
        parse_limits(edit(entry_cutoff="14:00", entry_cutoff_expiry="14:30"))


def test_an_override_equal_to_its_base_is_accepted() -> None:
    """expiry-day-profile "Override equal to base": both total caps at 8."""
    assert limits(
        max_open_lots_per_underlying=8, max_open_lots_total=8, max_open_lots_total_expiry=8
    )


def test_an_expiry_override_without_a_base_value_is_accepted() -> None:
    """Unset base, nothing to compare: ``min_premium_expiry`` alone applies on expiry days."""
    assert limits(min_premium_expiry="15").min_premium is None


def test_a_giveback_override_that_adds_a_missing_leg_is_rejected() -> None:
    """expiry-day-profile "Override adds a missing leg": a base giveback with only giveback_pct,
    and giveback_abs_expiry set."""
    with pytest.raises(InvalidLimits, match="giveback_abs_expiry needs giveback_abs"):
        parse_limits(edit(giveback_abs=None, giveback_abs_expiry="500"))
    with pytest.raises(InvalidLimits, match="giveback_pct_expiry needs giveback_pct"):
        parse_limits(edit(giveback_pct=None, giveback_pct_expiry="20"))
    assert limits(giveback_abs=None, giveback_pct_expiry="20")  # a leg the base has: fine


def test_an_expiry_only_giveback_is_accepted_and_applies_only_on_the_expiry_floor() -> None:
    """expiry-day-profile "Expiry-only giveback": no base giveback, arm and abs overrides."""
    parsed = limits(
        giveback_arm=None, giveback_abs=None, giveback_pct=None,
        giveback_arm_expiry="2000", giveback_abs_expiry="1000",
    )  # fmt: skip

    assert parsed.floor_params().giveback_arm is None
    assert parsed.floor_params(expiry=True).giveback_arm == D(2000)
    assert parsed.floor_params(expiry=True).giveback_abs == D(1000)


def test_an_expiry_only_giveback_must_be_a_valid_term_of_its_own() -> None:
    """With the base term off, the overrides alone must form a valid giveback: a leg needs the
    arm, and the arm needs a leg."""
    no_base: dict[str, LimitValue] = {
        "giveback_arm": None, "giveback_abs": None, "giveback_pct": None,
    }  # fmt: skip
    with pytest.raises(InvalidLimits, match="expiry-day equity floor settings"):
        parse_limits(edit(**no_base, giveback_abs_expiry="1000"))
    with pytest.raises(InvalidLimits, match="expiry-day equity floor settings"):
        parse_limits(edit(**no_base, giveback_arm_expiry="2000"))


def test_the_expiry_cutoff_must_be_earlier_than_the_expiry_square_off() -> None:
    """expiry-day-profile "Expiry cutoff after the expiry square-off": 14:00 and 13:30. The
    effective values count: an override on one side meets the base on the other."""
    with pytest.raises(InvalidLimits, match=r"entry cutoff \(14:00\) must be earlier"):
        parse_limits(edit(entry_cutoff_expiry="14:00", square_off_time_expiry="13:30"))
    with pytest.raises(InvalidLimits, match=r"entry cutoff \(15:00\) must be earlier"):
        parse_limits(edit(square_off_time_expiry="14:30"))  # the base cutoff is 15:00
    with pytest.raises(InvalidLimits, match=r"entry cutoff \(14:30\) must be earlier"):
        parse_limits(edit(entry_cutoff_expiry="14:30", square_off_time_expiry="14:30"))
    assert limits(entry_cutoff_expiry="14:00", square_off_time_expiry="14:30")


def test_a_target_without_a_confirmation_count_is_rejected() -> None:
    """profit-target "Target without a confirmation count"."""
    with pytest.raises(InvalidLimits, match="daily_profit_target needs target_confirm_ticks"):
        parse_limits(edit(daily_profit_target="4000"))
    assert limits(daily_profit_target="4000", target_confirm_ticks=1)
    assert limits().daily_profit_target is None  # profit-target "No target set": optional


# ---- the expiry-day floor (task 3.3) --------------------------------------------------------


def test_the_expiry_floor_uses_each_set_giveback_override() -> None:
    """expiry-day-profile "Entering an expiring contract": a base giveback of 2,000 and an expiry
    giveback of 1,000 let at most 1,000 be given back from the peak."""
    parsed = limits(
        giveback_arm="3000", giveback_abs="2000", giveback_pct=None, giveback_abs_expiry="1000"
    )

    assert floor_of(parsed.floor_params(), D(6000)) == D(4000)
    assert floor_of(parsed.floor_params(expiry=True), D(6000)) == D(5000)
    assert parsed.floor_params(expiry=True).giveback_arm == D(3000)  # unset override: base


def test_the_expiry_flag_defaults_to_the_base_floor() -> None:
    parsed = parse_limits(FULL)

    assert parsed.floor_params() == parsed.floor_params(expiry=False)
    assert parsed.floor_params(expiry=True) != parsed.floor_params()
    assert parse_limits(BASE).floor_params(expiry=True) == parse_limits(BASE).floor_params()


# ---- the registry, both directions (task 4.1) -----------------------------------------------

# key -> (a tighter value, a looser value), each applied on top of BASE. Directions come from design
# §9; the table is written by hand so that a wrong direction in the registry fails here.
DIRECTION_CASES: dict[str, tuple[LimitValue, LimitValue]] = {
    "per_trade_max_risk": ("800", "1200"),  # down
    "daily_max_loss": ("2500", "3500"),  # down
    "catastrophic_sl_pct": ("30", "50"),  # down
    "price_band_pct": ("8", "12"),  # down
    "outlier_pct": ("15", "25"),  # down
    "exit_buffer_pct": ("3", "1"),  # UP: a wider buffer makes the kill's exits more aggressive
    "max_trades_per_day": (4, 6),  # down
    "loss_streak_limit": (2, 4),  # down
    "max_open_total": (3, 5),  # down
    "max_open_per_underlying": (1, 3),  # down
    "max_chase_steps": (6, 4),  # UP
    "cooldown_seconds": (1200, 600),  # UP: a longer cooldown is tighter
    "stale_s": (4, 6),  # down
    "feed_down_s": (10, 20),  # down
    "mtm_blind_s": (40, 80),  # down
    "chase_interval_s": (1, 3),  # down
    "exit_escalate_s": (20, 40),  # down
    "entry_cutoff": ("14:30", "15:10"),  # down: earlier is tighter
    "square_off_time": ("15:10", "15:20"),  # down: earlier is tighter
    "trail_loss_x": ("800", "1200"),  # down
    "trail_loss_y": ("600", "400"),  # UP
    "lock_trigger": ("900", "1100"),  # down
    "lock_at": ("300", "100"),  # UP
    "trail_step": ("400", "600"),  # down
    "trail_by": ("300", "200"),  # UP
    "giveback_arm": ("1500", "2500"),  # down
    "giveback_abs": ("800", "1200"),  # down
    "giveback_pct": ("25", "35"),  # down
    # add-risk-guards (design §8), each applied on top of FULL.
    "max_open_lots_per_underlying": (9, 11),  # down
    "max_open_lots_total": (19, 21),  # down
    "max_gross_lots_per_day": (49, 51),  # down
    "max_entries_per_minute": (4, 6),  # down
    "reject_breaker_n": (2, 4),  # down: tripping on fewer rejections is tighter
    "min_entry_gap_s": (10, 2),  # UP: a longer gap between entries is tighter
    "daily_profit_target": ("3000", "5000"),  # down: a lower target stops the day sooner
    "target_confirm_ticks": (1, 3),  # down: fewer confirmations stop the day sooner
    "min_premium": ("6", "4"),  # UP: a higher minimum refuses more cheap options
    "max_strike_distance_pct": ("2", "5"),  # down
    "allowed_underlyings": (["NIFTY"], ["BANKNIFTY", "FINNIFTY", "NIFTY"]),  # SUBSET
    "denied_underlyings": (["ABC", "PQR", "XYZ"], ["XYZ"]),  # SUPERSET
    "entry_cutoff_expiry": ("13:30", "14:15"),  # down: earlier is tighter
    "square_off_time_expiry": ("14:20", "14:45"),  # down: earlier is tighter
    "max_open_lots_per_underlying_expiry": (1, 3),  # down
    "max_open_lots_total_expiry": (3, 5),  # down
    "min_premium_expiry": ("20", "10"),  # UP
    "giveback_arm_expiry": ("1000", "1400"),  # down: arming earlier is tighter
    "giveback_abs_expiry": ("500", "700"),  # down
    "giveback_pct_expiry": ("15", "25"),  # down
}
HANDLED_ELSEWHERE = {"lock_mode"} | {MODE_PREFIX + rule for rule in RULES}


def test_every_registry_row_has_a_direction_case() -> None:
    assert set(DIRECTION_CASES) | HANDLED_ELSEWHERE == set(REGISTRY)


@pytest.mark.parametrize("key", sorted(DIRECTION_CASES))
def test_a_move_in_the_tightening_direction_is_accepted_and_the_opposite_is_refused(
    key: str,
) -> None:
    """On top of FULL, so the optional limits have a value to move from and every override sits
    beside its base (the base moves stay consistent with the overrides)."""
    tighter, looser = DIRECTION_CASES[key]
    base = parse_limits(FULL)

    assert loosened_keys(base, parse_limits(full(**{key: tighter}))) == []
    assert loosened_keys(base, parse_limits(full(**{key: looser}))) == [key]
    # And the edit as a whole, in market hours.
    assert validate_edit(base, full(**{key: tighter}), now=MID_DAY, peak=D(0), floor=None)
    with pytest.raises(EditRejected, match=key) as refused:
        validate_edit(base, full(**{key: looser}), now=MID_DAY, peak=D(0), floor=None)
    assert refused.value.code == TIGHTEN_ONLY


def test_the_registry_directions_match_the_declared_table() -> None:
    """The counterpart of the cases above: a row whose direction is flipped is caught even if its
    case is flipped with it, because the declared direction is asserted here too."""
    up = {"exit_buffer_pct", "max_chase_steps", "cooldown_seconds", "trail_loss_y", "lock_at",
          "trail_by", "min_entry_gap_s", "min_premium", "min_premium_expiry"}  # fmt: skip
    sets = {"allowed_underlyings": Direction.SUBSET, "denied_underlyings": Direction.SUPERSET}
    for key in DIRECTION_CASES:
        expected = sets.get(key, Direction.UP if key in up else Direction.DOWN)
        assert REGISTRY[key].direction is expected, key


def test_every_expiry_override_names_its_base_and_shares_its_unit_and_direction() -> None:
    overrides = {key: spec.override_of for key, spec in REGISTRY.items() if spec.override_of}

    assert overrides == {key: key.removesuffix("_expiry") for key in REGISTRY if "_expiry" in key}
    for key, base in overrides.items():
        assert REGISTRY[key].optional, key
        assert REGISTRY[key].unit is REGISTRY[base].unit, key
        assert REGISTRY[key].direction is REGISTRY[base].direction, key
        assert REGISTRY[key].floor_term is REGISTRY[base].floor_term, key


def test_shrinking_an_allow_list_mid_day_is_accepted() -> None:
    """risk-limits "Shrinking an allow list mid-day"."""
    stored = parse_limits(full(allowed_underlyings=["NIFTY", "BANKNIFTY"]))

    new = validate_edit(
        stored, full(allowed_underlyings=["nifty"]), now=MID_DAY, peak=D(0), floor=None
    )
    assert new.allowed_underlyings == frozenset({"NIFTY"})


def test_removing_from_a_deny_list_mid_day_is_rejected_naming_it() -> None:
    """risk-limits "Removing from a deny list mid-day"."""
    stored = parse_limits(full(denied_underlyings=["XYZ", "ABC"]))

    with pytest.raises(EditRejected, match="denied_underlyings") as refused:
        validate_edit(stored, full(denied_underlyings=["XYZ"]), now=MID_DAY, peak=D(0), floor=None)
    assert refused.value.code == TIGHTEN_ONLY


def test_swapping_allow_list_members_mid_day_is_rejected() -> None:
    """risk-limits "Swapping members mid-day": neither a subset nor a superset."""
    stored = parse_limits(full(allowed_underlyings=["NIFTY"]))

    with pytest.raises(EditRejected, match="allowed_underlyings") as refused:
        validate_edit(
            stored, full(allowed_underlyings=["BANKNIFTY"]), now=MID_DAY, peak=D(0), floor=None
        )
    assert refused.value.code == TIGHTEN_ONLY


def test_the_same_symbols_in_another_case_are_no_change() -> None:
    stored = parse_limits(full(denied_underlyings=["XYZ"]))

    assert loosened_keys(stored, parse_limits(full(denied_underlyings=["xyz"]))) == []


def test_removing_an_expiry_override_mid_day_is_rejected() -> None:
    """expiry-day-profile "Removing an override mid-day"."""
    stored = limits(entry_cutoff_expiry="14:00")

    with pytest.raises(EditRejected, match="entry_cutoff_expiry") as refused:
        validate_edit(stored, BASE, now=MID_DAY, peak=D(0), floor=None)
    assert refused.value.code == TIGHTEN_ONLY
    assert validate_edit(stored, BASE, now=AFTER_CLOSE, peak=D(0), floor=None)  # after the close


@pytest.mark.parametrize(
    "key",
    [k for k, spec in REGISTRY.items() if spec.optional and spec.leg_of is None],
)
def test_switching_an_optional_limit_on_tightens_and_off_loosens(key: str) -> None:
    on = parse_limits(FULL)
    off = replace(on, **{key: None})  # built directly: skips the floor consistency checks

    assert loosened_keys(off, on) == []  # None -> value: enabling is tighter
    assert loosened_keys(on, off) == [key]  # value -> None: disabling is looser


@pytest.mark.parametrize("key", ["giveback_abs", "giveback_pct"])
def test_adding_a_giveback_leg_to_a_term_that_is_on_loosens_and_dropping_one_tightens(
    key: str,
) -> None:
    """The giveback is the LARGER of its two legs, so a second leg can only widen it (and lower
    the floor). This is the opposite of switching a whole term on, which raises the floor."""
    both = parse_limits(BASE)  # arm, abs and pct
    one_leg = replace(both, **{key: None})

    assert loosened_keys(one_leg, both) == [key]  # adding the second leg: looser
    assert loosened_keys(both, one_leg) == []  # dropping it: tighter


def test_a_giveback_leg_can_come_with_its_arm_when_the_whole_term_is_switched_on() -> None:
    off = replace(parse_limits(BASE), giveback_arm=None, giveback_abs=None, giveback_pct=None)
    on = parse_limits(BASE)

    assert loosened_keys(off, on) == []  # a new term raises the floor: no part of it loosens
    # Switching it off loosens, and the arming limit is the one that says so (dropping a leg alone
    # would tighten, so the legs are not named).
    assert loosened_keys(on, off) == ["giveback_arm"]


def test_adding_a_giveback_leg_mid_day_is_refused() -> None:
    """The review's case: a percent-only giveback gains an absolute leg while the peak is still
    below the arm level, so a replay of today's peak sees no change. At a later peak of 5,000 the
    floor would drop from 3,500 to 2,000."""
    stored = parse_limits(edit(giveback_abs=None))
    wider = edit(giveback_abs="3000")

    with pytest.raises(EditRejected, match="giveback_abs") as refused:
        validate_edit(stored, wider, now=MID_DAY, peak=D(0), floor=None)
    assert refused.value.code == TIGHTEN_ONLY
    after = parse_limits(wider)
    assert floor_of(after.floor_params(), D(5000)) < floor_of(stored.floor_params(), D(5000))
    assert validate_edit(stored, wider, now=AFTER_CLOSE, peak=D(0), floor=None)  # after the close


def test_a_rule_mode_moves_shadow_to_enforce_only() -> None:
    for rule, may_shadow in RULES.items():
        if not may_shadow:
            continue
        shadow = parse_limits(edit(**{MODE_PREFIX + rule: "shadow"}))
        enforce = parse_limits(edit(**{MODE_PREFIX + rule: "enforce"}))
        unset = parse_limits(BASE)  # no mode set: enforces

        assert loosened_keys(shadow, enforce) == []
        assert loosened_keys(shadow, unset) == []
        assert loosened_keys(enforce, shadow) == [MODE_PREFIX + rule]
        assert loosened_keys(unset, shadow) == [MODE_PREFIX + rule]
        assert loosened_keys(enforce, unset) == []  # explicit enforce equals none: no change


def test_lock_mode_follows_its_ranking() -> None:
    off, lock, both = (limits(lock_mode=m) for m in ("OFF", "LOCK", "LOCK_AND_TRAIL"))

    assert loosened_keys(off, lock) == [] and loosened_keys(lock, both) == []
    assert loosened_keys(off, both) == []
    assert loosened_keys(lock, off) == ["lock_mode"]
    assert loosened_keys(both, lock) == ["lock_mode"]
    assert loosened_keys(both, off) == ["lock_mode"]


def test_only_off_to_trail_is_a_tightening_among_the_moves_that_involve_trail() -> None:
    """TRAIL counts its steps from a different start than LOCK_AND_TRAIL, so the ranking cannot
    place it: all but OFF -> TRAIL wait for the close."""
    modes = list(LockMode)
    for old in modes:
        for new in modes:
            if old is new or LockMode.TRAIL not in (old, new):
                continue
            expected = old is LockMode.OFF and new is LockMode.TRAIL
            assert trail_move_tightens(old, new) is expected
            before, after = limits(lock_mode=old.value), limits(lock_mode=new.value)
            assert (loosened_keys(before, after) == []) is expected


# ---- validate_edit (task 4.2) ---------------------------------------------------------------


def test_loosening_mid_day_is_rejected_naming_the_limit() -> None:
    """Spec "Loosening mid-day": at 11:00 IST an edit raises the per-trade maximum risk."""
    with pytest.raises(EditRejected) as refused:
        validate_edit(
            parse_limits(BASE), edit(per_trade_max_risk="1500"),
            now=MID_DAY, peak=D(0), floor=None,
        )  # fmt: skip

    assert refused.value.code == TIGHTEN_ONLY
    assert any("per_trade_max_risk" in p for p in refused.value.problems)


def test_tightening_mid_day_is_accepted() -> None:
    new = validate_edit(
        parse_limits(BASE), edit(per_trade_max_risk="500"), now=MID_DAY, peak=D(0), floor=None
    )

    assert new.per_trade_max_risk == D(500)


def test_loosening_after_the_close_is_accepted() -> None:
    """Spec "Loosening after the close": at 16:00 IST."""
    new = validate_edit(
        parse_limits(BASE), edit(per_trade_max_risk="1500"), now=AFTER_CLOSE, peak=D(0), floor=None
    )

    assert new.per_trade_max_risk == D(1500)


def test_loosening_on_a_weekend_is_accepted() -> None:
    assert validate_edit(
        parse_limits(BASE), edit(per_trade_max_risk="1500"), now=WEEKEND, peak=D(0), floor=None
    )


@pytest.mark.parametrize(
    ("at", "allowed"),
    [
        (datetime(2026, 11, 16, 3, 44, 59, tzinfo=UTC), True),  # 09:14:59 IST
        (datetime(2026, 11, 16, 3, 45, tzinfo=UTC), False),  # 09:15:00 IST: tighten-only starts
        (datetime(2026, 11, 16, 9, 59, 59, tzinfo=UTC), False),  # 15:29:59 IST
        (datetime(2026, 11, 16, 10, 0, tzinfo=UTC), True),  # 15:30:00 IST: free again
    ],
)
def test_the_tighten_only_window_is_0915_to_1530_ist(at: datetime, allowed: bool) -> None:
    stored, loosening = parse_limits(BASE), edit(per_trade_max_risk="1500")

    if allowed:
        assert validate_edit(stored, loosening, now=at, peak=D(0), floor=None)
    else:
        with pytest.raises(EditRejected):
            validate_edit(stored, loosening, now=at, peak=D(0), floor=None)


def test_the_first_set_is_accepted_at_any_hour() -> None:
    for at in (MID_DAY, AFTER_CLOSE, WEEKEND):
        assert validate_edit(None, BASE, now=at, peak=D(0), floor=None) == parse_limits(BASE)


def test_an_invalid_first_set_is_still_rejected() -> None:
    with pytest.raises(EditRejected) as refused:
        validate_edit(None, edit(price_band_pct="-1"), now=MID_DAY, peak=D(0), floor=None)

    assert refused.value.code == LIMITS_INVALID


def test_an_edit_is_applied_completely_or_not_at_all() -> None:
    """One tightening and one loosening: nothing is accepted, and only the loosening is named."""
    with pytest.raises(EditRejected) as refused:
        validate_edit(
            parse_limits(BASE), edit(per_trade_max_risk="500", max_trades_per_day=9),
            now=MID_DAY, peak=D(0), floor=None,
        )  # fmt: skip

    assert refused.value.code == TIGHTEN_ONLY
    assert len(refused.value.problems) == 1 and "max_trades_per_day" in refused.value.problems[0]


def test_every_loosened_limit_is_named() -> None:
    with pytest.raises(EditRejected) as refused:
        validate_edit(
            parse_limits(BASE), edit(per_trade_max_risk="1500", max_trades_per_day=9, stale_s=6),
            now=MID_DAY, peak=D(0), floor=None,
        )  # fmt: skip

    named = " ".join(refused.value.problems)
    assert "per_trade_max_risk" in named and "max_trades_per_day" in named and "stale_s" in named


def test_an_invalid_edit_is_rejected_for_its_values_before_the_direction_is_judged() -> None:
    with pytest.raises(EditRejected) as refused:
        validate_edit(
            parse_limits(BASE), edit(exit_buffer_pct="3", price_band_pct="2"),
            now=MID_DAY, peak=D(0), floor=None,
        )  # fmt: skip

    assert refused.value.code == LIMITS_INVALID


def test_edit_would_lower_the_floor() -> None:
    """Spec "Edit would lower the floor": removing the lock term after the lock has raised it."""
    stored = parse_limits(BASE)  # lock at 1000 sets the floor to 200
    assert floor_of(stored.floor_params(), D(1500)) == D(200)

    with pytest.raises(EditRejected) as refused:
        validate_edit(
            stored, edit(lock_mode="OFF", lock_trigger=None, lock_at=None),
            now=MID_DAY, peak=D(1500), floor=D(200),
        )  # fmt: skip

    assert refused.value.code == TIGHTEN_ONLY
    assert any("lower today's floor" in p for p in refused.value.problems)


def test_the_floor_replay_uses_the_engines_ratchet_not_just_the_formula() -> None:
    """The engine's floor (900) can sit above what the stored terms give at today's peak (200).
    ``lock_at`` 200 -> 300 tightens by direction and raises the formula's floor, yet the new floor
    (300) is under the ratchet, so the edit is refused. Replaying the formula alone would pass."""
    stored = parse_limits(BASE)
    assert floor_of(stored.floor_params(), D(1500)) == D(200)

    with pytest.raises(EditRejected) as refused:
        validate_edit(stored, edit(lock_at="300"), now=MID_DAY, peak=D(1500), floor=D(900))

    assert refused.value.code == TIGHTEN_ONLY
    assert any("from 900 to 300" in p for p in refused.value.problems)


EXPIRY_ABS: dict[str, LimitValue] = {"giveback_pct": None, "giveback_abs_expiry": "600"}


def test_the_floor_replay_uses_the_expiry_giveback_once_the_day_has_expiring_exposure() -> None:
    """add-risk-guards task 8.2: the re-simulation uses the flag the monitor ratchets with. At a
    peak of 5,000 the base giveback (1,000) gives 4,000 and the expiry giveback (600) 4,400, which
    is today's floor once the account has had expiring exposure. ``lock_at`` 200 -> 300 tightens
    and leaves that floor where it is; replayed with the base giveback it would look like a drop
    from 4,400 to 4,000."""
    stored = limits(**EXPIRY_ABS)
    tighter = edit(**EXPIRY_ABS, lock_at="300")
    assert floor_of(stored.floor_params(), D(5000)) == D(4000)
    assert floor_of(stored.floor_params(expiry=True), D(5000)) == D(4400)

    new = validate_edit(stored, tighter, now=MID_DAY, peak=D(5000), floor=D(4400), expiry=True)

    assert new.lock_at == D(300)
    with pytest.raises(EditRejected, match="from 4400 to 4000"):
        validate_edit(stored, tighter, now=MID_DAY, peak=D(5000), floor=D(4400), expiry=False)


def test_without_a_floor_term_changing_there_is_nothing_to_replay() -> None:
    assert validate_edit(
        parse_limits(BASE), edit(per_trade_max_risk="500"), now=MID_DAY, peak=D(1500), floor=D(900)
    )


def test_a_floor_term_edit_that_raises_the_floor_is_accepted() -> None:
    new = validate_edit(
        parse_limits(BASE), edit(lock_at="300"), now=MID_DAY, peak=D(1500), floor=D(200)
    )

    assert floor_of(new.floor_params(), D(1500)) == D(300)


def test_a_peak_the_floor_cannot_be_replayed_with_refuses_a_floor_term_edit() -> None:
    """Fail closed: a corrupt peak must not wave a floor-term edit through."""
    with pytest.raises(EditRejected, match="re-simulated"):
        validate_edit(
            parse_limits(BASE), edit(lock_at="300"),
            now=MID_DAY, peak=D("NaN"), floor=None,
        )  # fmt: skip


def test_equity_floor_to_shadow_is_rejected() -> None:
    """Spec "Equity floor to shadow", in market hours and outside them."""
    for at in (MID_DAY, AFTER_CLOSE):
        with pytest.raises(EditRejected) as refused:
            validate_edit(
                parse_limits(BASE), edit(**{MODE_PREFIX + "equity_floor": "shadow"}),
                now=at, peak=D(0), floor=None,
            )  # fmt: skip
        assert refused.value.code == LIMITS_INVALID


def test_a_rule_moves_from_shadow_to_enforce_mid_day_but_not_back() -> None:
    shadowed = parse_limits(edit(**{MODE_PREFIX + "per_trade_risk": "shadow"}))

    enforced = validate_edit(
        shadowed, edit(**{MODE_PREFIX + "per_trade_risk": "enforce"}),
        now=MID_DAY, peak=D(0), floor=None,
    )  # fmt: skip
    assert enforced.mode_of("per_trade_risk") is RuleMode.ENFORCE
    with pytest.raises(EditRejected, match="per_trade_risk"):
        validate_edit(
            enforced, edit(**{MODE_PREFIX + "per_trade_risk": "shadow"}),
            now=MID_DAY, peak=D(0), floor=None,
        )  # fmt: skip


def test_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_edit(None, BASE, now=datetime(2026, 11, 16, 11, 0), peak=D(0), floor=None)


# ---- the floor never falls under an edit that "tightens" (property) --------------------------
#
# The registry's directions are a claim about the floor's arithmetic: that every move it calls a
# tightening can only raise (or keep) the floor at every peak. ``validate_edit`` also replays
# today's peak, but that only looks at one peak; a later, higher one could still meet a lower
# floor. So the claim is checked here against ``floor_of`` for random pairs of floor settings.

# Each group is a set of keys that must be valid together; the choices are valid groups only, so
# generated settings parse and the property is not starved by invalid combinations.
_TRAIL_LOSS = [(None, None), ("800", "400"), ("1000", "500"), ("1000", "1000"), ("1200", "300")]
_LOCK = [(None, None), ("900", "100"), ("1000", "200"), ("1000", "300"), ("1100", "-500")]
_TRAIL = [(None, None), ("400", "300"), ("500", "250"), ("500", "300"), ("600", "200")]
_GIVEBACK = [
    (None, None, None),
    ("2000", "1000", None), ("2000", None, "30"), ("2000", "1000", "30"),
    ("1500", "1000", "30"), ("2000", "800", "30"), ("2000", "1000", "25"),
    ("2500", "1000", "30"), ("2000", "500", "20"), ("1500", None, "10"),
]  # fmt: skip
# The expiry giveback overrides (arm, abs, pct). Many combine with a base giveback into an
# inconsistent set (looser than the base, or a leg the base lacks); those are filtered out by
# ``_parses``, and enough valid ones remain: with no base giveback, with each base leg, and
# replacing one or several base values.
_GIVEBACK_EXPIRY = [
    (None, None, None),
    ("1500", None, None), ("1000", None, None),
    (None, "800", None), (None, "500", None), (None, None, "20"), (None, None, "25"),
    (None, "800", "20"), ("1500", "800", None), ("1500", None, "20"), ("1500", "800", "20"),
    ("2000", "1000", None), ("2000", None, "30"), ("1200", "500", "10"),
]  # fmt: skip
_MODES = ["OFF", "LOCK", "TRAIL", "LOCK_AND_TRAIL"]
_DAILY = ["2000", "2500", "3000", "3500"]


@st.composite
def floor_settings(
    draw: st.DrawFn, like: dict[str, LimitValue] | None = None
) -> dict[str, LimitValue]:
    """Floor-term settings; each group stays as in ``like`` about half the time, so that pairs
    differ in few keys and many pairs are pure tightenings."""

    def pick(group: str, choices: list[Any]) -> Any:
        if like is not None and draw(st.booleans()):
            return tuple(like[k] for k in group.split(","))
        return draw(st.sampled_from(choices))

    x, y = pick("trail_loss_x,trail_loss_y", _TRAIL_LOSS)
    trigger, at = pick("lock_trigger,lock_at", _LOCK)
    step, by = pick("trail_step,trail_by", _TRAIL)
    arm, abs_, pct = pick("giveback_arm,giveback_abs,giveback_pct", _GIVEBACK)
    arm_e, abs_e, pct_e = pick(
        "giveback_arm_expiry,giveback_abs_expiry,giveback_pct_expiry", _GIVEBACK_EXPIRY
    )
    mode = (like["lock_mode"] if like is not None and draw(st.booleans())
            else draw(st.sampled_from(_MODES)))  # fmt: skip
    daily = (
        like["daily_max_loss"]
        if like is not None and draw(st.booleans())
        else draw(st.sampled_from(_DAILY))
    )
    return {
        "daily_max_loss": daily, "trail_loss_x": x, "trail_loss_y": y, "lock_mode": mode,
        "lock_trigger": trigger, "lock_at": at, "trail_step": step, "trail_by": by,
        "giveback_arm": arm, "giveback_abs": abs_, "giveback_pct": pct,
        "giveback_arm_expiry": arm_e, "giveback_abs_expiry": abs_e, "giveback_pct_expiry": pct_e,
    }  # fmt: skip


def _parses(settings: dict[str, LimitValue]) -> RiskLimits | None:
    try:
        return parse_limits({**BASE, **settings})
    except InvalidLimits:
        return None


@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(
    data=st.data(),
    peak=st.decimals(min_value=0, max_value=30000, places=2),
    expiry=st.booleans(),
)
def test_an_edit_that_loosened_keys_calls_a_tightening_never_lowers_the_floor_at_any_peak(
    data: st.DataObject, peak: Decimal, expiry: bool
) -> None:
    """With the expiry giveback keys in play, for the base floor and the expiry-day floor
    (``expiry`` drawn both ways)."""
    old_settings = data.draw(floor_settings())
    new_settings = data.draw(floor_settings(like=old_settings))
    old, new = _parses(old_settings), _parses(new_settings)
    assume(old is not None and new is not None)
    assert old is not None and new is not None
    assume(loosened_keys(old, new) == [])

    after = floor_of(new.floor_params(expiry=expiry), peak)
    before = floor_of(old.floor_params(expiry=expiry), peak)
    assert after >= before, (old_settings, new_settings, peak, expiry)


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=st.data(), peak=st.decimals(min_value=0, max_value=30000, places=2))
def test_the_expiry_floor_is_never_below_the_base_floor_at_any_peak(
    data: st.DataObject, peak: Decimal
) -> None:
    """Design §10: the overrides are no looser than their bases and add no leg the base term
    lacks, so turning the expiry giveback on can only raise the floor."""
    settings_ = data.draw(floor_settings())
    parsed = _parses(settings_)
    assume(parsed is not None)
    assert parsed is not None

    expiry_floor = floor_of(parsed.floor_params(expiry=True), peak)
    assert expiry_floor >= floor_of(parsed.floor_params(), peak), (settings_, peak)


def test_switching_the_base_giveback_on_cannot_widen_an_expiry_only_giveback() -> None:
    """The case ``_widened_legs`` looks at both floors for: an expiry-only giveback of 1,000
    gains a 30% leg from the base term switched on in the same edit. At a peak of 5,000 the
    expiry-day floor would drop from 4,000 to 3,500, though no expiry key changed."""
    no_base: dict[str, LimitValue] = {
        "giveback_arm": None, "giveback_abs": None, "giveback_pct": None,
        "giveback_arm_expiry": "1500", "giveback_abs_expiry": "1000",
    }  # fmt: skip
    stored = limits(**no_base)
    switched_on = limits(
        **{**no_base, "giveback_arm": "2000", "giveback_abs": "1000", "giveback_pct": "30"}
    )
    assert floor_of(switched_on.floor_params(expiry=True), D(5000)) < floor_of(
        stored.floor_params(expiry=True), D(5000)
    )

    assert loosened_keys(stored, switched_on) == ["giveback_pct"]
    # Without the extra leg, switching the base term on is a tightening.
    same_legs = limits(**{**no_base, "giveback_arm": "2000", "giveback_abs": "1000"})
    assert loosened_keys(stored, same_legs) == []


def test_adding_a_leg_to_an_expiry_only_giveback_loosens() -> None:
    stored = limits(
        giveback_arm=None, giveback_abs=None, giveback_pct=None,
        giveback_arm_expiry="1500", giveback_abs_expiry="1000",
    )  # fmt: skip
    wider = replace(stored, giveback_pct_expiry=D(30))

    assert loosened_keys(stored, wider) == ["giveback_pct_expiry"]
