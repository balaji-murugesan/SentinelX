"""Tick bus, provider types and subscription registry (tasks 4.1, 4.2, part of 4.3).

Spec market-feed: "Fan-out to consumers", "Feed status events", "Subscriptions only on
demand" (cap, duplicates), plus Tick immutability.
"""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from contracts.market import InstrumentKey
from engine.market_data.bus import Item, TickBus, consume
from engine.market_data.provider import (
    FeedState,
    FeedStatus,
    SubscriptionLimitError,
    Subscriptions,
    Tick,
)

NIFTY = InstrumentKey.parse("IDX_I:13")
BANK = InstrumentKey.parse("IDX_I:25")
T0 = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)


def tick(price: str, key: InstrumentKey = NIFTY, seconds: int = 0) -> Tick:
    at = T0 + timedelta(seconds=seconds)
    return Tick(key, Decimal(price), at, at)


def drain(subscription: object) -> list[Item]:
    items = []
    while (item := subscription.get_nowait()) is not None:  # type: ignore[attr-defined]
        items.append(item)
    return items


# ---- Tick -------------------------------------------------------------------------


def test_tick_is_immutable() -> None:
    t = tick("100")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.ltp = Decimal("101")  # type: ignore[misc]


# ---- fan-out ----------------------------------------------------------------------


def test_every_consumer_gets_every_tick_in_order() -> None:
    bus = TickBus()
    consumers = [bus.subscribe(f"c{i}") for i in range(3)]
    sent = [tick("100", seconds=0), tick("100.05", seconds=1), tick("99.95", seconds=2)]
    for t in sent:
        bus.publish(t)
    for consumer in consumers:
        assert drain(consumer) == sent


def test_slow_consumer_drops_oldest_and_others_get_everything() -> None:
    bus = TickBus()
    slow = bus.subscribe("slow", maxsize=2)
    fast = bus.subscribe("fast")
    sent = [tick(str(100 + i), seconds=i) for i in range(5)]
    with capture_logs() as logs:
        for t in sent:
            bus.publish(t)
    assert drain(slow) == sent[-2:]  # oldest three dropped
    assert slow.dropped == 3
    assert drain(fast) == sent
    warnings = [r for r in logs if r["event"].startswith("slow market-data consumer")]
    assert len(warnings) == 1  # rate-limited: one warning, not one per drop
    assert warnings[0]["consumer"] == "slow"


def test_closed_subscription_receives_nothing() -> None:
    bus = TickBus()
    gone = bus.subscribe("gone")
    gone.close()
    bus.publish(tick("100"))
    assert gone.get_nowait() is None


async def test_consumer_error_is_logged_and_skipped() -> None:
    bus = TickBus()
    failing = bus.subscribe("failing")
    healthy = bus.subscribe("healthy")
    handled: list[Item] = []

    async def handle(item: Item) -> None:
        if isinstance(item, Tick) and item.ltp == Decimal("101"):
            raise RuntimeError("consumer bug")
        handled.append(item)

    with capture_logs() as logs:
        task = asyncio.create_task(consume(failing, handle))
        for price in ("100", "101", "102"):
            bus.publish(tick(price))
        await asyncio.sleep(0.01)
        task.cancel()
    assert [t.ltp for t in handled if isinstance(t, Tick)] == [Decimal("100"), Decimal("102")]
    assert any(r["event"] == "market-data consumer failed on an item" for r in logs)
    assert len(drain(healthy)) == 3


# ---- feed status ------------------------------------------------------------------


def test_status_events_keep_order_with_ticks() -> None:
    bus = TickBus()
    consumer = bus.subscribe("c")
    reconnecting = FeedStatus(FeedState.RECONNECTING, "connection closed", T0)
    bus.publish(tick("100"))
    bus.publish(reconnecting)
    bus.publish(tick("101", seconds=5))
    assert drain(consumer)[1] == reconnecting
    assert bus.status == reconnecting


def test_last_tick_time() -> None:
    bus = TickBus()
    assert bus.last_tick_time(NIFTY) is None
    bus.publish(tick("100", seconds=3))
    assert bus.last_tick_time(NIFTY) == T0 + timedelta(seconds=3)
    assert bus.last_tick_time(BANK) is None


# ---- subscriptions: on demand, cap, duplicates -------------------------------------


def test_new_keys_returned_once() -> None:
    subs = Subscriptions(cap=10)
    assert subs.add([NIFTY, BANK], "prefill") == [NIFTY, BANK]
    assert subs.add([NIFTY], "positions") == []  # duplicate: nothing new to send


def test_cap_is_all_or_nothing() -> None:
    subs = Subscriptions(cap=2)
    subs.add([NIFTY], "prefill")
    extra = [BANK, InstrumentKey.parse("NSE_EQ:2885")]
    with pytest.raises(SubscriptionLimitError, match="FEED_MAX_SUBSCRIPTIONS=2"):
        subs.add(extra, "positions")
    assert subs.keys == [NIFTY]  # unchanged


def test_key_stays_while_another_owner_wants_it() -> None:
    subs = Subscriptions(cap=10)
    subs.add([NIFTY], "prefill")
    subs.add([NIFTY], "positions")
    assert subs.remove([NIFTY], "positions") == []
    assert NIFTY in subs
    assert subs.remove([NIFTY], "prefill") == [NIFTY]
    assert NIFTY not in subs


def test_removing_unknown_owner_is_harmless() -> None:
    subs = Subscriptions(cap=10)
    subs.add([NIFTY], "prefill")
    assert subs.remove([NIFTY, BANK], "nobody") == []
    assert NIFTY in subs
