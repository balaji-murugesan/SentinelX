"""PositionFeed (task 5.2).

Scenario from spec positions: "Position closed" - the positions owner releases the instrument's
subscription when the position is flat, and other owners' subscriptions stay.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from contracts.market import InstrumentKey
from contracts.orders import Position, ProductType
from engine.market_data.bus import TickBus
from engine.market_data.provider import SubscriptionLimitError, Subscriptions
from engine.market_data.simulated import SimulatedFeed
from engine.positions.feed import POSITIONS_OWNER, PositionFeed

T0 = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")
STOCK = InstrumentKey.parse("NSE_EQ:1333")


def position(
    quantity: int, key: InstrumentKey = OPTION, product: ProductType = ProductType.INTRADAY
) -> Position:
    return Position(
        account_id="main", key=key, product=product, quantity=quantity,
        average_price=Decimal("100") if quantity else Decimal(0), updated_at=T0,
    )  # fmt: skip


def make(cap: int = 10) -> tuple[SimulatedFeed, Subscriptions, PositionFeed]:
    subscriptions = Subscriptions(cap)
    feed = SimulatedFeed(TickBus(), subscriptions, script=[])
    return feed, subscriptions, PositionFeed(feed)


async def test_first_fill_subscribes_and_flat_releases() -> None:
    _, subscriptions, positions = make()

    await positions.update(position(65))
    assert OPTION in subscriptions

    await positions.update(position(0))
    assert OPTION not in subscriptions


async def test_position_closed_keeps_another_owners_subscription() -> None:
    feed, subscriptions, positions = make()
    await feed.subscribe([OPTION], "prefill")  # another owner holds the same key

    await positions.update(position(-65))  # a short counts as open too
    await positions.update(position(0))
    assert OPTION in subscriptions  # prefill still wants it

    await feed.unsubscribe([OPTION], "prefill")
    # Gone now: proves the positions owner really released (else it would still hold the key).
    assert OPTION not in subscriptions


async def test_repeated_updates_do_not_double_subscribe_or_release_early() -> None:
    _, subscriptions, positions = make()

    await positions.update(position(65))
    await positions.update(position(130))  # scale in: same key, still one subscription
    await positions.update(position(65))  # partly out: still open
    assert positions.keys == [OPTION]
    assert OPTION in subscriptions

    await positions.update(position(0))
    await positions.update(position(0))  # repeat on a flat position is a no-op
    assert positions.keys == []
    assert OPTION not in subscriptions


async def test_key_stays_until_every_product_is_flat() -> None:
    _, subscriptions, positions = make()

    await positions.update(position(65, product=ProductType.INTRADAY))
    await positions.update(position(65, product=ProductType.MARGIN))
    await positions.update(position(0, product=ProductType.INTRADAY))
    assert OPTION in subscriptions  # the MARGIN position still needs the price

    await positions.update(position(0, product=ProductType.MARGIN))
    assert OPTION not in subscriptions


async def test_flat_position_that_was_never_open_is_ignored() -> None:
    _, subscriptions, positions = make()
    await positions.update(position(0, key=STOCK))
    assert subscriptions.keys == []


async def test_feed_at_its_cap_is_not_recorded_so_the_next_update_retries() -> None:
    feed, subscriptions, positions = make(cap=1)
    await feed.subscribe([STOCK], "prefill")  # takes the only slot

    with pytest.raises(SubscriptionLimitError):
        await positions.update(position(65))
    assert positions.keys == []

    await feed.unsubscribe([STOCK], "prefill")
    await positions.update(position(65))  # retried once there is room
    assert subscriptions.keys == [OPTION]
    assert POSITIONS_OWNER == "positions"  # the name the spec and design §8 give
