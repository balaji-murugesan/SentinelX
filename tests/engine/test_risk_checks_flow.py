"""Entry pacing (add-risk-guards task 5.2; order-flow-guards spec "Entries per rolling minute are
capped" and "Entries keep a minimum gap").

Scenarios: "Fourth entry in a minute", "Window has moved on", "Too soon after the last entry" and
"An exit right after an entry", with the window edge at exactly 60 s and the gap boundary. "Gap
survives a restart" is an end-to-end test (test_risk_end_to_end.py), because it needs the day
book's persisted ``last_entry_at``.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.orders import OrderSide
from engine.risk.checks.flow import ENTRY_GAP, ENTRY_RATE, EntryPacing
from engine.risk.checks.registry import default_checks
from engine.risk.decision import Severity
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import LIMITS, context, day_state, order, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_risk_gate import harness
from tests.engine.test_risk_limits import BASE

BUY, SELL = OrderSide.BUY, OrderSide.SELL


def at(hour: int, minute: int, second: int) -> datetime:
    """An instant that is hour:minute:second IST on Monday 2026-11-16 (IST = UTC + 5:30)."""
    return datetime(2026, 11, 16, hour, minute, second, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def pacing(per_minute: int = 99, gap_s: int = 1) -> RiskLimits:
    return replace(LIMITS, max_entries_per_minute=per_minute, min_entry_gap_s=gap_s)


THREE = (at(10, 0, 5), at(10, 0, 30), at(10, 0, 50))


def judge(now: datetime, limits: RiskLimits, recent=(), last=None):  # type: ignore[no-untyped-def]
    ctx = replace(
        context(limits=limits, now=now, day=day_state(last_entry_at=last)),
        recent_entries=tuple(recent),
    )
    return EntryPacing().pre_trade(ctx, order(BUY, 65))


# ---- per minute ------------------------------------------------------------------------------


def test_fourth_entry_in_a_minute() -> None:
    """Spec "Fourth entry in a minute": a maximum of 3, entries at 10:00:05, :30 and :50, and an
    entry at 10:00:58."""
    decision = judge(at(10, 0, 58), pacing(per_minute=3), THREE)

    assert decision.severity is Severity.REJECT and decision.code == ENTRY_RATE
    assert decision.inputs == {"entries_last_minute": 3, "limit": 3}


def test_window_has_moved_on() -> None:
    """Spec "Window has moved on": the same three entries and an entry at 10:01:06."""
    assert judge(at(10, 1, 6), pacing(per_minute=3), THREE).code is None


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (at(10, 1, 4), False),  # the first entry is 59 s old
        (at(10, 1, 5), False),  # exactly 60 s old: still inside the window
        (at(10, 1, 6), True),  # 61 s old: out
    ],
)
def test_the_window_edge_is_exactly_60_seconds(now: datetime, allowed: bool) -> None:
    decision = judge(now, pacing(per_minute=3), THREE)

    assert (decision.code is None) is allowed
    if not allowed:
        assert decision.code == ENTRY_RATE


def test_one_below_the_maximum_is_allowed() -> None:
    assert judge(at(10, 0, 58), pacing(per_minute=4), THREE).code is None


def test_times_older_than_the_window_are_ignored_even_if_passed() -> None:
    old = (at(9, 58, 0), at(9, 59, 0), at(9, 59, 30))

    assert judge(at(10, 0, 58), pacing(per_minute=1), old).code is None


# ---- minimum gap -----------------------------------------------------------------------------


def test_too_soon_after_the_last_entry() -> None:
    """Spec "Too soon after the last entry": a gap of 20 s, an entry at 10:00:00, and an entry at
    10:00:12."""
    decision = judge(at(10, 0, 12), pacing(gap_s=20), last=at(10, 0, 0))

    assert decision.severity is Severity.REJECT and decision.code == ENTRY_GAP
    assert decision.inputs["seconds_since"] == "12" and decision.inputs["limit"] == 20


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (at(10, 0, 19), False),
        (at(10, 0, 20), True),  # exactly the gap: "fewer than" 20 s have not passed
        (at(10, 0, 21), True),
    ],
)
def test_the_gap_boundary(now: datetime, allowed: bool) -> None:
    decision = judge(now, pacing(gap_s=20), last=at(10, 0, 0))

    assert (decision.code is None) is allowed


def test_a_part_second_short_of_the_gap_is_refused() -> None:
    decision = judge(at(10, 0, 20) - timedelta(milliseconds=1), pacing(gap_s=20), last=at(10, 0, 0))

    assert decision.code == ENTRY_GAP


def test_no_entry_yet_today_means_no_gap_to_keep() -> None:
    assert judge(at(10, 0, 0), pacing(gap_s=20), last=None).code is None


def test_the_per_minute_cap_is_named_before_the_gap() -> None:
    decision = judge(at(10, 0, 51), pacing(per_minute=3, gap_s=20), THREE, last=at(10, 0, 50))

    assert decision.code == ENTRY_RATE


# ---- exits -----------------------------------------------------------------------------------


async def test_an_exit_right_after_an_entry() -> None:
    """Spec "An exit right after an entry": 2 s after an entry was allowed, and with the minute's
    entries used up, the exit is not refused by pacing and is not counted."""
    now = at(10, 30, 2)
    state = StubState(day_state(last_entry_at=now - timedelta(seconds=2)))
    state.recent = (now - timedelta(seconds=2),)
    h = await harness(
        checks=[EntryPacing()],
        limits_values={**BASE, "max_entries_per_minute": 1, "min_entry_gap_s": 20},
        positions=[position(quantity=65)], state=state, now=now,
    )  # fmt: skip

    assert await h.gate.check(order(SELL, 65)) is None
    assert state.counted == [] and h.rows("entry_pacing") == []
    entry = await h.gate.check(order(BUY, 65))  # an entry at the same moment is refused
    assert entry is not None and entry.startswith(ENTRY_RATE)


def test_entry_pacing_is_a_registered_entry_rule() -> None:
    assert EntryPacing().applies_to_exits is False and EntryPacing().name == "entry_pacing"
    assert any(isinstance(c, EntryPacing) for c in default_checks())
