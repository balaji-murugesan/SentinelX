"""SimulatedFeed: scripted paths and the seeded random walk (tasks 4.3, 4.4).

Spec market-feed: "Simulated scripted paths", "Simulated random walk", "Subscribe while
running", "Subscriptions only on demand" (master loaded, cap reached).
"""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.market import InstrumentKey
from engine.broker.dhan.instruments_source import parse_instrument_file
from engine.market_data.bus import TickBus
from engine.market_data.instruments import InstrumentMaster
from engine.market_data.provider import (
    FeedState,
    FeedStatus,
    SubscriptionLimitError,
    Subscriptions,
    Tick,
)
from engine.market_data.simulated import (
    RandomWalk,
    ScriptStep,
    SimulatedFeed,
    WalkConfig,
    snap_to_tick,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "instruments_detailed.csv"
NIFTY = InstrumentKey.parse("IDX_I:13")
BANK = InstrumentKey.parse("IDX_I:25")
WALK = WalkConfig(
    start_price=Decimal("24500"),
    step_ticks=2,
    interval_s=0.005,
    default_tick=Decimal("0.05"),
    seed=7,
)


def ticks_of(subscription: object) -> list[Tick]:
    items = []
    while (item := subscription.get_nowait()) is not None:  # type: ignore[attr-defined]
        if isinstance(item, Tick):
            items.append(item)
    return items


# ---- scripted ---------------------------------------------------------------------


async def test_script_delivered_in_order() -> None:
    bus = TickBus()
    consumer = bus.subscribe("test")
    prices = ["120", "118.5", "115", "110.05", "108"]
    feed = SimulatedFeed(
        bus,
        Subscriptions(cap=10),
        script=[ScriptStep(NIFTY, Decimal(p)) for p in prices],
    )
    await feed.subscribe([NIFTY], "test")
    await feed.run(asyncio.Event())
    first = consumer.get_nowait()
    assert isinstance(first, FeedStatus)
    assert first.state is FeedState.CONNECTED
    received = ticks_of(consumer)
    assert [t.ltp for t in received] == [Decimal(p) for p in prices]
    assert all(t.key == NIFTY and t.received_at.tzinfo is not None for t in received)


async def test_script_skips_unsubscribed_instruments() -> None:
    bus = TickBus()
    consumer = bus.subscribe("test")
    feed = SimulatedFeed(
        bus,
        Subscriptions(cap=10),
        script=[ScriptStep(NIFTY, Decimal("100")), ScriptStep(BANK, Decimal("200"))],
    )
    await feed.subscribe([NIFTY], "test")
    await feed.run(asyncio.Event())
    assert [t.key for t in ticks_of(consumer)] == [NIFTY]


def test_needs_exactly_one_mode() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        SimulatedFeed(TickBus(), Subscriptions(cap=1))


# ---- random walk ------------------------------------------------------------------


async def run_walk(seconds: float, *, subscribe_late: bool = False) -> list[Tick]:
    bus = TickBus()
    consumer = bus.subscribe("test")
    feed = SimulatedFeed(bus, Subscriptions(cap=10), walk=WALK)
    await feed.subscribe([NIFTY], "test")
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(seconds)
    if subscribe_late:
        await feed.subscribe([BANK], "late")
        await asyncio.sleep(seconds)
    stop.set()
    await task
    return ticks_of(consumer)


def walk_prices(config: WalkConfig, tick: Decimal, n: int) -> list[Decimal]:
    walk = RandomWalk(config)
    return [walk.next_price(NIFTY, tick) for _ in range(n)]


def test_same_seed_same_path() -> None:
    assert walk_prices(WALK, Decimal("0.05"), 200) == walk_prices(WALK, Decimal("0.05"), 200)


def test_different_seed_different_path() -> None:
    other = WalkConfig(WALK.start_price, WALK.step_ticks, WALK.interval_s, WALK.default_tick, 8)
    assert walk_prices(WALK, Decimal("0.05"), 50) != walk_prices(other, Decimal("0.05"), 50)


@given(
    seed=st.integers(0, 2**32),
    start=st.decimals("0.01", "100000", places=2),
    tick=st.sampled_from([Decimal("0.01"), Decimal("0.05"), Decimal("0.10"), Decimal("1")]),
    step_ticks=st.integers(1, 50),
)
def test_walk_stays_on_tick_grid_and_positive(
    seed: int, start: Decimal, tick: Decimal, step_ticks: int
) -> None:
    """Property: whatever the inputs, every price is a positive whole multiple of the tick."""
    config = WalkConfig(start, step_ticks, 1.0, tick, seed)
    for price in walk_prices(config, tick, 300):
        assert price >= tick
        assert price % tick == 0


def test_snap_to_tick() -> None:
    assert snap_to_tick(Decimal("24500.03"), Decimal("0.05")) == Decimal("24500.05")
    assert snap_to_tick(Decimal("0.01"), Decimal("0.05")) == Decimal("0.05")


async def test_walk_streams_until_stopped() -> None:
    ticks = await run_walk(0.05)
    assert len(ticks) >= 3
    assert ticks[0].ltp == Decimal("24500")


async def test_subscribe_while_running() -> None:
    ticks = await run_walk(0.05, subscribe_late=True)
    assert BANK in {t.key for t in ticks}


async def test_master_loaded_subscribes_nothing() -> None:
    """Loading thousands of contracts never streams them: only explicit requests do."""
    master = InstrumentMaster()
    subs = Subscriptions(cap=200)
    master.replace(parse_instrument_file(FIXTURE).instruments, date(2026, 10, 27))
    feed = SimulatedFeed(TickBus(), subs, walk=WALK, tick_size=lambda k: None)
    assert subs.keys == []
    await feed.subscribe([NIFTY], "prefill")
    assert subs.keys == [NIFTY]


async def test_cap_reached_through_feed() -> None:
    feed = SimulatedFeed(TickBus(), Subscriptions(cap=1), walk=WALK)
    await feed.subscribe([NIFTY], "prefill")
    with pytest.raises(SubscriptionLimitError):
        await feed.subscribe([BANK], "positions")


async def test_walk_uses_instrument_tick_size() -> None:
    bus = TickBus()
    consumer = bus.subscribe("test")
    one_rupee = WalkConfig(Decimal("100"), 3, 0.002, Decimal("0.05"), 1)
    feed = SimulatedFeed(
        bus, Subscriptions(cap=10), walk=one_rupee, tick_size=lambda _: Decimal("1")
    )
    await feed.subscribe([NIFTY], "test")
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await task
    assert all(t.ltp % 1 == 0 for t in ticks_of(consumer))
