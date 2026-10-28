"""Warm-up, join, partial candles, refill and history failures (add-candles tasks 5.1-5.4).

Scenarios from spec candles: "Enough history at entry", "Warm-up before first use", "Started
mid-candle", "Ticks dropped", "Two-minute outage", "History endpoint down", "Data plan
inactive" (service side).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from structlog.testing import capture_logs

from contracts.market import InstrumentKey
from engine.market_data.bus import TickBus
from engine.market_data.candle_service import CandleService
from engine.market_data.candles import Candle
from engine.market_data.clock import IST
from engine.market_data.history_source import HistoryUnavailable
from engine.market_data.provider import FeedState, FeedStatus, Subscriptions, Tick
from engine.market_data.simulated import SimulatedFeed
from tests.engine.candle_fakes import FakeHistory

OPTION = InstrumentKey.parse("NSE_FNO:53720")
GRACE = timedelta(seconds=2)


def at(day: int, hour: int, minute: int, second: float = 0) -> datetime:
    base = datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(UTC)
    return base + timedelta(seconds=second)


def today(hour: int, minute: int, second: float = 0) -> datetime:
    return at(28, hour, minute, second)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def history_minute(start: datetime, price: str = "100", volume: int = 65) -> Candle:
    p = Decimal(price)
    return Candle(OPTION, 1, start, p, p + 1, p - 1, p, volume)


def session_minutes(day: int, until: tuple[int, int] = (15, 30)) -> list[Candle]:
    """History candles for every minute from 09:15 up to (not including) ``until``."""
    out, moment = [], at(day, 9, 15)
    while moment < at(day, *until):
        out.append(history_minute(moment))
        moment += timedelta(minutes=1)
    return out


def tick(when: datetime, price: str = "100") -> Tick:
    return Tick(OPTION, Decimal(price), exchange_time=when, received_at=when)


def make(
    clock: Clock, history: FakeHistory, queue_size: int = 10_000
) -> tuple[CandleService, TickBus]:
    bus = TickBus()
    feed = SimulatedFeed(bus, Subscriptions(10), script=[])
    service = CandleService(
        feed,
        bus,
        history,
        intervals=(1, 3, 5, 15),
        history_size=200,
        grace=GRACE,
        now=clock,
        queue_size=queue_size,
    )
    return service, bus


# ---- 5.1 warm-up and join -----------------------------------------------------------------


async def test_enough_history_at_entry() -> None:
    history = FakeHistory(session_minutes(27) + session_minutes(28, until=(10, 30)))
    clock = Clock(today(10, 30))
    service, _ = make(clock, history)
    await service.request([OPTION], "positions")
    ones = service.last(OPTION, 1, 500)
    assert len(ones) == 200 and ones[-1].start == today(10, 29)
    fifteens = service.last(OPTION, 15, 500)
    assert len(fifteens) == 25 + 5  # yesterday's full session + 09:15..10:15 today
    assert fifteens[-1].start == today(10, 15)
    assert service.last(OPTION, 5, 1)[0].start == today(10, 25)
    assert service.last(OPTION, 3, 1)[0].start == today(10, 27)
    assert all(not c.partial for m in (1, 3, 5, 15) for c in service.last(OPTION, m, 500))
    [(_, start, end)] = history.calls
    assert end == today(10, 30) and (end - start).days == 16


async def test_warm_up_before_first_use() -> None:
    history = FakeHistory(session_minutes(27))
    service, _ = make(Clock(today(9, 20)), history)
    await service.request([OPTION], "positions")  # returns only once history is installed
    candles = service.last(OPTION, 15, 22)
    assert len(candles) == 22 and all(c.start < today(9, 15) for c in candles)


async def test_started_mid_candle() -> None:
    history = FakeHistory(session_minutes(28, until=(10, 32)))
    clock = Clock(today(10, 32, 20))
    service, _ = make(clock, history)
    await service.request([OPTION], "positions")
    assert service.last(OPTION, 5, 1)[0].start == today(10, 25)  # history ends at 10:25
    service.on_tick(tick(today(10, 32, 30)))
    service.on_tick(tick(today(10, 34, 10)))
    for minute in (33, 34, 35):
        clock.now = today(10, minute) + GRACE
        service.close_due(clock.now)
    fives = service.last(OPTION, 5, 500)
    assert fives[-1].start == today(10, 30) and fives[-1].partial
    starts = [c.start for c in fives]
    assert starts == sorted(set(starts))  # no duplicate or overlapping intervals
    ones = service.last(OPTION, 1, 500)
    assert [c.start for c in ones] == sorted({c.start for c in ones})
    assert ones[-2].start == today(10, 32) and ones[-2].partial  # live, seen from 10:32:20


# ---- 5.2 partial from feed status and drops -------------------------------------------------


async def test_ticks_dropped_mark_minute_and_containing_candles_partial() -> None:
    clock = Clock(today(11, 20, 30))
    service, bus = make(clock, FakeHistory(), queue_size=2)
    await service.request([OPTION], "positions")
    # A clean minute first, seen whole from 11:20:30's next minute on.
    stop = asyncio.Event()
    runner = asyncio.create_task(service.run(stop))
    for second in range(5):  # five ticks at once into a queue of two: three dropped
        bus.publish(tick(today(11, 21, second)))
    await asyncio.sleep(0.05)
    for second in (10, 11):
        bus.publish(tick(today(11, 22, second)))
    await asyncio.sleep(0.05)
    stop.set()
    await runner
    for minute in (22, 23, 24, 25):
        service.close_due(today(11, minute) + GRACE)
    by_start = {c.start: c for c in service.last(OPTION, 1, 10)}
    assert by_start[today(11, 21)].partial
    assert not by_start[today(11, 22)].partial
    five = service.last(OPTION, 5, 1)[0]
    assert five.start == today(11, 20) and five.partial


async def test_whole_minute_while_connected_is_not_partial() -> None:
    clock = Clock(today(11, 0, 30))
    service, _ = make(clock, FakeHistory())
    await service.request([OPTION], "positions")
    service.on_status(FeedStatus(FeedState.CONNECTED, "connected", today(11, 0, 30)))
    service.on_tick(tick(today(11, 1, 5)))
    service.close_due(today(11, 2) + GRACE)
    [candle] = service.last(OPTION, 1, 5)
    assert not candle.partial


# ---- 5.3 refill after reconnect ------------------------------------------------------------


async def test_two_minute_outage() -> None:
    history = FakeHistory(
        [history_minute(today(12, m), "200") for m in (0, 1, 2)]  # Dhan's version
    )
    clock = Clock(today(11, 59, 30))
    service, _ = make(clock, history)
    events = service.events.subscribe("test")
    await service.request([OPTION], "positions")
    service.on_status(FeedStatus(FeedState.CONNECTED, "connected", today(11, 59, 30)))
    service.on_tick(tick(today(12, 0, 10)))
    service.on_status(FeedStatus(FeedState.RECONNECTING, "lost", today(12, 0, 30)))
    service.close_due(today(12, 1) + GRACE)  # 12:00 closes partial
    service.on_status(FeedStatus(FeedState.CONNECTED, "back", today(12, 2, 40)))
    service.on_tick(tick(today(12, 2, 45)))
    service.on_tick(tick(today(12, 3, 10)))  # closes 12:02 (partial)
    clock.now = today(12, 3, 6)  # past 12:03 + 5 s: the refill may run
    for _ in range(10):
        await asyncio.sleep(0)
    published = []
    while (item := events.get_nowait()) is not None:
        published.append(item)
    ones = {(c.start, c.replaced): c for c in published if c.minutes == 1}
    assert ones[(today(12, 0), False)].partial
    assert ones[(today(12, 2), False)].partial
    assert ones[(today(12, 1), False)].close == Decimal("200")  # missed: from history
    for minute in (0, 2):
        replaced = ones[(today(12, minute), True)]
        assert not replaced.partial and replaced.close == Decimal("200")
    held = {c.start: c for c in service.last(OPTION, 1, 10)}
    assert all(not held[today(12, m)].partial for m in (0, 1, 2))
    three = service.last(OPTION, 3, 1)[0]
    assert three.start == today(12, 0) and not three.partial


async def test_startup_connect_is_not_an_outage() -> None:
    history = FakeHistory()
    service, _ = make(Clock(today(10, 0)), history)
    await service.request([OPTION], "positions")
    service.on_status(FeedStatus(FeedState.CONNECTING, "starting", today(10, 0)))
    service.on_status(FeedStatus(FeedState.CONNECTED, "connected", today(10, 0, 1)))
    await asyncio.sleep(0)
    assert len(history.calls) == 1  # the warm-up only, no refill


# ---- 5.4 history failures contained --------------------------------------------------------


async def test_history_endpoint_down() -> None:
    history = FakeHistory(session_minutes(27))
    history.fail = HistoryUnavailable("Dhan unreachable (ConnectTimeout)")
    clock = Clock(today(10, 0, 30))
    service, _ = make(clock, history)
    with capture_logs() as logs:
        await service.request([OPTION], "positions")
    errors = [r for r in logs if r["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["instrument"] == str(OPTION) and "ConnectTimeout" in errors[0]["reason"]
    service.on_tick(tick(today(10, 1, 5)))
    service.close_due(today(10, 2) + GRACE)
    assert [c.start for c in service.last(OPTION, 1, 50)] == [today(10, 1)]  # live only
    # The next reconnect retries the warm-up.
    history.fail = None
    service.on_status(FeedStatus(FeedState.CONNECTED, "connected", today(10, 1)))
    service.on_status(FeedStatus(FeedState.RECONNECTING, "lost", today(10, 2, 10)))
    service.on_status(FeedStatus(FeedState.CONNECTED, "back", today(10, 2, 20)))
    clock.now = today(10, 3, 6)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(service.last(OPTION, 15, 50)) == 25  # yesterday's session installed


async def test_permanent_history_failure_stops_requests() -> None:
    history = FakeHistory()
    history.fail = HistoryUnavailable("rejected: DH-901", permanent=True)
    service, _ = make(Clock(today(10, 0)), history)
    await service.request([OPTION], "positions")
    other = InstrumentKey.parse("IDX_I:13")
    await service.request([other], "positions")
    assert len(history.calls) == 1  # nothing more is asked of a rejected history source
