"""The real gate with the real registered rules, end to end (add-risk-engine group 6).

Each rule is tested on its own elsewhere; this checks they work together through ``RiskEngine``:
the first registered rule names the reason, "Why didn't it trade" is answerable from the audit,
shadow mode lets a rule observe without binding, and exits are judged by price sanity alone.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from contracts.commands import LimitValue
from contracts.orders import Order, OrderSide, OrderType
from engine.risk.checks.registry import default_checks
from engine.risk.decision import Quote
from engine.risk.limits import MODE_PREFIX
from tests.engine.risk_builders import OPTION, day_state, order, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_risk_gate import Harness
from tests.engine.test_risk_limits import BASE

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def entry(stop: str | None = "190.00", price: str = "200.00", qty: int = 65) -> Order:
    return order(
        BUY, qty, order_type=OrderType.LIMIT, limit_price=D(price),
        initial_stop=None if stop is None else D(stop),
    )  # fmt: skip


async def pipeline(
    *,
    limits: dict[str, LimitValue] | None = None,
    age_s: float = 1,
    positions: list[Any] | None = None,
) -> Harness:
    state = StubState(day_state(), quotes={OPTION: Quote(D("200.00"), timedelta(seconds=age_s))})
    return await Harness(
        checks=default_checks(),  # type: ignore[arg-type]
        limits_values=limits or BASE,
        state=state,
        positions=positions,
    ).ready()


async def test_a_sound_entry_passes_every_rule_and_is_counted() -> None:
    h = await pipeline()

    assert await h.gate.check(entry()) is None

    assert h.state.entries_counted == 1  # type: ignore[attr-defined]
    assert h.verdict().decision == "ALLOW"


async def test_the_first_registered_rule_names_the_reason() -> None:
    """No stop and a stale price: both rules refuse, the stop rule is registered first."""
    h = await pipeline(age_s=30)

    reason = await h.gate.check(entry(stop=None))

    assert (reason or "").startswith("RISK_NO_STOP")
    assert {r.rule for r in h.rows() if r.rule != "gate"} >= {"initial_stop", "stale_data"}


async def test_why_didnt_it_trade() -> None:
    """Spec "Why didn't it trade": rejected for the per-trade risk, and the audit has the rule,
    the code, the computed risk and the limit."""
    h = await pipeline()

    reason = await h.gate.check(
        entry(stop="180.00")
    )  # 65 x 20.00 = 1,300.00 against a limit of 1,000

    assert (reason or "").startswith("RISK_PER_TRADE")
    [row] = h.rows("per_trade_risk")
    assert row.reason_code == "RISK_PER_TRADE" and row.enforced
    assert row.inputs["risk"] == "1300.00" and row.inputs["limit"] == "1000"
    assert h.state.entries_counted == 0  # type: ignore[attr-defined]


async def test_a_stale_quote_refuses_the_entry() -> None:
    h = await pipeline(age_s=8)

    assert (await h.gate.check(entry()) or "").startswith("RISK_STALE_QUOTE")


async def test_the_cutoff_refuses_the_entry() -> None:
    h = await pipeline(limits={**BASE, "entry_cutoff": "10:00"})  # the harness clock is 10:30 IST

    assert (await h.gate.check(entry()) or "").startswith("RISK_ENTRY_CUTOFF")


async def test_a_shadow_rule_observes_without_binding() -> None:
    h = await pipeline(limits={**BASE, MODE_PREFIX + "per_trade_risk": "shadow"})

    assert await h.gate.check(entry(stop="180.00")) is None  # would be refused if enforced

    [row] = h.rows("per_trade_risk")
    assert row.reason_code == "RISK_PER_TRADE" and row.enforced is False


async def test_an_exit_needs_no_stop_and_ignores_every_entry_rule() -> None:
    h = await pipeline(
        limits={**BASE, "entry_cutoff": "10:00"}, positions=[position(quantity=65)]
    )  # past the cutoff

    exit_ = order(SELL, 65, order_type=OrderType.LIMIT, limit_price=D("199.00"))

    assert await h.gate.check(exit_) is None
    assert h.state.entries_counted == 0  # type: ignore[attr-defined]


async def test_an_exit_is_still_judged_by_price_sanity() -> None:
    h = await pipeline(positions=[position(quantity=65)])

    fat_finger = order(SELL, 65, order_type=OrderType.LIMIT, limit_price=D("100.00"))  # 50% off

    assert (await h.gate.check(fat_finger) or "").startswith("RISK_PRICE_SANITY")


async def test_an_exit_with_no_fresh_price_goes_through_with_the_skipped_check_audited() -> None:
    h = await pipeline(age_s=30, positions=[position(quantity=65)])

    exit_ = order(SELL, 65, order_type=OrderType.LIMIT, limit_price=D("100.00"))

    assert await h.gate.check(exit_) is None
    [row] = h.rows("price_sanity")
    assert row.decision == "ALLOW" and row.reason_code == "RISK_PRICE_UNVERIFIED"
