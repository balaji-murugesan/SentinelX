"""The stop rules (add-risk-engine task 6.1; risk-rules spec).

Scenarios: "Missing stop", "Stop on the wrong side", "Risk above the limit", "Risk within the
limit" and "Stop wider than the backstop", plus the edges: sells, the tick grid, the exact
boundaries, market orders priced from the latest quote, and no price at all.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.orders import Order, OrderSide, OrderType
from engine.risk.checks.stops import (
    NO_STOP,
    PER_TRADE,
    STOP_BEYOND_CATASTROPHIC,
    InitialStop,
    PerTradeRisk,
    StopDistance,
)
from engine.risk.decision import RiskContext, Severity
from tests.engine.risk_builders import LIMITS, OPTION, context, order

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def limit_order(
    side: OrderSide, price: str, stop: str | None, quantity: int = 65, **more: object
) -> Order:
    return order(
        side, quantity, order_type=OrderType.LIMIT, limit_price=D(price),
        initial_stop=None if stop is None else D(stop), **more,
    )  # fmt: skip


# ---- InitialStop -----------------------------------------------------------------------------


def test_missing_stop() -> None:
    """Spec "Missing stop"."""
    decision = InitialStop().pre_trade(context(), limit_order(BUY, "200.00", None))

    assert decision.severity is Severity.REJECT and decision.code == NO_STOP


def test_stop_on_the_wrong_side() -> None:
    """Spec "Stop on the wrong side": a buy at 200.00 with a stop of 205.00."""
    decision = InitialStop().pre_trade(context(), limit_order(BUY, "200.00", "205.00"))

    assert decision.severity is Severity.REJECT and decision.code == NO_STOP
    assert decision.inputs["stop"] == "205.00" and decision.inputs["entry"] == "200.00"


@pytest.mark.parametrize(
    ("side", "stop", "allowed"),
    [
        (BUY, "190.00", True),
        (BUY, "199.95", True),
        (BUY, "200.00", False),  # a stop at the entry price is not on the loss side
        (BUY, "205.00", False),
        (SELL, "210.00", True),
        (SELL, "200.05", True),
        (SELL, "200.00", False),
        (SELL, "190.00", False),  # a short is wrong when the price rises
    ],
)
def test_the_stop_is_below_a_buy_and_above_a_sell(
    side: OrderSide, stop: str, allowed: bool
) -> None:
    decision = InitialStop().pre_trade(context(), limit_order(side, "200.00", stop))

    assert (decision.severity is Severity.ALLOW) is allowed


def test_the_stop_must_be_on_the_tick_grid() -> None:
    decision = InitialStop().pre_trade(context(), limit_order(BUY, "200.00", "190.03"))

    assert decision.code == NO_STOP and "tick grid" in decision.detail
    assert InitialStop().pre_trade(context(), limit_order(BUY, "200.00", "190.05")).code is None


def test_a_market_order_is_judged_against_the_latest_price() -> None:
    market = order(BUY, 65, initial_stop=D("205.00"))  # market order: the entry price is the quote
    fine = order(BUY, 65, initial_stop=D("190.00"))
    ctx = context(prices={OPTION: "200"})

    assert InitialStop().pre_trade(ctx, market).code == NO_STOP
    assert InitialStop().pre_trade(ctx, fine).code is None


def test_a_stop_market_entry_is_judged_against_its_trigger() -> None:
    stop_entry = order(
        BUY, 65, order_type=OrderType.STOP_MARKET, trigger_price=D("200.00"),
        initial_stop=D("190.00"),
    )  # fmt: skip

    assert InitialStop().pre_trade(context(), stop_entry).code is None


def test_with_no_price_to_compare_the_stop_rule_has_no_opinion_when_the_stop_is_present() -> None:
    """A market order with no latest price: the stale-data rule refuses it, not this one."""
    decision = InitialStop().pre_trade(context(), order(BUY, 65, initial_stop=D("190.00")))

    assert decision.severity is Severity.ALLOW


def test_an_unknown_instrument_fails_closed() -> None:
    from contracts.market import InstrumentKey

    unknown = InstrumentKey.parse("NSE_FNO:99999")
    decision = InitialStop().pre_trade(
        context(), order(BUY, 65, key=unknown, initial_stop=D("190.00"))
    )

    assert decision.code == NO_STOP and "unavailable" in decision.detail


# ---- PerTradeRisk ----------------------------------------------------------------------------


def test_risk_above_the_limit() -> None:
    """Spec "Risk above the limit": limit 1,000, 65 units at 200.00, stop 180.00: risk 1,300.00."""
    decision = PerTradeRisk().pre_trade(context(), limit_order(BUY, "200.00", "180.00"))

    assert decision.severity is Severity.REJECT and decision.code == PER_TRADE
    assert decision.inputs["risk"] == "1300.00" and decision.inputs["limit"] == "1000"
    assert decision.inputs["quantity"] == 65


def test_risk_within_the_limit() -> None:
    """Spec "Risk within the limit": stop 190.00 gives 650.00."""
    decision = PerTradeRisk().pre_trade(context(), limit_order(BUY, "200.00", "190.00"))

    assert decision.severity is Severity.ALLOW


def test_risk_exactly_at_the_limit_is_allowed() -> None:
    limits = replace(LIMITS, per_trade_max_risk=D("650.00"))

    assert (
        PerTradeRisk().pre_trade(context(limits=limits), limit_order(BUY, "200.00", "190.00")).code
        is None
    )
    assert (
        PerTradeRisk()
        .pre_trade(
            context(limits=replace(limits, per_trade_max_risk=D("649.95"))),
            limit_order(BUY, "200.00", "190.00"),
        )
        .code
        == PER_TRADE
    )


def test_a_short_entrys_risk_is_measured_upward() -> None:
    decision = PerTradeRisk().pre_trade(context(), limit_order(SELL, "200.00", "220.00"))

    assert decision.code == PER_TRADE and decision.inputs["risk"] == "1300.00"


def test_per_trade_risk_has_no_opinion_without_a_stop_or_a_price() -> None:
    assert PerTradeRisk().pre_trade(context(), limit_order(BUY, "200.00", None)).code is None
    assert PerTradeRisk().pre_trade(context(), order(BUY, 65, initial_stop=D("190"))).code is None


# ---- StopDistance ----------------------------------------------------------------------------


def backstop(pct: str) -> RiskContext:
    return context(limits=replace(LIMITS, catastrophic_sl_pct=D(pct), per_trade_max_risk=D(10**6)))


def test_stop_wider_than_the_backstop() -> None:
    """Spec "Stop wider than the backstop": catastrophic 20%, buy at 200.00, stop 150.00."""
    decision = StopDistance().pre_trade(backstop("20"), limit_order(BUY, "200.00", "150.00"))

    assert decision.severity is Severity.REJECT and decision.code == STOP_BEYOND_CATASTROPHIC


@pytest.mark.parametrize(
    ("stop", "allowed"),
    [
        ("160.05", True),  # 19.975%: inside
        ("160.00", False),  # exactly 20%: "as far as the catastrophic stop, or farther"
        ("159.95", False),
        ("150.00", False),
    ],
)
def test_the_stop_must_be_strictly_inside_the_catastrophic_distance(
    stop: str, allowed: bool
) -> None:
    decision = StopDistance().pre_trade(backstop("20"), limit_order(BUY, "200.00", stop))

    assert (decision.severity is Severity.ALLOW) is allowed


def test_the_backstop_distance_is_measured_the_same_way_for_a_short() -> None:
    inside = StopDistance().pre_trade(backstop("20"), limit_order(SELL, "200.00", "239.95"))
    outside = StopDistance().pre_trade(backstop("20"), limit_order(SELL, "200.00", "240.00"))

    assert inside.code is None and outside.code == STOP_BEYOND_CATASTROPHIC


def test_stop_distance_has_no_opinion_without_a_stop_or_a_price() -> None:
    assert StopDistance().pre_trade(context(), limit_order(BUY, "200.00", None)).code is None
    assert StopDistance().pre_trade(context(), order(BUY, 65, initial_stop=D("190"))).code is None


@pytest.mark.parametrize("check", [InitialStop(), PerTradeRisk(), StopDistance()])
def test_these_are_entry_rules(check: object) -> None:
    assert check.applies_to_exits is False  # type: ignore[attr-defined]
