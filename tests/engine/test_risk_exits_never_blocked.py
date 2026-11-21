"""Exits are never blocked by entry rules (add-risk-engine task 12.2, extended by add-risk-guards
task 10.1; risk-gate spec "Exits are never blocked"; safety rule 9).

A property test over the REAL gate with EVERY registered rule: whatever the day looks like (any halt
level, counters at or over their limits, cooldown running, MTM below the floor or unknown, a silent
feed, the state unavailable, limits tight or missing or never set), an exit that is priced sensibly
is accepted. The only rule that applies to exits is price sanity, so the exit here is priced at the
last price. The same run records which rules were asked about the exit, so a rule that began to
apply to exits would be caught even if it happened to allow them.

add-risk-guards widened the drawn day to every input its rules read, so each new rule would refuse
the exit if it were ever asked: the day's gross lots, the last entry time and the expiry flag; the
entries of the last minute; the broker rejections of the last five minutes; the ban list missing,
empty or banning the exit's own underlying; a contract that expires today; and limits with every
optional guard set as tight as it goes. The standing halt the gate sees is no longer drawn from a
fixed list: every check in the REAL continuous registry (``continuous_checks()``) is run on the
drawn account, and the most severe decision is what the monitor would hold (and a TARGET_HALT or
KILL also locks the day, as ``RiskMonitor._act`` does). Both registries are iterated, never listed,
so a rule added later is covered without touching this file.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderType
from contracts.snapshot import HaltLevel
from engine.positions.book import position_id
from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.decision import (
    Decision,
    Quote,
    RiskContext,
    RiskTrigger,
    Severity,
    halt_new_entries,
    most_severe,
    run_continuous,
)
from engine.risk.limits import parse_limits
from tests.engine.risk_builders import NOW, OPTION, TODAY, day_state, instrument_of, order, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_risk_gate import Harness
from tests.engine.test_risk_limits import BASE

D = Decimal
TIGHT = {
    **BASE, "per_trade_max_risk": "1", "daily_max_loss": "1", "max_trades_per_day": 1,
    "max_open_total": 1, "max_open_per_underlying": 1, "loss_streak_limit": 1,
    "entry_cutoff": "09:16", "square_off_time": "09:17",
}  # fmt: skip
# Every add-risk-guards limit as tight as it goes (test values, not a recommendation): one lot, one
# entry a minute an hour apart, a breaker that trips on one rejection, a target of one rupee
# confirmed at once, the exit's own underlying (NIFTY) denied and not allowed, a minimum premium
# far above the exit's price, a strike distance no option meets, and expiry cut-offs before 09:16.
GUARDED = {
    **TIGHT,
    "max_open_lots_per_underlying": 1, "max_open_lots_total": 1, "max_gross_lots_per_day": 1,
    "max_entries_per_minute": 1, "reject_breaker_n": 1, "min_entry_gap_s": 3600,
    "daily_profit_target": "1", "target_confirm_ticks": 1,
    "min_premium": "1000", "max_strike_distance_pct": "0.01",
    "allowed_underlyings": ["BANKNIFTY"], "denied_underlyings": ["NIFTY"],
    "entry_cutoff_expiry": "09:15", "square_off_time_expiry": "09:16",
    "max_open_lots_per_underlying_expiry": 1, "max_open_lots_total_expiry": 1,
    "min_premium_expiry": "2000",
}  # fmt: skip
LAST = D("100.00")
# The exit's instrument as it is on its expiry day (the same NIFTY option, expiring today).
EXPIRING = instrument_of(OPTION).model_copy(update={"expiry": TODAY})  # type: ignore[union-attr]


def instruments_for(expiring: bool) -> Any:
    def lookup(key: InstrumentKey) -> Instrument | None:
        return EXPIRING if expiring and key == OPTION else instrument_of(key)

    return lookup


class Spy:
    """Wraps a real rule, delegates everything, and records the orders it was asked about."""

    def __init__(self, inner: Any, asked: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._asked = asked
        self.name = inner.name
        self.applies_to_exits = inner.applies_to_exits

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        self._asked.append((self.name, order.order_id))
        decision: Decision = self._inner.pre_trade(ctx, order)
        return decision


halt_levels = st.sampled_from(list(HaltLevel))  # TARGET_HALT included
recent_times = st.lists(st.integers(0, 3600).map(lambda s: NOW - timedelta(seconds=s)), max_size=4)
day_states = st.builds(
    lambda level, entries, streak, cooldown, peak, floor, lots, last, expiry: day_state(
        halt_level=level,
        halt_reason=None if level is HaltLevel.NONE else "locked",
        entries_today=entries,
        loss_streak=streak,
        cooldown_until=None if cooldown is None else NOW + timedelta(seconds=cooldown),
        peak=peak,
        floor=floor,
        entry_lots_today=lots,
        last_entry_at=last,
        expiry_exposure=expiry,
    ),  # fmt: skip
    halt_levels,
    st.integers(0, 200),
    st.integers(0, 20),
    st.one_of(st.none(), st.integers(1, 100_000)),
    st.decimals(0, 1_000_000, places=2),
    st.one_of(st.none(), st.decimals(-1_000_000, 1_000_000, places=2)),
    st.integers(0, 200),
    st.one_of(st.none(), st.integers(0, 3600).map(lambda s: NOW - timedelta(seconds=s))),
    st.booleans(),
)
mtms = st.one_of(st.none(), st.decimals(-10_000_000, 10_000_000, places=2))
limits_choices = st.sampled_from([None, BASE, TIGHT, GUARDED])  # None: the owner never set any
unavailable = st.one_of(st.none(), st.just("the day's counters cannot be trusted"))
quotes = st.one_of(st.none(), st.integers(0, 3600))  # None: no price at all; else its age in s
# None: not loaded (F&O entries fail closed); empty: no bans; {"NIFTY"}: the exit's own underlying.
ban_lists = st.sampled_from([None, frozenset(), frozenset({"NIFTY"})])


@st.composite
def accounts(draw: st.DrawFn) -> dict[str, Any]:
    """What the continuous checks read about the account, beyond the day and MTM."""
    return {
        "feed_silent_for": draw(st.one_of(st.none(), st.integers(0, 3600))),
        "feed_subscribed": draw(st.booleans()),
        "unpriced_for": draw(st.one_of(st.none(), st.integers(0, 3600))),
        "unprotected": draw(st.booleans()),
        "target_confirmations": draw(st.integers(0, 5)),
    }


def continuous_halt(
    limits: Any, day: Any, mtm: Decimal | None, account: dict[str, Any], rejections: Any,
    bans: frozenset[str] | None, expiring: bool, quote: Quote | None,
) -> tuple[Decision | None, Any]:  # fmt: skip
    """Run every registered continuous check on the drawn account and return what the monitor would
    hold as the standing halt, with the day as the monitor would leave it (a TARGET_HALT or a KILL
    locks it at that level; the levels only rise). No limits: the monitor judges nothing."""
    if limits is None:
        return None, day
    held = position(OPTION, 65, "100.00")
    ctx = RiskContext(
        account_id="main", now=NOW, limits=parse_limits(limits), day=day, mtm_day=mtm,
        positions={position_id(held): held}, orders={}, instruments=instruments_for(expiring),
        quote=lambda key: quote if key == OPTION else None,
        feed_silent_for=None if account["feed_silent_for"] is None
        else timedelta(seconds=account["feed_silent_for"]),
        feed_subscribed=account["feed_subscribed"],
        unpriced_for={} if account["unpriced_for"] is None
        else {OPTION: timedelta(seconds=account["unpriced_for"])},
        unprotected=frozenset({position_id(held)}) if account["unprotected"] else frozenset(),
        broker_rejections=tuple(rejections),
        target_confirmations=account["target_confirmations"],
        ban_list=bans,
    )  # fmt: skip
    winner = most_severe(run_continuous(continuous_checks(), ctx, RiskTrigger.TIMER))
    if winner is None:
        return None, day
    locks = {Severity.TARGET_HALT: HaltLevel.TARGET_HALT, Severity.KILL: HaltLevel.KILL}
    level = locks.get(winner.effective)
    if level is not None and level > day.halt_level:
        day = replace(day, halt_level=level, halt_reason=winner.decision.code)
    return winner.decision, day


async def exit_is_accepted(
    day: Any, mtm: Decimal | None, limits: Any, halt: Decision | None, why: str | None,
    quote_age: int | None, spy_log: list[tuple[str, str]], *, recent: Any = (),
    rejections: Any = (), bans: frozenset[str] | None = frozenset(), expiring: bool = False,
) -> str | None:  # fmt: skip
    quote = None if quote_age is None else Quote(LAST, timedelta(seconds=quote_age))
    state = StubState(
        day,
        mtm_day=mtm,
        unavailable=why,
        entry_halt=halt,
        quotes={} if quote is None else {OPTION: quote},
    )
    state.recent, state.rejections, state.bans = tuple(recent), tuple(rejections), bans
    held = position(OPTION, 65, "100.00")
    checks = [Spy(c, spy_log) for c in default_checks()]
    harness = await Harness(
        limits_values=limits,
        positions=[held],
        state=state,
        checks=checks,  # type: ignore[arg-type]
        instruments=instruments_for(expiring),
    ).ready()
    exit_ = order(
        OrderSide.SELL, 65, order_type=OrderType.LIMIT, limit_price=LAST, order_id="exit-1"
    )
    assert position_id(held) in harness.trading.positions
    return await harness.gate.check(exit_)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    day=day_states, mtm=mtms, limits=limits_choices, why=unavailable, quote_age=quotes,
    account=accounts(), recent=recent_times, rejections=recent_times, bans=ban_lists,
    expiring=st.booleans(), fixed_halt=st.booleans(),
)  # fmt: skip
def test_an_exit_is_never_refused_by_an_entry_rule_whatever_the_day_looks_like(
    day: Any, mtm: Decimal | None, limits: Any, why: str | None, quote_age: int | None,
    account: dict[str, Any], recent: list[Any], rejections: list[Any],
    bans: frozenset[str] | None, expiring: bool, fixed_halt: bool,
) -> None:  # fmt: skip
    asked: list[tuple[str, str]] = []
    quote = None if quote_age is None else Quote(LAST, timedelta(seconds=quote_age))
    halt, day = continuous_halt(limits, day, mtm, account, rejections, bans, expiring, quote)
    if halt is None and fixed_halt:  # the standing halt the gate tests have always used
        halt = halt_new_entries("RISK_FEED_DOWN", "no tick")

    refusal = asyncio.run(
        exit_is_accepted(
            day,
            mtm,
            limits,
            halt,
            why,
            quote_age,
            asked,
            recent=recent,
            rejections=rejections,
            bans=bans,
            expiring=expiring,
        )  # fmt: skip
    )

    assert refusal is None, refusal
    # Only the rules that declare they apply to exits were asked about it.
    applying = {c.name for c in default_checks() if c.applies_to_exits}
    assert {name for name, order_id in asked if order_id == "exit-1"} <= applying


def test_the_only_rule_that_applies_to_exits_is_price_sanity() -> None:
    """Pins the premise of the property above: adding a second rule that applies to exits is a
    decision, and this fails until it is made on purpose."""
    assert [c.name for c in default_checks() if c.applies_to_exits] == ["price_sanity"]


def test_the_same_day_refuses_an_entry_so_the_property_is_not_vacuous() -> None:
    """A locked day with tight limits refuses an entry for several reasons. If the harness let
    everything through the property above would prove nothing."""
    entry = order(OrderSide.BUY, 65, order_type=OrderType.LIMIT, limit_price=LAST)

    async def attempt() -> str | None:
        state = StubState(
            day_state(halt_level=HaltLevel.KILL, halt_reason="locked", entries_today=9),
            quotes={OPTION: Quote(LAST, timedelta(seconds=1))},
        )
        harness = await Harness(
            limits_values=TIGHT,
            positions=[],
            state=state,
            checks=list(default_checks()),  # type: ignore[arg-type]
        ).ready()
        return await harness.gate.check(entry)

    assert (asyncio.run(attempt()) or "").startswith("RISK_HALTED_DAY")


def test_each_new_entry_rule_refuses_an_entry_on_a_day_the_property_draws() -> None:
    """Non-vacuity for the add-risk-guards draws: on one day the property can draw (an open day, so
    the rules run), each new pre-trade rule, asked about an ENTRY in the exit's instrument,
    refuses it. So if one of them were asked about the exit on such a day it would refuse it, and
    the property would fail."""
    parse_limits(GUARDED)  # the tight guard limits are a valid set, so the harness loads them
    held = position(OPTION, 65, "100.00")
    entry = order(OrderSide.BUY, 65, order_type=OrderType.LIMIT, limit_price=LAST)
    ctx = RiskContext(
        account_id="main", now=NOW, limits=parse_limits(GUARDED),
        day=day_state(entry_lots_today=1, last_entry_at=NOW - timedelta(seconds=10)),
        mtm_day=D(0), positions={position_id(held): held}, orders={},
        instruments=instruments_for(False), quote=lambda key: Quote(LAST, timedelta(seconds=1)),
        recent_entries=(NOW - timedelta(seconds=10),), broker_rejections=(NOW,), ban_list=None,
    )  # fmt: skip
    new_rules = {"lot_caps", "entry_pacing", "reject_breaker", "instrument_policy"}
    decisions = {c.name: c.pre_trade(ctx, entry) for c in default_checks() if c.name in new_rules}

    assert set(decisions) == new_rules
    refused = {name for name, d in decisions.items() if d.severity >= Severity.REJECT}
    assert refused == new_rules, {name: d.code for name, d in decisions.items()}


def test_every_continuous_check_fires_on_an_account_the_property_draws() -> None:
    """Non-vacuity for the continuous registry: for every registered continuous check there is an
    account in the property's range on which it decides something other than ALLOW, so the
    standing halts the property feeds the gate are real ones. Iterates the registry: a check added
    later must be reachable from these draws too (widen ``accounts`` if it is not)."""
    held = position(OPTION, 65, "100.00")
    hostile = [
        # The floor breached, the feed silent, the book blind, a position unprotected, the breaker
        # tripped: an MTM far below the floor in market hours with no tick for an hour.
        {"mtm": D("-1000000"), "confirmations": 0},
        # The profit target confirmed: MTM far above a one-rupee target.
        {"mtm": D("1000000"), "confirmations": 5},
    ]
    fired: set[str] = set()
    for case in hostile:
        ctx = RiskContext(
            account_id="main", now=NOW, limits=parse_limits(GUARDED),
            day=day_state(floor=D("-1")), mtm_day=case["mtm"],
            positions={position_id(held): held}, orders={}, instruments=instruments_for(False),
            quote=lambda key: None, feed_silent_for=timedelta(seconds=3600),
            feed_subscribed=True, unpriced_for={OPTION: timedelta(seconds=3600)},
            unprotected=frozenset({position_id(held)}), broker_rejections=(NOW,),
            target_confirmations=case["confirmations"], ban_list=None,
        )  # fmt: skip
        for check in continuous_checks():
            if check.continuous(ctx, RiskTrigger.TIMER).severity > Severity.ALLOW:
                fired.add(type(check).__name__)

    assert fired == {type(c).__name__ for c in continuous_checks()}
