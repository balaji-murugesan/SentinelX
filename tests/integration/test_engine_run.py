"""The wired engine (task 8.4): ``engine.main.run`` against a REAL Redis, and for two tests a real
Postgres. Scenarios: a paper order through the command stream, a snapshot-writer crash that leaves
orders and the heartbeat running, a HALTED start that is shown and cleared only by resume,
state surviving a restart on Postgres, and an engine that stays up with Postgres unreachable.

Since add-risk-engine every order passes the real risk gate, so these tests set limits first and
send entries with an initial stop (``tests.integration.risk_setup``).

Needs TEST_REDIS_URL (database 15); the Postgres tests also need TEST_DATABASE_URL. Skipped
loudly without them locally, never in CI.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from structlog.testing import capture_logs

from cli import orders as cli
from contracts.commands import COMMAND_STREAM, CommandResult, PlaceOrder, reply_stream
from contracts.orders import OrderState
from contracts.settings import load_settings
from engine.config import EngineSettings
from engine.healthcheck import is_healthy
from engine.main import run
from engine.market_data.service import MarketDataService
from engine.orders.snapshot import SnapshotWriter
from engine.persistence.postgres import PostgresRepository
from engine.persistence.repository import RecoveryHalt, TradingRepository
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import NOW, command, master
from tests.engine.test_trading_repository import prepare_database
from tests.engine.trading_fakes import MemoryRepository
from tests.integration.risk_setup import (
    place_when_ready,
    send_ban_list,
    send_kill,
    send_limits,
    with_stop,
)
from tests.integration.test_command_stream import reply_for

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IN_CI = os.environ.get("CI") == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_REDIS_URL and not IN_CI,
        reason="TEST_REDIS_URL not set: no real Redis to test",
    ),
]
needs_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL and not IN_CI, reason="TEST_DATABASE_URL not set: no real Postgres"
)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def settings_for(
    monkeypatch: pytest.MonkeyPatch, heartbeat: Path, database_url: str
) -> EngineSettings:
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL or "redis://localhost:6379/15")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ENGINE_HEARTBEAT_PATH", str(heartbeat))
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_S", "0.05")
    # A price for the test option, so the gate has a quote to judge an entry by.
    monkeypatch.setenv("FEED_INSTRUMENTS", "NSE_FNO:53720")
    monkeypatch.setenv("SIM_INTERVAL_S", "0.05")
    return load_settings(EngineSettings)


class Engine:
    """``run()`` as a background task with a stop event, like the container's main process."""

    def __init__(
        self,
        settings: EngineSettings,
        redis: Redis,
        repository: TradingRepository | None,
        **options: Any,
    ) -> None:
        async def no_instruments(_today: date) -> None:
            return None  # no instrument download in this test

        self.settings = settings
        self.stop = asyncio.Event()
        market_data = MarketDataService(settings, load=no_instruments)
        market_data.master = master()  # the two test instruments, no download
        self.task = asyncio.create_task(
            run(
                settings,
                redis=redis,
                stop=self.stop,
                market_data=market_data,
                repository=repository,
                **options,
            )
        )

    async def shutdown(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=10)


async def send(redis: Redis, cmd: PlaceOrder, wait_s: float = 2) -> CommandResult:
    await redis.xadd(COMMAND_STREAM, {"data": cmd.model_dump_json()})
    if wait_s <= 2:
        return await reply_for(redis, cmd.command_id)
    for _ in range(int(wait_s / 0.05)):  # a slow answer, e.g. Postgres timing out
        entries = await redis.xrange(reply_stream(cmd.command_id))
        if entries:
            return CommandResult.model_validate_json(entries[0][1]["data"])
        await asyncio.sleep(0.05)
    raise AssertionError(f"no reply for {cmd.command_id}")


async def eventually(check: Callable[[], Awaitable[bool]], seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.05)):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


async def snapshot_shows(redis: Redis, predicate: Callable[[Any], bool]) -> bool:
    snapshot = await cli.read_snapshot(redis, "main")
    return snapshot is not None and predicate(snapshot)


async def test_paper_order_goes_through_the_command_stream(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = MemoryRepository()
    engine = Engine(
        settings_for(monkeypatch, tmp_path / "hb", "postgresql+asyncpg://u:p@h/x"),
        redis,
        repo,
        risk_repository=MemoryRiskRepository(),
    )
    try:
        assert (await send_limits(redis)).accepted
        assert (await send_ban_list(redis)).accepted
        # A resting buy well below the simulated price (it starts at 100), so it stays open.
        _, reply = await place_when_ready(redis, lambda k: with_stop(command(k, limit="95"), "85"))
        await eventually(lambda: snapshot_shows(redis, lambda s: len(s.open_orders) == 1))
        # The snapshot carries the engine's risk figures: one entry counted, nothing halted.
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: s.risk is not None and s.risk.entries_today == 1
            )
        )
        snapshot = await cli.read_snapshot(redis, "main")
        assert is_healthy(tmp_path / "hb")
    finally:
        await engine.shutdown()

    assert reply.accepted and reply.order_id
    assert repo.orders[reply.order_id].state is OrderState.OPEN
    assert repo.orders[reply.order_id].paper
    assert snapshot is not None and snapshot.risk is not None
    assert snapshot.risk.halt_level == 0 and snapshot.risk.floor == -10000000


