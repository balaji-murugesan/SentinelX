"""Entry pacing (add-risk-guards task 5.2; order-flow-guards spec "Entries per rolling minute are
capped" and "Entries keep a minimum gap") and the rejection breaker (task 6.2; "Broker rejections
trip a breaker").

Scenarios: "Fourth entry in a minute", "Window has moved on", "Too soon after the last entry" and
"An exit right after an entry", with the window edge at exactly 60 s and the gap boundary. "Gap
survives a restart" is an end-to-end test (test_risk_end_to_end.py), because it needs the day
book's persisted ``last_entry_at``. Breaker: "Margin rejection storm", "Breaker clears" (with the
window edge at exactly 300 s) and "Exit while tripped"; its once-per-trip critical log is a monitor
test (test_risk_monitor.py), and the broker-only counting is in test_order_service_rejections.py.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.orders import Order, OrderSide
from engine.risk.checks.flow import (
    ENTRY_GAP,
    ENTRY_RATE,
    REJECT_BREAKER,
    EntryPacing,
    RejectBreaker,
)
from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.decision import ALLOW, Decision, RiskContext, RiskTrigger, Severity
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


# ---- the rejection breaker (task 6.2; "Broker rejections trip a breaker") ----------------------


def breaker(n: int = 3) -> RiskLimits:
    return replace(LIMITS, reject_breaker_n=n)


STORM = (at(10, 0, 0), at(10, 1, 0), at(10, 2, 0))  # three rejections within 2 minutes


def tripped(now: datetime, limits: RiskLimits, rejections=()):  # type: ignore[no-untyped-def]
    """The breaker's pre-trade and continuous decisions, which must always agree."""
    ctx = replace(context(limits=limits, now=now), broker_rejections=tuple(rejections))
    before = RejectBreaker().pre_trade(ctx, order(BUY, 65))
    during = RejectBreaker().continuous(ctx, RiskTrigger.TIMER)
    assert before == during
    return before


def test_margin_rejection_storm() -> None:
    """Spec "Margin rejection storm": ``reject_breaker_n`` = 3, three broker rejections within 2
    minutes, then an entry: refused with ``RISK_REJECT_BREAKER``, a halt of new entries."""
    decision = tripped(at(10, 2, 5), breaker(3), STORM)

    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == REJECT_BREAKER
    assert decision.inputs == {"rejections_5m": 3, "limit": 3}


def test_one_rejection_short_of_the_threshold_is_allowed() -> None:
    assert tripped(at(10, 2, 5), breaker(4), STORM).code is None
    assert tripped(at(10, 2, 5), breaker(3), STORM[1:]).code is None


def test_breaker_clears() -> None:
    """Spec "Breaker clears": tripped on rejections at 10:00, 10:01 and 10:02; an entry at 10:05:30
    is not refused (the 10:00 rejection is 330 s old, two remain)."""
    assert tripped(at(10, 5, 30), breaker(3), STORM).code is None


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (at(10, 4, 59), False),  # the first rejection is 299 s old
        (at(10, 5, 0), False),  # exactly 300 s old: still inside the window (the tight side)
        (at(10, 5, 0) + timedelta(microseconds=1), True),  # a moment older: out, two remain
    ],
)
def test_the_window_edge_is_exactly_300_seconds(now: datetime, allowed: bool) -> None:
    decision = tripped(now, breaker(3), STORM)

    assert (decision.code is None) is allowed
    if not allowed:
        assert decision.code == REJECT_BREAKER


async def test_exit_while_tripped() -> None:
    """Spec "Exit while tripped": through the real gate, the tripped breaker refuses an entry and
    does not refuse the exit."""
    now = at(10, 2, 5)
    state = StubState(day_state())
    state.rejections = STORM
    h = await harness(
        checks=[RejectBreaker()], limits_values={**BASE, "reject_breaker_n": 3},
        positions=[position(quantity=65)], state=state, now=now,
    )  # fmt: skip

    assert await h.gate.check(order(SELL, 65)) is None
    assert h.rows("reject_breaker") == []  # the breaker was not even asked about the exit
    entry = await h.gate.check(order(BUY, 65))
    assert entry is not None and entry.startswith(REJECT_BREAKER)
    assert h.rows("reject_breaker")[-1].reason_code == REJECT_BREAKER


async def test_the_gate_gives_the_rules_the_broker_rejections() -> None:
    state = StubState(day_state())
    state.rejections = STORM
    seen: list[RiskContext] = []

    class Seeing:
        name, applies_to_exits = "max_trades", False

        def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
            seen.append(ctx)
            return ALLOW

    h = await harness(checks=[Seeing()], state=state, now=at(10, 2, 5))
    await h.gate.check(order(BUY, 65))

    assert seen[0].broker_rejections == STORM


def test_the_breaker_is_registered_in_both_phases_and_never_judges_exits() -> None:
    assert RejectBreaker().applies_to_exits is False and RejectBreaker().name == "reject_breaker"
    assert [type(c) for c in default_checks()].count(RejectBreaker) == 1
    assert [type(c) for c in continuous_checks()].count(RejectBreaker) == 1
