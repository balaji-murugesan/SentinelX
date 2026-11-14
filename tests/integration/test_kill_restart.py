"""Kill -9 and restart (task 8.5): a REAL engine process, killed without warning, then restarted.

Scenarios: trading-state "Kill -9 and restart" and paper-broker "Restart with a resting limit"
(the later fill of that order is covered at broker level, task 4.3).

Unlike ``test_engine_run.py`` (``run()`` as an in-process asyncio task, stopped politely with an
event), this starts ``python -m engine.main`` as a child process, like the Docker container does,
so it can be killed with no chance to clean up. Positions and a resting limit order are built
through the command stream, the process is killed, a new one starts on the same Redis and
Postgres, and what is stored must come back unchanged.

Needs TEST_REDIS_URL (database 15) AND TEST_DATABASE_URL (a ``*_test`` database). Skipped loudly
without them locally, never in CI.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from shutil import copyfile
from typing import Any

import pytest
from pydantic import BaseModel
from redis.asyncio import Redis

from cli import orders as cli
from contracts.commands import PlaceOrder
from contracts.market import InstrumentKey
from contracts.orders import OrderSide, OrderState, Position
from contracts.snapshot import Snapshot
from engine.healthcheck import is_healthy
from engine.persistence.postgres import PostgresRepository
from engine.persistence.repository import LoadedState
from tests.engine.test_order_service import command
from tests.engine.test_trading_repository import prepare_database
from tests.integration.test_engine_run import eventually, send, snapshot_shows

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IN_CI = os.environ.get("CI") == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (TEST_REDIS_URL and TEST_DATABASE_URL) and not IN_CI,
        reason="TEST_REDIS_URL and TEST_DATABASE_URL must both be set: needs real Redis + Postgres",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
INSTRUMENTS_FIXTURE = ROOT / "tests" / "fixtures" / "dhan" / "instruments_detailed.csv"
OPTION = InstrumentKey.parse("NSE_FNO:40003")  # NIFTY call in the fixture: lot 65, tick 5.00
LOT = 65
FAR_LIMIT = "100000"  # on the 5.00 tick grid, far above anything the simulated walk reaches
ACCOUNT = "main"

# Dropped from the inherited environment, so nothing in the developer's shell (a Dhan token, a
# live-mode flag, a different feed) can reach the engine this test starts (CLAUDE.md safety rules
# 1 and 11: paper by default, no real broker in tests). This is a blacklist: when a setting with
# a NEW prefix is added to EngineSettings (a Telegram token, a risk or kill setting...), add its
# prefix here so a value in the developer's shell can never reach the child engine.
SCRUBBED = (
    "DHAN_", "FEED_", "SIM_", "PAPER_", "ORDER_", "SNAPSHOT_", "OTEL_", "ENGINE_", "CANDLE",
    "ACCOUNT_", "INSTRUMENT_", "MIGRATE_", "POSTGRES_", "TRADING_MODE", "LIVE_CONFIRM",
    "REDIS_URL", "DATABASE_URL",
)  # fmt: skip


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def engine_env(tmp_path: Path) -> dict[str, str]:
    """The child process's environment: the shell's, minus SCRUBBED, plus explicit test values."""
    assert TEST_REDIS_URL and TEST_DATABASE_URL
    cache = tmp_path / "instruments"
    cache.mkdir()
    # The download below is pointed at a closed port, so it fails at once and the engine falls
    # back to the newest cached file: the fixture, with no network.
    copyfile(INSTRUMENTS_FIXTURE, cache / "instruments-2026-11-09.csv")
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED)}
    env.update(
        PYTHONPATH=str(ROOT / "src"),  # pytest finds src/ via pyproject; the child needs it too
        TRADING_MODE="paper",
        LIVE_CONFIRM="",
        REDIS_URL=TEST_REDIS_URL,
        DATABASE_URL=TEST_DATABASE_URL,
        ENGINE_HEARTBEAT_PATH=str(tmp_path / "heartbeat"),
        FEED_SOURCE="simulated",
        FEED_INSTRUMENTS="NSE_FNO:40003",  # the prefill subscription is the paper broker's prices
        SIM_SEED="7",
        SIM_INTERVAL_S="0.1",
        SNAPSHOT_INTERVAL_S="0.1",
        DHAN_INSTRUMENTS_URL="http://127.0.0.1:1/none",
        INSTRUMENT_CACHE_DIR=str(cache),
        OTEL_EXPORTER_OTLP_ENDPOINT="",
    )
    return env


class EngineProcess:
    """``python -m engine.main`` as a child process (C#: ``Process.Start`` with a redirected
    environment). Its output goes to a file so a failing test can show it."""

    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self._env = env
        self._log_path = log_path
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        # ``with`` is a context manager, like C# ``using``: our handle closes at the end of the
        # block, the child keeps its own copy and goes on writing.
        with self._log_path.open("ab") as log:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "engine.main"],
                cwd=ROOT,
                env=self._env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def log(self) -> str:
        return self._log_path.read_text(errors="replace")

    def assert_running(self) -> None:
        assert self._proc is not None
        code = self._proc.poll()
        assert code is None, f"engine exited early (code {code}):\n{self.log()[-3000:]}"

    def kill(self) -> int:
        """SIGKILL on Linux, TerminateProcess on Windows: no signal handler, no ``finally``."""
        assert self._proc is not None
        self._proc.kill()
        return self._proc.wait(timeout=10)

    def kill_if_running(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self.kill()


async def wait_for_snapshot(
    redis: Redis, engine: EngineProcess, *, newer_than: datetime | None = None
) -> Snapshot:
    """Wait for the engine's snapshot in Redis (optionally one taken after ``newer_than``)."""

    async def ready() -> bool:
        engine.assert_running()  # fail with the engine's log if it died, not after a long timeout
        snapshot = await cli.read_snapshot(redis, ACCOUNT)
        return snapshot is not None and (newer_than is None or snapshot.taken_at > newer_than)

    await eventually(ready, seconds=45)  # a cold Python start plus recovery takes a few seconds
    snapshot = await cli.read_snapshot(redis, ACCOUNT)
    assert snapshot is not None
    return snapshot


