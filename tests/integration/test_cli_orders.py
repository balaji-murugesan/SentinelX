"""CLI order commands against a REAL Redis (task 7.3; order-commands "CLI order round trip" and
"Engine not answering"). Needs TEST_REDIS_URL (database 15); skipped loudly without it."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from cli import orders as cli
from cli.__main__ import main
from contracts.commands import COMMAND_STREAM, CommandResult, ResumeRecovery
from contracts.orders import OrderSide, OrderState, ProductType
from engine.broker.adapter import BrokerOrder
from engine.broker.paper import PaperBroker
from engine.market_data.bus import TickBus
from engine.orders.commands import CommandConsumer
from engine.orders.recovery import recover, resume_recovery
from engine.orders.snapshot import SnapshotWriter
from tests.engine.test_order_service import NOW, OPTION, build, command, master
from tests.engine.test_recovery import BookAdapter
from tests.engine.test_snapshot import tick

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


async def test_cli_order_round_trip_shows_the_engines_mtm(redis: Redis) -> None:
    bus = TickBus()
    broker = PaperBroker(
        bus, master().get, slippage_ticks=1, max_price_age=timedelta(seconds=5), now=lambda: NOW
    )
    service, _, _ = build(adapter=broker)
    consumer = CommandConsumer(redis, service, account_id="main")
    snapshots = SnapshotWriter(
        redis, service, bus.subscribe("snapshot"), account_id="main", interval_s=0.05
    )
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(broker.run(stop)),
        asyncio.create_task(service.run_updates()),
        asyncio.create_task(consumer.run(stop, block_ms=50)),
        asyncio.create_task(snapshots.run(stop)),
    ]
    lines: list[str] = []
    try:
        bus.publish(tick(OPTION, "100"))
        await asyncio.sleep(0.1)  # let the paper broker record the price

        code = await cli.place_order(
            redis, account_id="main", key=OPTION, side=OrderSide.BUY, quantity=65,
            product=ProductType.INTRADAY, limit_price=None, out=lines.append,
        )  # fmt: skip
        assert code == 0 and lines[0].startswith("Accepted. order id: ")

        bus.publish(tick(OPTION, "102"))
        shown: list[str] = []
        for _ in range(100):
            shown.clear()
            await cli.show_positions(redis, account_id="main", out=shown.append)
            if any("MTM" in line and "last 102" in line for line in shown):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        tasks[1].cancel()  # run_updates has no stop event; the engine cancels it the same way
        await asyncio.gather(*tasks, return_exceptions=True)

    (row,) = [line for line in shown if "qty" in line]
    assert "qty     65" in row and "last 102.00" in row
    # Paper buy fills one tick (0.05) adverse: average 100.05, so MTM = (102 - 100.05) x 65.
    assert f"MTM {Decimal('126.75'):.2f}" in row


async def test_no_reply_means_result_unknown_and_nothing_is_resent(redis: Redis) -> None:
    lines: list[str] = []

    code = await cli.place_order(
        redis, account_id="main", key=OPTION, side=OrderSide.BUY, quantity=65,
        product=ProductType.INTRADAY, limit_price=None, out=lines.append, wait_s=0.3,
    )  # fmt: skip

    assert code == cli.UNKNOWN
    assert "Result unknown" in lines[0] and "NOT resent" in lines[0]
    assert await redis.xlen(COMMAND_STREAM) == 1  # sent exactly once


async def test_orders_without_a_snapshot_says_so(redis: Redis) -> None:
    lines: list[str] = []
    assert await cli.show_orders(redis, account_id="main", out=lines.append) == cli.UNKNOWN
    assert "No snapshot" in lines[0]


def test_help_lists_the_order_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for name in ("order", "orders", "positions"):
        assert name in text
    with pytest.raises(SystemExit):
        main(["order", "--help"])
    out = capsys.readouterr().out
    assert "place" in out and "cancel" in out


async def test_halted_snapshot_shows_a_halted_block_and_resume_clears_it(redis: Redis) -> None:
    service, _, repo = build()
    placed = await service.place(command("k1", limit="100"))
    adapter = BookAdapter()
    restarted, _, _ = build(adapter=adapter, repository=repo)
    await recover(restarted, repo, adapter, account_id="main")  # empty broker book: HALTED

    async def resume(cmd: ResumeRecovery) -> CommandResult:
        return await resume_recovery(restarted, repo, adapter, cmd, account_id="main")

    bus = TickBus()
    consumer = CommandConsumer(redis, restarted, account_id="main", resume=resume)
    snapshots = SnapshotWriter(
        redis, restarted, bus.subscribe("snapshot"), account_id="main", interval_s=0.05
    )
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(consumer.run(stop, block_ms=50)),
        asyncio.create_task(snapshots.run(stop)),
    ]
    shown: list[str] = []
    try:
        for _ in range(100):  # wait for the first snapshot
            shown.clear()
            if await cli.show_orders(redis, account_id="main", out=shown.append) == cli.ACCEPTED:
                break
            await asyncio.sleep(0.05)
        assert any("HALTED" in line for line in shown)
        assert any("not at the broker" in line for line in shown)
        assert any("recovery resume" in line for line in shown)

        lines: list[str] = []
        refused = await cli.resume_recovery(redis, account_id="main", out=lines.append)
        assert refused == cli.REJECTED and "still halted" in lines[0]

        adapter.entries = [BrokerOrder("b-1", placed.order_id or "", OrderState.OPEN, 0, None)]
        lines.clear()
        accepted = await cli.resume_recovery(redis, account_id="main", out=lines.append)
        assert accepted == cli.ACCEPTED and lines == ["Accepted."]
        for _ in range(100):
            shown.clear()
            await cli.show_orders(redis, account_id="main", out=shown.append)
            if not any("HALTED" in line for line in shown):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert not any("HALTED" in line for line in shown)


def test_help_lists_recovery_resume(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["recovery", "--help"])
    assert "resume" in capsys.readouterr().out
