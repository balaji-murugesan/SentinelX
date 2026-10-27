"""DhanFeed against a fake Dhan WebSocket server on 127.0.0.1 (tasks 5.2-5.4).

Spec market-feed: "Configured subscriptions" (subscribe while running), "Subscriptions only on
demand" (duplicate / cap: nothing sent), "Tick content", "Automatic reconnect", "No reconnect
on credential errors", "Silent feed detection", "Feed address never logged".
"""

import asyncio
import json
import logging
import socket
import struct
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from structlog.testing import capture_logs
from websockets.asyncio.server import Request, Response, ServerConnection, serve

from contracts.market import InstrumentKey
from engine.broker.dhan.feed import Backoff, DhanFeed
from engine.broker.dhan.packets import DISCONNECT, HEADER
from engine.market_data.bus import Subscription, TickBus
from engine.market_data.provider import (
    FeedState,
    FeedStatus,
    SubscriptionLimitError,
    Subscriptions,
    Tick,
)
from tests.engine.test_dhan_packets import LTT_UTC, header, quote

TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJzZWNyZXQiOiJ0b2tlbiJ9.c2lnbmF0dXJl"
CLIENT_ID = "1000000001"
OPTION = InstrumentKey.parse("NSE_FNO:52175")
NIFTY = InstrumentKey.parse("IDX_I:13")

Behaviour = Callable[["FakeDhan", ServerConnection, int], Awaitable[None]]


class FakeDhan:
    """Records connections and JSON requests; ``behaviour`` decides what each connection does."""

    def __init__(self, behaviour: Behaviour, reject_with: HTTPStatus | None = None) -> None:
        self.behaviour = behaviour
        self.reject_with = reject_with
        self.connections = 0
        self.paths: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.paths.append(ws.request.path if ws.request else "")
        reader = asyncio.create_task(self._collect(ws))
        try:
            await self.behaviour(self, ws, self.connections)
        finally:
            reader.cancel()

    async def _collect(self, ws: ServerConnection) -> None:
        async for message in ws:
            self.requests.append(json.loads(message))

    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if self.reject_with is not None:
            self.connections += 1
            return connection.respond(self.reject_with, "rejected\n")
        return None


async def until(predicate: Callable[[], bool], limit_s: float = 3.0) -> None:
    """Poll ``predicate`` until true, failing after ``limit_s``. Polling is deliberate here: the
    conditions (server counters, received ticks) have no event to wait on."""

    async def poll() -> None:
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), limit_s)


async def wait_for_subscribe(server: FakeDhan, count: int = 1) -> None:
    await until(lambda: len([r for r in server.requests if r["RequestCode"] == 17]) >= count)


async def stream_one_quote(server: FakeDhan, ws: ServerConnection, _n: int) -> None:
    await wait_for_subscribe(server)
    await ws.send(quote(24500.05))
    await ws.wait_closed()


class Harness:
    def __init__(self, feed: DhanFeed, bus: TickBus, consumer: Subscription) -> None:
        self.feed, self.bus, self.consumer = feed, bus, consumer
        self.items: list[Tick | FeedStatus] = []

    def collect(self) -> list[Tick | FeedStatus]:
        while (item := self.consumer.get_nowait()) is not None:
            self.items.append(item)
        return self.items

    def ticks(self) -> list[Tick]:
        return [i for i in self.collect() if isinstance(i, Tick)]

    def states(self) -> list[FeedStatus]:
        return [i for i in self.collect() if isinstance(i, FeedStatus)]


