"""Exits are never blocked by entry rules (add-risk-engine task 12.2; risk-gate spec "Exits are
never blocked"; safety rule 9).

A property test over the REAL gate with EVERY registered rule: whatever the day looks like (any halt
level, counters at or over their limits, cooldown running, MTM below the floor or unknown, a silent
feed, the state unavailable, limits tight or missing or never set), an exit that is priced sensibly
is accepted. The only rule that applies to exits is price sanity, so the exit here is priced at the
last price. The same run records which rules were asked about the exit, so a rule that began to
apply to exits would be caught even if it happened to allow them.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from contracts.orders import Order, OrderSide, OrderType
from contracts.snapshot import HaltLevel
from engine.positions.book import position_id
from engine.risk.checks.registry import default_checks
from engine.risk.decision import Decision, Quote, RiskContext, halt_new_entries
from tests.engine.risk_builders import NOW, OPTION, day_state, order, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_risk_gate import Harness
from tests.engine.test_risk_limits import BASE

D = Decimal
TIGHT = {
    **BASE, "per_trade_max_risk": "1", "daily_max_loss": "1", "max_trades_per_day": 1,
    "max_open_total": 1, "max_open_per_underlying": 1, "loss_streak_limit": 1,
    "entry_cutoff": "09:16", "square_off_time": "09:17",
}  # fmt: skip
LAST = D("100.00")


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


halt_levels = st.sampled_from([HaltLevel.NONE, HaltLevel.MTM_BLIND, HaltLevel.KILL])
day_states = st.builds(
    lambda level, entries, streak, cooldown, peak, floor: day_state(
        halt_level=level,
        halt_reason=None if level is HaltLevel.NONE else "locked",
        entries_today=entries,
        loss_streak=streak,
        cooldown_until=None if cooldown is None else NOW + timedelta(seconds=cooldown),
        peak=peak,
        floor=floor,
    ),  # fmt: skip
    halt_levels,
    st.integers(0, 200),
    st.integers(0, 20),
    st.one_of(st.none(), st.integers(1, 100_000)),
    st.decimals(0, 1_000_000, places=2),
    st.one_of(st.none(), st.decimals(-1_000_000, 1_000_000, places=2)),
)
mtms = st.one_of(st.none(), st.decimals(-10_000_000, 10_000_000, places=2))
limits_choices = st.sampled_from([None, BASE, TIGHT])  # None: the owner never set any
standing = st.one_of(st.none(), st.just(halt_new_entries("RISK_FEED_DOWN", "no tick")))
unavailable = st.one_of(st.none(), st.just("the day's counters cannot be trusted"))
quotes = st.one_of(st.none(), st.integers(0, 3600))  # None: no price at all; else its age in s


async def exit_is_accepted(
    day: Any, mtm: Decimal | None, limits: Any, halt: Decision | None, why: str | None,
    quote_age: int | None, spy_log: list[tuple[str, str]],
) -> str | None:  # fmt: skip
    state = StubState(
        day,
        mtm_day=mtm,
        unavailable=why,
        entry_halt=halt,
        quotes={} if quote_age is None else {OPTION: Quote(LAST, timedelta(seconds=quote_age))},
    )
    held = position(OPTION, 65, "100.00")
    checks = [Spy(c, spy_log) for c in default_checks()]
    harness = await Harness(
        limits_values=limits,
        positions=[held],
        state=state,
        checks=checks,  # type: ignore[arg-type]
    ).ready()
    exit_ = order(
        OrderSide.SELL, 65, order_type=OrderType.LIMIT, limit_price=LAST, order_id="exit-1"
    )
    assert position_id(held) in harness.trading.positions
    return await harness.gate.check(exit_)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    day=day_states, mtm=mtms, limits=limits_choices, halt=standing, why=unavailable,
    quote_age=quotes,
)  # fmt: skip
def test_an_exit_is_never_refused_by_an_entry_rule_whatever_the_day_looks_like(
    day: Any, mtm: Decimal | None, limits: Any, halt: Decision | None, why: str | None,
    quote_age: int | None,
) -> None:  # fmt: skip
    asked: list[tuple[str, str]] = []

    refusal = asyncio.run(exit_is_accepted(day, mtm, limits, halt, why, quote_age, asked))

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
