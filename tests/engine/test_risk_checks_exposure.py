"""The exposure and day-counter rules (add-risk-engine tasks 6.4 and 6.5; risk-rules spec; equity-
floor "Entries must leave headroom above the floor").

Scenarios: "Limit reached" (max trades), "Third loss in a row" (cooldown, check side),
"Total reached", "Carried position counts", "Two entries in flight", "After the cutoff" and "Not
enough headroom", plus add-ons, the per-underlying limit, boundaries and the floor ratchet.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderState, OrderType, Position, ProductType
from engine.risk.checks.counters import COOLDOWN, MAX_TRADES, Cooldown, MaxTrades
from engine.risk.checks.exposure import (
    ENTRY_CUTOFF,
    FLOOR_HEADROOM,
    OPEN_POSITIONS,
    OPEN_POSITIONS_UNDERLYING,
    EntryCutoff,
    FloorHeadroom,
    OpenPositions,
)
from engine.risk.decision import Severity, run_pre_trade
from engine.risk.floor import LockMode
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import (
    LIMITS,
    NOW,
    STOCK,
    context,
    day_state,
    make_instrument,
    order,
    position,
)

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def key(n: int) -> InstrumentKey:
    return InstrumentKey.parse(f"NSE_FNO:{50000 + n}")


def many(n: int, underlying: str = "NIFTY") -> tuple[list[Instrument], list[Position]]:
    """``n`` instruments of one underlying, and an open long position in each."""
    instruments = [make_instrument(str(key(i)), underlying) for i in range(n)]
    return instruments, [position(key=i.key, quantity=65) for i in instruments]


# ---- MaxTrades -------------------------------------------------------------------------------


def test_limit_reached() -> None:
    """Spec "Limit reached": the maximum is 5 and five entries were allowed today."""
    ctx = context(day=day_state(entries_today=5))

    decision = MaxTrades().pre_trade(ctx, order(BUY, 65))

    assert decision.severity is Severity.REJECT and decision.code == MAX_TRADES
    assert decision.inputs == {"entries_today": 5, "limit": 5}


@pytest.mark.parametrize(("done", "allowed"), [(0, True), (4, True), (5, False), (9, False)])
def test_entries_up_to_the_limit_are_allowed(done: int, allowed: bool) -> None:
    ctx = context(day=day_state(entries_today=done))

    assert (MaxTrades().pre_trade(ctx, order(BUY, 65)).severity is Severity.ALLOW) is allowed


# ---- Cooldown --------------------------------------------------------------------------------


def test_entries_are_refused_until_the_cooldown_has_passed() -> None:
    """Spec "Third loss in a row": the third loss closes at 10:00 and the cooldown ends 10:30."""
    until = NOW + timedelta(minutes=30)
    state = day_state(loss_streak=3, cooldown_until=until)

    during = Cooldown().pre_trade(context(day=state), order(BUY, 65))
    at_end = Cooldown().pre_trade(context(day=state, now=until), order(BUY, 65))
    after = Cooldown().pre_trade(
        context(day=state, now=until + timedelta(seconds=1)), order(BUY, 65)
    )

    assert during.severity is Severity.REJECT and during.code == COOLDOWN
    assert during.inputs["loss_streak"] == 3
    assert at_end.severity is Severity.ALLOW and after.severity is Severity.ALLOW


def test_no_cooldown_means_no_refusal() -> None:
    assert Cooldown().pre_trade(context(day=day_state(loss_streak=2)), order(BUY, 65)).code is None


# ---- OpenPositions ---------------------------------------------------------------------------


def total(limit: int, per_underlying: int = 99) -> RiskLimits:
    return replace(LIMITS, max_open_total=limit, max_open_per_underlying=per_underlying)


def test_total_reached() -> None:
    """Spec "Total reached": the total limit is 5 and five positions are open."""
    instruments, positions = many(5)
    new = make_instrument(str(key(99)), "BANKNIFTY")
    ctx = context(limits=total(5), positions=positions, extra_instruments=[*instruments, new])

    decision = OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key))

    assert decision.severity is Severity.REJECT and decision.code == OPEN_POSITIONS
    assert decision.inputs == {"open_or_pending": 5, "limit": 5}


def test_one_below_the_total_is_allowed() -> None:
    instruments, positions = many(4)
    new = make_instrument(str(key(99)), "BANKNIFTY")
    ctx = context(limits=total(5), positions=positions, extra_instruments=[*instruments, new])

    assert OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key)).code is None


def test_carried_position_counts() -> None:
    """Spec "Carried position counts": one of the five is a CNC holding carried from yesterday."""
    instruments, positions = many(4)
    carried = position(key=STOCK, quantity=100, product=ProductType.CNC)
    new = make_instrument(str(key(99)), "BANKNIFTY")
    ctx = context(
        limits=total(5), positions=[*positions, carried], extra_instruments=[*instruments, new]
    )

    decision = OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key))

    assert decision.code == OPEN_POSITIONS


def test_two_entries_in_flight() -> None:
    """Spec "Two entries in flight": four open positions and one unfilled entry in another
    instrument reach the limit of 5, so a second new entry is refused."""
    instruments, positions = many(4)
    resting_key, new = (
        make_instrument(str(key(50)), "FINNIFTY"),
        make_instrument(str(key(99)), "SENSEX"),
    )
    resting = order(BUY, 65, key=resting_key.key, order_id="r1", state=OrderState.OPEN)
    ctx = context(
        limits=total(5), positions=positions, orders=[resting],
        extra_instruments=[*instruments, resting_key, new],
    )  # fmt: skip

    decision = OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key))

    assert decision.code == OPEN_POSITIONS and decision.inputs["open_or_pending"] == 5


def test_resting_exits_and_finished_orders_do_not_count() -> None:
    instruments, positions = many(4)
    new = make_instrument(str(key(99)), "SENSEX")
    held = instruments[0].key
    resting_exit = order(SELL, 65, key=held, order_id="x1", state=OrderState.OPEN)  # reduces
    finished = order(BUY, 65, key=key(77), order_id="f1", state=OrderState.FILLED)
    cancelled = order(BUY, 65, key=key(78), order_id="c1", state=OrderState.CANCELLED)
    ctx = context(
        limits=total(5), positions=positions, orders=[resting_exit, finished, cancelled],
        extra_instruments=[*instruments, new],
    )  # fmt: skip

    assert OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key)).code is None


def test_adding_to_a_position_already_held_is_not_a_new_position() -> None:
    instruments, positions = many(5)
    ctx = context(limits=total(5), positions=positions, extra_instruments=instruments)

    add_on = order(BUY, 65, key=instruments[0].key)  # same instrument and product

    assert OpenPositions().pre_trade(ctx, add_on).code is None


def test_a_flip_in_a_held_instrument_is_not_a_new_position_either() -> None:
    instruments, positions = many(5)
    ctx = context(limits=total(5), positions=positions, extra_instruments=instruments)

    assert OpenPositions().pre_trade(ctx, order(SELL, 130, key=instruments[0].key)).code is None


def test_the_same_instrument_under_another_product_is_a_new_position() -> None:
    instruments, positions = many(5)
    ctx = context(limits=total(5), positions=positions, extra_instruments=instruments)

    margin = order(BUY, 65, key=instruments[0].key, product=ProductType.MARGIN)

    assert OpenPositions().pre_trade(ctx, margin).code == OPEN_POSITIONS


def test_an_unfilled_entry_in_this_very_instrument_is_not_another_position() -> None:
    instruments, positions = many(4)
    new = make_instrument(str(key(99)), "SENSEX")
    resting = order(BUY, 65, key=new.key, order_id="r1", state=OrderState.OPEN)
    ctx = context(
        limits=total(5), positions=positions, orders=[resting],
        extra_instruments=[*instruments, new],
    )  # fmt: skip

    assert OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key)).code is None


def test_the_per_underlying_limit() -> None:
    instruments, positions = many(2, "NIFTY")
    other, same = make_instrument(str(key(98)), "BANKNIFTY"), make_instrument(str(key(99)), "NIFTY")
    ctx = context(
        limits=total(10, per_underlying=2), positions=positions,
        extra_instruments=[*instruments, other, same],
    )  # fmt: skip

    refused = OpenPositions().pre_trade(ctx, order(BUY, 65, key=same.key))
    allowed = OpenPositions().pre_trade(ctx, order(BUY, 65, key=other.key))

    assert refused.code == OPEN_POSITIONS_UNDERLYING and refused.inputs["underlying"] == "NIFTY"
    assert allowed.code is None


def test_a_held_position_with_no_instrument_record_counts_against_the_underlying() -> None:
    """It cannot be placed under any underlying, so it is counted against this one: refusing one
    entry too many is better than letting an unknown contract hide a concentration."""
    unknown = position(key=key(98), quantity=65)
    new = make_instrument(str(key(99)), "NIFTY")
    ctx = context(limits=total(10, per_underlying=1), positions=[unknown], extra_instruments=[new])

    decision = OpenPositions().pre_trade(ctx, order(BUY, 65, key=new.key))

    assert decision.code == OPEN_POSITIONS_UNDERLYING


def test_an_unknown_underlying_fails_closed() -> None:
    ctx = context(limits=total(10, per_underlying=2))

    decision = OpenPositions().pre_trade(ctx, order(BUY, 65, key=key(99)))

    assert decision.code == OPEN_POSITIONS_UNDERLYING and "unavailable" in decision.detail


# ---- EntryCutoff -----------------------------------------------------------------------------


def at_ist(hour: int, minute: int, second: int = 0) -> datetime:
    """An instant that is hour:minute IST on Monday 2026-11-16 (IST = UTC + 5:30)."""
    return datetime(2026, 11, 16, hour, minute, second, tzinfo=UTC) - timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    ("when", "allowed"),
    [
        (at_ist(14, 59, 59), True),
        (at_ist(15, 0), False),  # "at or after the cutoff"
        (at_ist(15, 1), False),
        (at_ist(9, 15), True),
    ],
)
def test_after_the_cutoff(when: datetime, allowed: bool) -> None:
    """Spec "After the cutoff": the cutoff is 15:00 and an entry arrives at 15:01 IST."""
    decision = EntryCutoff().pre_trade(context(now=when), order(BUY, 65))

    assert (decision.severity is Severity.ALLOW) is allowed
    if not allowed:
        assert decision.code == ENTRY_CUTOFF and decision.inputs["cutoff"] == "15:00"


# ---- FloorHeadroom ---------------------------------------------------------------------------


def floors(**changes: object) -> RiskLimits:
    """Limits with only the starting loss (floor -2000) unless more terms are given."""
    base = dict(
        daily_max_loss=D(2000), lock_mode=LockMode.OFF, trail_loss_x=None, trail_loss_y=None,
        lock_trigger=None,
        lock_at=None, trail_step=None, trail_by=None, giveback_arm=None, giveback_abs=None,
        giveback_pct=None,
    )  # fmt: skip
    return replace(LIMITS, **{**base, **changes})  # type: ignore[arg-type]


def risking_400() -> Order:
    return order(
        BUY, 100, key=STOCK, order_type=OrderType.LIMIT, limit_price=D("200.00"),
        initial_stop=D("196.00"),
    )  # fmt: skip


def test_not_enough_headroom() -> None:
    """Spec "Not enough headroom": MTM_day -1,700, floor -2,000, an entry risking 400."""
    ctx = context(limits=floors(), mtm_day=D(-1700))

    decision = FloorHeadroom().pre_trade(ctx, risking_400())

    assert decision.severity is Severity.REJECT and decision.code == FLOOR_HEADROOM
    assert decision.inputs == {"mtm_day": "-1700", "risk": "400.00", "floor": "-2000"}


@pytest.mark.parametrize(
    ("mtm", "allowed"),
    [(-1000, True), (-1599, True), (-1600, False), (-1700, False)],  # -1600 - 400 = -2000: at floor
)
def test_landing_exactly_on_the_floor_is_refused(mtm: int, allowed: bool) -> None:
    ctx = context(limits=floors(), mtm_day=D(mtm))

    assert (FloorHeadroom().pre_trade(ctx, risking_400()).severity is Severity.ALLOW) is allowed


def test_an_unknown_mtm_refuses_entries() -> None:
    decision = FloorHeadroom().pre_trade(context(limits=floors(), mtm_day=None), risking_400())

    assert decision.code == FLOOR_HEADROOM and decision.inputs["mtm_day"] == "unknown"


def test_the_engines_floor_is_used_when_it_is_above_the_formula() -> None:
    """The ratchet: the floor never falls, so a floor of -1000 holds although the formula says
    -2000."""
    state = day_state(floor=D(-1000))

    assert (
        FloorHeadroom()
        .pre_trade(context(limits=floors(), day=state, mtm_day=D(-500)), risking_400())
        .code
        is None
    )  # -900 is above -1000
    assert (
        FloorHeadroom()
        .pre_trade(context(limits=floors(), day=state, mtm_day=D(-700)), risking_400())
        .code
        == FLOOR_HEADROOM
    )  # -1100 is under it, though above the formula's -2000


def test_a_higher_peak_raises_the_floor_the_headroom_is_measured_from() -> None:
    """With the lock term (floor 200 once the peak reaches 1,000), peak 1,500 gives floor 200."""
    limits = floors(lock_mode=LockMode.LOCK, lock_trigger=D(1000), lock_at=D(200))
    state = day_state(peak=D(1500))

    assert (
        FloorHeadroom()
        .pre_trade(context(limits=limits, day=state, mtm_day=D(1000)), risking_400())
        .code
        is None
    )  # 600 > 200
    assert (
        FloorHeadroom()
        .pre_trade(context(limits=limits, day=state, mtm_day=D(550)), risking_400())
        .code
        == FLOOR_HEADROOM
    )  # 150 <= 200


def test_an_entry_whose_risk_cannot_be_measured_is_assumed_to_risk_the_most_it_may() -> None:
    """No stop to measure from: the per-trade maximum (1,000) stands in, so -1,000 - 1,000 is at
    the floor of -2,000."""
    no_stop = order(BUY, 100, key=STOCK, order_type=OrderType.LIMIT, limit_price=D("200.00"))

    decision = FloorHeadroom().pre_trade(context(limits=floors(), mtm_day=D(-1000)), no_stop)

    assert decision.code == FLOOR_HEADROOM and decision.inputs["risk"] == "1000"


def test_limits_that_cannot_make_a_floor_make_the_check_raise_and_the_gate_reject() -> None:
    broken = floors(trail_loss_x=D(500), trail_loss_y=D(600))  # y > x: floor outruns the peak

    with pytest.raises(ValueError, match="trail_loss"):
        FloorHeadroom().pre_trade(context(limits=broken), risking_400())

    [result] = run_pre_trade([FloorHeadroom()], context(limits=broken), risking_400())
    assert result.decision.severity is Severity.REJECT  # fail closed, via the check runner


@pytest.mark.parametrize(
    "check", [MaxTrades(), Cooldown(), OpenPositions(), EntryCutoff(), FloorHeadroom()]
)
def test_these_are_entry_rules(check: object) -> None:
    assert check.applies_to_exits is False  # type: ignore[attr-defined]
    assert check.name in {  # type: ignore[attr-defined]
        "max_trades", "cooldown", "max_open_positions", "entry_cutoff", "equity_floor"
    }  # fmt: skip


def test_an_add_on_to_a_held_position_needs_no_instrument_facts() -> None:
    """Adding to a position already held is never a new position, so it is allowed even when the
    instrument's facts (and so its underlying) are unavailable."""
    unknown = key(99)
    ctx = context(limits=total(1, per_underlying=1), positions=[position(key=unknown, quantity=65)])

    assert OpenPositions().pre_trade(ctx, order(BUY, 65, key=unknown)).code is None