async def buy_one_lot(redis: Redis, engine: EngineProcess) -> tuple[str, str]:
    """Market-buy one lot; return (idempotency key, order id).

    Paper fills need a fresh price, which only exists once the feed has ticked, so an early
    refusal is retried with a NEW key (a refused key is final: its result is stored).
    """
    for attempt in range(20):
        engine.assert_running()
        key = f"buy-{attempt}"
        # wait_s=10: the consumer starts only after recovery's first attempt (a cold Postgres
        # connect), which can take longer than ``send``'s 2 s default on a slow runner.
        reply = await send(redis, command(key, instrument=OPTION, quantity=LOT), wait_s=10)
        if reply.accepted and reply.order_id:
            return key, reply.order_id
        await asyncio.sleep(0.5)
    raise AssertionError(f"market buy was never accepted:\n{engine.log()[-3000:]}")


def far_limit_sell(key: str) -> PlaceOrder:
    """A limit sell far above the price walk: it is accepted and then simply rests."""
    return command(key, instrument=OPTION, side=OrderSide.SELL, quantity=LOT, limit=FAR_LIMIT)


def crash_invariant_view(state: LoadedState, snapshot: Snapshot) -> dict[str, Any]:
    """The part of the engine's state that a kill -9 must NOT change, as plain comparable data.

    The test builds this twice, from the Postgres rows and the Redis snapshot read just before
    the kill and again after the restart, and requires the two to be equal.

    Available to build it from:
      * ``state.open_orders`` / ``state.fills`` / ``state.positions``: the rows in Postgres
        (``Order``, ``Fill``, ``Position`` in ``contracts.orders``).
      * ``snapshot.open_orders`` and ``snapshot.positions``: what the engine shows clients;
        each ``PositionView`` is a ``Position`` plus ``last_price``, ``mtm`` and ``net_pnl``.
    Not everything is stable: the snapshot's ``taken_at`` and the live figures move with the
    simulated price, and timestamps may be rewritten by recovery.
    """

    def dump(models: Iterable[BaseModel], sort_key: Callable[[Any], str]) -> list[dict[str, Any]]:
        # ``model_dump()`` turns a pydantic model into a plain dict (C#: a projection to an
        # anonymous type); sorting first means Postgres row order can never cause a failure.
        return [m.model_dump() for m in sorted(models, key=sort_key)]

    def position_id(p: Position) -> str:
        return f"{p.key}:{p.product}"

    return {
        # Every stored field, timestamps included: recovery must rewrite nothing it reloads.
        "orders": dump(state.open_orders, lambda o: o.order_id),
        "fills": dump(state.fills, lambda f: f.fill_id),
        "positions": dump(state.positions, position_id),
        # What clients see must agree with the database. Only the stored ``Position`` is taken
        # from each view: ``last_price``, ``mtm`` and ``net_pnl`` move with the simulated price.
        "snapshot_orders": dump(snapshot.open_orders, lambda o: o.order_id),
        "snapshot_positions": dump([v.position for v in snapshot.positions], position_id),
    }