async def start(
    server: FakeDhan,
    url: str | None = None,
    keys: list[InstrumentKey] | None = None,
    **options: Any,
) -> tuple[Harness, asyncio.Event, asyncio.Task[None], Any]:
    ws_server = await serve(server.handler, "127.0.0.1", 0, process_request=server.process_request)
    port = ws_server.sockets[0].getsockname()[1]
    bus = TickBus()
    subscriptions = Subscriptions(cap=options.pop("cap", 200))
    subscriptions.add(keys if keys is not None else [OPTION], "prefill")
    defaults: dict[str, Any] = {
        "reconnect_initial_s": 0.01,
        "reconnect_max_s": 0.05,
        "silence_s": 5.0,
        "market_hours": lambda _now: False,
    }
    feed = DhanFeed(
        bus,
        subscriptions,
        url=url or f"ws://127.0.0.1:{port}",
        client_id=CLIENT_ID,
        token=TOKEN,
        **{**defaults, **options},
    )
    harness = Harness(feed, bus, bus.subscribe("test"))
    stop = asyncio.Event()
    return harness, stop, asyncio.create_task(feed.run(stop)), ws_server


async def finish(stop: asyncio.Event, task: asyncio.Task[None], ws_server: Any) -> None:
    stop.set()
    await asyncio.wait_for(task, 3)
    ws_server.close()
    await ws_server.wait_closed()


# ---- connect, subscribe, ticks ----------------------------------------------------


async def test_login_query_and_quote_subscription() -> None:
    server = FakeDhan(stream_one_quote)
    h, stop, task, srv = await start(server)
    await until(lambda: len(h.ticks()) == 1)
    query = parse_qs(urlsplit(server.paths[0]).query)
    assert query == {
        "version": ["2"],
        "token": [TOKEN],
        "clientId": [CLIENT_ID],
        "authType": ["2"],
    }
    assert server.requests[0] == {
        "RequestCode": 17,
        "InstrumentCount": 1,
        "InstrumentList": [{"ExchangeSegment": "NSE_FNO", "SecurityId": "52175"}],
    }
    [tick] = h.ticks()
    assert (tick.key, tick.ltp, tick.ltq, tick.volume) == (OPTION, Decimal("24500.05"), 75, 123456)
    assert tick.received_at.tzinfo is not None and tick.exchange_time.tzinfo is not None
    assert [s.state for s in h.states()][-1] is FeedState.CONNECTED
    await finish(stop, task, srv)


async def test_subscribe_batches_of_100() -> None:
    keys = [InstrumentKey.parse(f"NSE_FNO:{n}") for n in range(1, 151)]
    server = FakeDhan(stream_one_quote)
    h, stop, task, srv = await start(server, keys=keys)
    await wait_for_subscribe(server, 2)
    assert [r["InstrumentCount"] for r in server.requests] == [100, 50]
    await finish(stop, task, srv)


async def test_runtime_subscribe_duplicate_and_cap() -> None:
    server = FakeDhan(stream_one_quote)
    h, stop, task, srv = await start(server, cap=2)
    await wait_for_subscribe(server)
    await h.feed.subscribe([NIFTY], "positions")  # new: one message with one instrument
    await until(lambda: len(server.requests) == 2)
    assert server.requests[1]["InstrumentList"] == [
        {"ExchangeSegment": "IDX_I", "SecurityId": "13"}
    ]
    await h.feed.subscribe([NIFTY, OPTION], "other")  # duplicates: nothing sent
    with pytest.raises(SubscriptionLimitError):
        await h.feed.subscribe([InstrumentKey.parse("IDX_I:25")], "x")  # over cap: nothing sent
    await h.feed.unsubscribe([NIFTY], "positions")  # "other" still wants NIFTY: nothing sent
    await h.feed.unsubscribe([NIFTY], "other")  # now nobody: unsubscribe (code 18)
    await until(lambda: len(server.requests) == 3)
    assert server.requests[2]["RequestCode"] == 18
    await finish(stop, task, srv)


async def test_unknown_packet_skipped_and_counted() -> None:
    async def behaviour(server: FakeDhan, ws: ServerConnection, _n: int) -> None:
        await wait_for_subscribe(server)
        await ws.send(HEADER.pack(99, 8, 2, 1))
        await ws.send(quote(10.0))
        await ws.wait_closed()

    h, stop, task, srv = await start(FakeDhan(behaviour))
    await until(lambda: len(h.ticks()) == 1)
    assert h.feed.undecodable == 1
    await finish(stop, task, srv)


