"""merge_day_state: the one-way rule behind the persisted risk state (add-risk-engine task 2.3).

Property tests (``hypothesis``): whatever sequence of writes arrives, in whatever order, the
fields that may only rise never fall, and re-sending a write changes nothing. The repository
contract suite proves Postgres follows the same rule on enumerated cases.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import reduce

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.snapshot import HaltLevel
from engine.risk.state import DayState, merge_day_state

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)

money = st.decimals(min_value=-100_000, max_value=100_000, places=2, allow_nan=False)
times = st.integers(min_value=0, max_value=600).map(lambda m: NOW + timedelta(minutes=m))


@st.composite
def states(draw: st.DrawFn) -> DayState:
    return DayState(
        account_id="main",
        trade_date=date(2026, 11, 16),
        updated_at=draw(times),
        peak=draw(money),
        floor=draw(st.none() | money),
        halt_level=draw(st.sampled_from(list(HaltLevel))),
        halt_reason=draw(st.none() | st.sampled_from(["a", "b", "c"])),
        entries_today=draw(st.integers(min_value=0, max_value=50)),
        loss_streak=draw(st.integers(min_value=0, max_value=10)),
        cooldown_until=draw(st.none() | times),
        close_marks=draw(st.none() | st.just({"NSE_FNO:1|INTRADAY": "100.00"})),
        entry_lots_today=draw(st.integers(min_value=0, max_value=100)),
        last_entry_at=draw(st.none() | times),
        expiry_exposure=draw(st.booleans()),
    )


def merged(writes: list[DayState]) -> DayState:
    return reduce(lambda stored, new: merge_day_state(stored, new), writes[1:], writes[0])


def known(values: list[Decimal | datetime | None]) -> list[Decimal | datetime]:
    return [v for v in values if v is not None]


@given(st.lists(states(), min_size=1, max_size=12))
def test_rising_fields_never_fall_whatever_the_write_order(writes: list[DayState]) -> None:
    result = merged(writes)

    assert result.peak == max(w.peak for w in writes)
    assert result.halt_level == max(w.halt_level for w in writes)
    assert result.entries_today == max(w.entries_today for w in writes)
    floors = known([w.floor for w in writes])
    assert result.floor == (max(floors) if floors else None)  # type: ignore[type-var]
    cooldowns = known([w.cooldown_until for w in writes])
    assert result.cooldown_until == (max(cooldowns) if cooldowns else None)  # type: ignore[type-var]
    # add-risk-guards task 2.2: maximum, maximum, OR.
    assert result.entry_lots_today == max(w.entry_lots_today for w in writes)
    entries = known([w.last_entry_at for w in writes])
    assert result.last_entry_at == (max(entries) if entries else None)
    assert result.expiry_exposure == any(w.expiry_exposure for w in writes)


@given(st.lists(states(), min_size=2, max_size=8), st.randoms(use_true_random=False))
def test_the_rising_fields_do_not_depend_on_the_order_of_the_writes(
    writes: list[DayState], random: object
) -> None:
    shuffled = list(writes)
    random.shuffle(shuffled)  # type: ignore[attr-defined]

    a, b = merged(writes), merged(shuffled)

    assert (a.peak, a.floor, a.halt_level, a.entries_today, a.cooldown_until) == (
        b.peak, b.floor, b.halt_level, b.entries_today, b.cooldown_until,
    )  # fmt: skip
    assert (a.entry_lots_today, a.last_entry_at, a.expiry_exposure) == (
        b.entry_lots_today, b.last_entry_at, b.expiry_exposure,
    )  # fmt: skip


@given(states(), states())
def test_repeating_a_write_changes_nothing(stored: DayState, new: DayState) -> None:
    once = merge_day_state(stored, new)

    assert merge_day_state(once, new) == once


@given(states(), states())
def test_the_result_is_never_below_either_input_on_a_rising_field(
    stored: DayState, new: DayState
) -> None:
    result = merge_day_state(stored, new)

    for field in ("peak", "halt_level", "entries_today", "entry_lots_today", "expiry_exposure"):
        assert getattr(result, field) >= getattr(stored, field)
        assert getattr(result, field) >= getattr(new, field)
    if stored.floor is not None:
        assert result.floor is not None and result.floor >= stored.floor
    for value in (stored.last_entry_at, new.last_entry_at):
        if value is not None:
            assert result.last_entry_at is not None and result.last_entry_at >= value


@given(states(), states())
def test_a_halt_reason_belongs_to_the_level_that_set_it(stored: DayState, new: DayState) -> None:
    result = merge_day_state(stored, new)

    if new.halt_level < stored.halt_level:
        assert result.halt_reason == stored.halt_reason  # a stale lower write cannot change it
    elif new.halt_level > stored.halt_level:
        # A higher level brings its own reason, even none: a wrong label is worse than no label.
        assert result.halt_reason == new.halt_reason
    elif new.halt_reason is not None:
        assert result.halt_reason == new.halt_reason  # the same level may replace the reason
    else:
        assert result.halt_reason == stored.halt_reason  # but a missing reason never wipes one


def test_the_first_write_is_stored_as_it_is() -> None:
    first = DayState(account_id="main", trade_date=date(2026, 11, 16), updated_at=NOW)

    assert merge_day_state(None, first) is first


def test_close_marks_keep_the_first_marks() -> None:
    first = DayState("main", date(2026, 11, 16), NOW, close_marks={"k": "1"})
    second = DayState("main", date(2026, 11, 16), NOW, close_marks={"k": "2"})

    assert merge_day_state(first, second).close_marks == {"k": "1"}
    assert merge_day_state(DayState("main", date(2026, 11, 16), NOW), second).close_marks == {
        "k": "2"
    }


# ---- add-risk-guards task 2.2: gross lots, last entry time, expiry exposure -----------------


def test_a_lower_lot_count_or_an_older_entry_time_never_wins() -> None:
    later = NOW + timedelta(minutes=10)
    stored = DayState("main", date(2026, 11, 16), NOW, entry_lots_today=6, last_entry_at=later)
    stale = DayState("main", date(2026, 11, 16), NOW, entry_lots_today=2, last_entry_at=NOW)
    unset = DayState("main", date(2026, 11, 16), NOW)  # 0 lots, no entry time

    for new in (stale, unset):
        result = merge_day_state(stored, new)
        assert result.entry_lots_today == 6 and result.last_entry_at == later


def test_a_higher_lot_count_and_a_newer_entry_time_are_taken() -> None:
    later = NOW + timedelta(minutes=10)
    stored = DayState("main", date(2026, 11, 16), NOW, entry_lots_today=2, last_entry_at=NOW)
    new = DayState("main", date(2026, 11, 16), NOW, entry_lots_today=6, last_entry_at=later)

    result = merge_day_state(stored, new)

    assert result.entry_lots_today == 6 and result.last_entry_at == later


def test_expiry_exposure_never_turns_off_within_a_day() -> None:
    on = DayState("main", date(2026, 11, 16), NOW, expiry_exposure=True)
    off = DayState("main", date(2026, 11, 16), NOW, expiry_exposure=False)

    assert merge_day_state(on, off).expiry_exposure is True  # a stale write cannot clear it
    assert merge_day_state(off, on).expiry_exposure is True  # switching it on is taken
    assert merge_day_state(off, off).expiry_exposure is False


def test_the_new_fields_start_fresh_each_day() -> None:
    fresh = DayState("main", date(2026, 11, 17), NOW)

    assert fresh.entry_lots_today == 0 and fresh.last_entry_at is None
    assert fresh.expiry_exposure is False


# ---- guards: what a DayState and a merge refuse (review findings) ---------------------------


def test_merging_two_different_days_is_refused() -> None:
    """Carrying yesterday's KILL into today (or losing it) would break the post-close reset."""
    today = DayState("main", date(2026, 11, 16), NOW, halt_level=HaltLevel.KILL)
    tomorrow = DayState("main", date(2026, 11, 17), NOW)
    other_account = DayState("other", date(2026, 11, 16), NOW)

    with pytest.raises(ValueError, match="cannot merge"):
        merge_day_state(today, tomorrow)
    with pytest.raises(ValueError, match="cannot merge"):
        merge_day_state(today, other_account)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["peak", "floor"])
def test_a_non_finite_peak_or_floor_is_refused(field: str, bad: str) -> None:
    """Postgres GREATEST treats NaN as larger than any number, so it would stick for the day."""
    with pytest.raises(ValueError, match="finite"):
        DayState("main", date(2026, 11, 16), NOW, **{field: Decimal(bad)})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["updated_at", "cooldown_until", "last_entry_at"])
def test_a_naive_datetime_is_refused(field: str) -> None:
    values: dict[str, object] = {"updated_at": NOW}
    values[field] = datetime(2026, 11, 16, 5, 0)  # no timezone
    with pytest.raises(ValueError, match="timezone-aware"):
        DayState("main", date(2026, 11, 16), **values)  # type: ignore[arg-type]
