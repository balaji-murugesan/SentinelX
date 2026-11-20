"""Price sanity and stale quotes (add-risk-engine task 6.2; risk-rules "Price sanity" and
stale-data-guard "Stale quotes block entries").

Scenarios: "Fat finger", "Exit without a fresh price" and "Old quote", plus boundaries and the
gate-level behaviour of an exit with a notice.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.orders import Order, OrderSide, OrderType
from engine.risk.checks.prices import (
    PRICE_SANITY,
    PRICE_UNVERIFIED,
    STALE_QUOTE,
    PriceSanity,
    StaleQuote,
)
from engine.risk.decision import Severity
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import LIMITS, OPTION, STOCK, context, order

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def limit(side: OrderSide, price: str, **more: object) -> Order:
    return order(side, 65, order_type=OrderType.LIMIT, limit_price=D(price), **more)


def band(pct: str) -> RiskLimits:
    return replace(LIMITS, price_band_pct=D(pct), exit_buffer_pct=D("1"))


# ---- PriceSanity -----------------------------------------------------------------------------


def test_fat_finger() -> None:
    """Spec "Fat finger": band 2%, a buy limit 5% above the latest price."""
    ctx = context(limits=band("2"), prices={OPTION: "100"})

    decision = PriceSanity().pre_trade(ctx, limit(BUY, "105.00"))

    assert decision.severity is Severity.REJECT and decision.code == PRICE_SANITY
    assert decision.inputs["latest"] == "100" and decision.inputs["band_pct"] == "2"


@pytest.mark.parametrize(
    ("price", "allowed"),
    [
        ("102.00", True),  # exactly 2% above: not "more than"
        ("102.05", False),
        ("98.00", True),  # exactly 2% below
        ("97.95", False),
        ("100.00", True),
    ],
)
def test_the_band_is_strict_and_runs_both_ways(price: str, allowed: bool) -> None:
    ctx = context(limits=band("2"), prices={OPTION: "100"})

    assert (PriceSanity().pre_trade(ctx, limit(BUY, price)).severity is Severity.ALLOW) is allowed
    assert (PriceSanity().pre_trade(ctx, limit(SELL, price)).severity is Severity.ALLOW) is allowed


def test_price_sanity_judges_an_exit_too() -> None:
    assert PriceSanity.applies_to_exits is True
    ctx = context(limits=band("2"), prices={OPTION: "100"})

    assert PriceSanity().pre_trade(ctx, limit(SELL, "150.00")).code == PRICE_SANITY


def test_a_market_or_stop_order_has_no_price_to_check() -> None:
    ctx = context(limits=band("2"), prices={OPTION: "100"})

    assert PriceSanity().pre_trade(ctx, order(BUY, 65)).code is None
    stop = order(BUY, 65, order_type=OrderType.STOP_MARKET, trigger_price=D("500"))
    assert PriceSanity().pre_trade(ctx, stop).code is None


def test_exit_without_a_fresh_price_is_allowed_with_a_notice() -> None:
    """Spec "Exit without a fresh price": accepted, and the skipped check is audited (the gate
    audits any decision that carries a code)."""
    no_tick = PriceSanity().pre_trade(context(limits=band("2")), limit(SELL, "150.00"))
    old_tick = PriceSanity().pre_trade(
        context(limits=band("2"), prices={OPTION: "100"}, age_s=30), limit(SELL, "150.00")
    )

    for decision in (no_tick, old_tick):
        assert decision.severity is Severity.ALLOW and decision.code == PRICE_UNVERIFIED
    assert no_tick.inputs["has_quote"] is False and old_tick.inputs["has_quote"] is True


def test_a_quote_exactly_at_the_stale_threshold_is_still_fresh() -> None:
    ctx = context(limits=band("2"), prices={OPTION: "100"}, age_s=LIMITS.stale_s)

    assert PriceSanity().pre_trade(ctx, limit(BUY, "150.00")).code == PRICE_SANITY


# ---- StaleQuote ------------------------------------------------------------------------------


def test_old_quote() -> None:
    """Spec "Old quote": threshold 5 s, last tick 8 s old."""
    ctx = context(prices={OPTION: "100"}, age_s=8)

    decision = StaleQuote().pre_trade(ctx, order(BUY, 65))

    assert decision.severity is Severity.REJECT and decision.code == STALE_QUOTE
    assert decision.inputs["age_s"] == 8 and decision.inputs["threshold_s"] == 5


def test_an_instrument_with_no_tick_is_stale() -> None:
    decision = StaleQuote().pre_trade(context(), order(BUY, 65))

    assert decision.code == STALE_QUOTE and "no tick" in decision.detail


@pytest.mark.parametrize(("age_s", "allowed"), [(0, True), (5, True), (5.01, False), (8, False)])
def test_the_threshold_is_strict(age_s: float, allowed: bool) -> None:
    ctx = context(prices={OPTION: "100"}, age_s=age_s)

    assert (StaleQuote().pre_trade(ctx, order(BUY, 65)).severity is Severity.ALLOW) is allowed


def test_a_fresh_quote_in_another_instrument_does_not_help() -> None:
    ctx = context(prices={STOCK: "100"})

    assert StaleQuote().pre_trade(ctx, order(BUY, 65, key=OPTION)).code == STALE_QUOTE


def test_stale_quotes_are_an_entry_rule() -> None:
    assert StaleQuote.applies_to_exits is False