async def test_price_rounded_to_instrument_tick() -> None:
    async def behaviour(server: FakeDhan, ws: ServerConnection, _n: int) -> None:
        await wait_for_subscribe(server)
        await ws.send(quote(140000.05))
        await ws.wait_closed()

    h, stop, task, srv = await start(FakeDhan(behaviour), tick_size=lambda _k: Decimal("0.05"))
    await until(lambda: len(h.ticks()) == 1)
    assert h.ticks()[0].ltp == Decimal("140000.05")
    await finish(stop, task, srv)


# ---- reconnect --------------------------------------------------------------------


async def test_forced_disconnect_reconnects_and_resubscribes() -> None:
    async def behaviour(server: FakeDhan, ws: ServerConnection, n: int) -> None:
        await wait_for_subscribe(server, n)
        if n == 1:
            await ws.close()  # simulated network drop
            return
        await ws.send(quote(24501.0))
        await ws.wait_closed()

    server = FakeDhan(behaviour)
    h, stop, task, srv = await start(server, keys=[OPTION, NIFTY])
    await until(lambda: len(h.ticks()) == 1)
    assert server.connections == 2
    subscribes = [r for r in server.requests if r["RequestCode"] == 17]
    assert subscribes[0]["InstrumentList"] == subscribes[1]["InstrumentList"]  # resubscribed
    states = [s.state for s in h.states()]
    assert states.index(FeedState.RECONNECTING) < len(states) - 1
    assert states[-1] is FeedState.CONNECTED
    await finish(stop, task, srv)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_repeated_failures_keep_retrying() -> None:
    server = FakeDhan(stream_one_quote)
    h, stop, task, srv = await start(server, url=f"ws://127.0.0.1:{free_port()}")
    # Windows needs ~2 s to report a refused connection, so allow time for several attempts.
    await until(lambda: len([s for s in h.states() if s.state is FeedState.RECONNECTING]) >= 3, 20)
    assert not task.done()
    await finish(stop, task, srv)


def test_backoff_grows_to_cap_with_jitter_and_resets() -> None:
    backoff = Backoff(1.0, 8.0)
    delays = [backoff.next_delay() for _ in range(6)]
    for delay, base in zip(delays, [1, 2, 4, 8, 8, 8], strict=True):
        assert base * 0.8 <= delay <= base * 1.2
    backoff.reset()
    assert backoff.next_delay() <= 1.2
    backoff.jump_to_max()
    assert backoff.next_delay() >= 8 * 0.8


async def test_too_many_connections_backs_off_to_max() -> None:
    async def behaviour(server: FakeDhan, ws: ServerConnection, n: int) -> None:
        await wait_for_subscribe(server, n)
        await ws.send(header(DISCONNECT, 0, 0) + struct.pack("<h", 805))
        await ws.wait_closed()

    with capture_logs() as logs:
        h, stop, task, srv = await start(FakeDhan(behaviour), reconnect_max_s=0.2)
        await until(lambda: any(r["event"].endswith("reconnecting") for r in logs))
        await finish(stop, task, srv)
    retry = next(r["retry_in"] for r in logs if r["event"].endswith("reconnecting"))
    assert retry >= 0.2 * 0.8


# ---- credential errors: stop for good ---------------------------------------------


async def test_token_expired_mid_session_stops() -> None:
    async def behaviour(server: FakeDhan, ws: ServerConnection, _n: int) -> None:
        await wait_for_subscribe(server)
        await ws.send(header(DISCONNECT, 0, 0) + struct.pack("<h", 807))
        await ws.wait_closed()

    server = FakeDhan(behaviour)
    h, stop, task, srv = await start(server)
    await asyncio.wait_for(task, 3)  # run() returns by itself
    await asyncio.sleep(0.1)
    assert server.connections == 1  # never retried
    down = h.states()[-1]
    assert down.state is FeedState.DOWN
    assert "807" in down.reason and "expired" in down.reason
    await finish(stop, task, srv)


