"""Market data wired into the engine run loop (task 6.1, design §11).

Everything runs through ``engine.main.run`` with a stand-in Redis and injected loaders/HTTP,
so nothing touches the network. Spec scenarios covered: ticks reach a consumer, a crashing
feed leaves the heartbeat running, a failed session check keeps the engine up (broker-session
"Engine stays up", "No retry loop"), and a slow instrument refresh doesn't stall ticks
(instruments "Refresh during market hours").
"""

import asyncio
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from contracts.market import Instrument, InstrumentKey
from contracts.settings import load_settings
from engine.config import EngineSettings
from engine.healthcheck import is_healthy
from engine.main import run
from engine.market_data.bus import Subscription
from engine.market_data.provider import FeedState, Tick
from engine.market_data.service import MarketDataService
from tests.engine.test_run import DownRedis

NIFTY = InstrumentKey.parse("IDX_I:13")


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> EngineSettings:
    values = {
        "REDIS_URL": "redis://redis:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/x",
        "ENGINE_HEARTBEAT_PATH": str(tmp_path / "hb"),
        "INSTRUMENT_CACHE_DIR": str(tmp_path / "instruments"),
        "FEED_SOURCE": "simulated",
        "FEED_INSTRUMENTS": str(NIFTY),
        "SIM_INTERVAL_S": "0.01",
        "SIM_SEED": "1",
        **env,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_settings(EngineSettings)


async def no_instruments(_today: date) -> tuple[Iterable[Instrument], date] | None:
    return None


async def until(predicate: Callable[[], bool], limit_s: float = 3.0) -> None:
    deadline = time.monotonic() + limit_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)  # noqa: ASYNC110 (polling a plain predicate, not an event)


async def next_tick(subscription: Subscription, limit_s: float = 3.0) -> Tick:
    async def first() -> Tick:
        async for item in subscription:
            if isinstance(item, Tick):
                return item
        raise AssertionError("subscription closed")

    return await asyncio.wait_for(first(), limit_s)


def start(
    settings: EngineSettings, service: MarketDataService
) -> tuple["asyncio.Task[None]", asyncio.Event]:
    stop = asyncio.Event()
    engine = asyncio.create_task(
        run(settings, redis=DownRedis(), stop=stop, market_data=service)  # type: ignore[arg-type]
    )
    return engine, stop


async def test_simulated_ticks_reach_a_consumer_through_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    service = MarketDataService(settings, load=no_instruments)
    consumer = service.bus.subscribe("test")
    engine, stop = start(settings, service)

    tick = await next_tick(consumer)
    assert tick.key == NIFTY  # the FEED_INSTRUMENTS prefill was subscribed

    stop.set()
    await asyncio.wait_for(engine, timeout=2)


class CrashingFeed:
    async def run(self, stop: asyncio.Event) -> None:
        raise RuntimeError("bug in the feed")

    async def subscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        pass

    async def unsubscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        pass


async def test_crashing_feed_leaves_heartbeat_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    service = MarketDataService(settings, load=no_instruments)
    service.provider = CrashingFeed()
    engine, stop = start(settings, service)

    await until(lambda: (s := service.bus.status) is not None and s.state is FeedState.DOWN)
    await asyncio.sleep(0.1)
    assert not engine.done(), "a market-data failure must not stop the engine"
    assert is_healthy(settings.engine_heartbeat_path)

    stop.set()
    await asyncio.wait_for(engine, timeout=2)


async def test_failed_session_check_keeps_engine_up_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def dhan(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            401, json={"errorCode": "DH-901", "errorType": "Invalid_Authentication"}
        )

    settings = make_settings(
        monkeypatch,
        tmp_path,
        FEED_SOURCE="dhan",
        DHAN_CLIENT_ID="1100223344",
        DHAN_ACCESS_TOKEN="expired-token",
        DHAN_API_BASE="https://dhan.test/v2",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(dhan)) as http:
        service = MarketDataService(settings, http=http, load=no_instruments)
        engine, stop = start(settings, service)

        await until(lambda: (s := service.bus.status) is not None and s.state is FeedState.DOWN)
        await asyncio.sleep(0.2)
        assert not engine.done()
        assert is_healthy(settings.engine_heartbeat_path)
        assert calls == ["/v2/profile"], "one session check, never retried"

        stop.set()
        await asyncio.wait_for(engine, timeout=2)


async def test_slow_instrument_refresh_does_not_stall_ticks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Clock pinned 0.1 s before the 08:45 IST refresh, so the second load starts at once.
    pinned = datetime(2026, 10, 27, 3, 14, 59, 900_000, tzinfo=UTC)
    loads = 0
    refreshing = False

    async def slow_load(_today: date) -> tuple[Iterable[Instrument], date] | None:
        nonlocal loads, refreshing
        loads += 1
        if loads == 2:
            refreshing = True
            await asyncio.to_thread(time.sleep, 0.5)  # a slow parse on a worker thread
            refreshing = False
        return None

    settings = make_settings(monkeypatch, tmp_path)
    service = MarketDataService(settings, load=slow_load, now=lambda: pinned)
    consumer = service.bus.subscribe("test")
    engine, stop = start(settings, service)

    during_refresh = 0
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and loads < 3:
        await next_tick(consumer)
        during_refresh += refreshing
    assert loads >= 2
    assert during_refresh >= 5, "ticks must keep flowing while the refresh parses"
    assert is_healthy(settings.engine_heartbeat_path)

    stop.set()
    await asyncio.wait_for(engine, timeout=2)