async def test_snapshot_writer_crash_leaves_orders_and_heartbeat_running(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    crashes: list[int] = []

    async def boom(self: SnapshotWriter) -> None:
        crashes.append(1)
        raise RuntimeError("snapshot writer bug")

    monkeypatch.setattr(SnapshotWriter, "write_once", boom)
    repo = MemoryRepository()
    engine = Engine(
        settings_for(monkeypatch, tmp_path / "hb", "postgresql+asyncpg://u:p@h/x"),
        redis,
        repo,
        risk_repository=MemoryRiskRepository(),
        snapshot_restart_s=0.05,
    )
    try:
        await eventually(lambda: _at_least(crashes, 2))  # crashed, restarted, crashed again
        assert (await send_limits(redis)).accepted
        assert (await send_ban_list(redis)).accepted
        _, reply = await place_when_ready(redis, lambda k: with_stop(command(k, limit="95"), "85"))
        assert is_healthy(tmp_path / "hb")
        assert not engine.task.done()
    finally:
        await engine.shutdown()

    assert reply.accepted and reply.order_id in repo.orders


async def _at_least(items: list[int], n: int) -> bool:
    return len(items) >= n


async def test_halted_start_is_shown_and_only_resume_clears_it(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = MemoryRepository()
    repo.halts["main"] = RecoveryHalt(("seeded difference for the test",), NOW)
    engine = Engine(
        settings_for(monkeypatch, tmp_path / "hb", "postgresql+asyncpg://u:p@h/x"),
        redis,
        repo,
        risk_repository=MemoryRiskRepository(),
    )
    with capture_logs() as logs:
        try:
            refused = await send(redis, command("k1", limit="100"))
            await eventually(lambda: snapshot_shows(redis, lambda s: s.halted))
            snapshot = await cli.read_snapshot(redis, "main")
            lines: list[str] = []
            code = await cli.resume_recovery(redis, account_id="main", out=lines.append)
            assert (await send_limits(redis)).accepted
            assert (await send_ban_list(redis)).accepted
            _, accepted = await place_when_ready(
                redis, lambda k: with_stop(command(k, limit="95"), "85")
            )
        finally:
            await engine.shutdown()

    assert not refused.accepted and "not reconciled" in (refused.reason or "")
    assert snapshot is not None and snapshot.halt_differences == ["seeded difference for the test"]
    assert code == cli.ACCEPTED and "main" not in repo.halts
    assert accepted.accepted
    errors = [e["event"] for e in logs if e["log_level"] == "error"]
    assert any("HALTED" in e and "NEW ORDERS REFUSED" in e for e in errors)
    assert any("seeded difference for the test" in e for e in errors)


@needs_postgres
async def test_run_on_real_postgres_keeps_state_across_a_restart(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    settings = settings_for(monkeypatch, tmp_path / "hb", TEST_DATABASE_URL)

    first = Engine(settings, redis, None)  # no injected repository: Postgres from DATABASE_URL
    try:
        assert (await send_limits(redis)).accepted  # stored in Postgres: the restart finds them
        assert (await send_ban_list(redis)).accepted  # and so is the ban list
        key, placed = await place_when_ready(
            redis, lambda k: with_stop(command(k, limit="95"), "85")
        )
    finally:
        await first.shutdown()
    assert placed.accepted and placed.order_id

    check = PostgresRepository(TEST_DATABASE_URL)
    try:
        stored = (await check.load("main")).open_orders
    finally:
        await check.close()
    assert [o.order_id for o in stored] == [placed.order_id]

    await redis.flushdb()  # the Redis snapshot and streams are disposable; Postgres is the truth
    second = Engine(settings, redis, None)
    try:
        again = await send(
            redis,
            with_stop(command(key, limit="95"), "85").model_copy(update={"command_id": "c-again"}),
        )
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: [o.order_id for o in s.open_orders] == [placed.order_id]
            )
        )
        snapshot = await cli.read_snapshot(redis, "main")
    finally:
        await second.shutdown()

    assert again.order_id == placed.order_id  # the original result: no second order
    assert snapshot is not None and not snapshot.halted  # recovery reconciled cleanly


async def test_engine_stays_up_and_refuses_orders_while_postgres_is_unreachable(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = settings_for(
        monkeypatch, tmp_path / "hb", "postgresql+asyncpg://u:p@127.0.0.1:1/x_test"
    )
    engine = Engine(settings, redis, None)
    try:
        reply = await send(redis, command("k1", limit="100"), wait_s=20)
        # The kill must not wait for the database (safety rule 8: it needs only Redis and the
        # engine): it is accepted and answered even though nothing can be stored.
        killed = await send_kill(redis, wait_s=30)
        assert is_healthy(tmp_path / "hb") and not engine.task.done()
    finally:
        await engine.shutdown()

    assert not reply.accepted and "cannot be saved" in (reply.reason or "")
    assert killed.accepted
