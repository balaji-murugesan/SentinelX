"""Entry or exit by position effect (add-risk-engine task 5.1; risk-gate spec).

Scenarios "Plain exit" and "Flip is an entry", plus the edges the rule depends on: product
separation, shorts, flat or missing positions, and other instruments and accounts.
"""

import pytest

from contracts.orders import OrderSide, Position, ProductType
from engine.orders.intent import Intent, classify
from engine.positions.book import PositionId, position_id
from tests.engine.risk_builders import OPTION, STOCK, order, position

BUY, SELL = OrderSide.BUY, OrderSide.SELL


def book(*held: Position) -> dict[PositionId, Position]:
    return {position_id(p): p for p in held}


def test_plain_exit() -> None:
    """Spec "Plain exit": long 65, a sell of 65."""
    positions = book(position(quantity=65))

    assert classify(order(SELL, 65), positions) is Intent.EXIT


def test_flip_is_an_entry() -> None:
    """Spec "Flip is an entry": long 65, a sell of 130 (65 closes, 65 would open a short)."""
    positions = book(position(quantity=65))

    assert classify(order(SELL, 130), positions) is Intent.ENTRY


@pytest.mark.parametrize(
    ("held", "side", "quantity", "expected"),
    [
        (65, SELL, 1, Intent.EXIT),  # a partial exit
        (65, SELL, 64, Intent.EXIT),
        (65, SELL, 65, Intent.EXIT),  # exactly the position
        (65, SELL, 66, Intent.ENTRY),  # one unit past it flips
        (65, BUY, 65, Intent.ENTRY),  # same side grows the position
        (-65, BUY, 1, Intent.EXIT),  # a short is closed by buying
        (-65, BUY, 65, Intent.EXIT),
        (-65, BUY, 66, Intent.ENTRY),  # flips to long
        (-65, SELL, 65, Intent.ENTRY),  # adds to the short
        (0, SELL, 65, Intent.ENTRY),  # flat: opens a short
        (0, BUY, 65, Intent.ENTRY),  # flat: opens a long
    ],
)
def test_the_rule_around_the_position_size(
    held: int, side: OrderSide, quantity: int, expected: Intent
) -> None:
    positions = book(position(quantity=held, average="100.00" if held else "0"))

    assert classify(order(side, quantity), positions) is expected


def test_with_no_position_at_all_an_order_is_an_entry() -> None:
    assert classify(order(SELL, 65), {}) is Intent.ENTRY
    assert classify(order(BUY, 65), {}) is Intent.ENTRY


def test_products_are_separate_positions() -> None:
    """A long 65 held INTRADAY is not reduced by a MARGIN sell: that opens a MARGIN short."""
    positions = book(position(quantity=65, product=ProductType.INTRADAY))

    assert classify(order(SELL, 65, product=ProductType.INTRADAY), positions) is Intent.EXIT
    assert classify(order(SELL, 65, product=ProductType.MARGIN), positions) is Intent.ENTRY


def test_instruments_are_separate_positions() -> None:
    positions = book(position(key=OPTION, quantity=65))

    assert classify(order(SELL, 65, key=STOCK), positions) is Intent.ENTRY


def test_other_accounts_positions_do_not_count() -> None:
    positions = book(position(quantity=65, account_id="spouse"))

    assert classify(order(SELL, 65), positions) is Intent.ENTRY


def test_an_open_sell_order_does_not_change_the_answer() -> None:
    """Resting orders are not netted (design §5): classify looks at the position alone."""
    positions = book(position(quantity=65))
    first, second = order(SELL, 65, order_id="a"), order(SELL, 65, order_id="b")

    assert classify(first, positions) is Intent.EXIT
    assert classify(second, positions) is Intent.EXIT


def test_a_place_command_is_classified_like_the_order_it_would_become() -> None:
    from datetime import UTC, datetime

    from contracts.commands import PlaceOrder
    from contracts.orders import OrderType

    def command(side: OrderSide, quantity: int) -> PlaceOrder:
        return PlaceOrder(
            command_id="c", idempotency_key="k", account_id="main", source="t",
            sent_at=datetime(2026, 11, 16, 5, 0, tzinfo=UTC), key=OPTION, side=side,
            quantity=quantity, product=ProductType.INTRADAY, order_type=OrderType.MARKET,
        )  # fmt: skip

    positions = book(position(quantity=65))

    assert classify(command(SELL, 65), positions) is Intent.EXIT
    assert classify(command(SELL, 130), positions) is Intent.ENTRY