async def test_kill_minus_9_then_restart_keeps_state_and_the_resting_limit(
    redis: Redis, tmp_path: Path
) -> None:
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    engine = EngineProcess(engine_env(tmp_path), tmp_path / "engine.log")
    repo = PostgresRepository(TEST_DATABASE_URL)
    try:
        # -- first life: build an open position and a resting limit order --------------------
        engine.start()
        await wait_for_snapshot(redis, engine)
        buy_key, buy_order_id = await buy_one_lot(redis, engine)
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: any(p.position.quantity == LOT for p in s.positions)
            ),
            seconds=10,
        )
        resting = await send(redis, far_limit_sell("sell-far"))
        assert resting.accepted and resting.order_id
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: any(o.order_id == resting.order_id for o in s.open_orders)
            ),
            seconds=10,
        )

        before_state = await repo.load(ACCOUNT)
        before_snapshot = await cli.read_snapshot(redis, ACCOUNT)
        assert before_snapshot is not None and is_healthy(tmp_path / "heartbeat")
        # Pin the preconditions so the comparison below can never pass on an empty state.
        assert [o.order_id for o in before_state.open_orders] == [resting.order_id]
        assert len(before_state.fills) == 1
        assert [p.quantity for p in before_state.positions] == [LOT]

        # -- the kill: no signal handler, no ``finally``, no goodbye -------------------------
        exit_code = engine.kill()
        assert exit_code != 0
        assert "engine stopped" not in engine.log()  # run()'s ``finally`` never got to run

        # -- second life: same Redis, same Postgres ------------------------------------------
        engine.start()
        # The consumer reads commands only after recovery, so this answer proves recovery is
        # done. Same idempotency key, new command id: the ORIGINAL result must come back.
        again = await send(
            redis,
            command(buy_key, instrument=OPTION, quantity=LOT).model_copy(
                update={"command_id": "c-after-restart"}
            ),
            wait_s=45,
        )
        after_snapshot = await wait_for_snapshot(redis, engine, newer_than=datetime.now(UTC))
        after_state = await repo.load(ACCOUNT)

        assert again.accepted and again.order_id == buy_order_id  # command keys survived
        assert crash_invariant_view(after_state, after_snapshot) == crash_invariant_view(
            before_state, before_snapshot
        )
        assert not after_snapshot.halted  # recovery found nothing to disagree about
        still_open = [o.order_id for o in after_state.open_orders if o.state is OrderState.OPEN]
        assert still_open == [resting.order_id]  # the resting limit order is rebuilt
        again_sell = await send(
            redis,
            far_limit_sell("sell-far").model_copy(update={"command_id": "c-sell-after-restart"}),
            wait_s=10,
        )
        assert again_sell.order_id == resting.order_id  # its command key survived too

        fresh = await send(redis, far_limit_sell("after-restart"), wait_s=10)
        assert fresh.accepted  # commands are processed again
    except BaseException:
        # This test runs in CI, where tmp_path is lost: show the engine's own log on any failure
        # (pytest prints captured output for a failed test).
        print(f"--- engine log (last 3000 chars) ---\n{engine.log()[-3000:]}")
        raise
    finally:
        engine.kill_if_running()
        await repo.close()
