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

Every order passes the real risk gate (add-risk-engine), so the test sets limits first and sends
the entry with an initial stop. In market hours the engine also rests a catastrophic stop for the
open position; that stop must survive the kill too, so it is part of the state compared, and the
test never assumes it is absent. The simulated price walk is pinned (seed 1014 stays between 90 and
115 for the length of the test), so neither that stop nor the far limit order below can fire.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime, timedelta
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
from contracts.snapshot import HaltLevel, Snapshot
from engine.healthcheck import is_healthy
from engine.market_data.clock import is_market_hours, utc_now
from engine.persistence.postgres import PostgresRepository
from engine.persistence.repository import LoadedState
from tests.engine.test_order_service import command
from tests.engine.test_trading_repository import prepare_database
from tests.integration.risk_setup import send_kill, send_limits, with_stop
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
FAR_LIMIT = "195"  # on the 5.00 tick grid, inside the price band, above the walk's ceiling of 115
ACCOUNT = "main"

# Dropped from the inherited environment, so nothing in the developer's shell (a Dhan token, a
# live-mode flag, a different feed) can reach the engine this test starts (CLAUDE.md safety rules
# 1 and 11: paper by default, no real broker in tests). This is a blacklist: when a setting with
# a NEW prefix is added to EngineSettings (a Telegram token, a risk or kill setting...), add its
# prefix here so a value in the developer's shell can never reach the child engine.
SCRUBBED = (
    "DHAN_", "FEED_", "SIM_", "PAPER_", "ORDER_", "SNAPSHOT_", "OTEL_", "ENGINE_", "CANDLE",
    "ACCOUNT_", "INSTRUMENT_", "MIGRATE_", "POSTGRES_", "TRADING_MODE", "LIVE_CONFIRM",
    "REDIS_URL", "DATABASE_URL", "RISK_",
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
        SIM_SEED="1014",  # a walk that stays between 90 and 115 for 120 steps (see the docstring)
        SIM_STEP_TICKS="1",
        SIM_INTERVAL_S="2",
        PAPER_PRICE_MAX_AGE_S="30",  # prices arrive every 2 s here
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
        order = with_stop(command(key, instrument=OPTION, quantity=LOT), "90")
        reply = await send(redis, order, wait_s=10)
        if reply.accepted and reply.order_id:
            return key, reply.order_id
        await asyncio.sleep(0.5)
    raise AssertionError(f"market buy was never accepted:\n{engine.log()[-3000:]}")


def far_limit_sell(key: str) -> PlaceOrder:
    """A limit sell far above the price walk: it is accepted and then simply rests."""
    return command(key, instrument=OPTION, side=OrderSide.SELL, quantity=LOT, limit=FAR_LIMIT)


def resting_and_stops(state: LoadedState) -> list[str]:
    """The ids of the open orders that are not the engine's own catastrophic stop. (The stop is
    there in market hours and not outside them; it is compared, with everything else, by
    ``crash_invariant_view``.)"""
    return [
        o.order_id
        for o in state.open_orders
        if o.state is OrderState.OPEN and o.source != "risk-stop"
    ]


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


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Name each field that differs ("snapshot_orders[0].state: 'submitted' -> 'open'").

    pytest truncates the nested dicts, which hides exactly the part that matters."""
    lines: list[str] = []
    for section, old_rows in before.items():
        new_rows = after[section]
        if len(old_rows) != len(new_rows):
            lines.append(f"{section}: {len(old_rows)} rows -> {len(new_rows)} rows")
        for index, (old, new) in enumerate(zip(old_rows, new_rows, strict=False)):
            lines += [
                f"{section}[{index}].{field}: {old[field]!r} -> {new[field]!r}"
                for field in old
                if old[field] != new[field]
            ]
    return lines


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
        assert (await send_limits(redis)).accepted  # stored in Postgres: the restart finds them
        buy_key, buy_order_id = await buy_one_lot(redis, engine)
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: any(p.position.quantity == LOT for p in s.positions)
            ),
            seconds=10,
        )
        resting = await send(redis, far_limit_sell("sell-far"))
        assert resting.accepted and resting.order_id
        # Wait for the order in state OPEN, not just present: the snapshot also lists Requested and
        # Submitted orders and is written without the order lock, so a snapshot taken mid-place
        # can contain the order in an earlier state than the one stored in Postgres.
        await eventually(
            lambda: snapshot_shows(
                redis,
                lambda s: any(
                    o.order_id == resting.order_id and o.state is OrderState.OPEN
                    for o in s.open_orders
                ),
            ),
            seconds=10,
        )

        if is_market_hours(utc_now()):  # the stop manager rests a stop for the open position
            await eventually(
                lambda: snapshot_shows(
                    redis,
                    lambda s: any(
                        o.source == "risk-stop" and o.state is OrderState.OPEN
                        for o in s.open_orders
                    ),
                ),
                seconds=20,
            )
        before_state = await repo.load(ACCOUNT)
        before_snapshot = await cli.read_snapshot(redis, ACCOUNT)
        assert before_snapshot is not None and is_healthy(tmp_path / "heartbeat")
        # Pin the preconditions so the comparison below can never pass on an empty state.
        assert resting_and_stops(before_state) == [resting.order_id]
        assert len(before_state.fills) == 1
        assert [p.quantity for p in before_state.positions] == [LOT]

        # -- the kill: no signal handler, no ``finally``, no goodbye -------------------------
        exit_code = engine.kill()
        assert exit_code != 0
        assert "engine stopped" not in engine.log()  # run()'s ``finally`` never got to run

        # -- second life: same Redis, same Postgres ------------------------------------------
        engine.start()
        # The consumer reads commands only after recovery's FIRST ATTEMPT has ended (even one that
        # failed on a slow Postgres connect), so this answer normally means recovery is done.
        # Same idempotency key, new command id: the ORIGINAL result must come back.
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
        before_view = crash_invariant_view(before_state, before_snapshot)
        after_view = crash_invariant_view(after_state, after_snapshot)
        assert after_view == before_view, "\n".join(changed_fields(before_view, after_view))
        assert not after_snapshot.halted  # recovery found nothing to disagree about
        assert resting_and_stops(after_state) == [resting.order_id]  # the resting limit is rebuilt
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


async def test_kill_minus_9_right_after_a_kill_keeps_the_day_locked_and_resumes_the_flatten(
    redis: Redis, tmp_path: Path
) -> None:
    """Spec "Crash mid-flatten" (kill-switch), with a real process: a kill is answered (the day lock
    is stored before the answer) and the engine is killed at once, wherever its flatten had got:
    the exit not yet placed, resting, or filled. After the restart the day is still locked, an
    entry is refused with RISK_HALTED_DAY, and in market hours the position ends flat (outside them
    nothing can be sold, so the position stays and the kill said so)."""
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    in_market_hours = is_market_hours(utc_now())
    if is_market_hours(utc_now() + timedelta(minutes=5)) != in_market_hours:
        # The open or the close falls inside this run (it takes a minute or two), and the two halves
        # of the assertions below depend on which side of it the kill and the flatten land.
        pytest.skip("too close to the market's open or close for a stable assertion")
    engine = EngineProcess(engine_env(tmp_path), tmp_path / "engine.log")
    repo = PostgresRepository(TEST_DATABASE_URL)
    try:
        engine.start()
        await wait_for_snapshot(redis, engine)
        assert (await send_limits(redis)).accepted
        await buy_one_lot(redis, engine)
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: any(p.position.quantity == LOT for p in s.positions)
            ),
            seconds=10,
        )

        answer = await send_kill(redis)
        assert answer.accepted
        assert in_market_hours or "market is closed" in (answer.reason or "")
        engine.kill()  # no goodbye: the flatten may or may not have gone out

        engine.start()
        await wait_for_snapshot(redis, engine, newer_than=datetime.now(UTC))
        await eventually(
            lambda: snapshot_shows(
                redis,
                lambda s: s.risk is not None and s.risk.halt_level is HaltLevel.KILL,
            ),
            seconds=30,
        )  # the lock survived the crash

        refusal = ""
        for attempt in range(60):  # the gate answers once the monitor has had its first pass
            engine.assert_running()
            entry = await send(
                redis, with_stop(command(f"after-{attempt}", instrument=OPTION, limit="95"), "90"),
                wait_s=10,
            )  # fmt: skip
            assert not entry.accepted  # never accepted on a locked day
            refusal = entry.reason or ""
            if refusal.startswith("RISK_HALTED_DAY"):
                break
            await asyncio.sleep(0.5)
        assert refusal.startswith("RISK_HALTED_DAY"), refusal

        def quantities(s: Snapshot) -> list[int]:
            return [p.position.quantity for p in s.positions]

        if in_market_hours:  # the monitor's next pass restarts (or finishes) the flatten
            await eventually(
                lambda: snapshot_shows(redis, lambda s: set(quantities(s)) <= {0}), seconds=60
            )
        else:  # nothing can be sold outside market hours: the position stays, the lock does too
            after = await cli.read_snapshot(redis, ACCOUNT)
            assert after is not None and quantities(after) == [LOT]
    except BaseException:
        print(f"--- engine log (last 3000 chars) ---\n{engine.log()[-3000:]}")
        raise
    finally:
        engine.kill_if_running()
        await repo.close()