@pytest.mark.parametrize("status", [HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN])
async def test_login_rejected_stops(status: HTTPStatus) -> None:
    server = FakeDhan(stream_one_quote, reject_with=status)
    h, stop, task, srv = await start(server)
    await asyncio.wait_for(task, 3)
    assert server.connections == 1
    assert h.states()[-1].state is FeedState.DOWN
    await finish(stop, task, srv)


# ---- silence watchdog -------------------------------------------------------------


async def quiet(server: FakeDhan, ws: ServerConnection, n: int) -> None:
    await wait_for_subscribe(server, n)
    await ws.wait_closed()


async def test_silent_during_market_hours_reconnects() -> None:
    server = FakeDhan(quiet)
    h, stop, task, srv = await start(server, silence_s=0.1, market_hours=lambda _now: True)
    await until(lambda: server.connections >= 2)
    assert any("silent feed" in s.reason for s in h.states() if s.state is FeedState.RECONNECTING)
    await finish(stop, task, srv)


async def test_silent_after_hours_no_reconnect() -> None:
    server = FakeDhan(quiet)
    h, stop, task, srv = await start(server, silence_s=0.1, market_hours=lambda _now: False)
    await wait_for_subscribe(server)
    await asyncio.sleep(0.4)
    assert server.connections == 1
    await finish(stop, task, srv)


# ---- LTT time-zone guard (packets.LTT_IS_IST_EPOCH, smoke test 7.2) -----------------


async def stream_two_quotes(server: FakeDhan, ws: ServerConnection, _n: int) -> None:
    await wait_for_subscribe(server)
    await ws.send(quote(24500.05))
    await ws.send(quote(24500.10))
    await ws.wait_closed()


def ltt_alarms(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in logs if "LTT_IS_IST_EPOCH" in r["event"]]


async def test_live_tick_5h30_off_logs_ltt_error_once() -> None:
    """What a wrong LTT_IS_IST_EPOCH looks like live: receive time 5h30m after exchange time."""
    received = LTT_UTC + timedelta(hours=5, minutes=30, seconds=1)
    with capture_logs() as logs:
        server = FakeDhan(stream_two_quotes)
        h, stop, task, srv = await start(
            server, market_hours=lambda _now: True, now=lambda: received
        )
        await until(lambda: len(h.ticks()) == 2)
        await finish(stop, task, srv)
    [alarm] = ltt_alarms(logs)  # loud, but only once per run
    assert alarm["log_level"] == "error"
    assert alarm["lag_s"] == 19801


@pytest.mark.parametrize(
    ("received", "market_open"),
    [
        (LTT_UTC + timedelta(milliseconds=300), True),  # correct constant: normal lag
        (LTT_UTC + timedelta(hours=5, minutes=30), False),  # after hours: lag is meaningless
    ],
)
async def test_no_ltt_error_for_normal_lag_or_after_hours(
    received: datetime, market_open: bool
) -> None:
    with capture_logs() as logs:
        server = FakeDhan(stream_two_quotes)
        h, stop, task, srv = await start(
            server, market_hours=lambda _now: market_open, now=lambda: received
        )
        await until(lambda: len(h.ticks()) == 2)
        await finish(stop, task, srv)
    assert ltt_alarms(logs) == []


# ---- the token never reaches logs -------------------------------------------------


async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        h, stop, task, srv = await start(
            FakeDhan(stream_one_quote), url=f"ws://127.0.0.1:{free_port()}"
        )
        await until(lambda: len(h.states()) >= 3, 20)
        await finish(stop, task, srv)
    text = caplog.text + json.dumps(logs, default=str) + repr(h.states())
    assert TOKEN not in text
    assert "token=" not in text


async def test_clean_stop_logs_no_disconnect() -> None:
    server = FakeDhan(quiet)
    with capture_logs() as logs:
        h, stop, task, srv = await start(server)
        await wait_for_subscribe(server)
        await finish(stop, task, srv)
    assert not any("reconnecting" in r["event"] for r in logs)
