"""Command stream against a REAL Redis (task 7.1; order-commands spec).

Scenarios: "Place command answered", "Same key twice", "Malformed command", "Other account"
and "Crash between persist and acknowledge". Needs TEST_REDIS_URL (database 15), like the
diagnostics round trip; skipped loudly without it, never in CI.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import pytest
from redis.asyncio import Redis

from contracts.commands import (
    COMMAND_GROUP,
    COMMAND_STREAM,
    REPLY_TTL_SECONDS,
    CommandResult,
    PlaceOrder,
    reply_stream,
)
from engine.orders.commands import CONSUMER, CommandConsumer
from engine.orders.service import OrderService
from tests.engine.test_order_service import build, command
from tests.engine.trading_fakes import MemoryRepository

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_REDIS_URL and os.environ.get("CI") != "true",
        reason="TEST_REDIS_URL not set: no real Redis to test",
    ),
]


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def send(redis: Redis, cmd: PlaceOrder) -> None:
    await redis.xadd(COMMAND_STREAM, {"data": cmd.model_dump_json()})


async def reply_for(redis: Redis, command_id: str) -> CommandResult:
    """Wait for the reply stream of one command and parse it."""
    for _ in range(100):
        entries = await cast("Any", redis.xrange(reply_stream(command_id)))
        if entries:
            return CommandResult.model_validate_json(entries[0][1]["data"])
        await asyncio.sleep(0.02)
    raise AssertionError(f"no reply for {command_id}")


async def pending_count(redis: Redis) -> int:
    summary = await cast("Any", redis.xpending(COMMAND_STREAM, COMMAND_GROUP))
    return int(summary["pending"])


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(100):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


async def run_consumer(
    redis: Redis, service: OrderService
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    consumer = CommandConsumer(redis, service, account_id="main")
    return stop, asyncio.create_task(consumer.run(stop, block_ms=50))


async def stop_consumer(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_place_command_is_answered_once_and_acknowledged(redis: Redis) -> None:
    service, adapter, repo = build()
    stop, task = await run_consumer(redis, service)
    try:
        await send(redis, command("k1"))
        reply = await reply_for(redis, "c-k1")
        await eventually(lambda: _no_pending(redis))
    finally:
        await stop_consumer(stop, task)

    assert reply.accepted and reply.order_id in repo.orders
    assert len(adapter.placed) == 1
    assert 0 < await redis.ttl(reply_stream("c-k1")) <= REPLY_TTL_SECONDS


async def _no_pending(redis: Redis) -> bool:
    return await pending_count(redis) == 0


async def test_same_key_twice_makes_one_order_and_two_equal_answers(redis: Redis) -> None:
    service, adapter, repo = build()
    first = command("dup")
    second = first.model_copy(update={"command_id": "c-dup-again"})  # same key, new command id
    stop, task = await run_consumer(redis, service)
    try:
        await send(redis, first)
        await send(redis, second)
        one = await reply_for(redis, "c-dup")
        two = await reply_for(redis, "c-dup-again")
    finally:
        await stop_consumer(stop, task)

    assert one.accepted and two.order_id == one.order_id
    assert len(repo.orders) == 1 and len(adapter.placed) == 1


async def test_malformed_command_is_skipped_and_the_consumer_keeps_reading(redis: Redis) -> None:
    service, adapter, repo = build()
    stop, task = await run_consumer(redis, service)
    try:
        await redis.xadd(COMMAND_STREAM, {"data": "not even json"})
        await redis.xadd(COMMAND_STREAM, {"other": "field"})
        await redis.xadd(COMMAND_STREAM, {"data": '{"command": "place_order"}'})  # invalid model
        await send(redis, command("after"))
        reply = await reply_for(redis, "c-after")
        await eventually(lambda: _no_pending(redis))  # poison entries were acknowledged
    finally:
        await stop_consumer(stop, task)

    assert reply.accepted
    assert len(repo.orders) == 1 and len(adapter.placed) == 1


async def test_command_for_another_account_is_rejected_without_an_order(redis: Redis) -> None:
    service, adapter, repo = build()
    foreign = command("other").model_copy(update={"account_id": "someone-else"})
    stop, task = await run_consumer(redis, service)
    try:
        await send(redis, foreign)
        reply = await reply_for(redis, "c-other")
    finally:
        await stop_consumer(stop, task)

    assert not reply.accepted
    assert reply.reason is not None and "someone-else" in reply.reason
    assert not repo.orders and not adapter.placed and not repo.results


async def test_crash_between_persist_and_acknowledge_replies_again_without_a_second_order(
    redis: Redis,
) -> None:
    repo = MemoryRepository()  # survives the "crash", like the Postgres database would
    before, adapter, _ = build(repository=repo)
    cmd = command("crash")

    # The first engine reads the command and persists the order, then is killed before XACK.
    await send(redis, cmd)
    await redis.xgroup_create(COMMAND_STREAM, COMMAND_GROUP, id="0", mkstream=True)
    delivered = await cast(
        "Any", redis.xreadgroup(COMMAND_GROUP, CONSUMER, {COMMAND_STREAM: ">"}, count=1)
    )
    assert delivered, "the command was delivered to the first engine"
    stored = await before.place(cmd)
    assert await pending_count(redis) == 1 and not await redis.exists(reply_stream("c-crash"))

    # A fresh engine (new OrderService, same database) restarts and reads its pending entries.
    after, adapter_after, _ = build(repository=repo)
    stop, task = await run_consumer(redis, after)
    try:
        reply = await reply_for(redis, "c-crash")
        await eventually(lambda: _no_pending(redis))
    finally:
        await stop_consumer(stop, task)

    assert reply.order_id == stored.order_id
    assert len(repo.orders) == 1
    assert len(adapter.placed) == 1 and not adapter_after.placed
