"""Candle store, boundary timer and candle subscriptions (add-candles tasks 3.1-3.3).

Scenarios from spec candles: "Read the last five", "Fewer candles than asked", "Close on time
with no further ticks", "Two owners, one releases", "Over the feed cap".
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.market import InstrumentKey
from engine.market_data.bus import TickBus
from engine.market_data.candle_service import CandleService
from engine.market_data.candles import Candle, CandleStore
from engine.market_data.clock import IST
from engine.market_data.provider import SubscriptionLimitError, Subscriptions, Tick
from engine.market_data.simulated import SimulatedFeed

OPTION = InstrumentKey.parse("NSE_FNO:53720")
NIFTY = InstrumentKey.parse("IDX_I:13")
GRACE = timedelta(seconds=2)


def at(hour: int, minute: int, second: float = 0) -> datetime:
    base = datetime(2026, 10, 28, hour, minute, tzinfo=IST).astimezone(UTC)
    return base + timedelta(seconds=second)


def tick(when: datetime, price: str, key: InstrumentKey = OPTION) -> Tick:
    return Tick(key, Decimal(price), exchange_time=when, received_at=when)


def candle(start: datetime, close: str = "1", minutes: int = 1) -> Candle:
    one = Decimal(close)
    return Candle(OPTION, minutes, start, one, one, one, one, volume=0)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_service(
    now: Callable[[], datetime], cap: int = 10, intervals: tuple[int, ...] = (1, 3, 5, 15)
) -> tuple[CandleService, Subscriptions]:
    bus = TickBus()
    subscriptions = Subscriptions(cap)
    feed = SimulatedFeed(bus, subscriptions, script=[])
    service = CandleService(feed, bus, intervals=intervals, history_size=200, grace=GRACE, now=now)
    return service, subscriptions


# ---- 3.1 store --------------------------------------------------------------------------


def test_read_the_last_five_oldest_first() -> None:
    store = CandleStore(200)
    for i in range(8):
        store.add(candle(at(10, 0) + timedelta(minutes=5 * i), minutes=5))
    last = store.last(OPTION, 5, 5)
    assert [c.start for c in last] == [at(10, 15) + timedelta(minutes=5 * i) for i in range(5)]


def test_fewer_candles_than_asked() -> None:
    store = CandleStore(200)
    for i in range(30):
        store.add(candle(at(10, 0) + timedelta(minutes=i)))
    assert len(store.last(OPTION, 1, 50)) == 30
    assert store.last(NIFTY, 1, 5) == []


def test_store_keeps_only_the_newest() -> None:
    store = CandleStore(3)
    for i in range(5):
        store.add(candle(at(10, i)))
    assert [c.start for c in store.last(OPTION, 1, 10)] == [at(10, 2), at(10, 3), at(10, 4)]


def test_replace_by_start_keeps_order() -> None:
    store = CandleStore(10)
    for i in range(4):
        store.add(candle(at(10, i), close="1"))
    store.add(candle(at(10, 1), close="9"))
    closes = [c.close for c in store.last(OPTION, 1, 10)]
    assert closes == [Decimal(x) for x in ("1", "9", "1", "1")]


# ---- 3.2 closing and publishing -------------------------------------------------------


async def test_close_on_time_with_no_further_ticks() -> None:
    clock = Clock(at(10, 5, 30))
    service, _ = make_service(clock)
    events = service.events.subscribe("test")
    await service.request([OPTION], "test")
    service.on_tick(tick(at(10, 5, 40), "100"))
    clock.now = at(10, 6, 1)
    service.close_due(clock.now)
    assert events.get_nowait() is None and service.last(OPTION, 1, 5) == []  # forming: hidden
    clock.now = at(10, 6, 2)
    service.close_due(clock.now)
    published = events.get_nowait()
    assert published is not None and published.start == at(10, 5)
    assert service.last(OPTION, 1, 5) == [published]


async def test_tick_for_later_minute_closes_before_the_timer() -> None:
    clock = Clock(at(10, 5))
    service, _ = make_service(clock)
    events = service.events.subscribe("test")
    await service.request([OPTION], "test")
    service.on_tick(tick(at(10, 5, 40), "100"))
    service.on_tick(
        tick(
            at(
                10,
                6,
                0,
            ),
            "101",
        )
    )
    published = events.get_nowait()
    assert published is not None and published.start == at(10, 5)


async def test_longer_intervals_close_at_their_boundary() -> None:
    clock = Clock(at(10, 0))
    service, _ = make_service(clock)
    events = service.events.subscribe("test")
    await service.request([OPTION], "test")
    for minute in range(15):
        service.on_tick(tick(at(10, minute, 10), str(100 + minute)))
        clock.now = at(10, minute + 1) + GRACE
        service.close_due(clock.now)
    published = []
    while (item := events.get_nowait()) is not None:
        published.append(item)
    by_interval = {m: [c.start for c in published if c.minutes == m] for m in (1, 3, 5, 15)}
    assert len(by_interval[1]) == 15
    assert by_interval[3] == [at(10, m) for m in range(0, 15, 3)]
    assert by_interval[5] == [at(10, 0), at(10, 5), at(10, 10)]
    assert by_interval[15] == [at(10, 0)]
    fifteen = service.last(OPTION, 15, 1)[0]
    assert (fifteen.open, fifteen.high, fifteen.close) == (Decimal(100), Decimal(114), Decimal(114))
    assert not fifteen.partial


async def test_aggregate_partial_when_watching_began_inside_it() -> None:
    clock = Clock(at(10, 2, 30))
    service, _ = make_service(clock)
    await service.request([OPTION], "test")
    for minute in (3, 4):
        service.on_tick(tick(at(10, minute, 10), "100"))
        clock.now = at(10, minute + 1) + GRACE
        service.close_due(clock.now)
    [five] = service.last(OPTION, 5, 5)
    assert five.start == at(10, 0) and five.partial
    assert not service.last(OPTION, 1, 5)[-1].partial  # 10:04 itself was seen whole


async def test_only_configured_intervals_are_announced() -> None:
    clock = Clock(at(10, 0))
    service, _ = make_service(clock, intervals=(5,))
    events = service.events.subscribe("test")
    await service.request([OPTION], "test")
    for minute in range(5):
        service.on_tick(tick(at(10, minute, 10), "100"))
        clock.now = at(10, minute + 1) + GRACE
        service.close_due(clock.now)
    item = events.get_nowait()
    assert item is not None and item.minutes == 5
    assert events.get_nowait() is None


async def test_timer_loop_closes_on_the_boundary() -> None:
    # A clock running 2.2 s before the boundary + grace of a session minute, at real speed.
    target = at(10, 6) + GRACE
    offset = target - datetime.now(UTC) - timedelta(seconds=0.2)
    service, _ = make_service(lambda: datetime.now(UTC) + offset)
    events = service.events.subscribe("test")
    await service.request([OPTION], "test")
    service.on_tick(tick(at(10, 5, 40), "100"))
    stop = asyncio.Event()
    runner = asyncio.create_task(service.run(stop))
    try:
        async with asyncio.timeout(3):
            item = await anext(events)
    finally:
        stop.set()
        await runner
    assert item.start == at(10, 5)


# ---- 3.3 candle subscriptions -------------------------------------------------------


async def test_two_owners_one_releases() -> None:
    clock = Clock(at(11, 0))
    service, subscriptions = make_service(clock)
    await service.request([OPTION], "positions")
    await service.request([OPTION], "cli")
    await service.release([OPTION], "cli")
    assert OPTION in subscriptions
    service.on_tick(tick(at(11, 0, 5), "100"))
    service.close_due(at(11, 1) + GRACE)
    assert len(service.last(OPTION, 1, 5)) == 1
    await service.release([OPTION], "positions")
    assert OPTION not in subscriptions
    assert service.last(OPTION, 1, 5) == []


async def test_over_the_feed_cap() -> None:
    clock = Clock(at(11, 0))
    service, subscriptions = make_service(clock, cap=1)
    await service.request([NIFTY], "a")
    with pytest.raises(SubscriptionLimitError):
        await service.request([OPTION], "b")
    assert subscriptions.keys == [NIFTY]
    service.on_tick(tick(at(11, 0, 5), "100"))
    service.close_due(at(11, 1) + GRACE)
    assert service.last(OPTION, 1, 5) == []  # no candle building started
