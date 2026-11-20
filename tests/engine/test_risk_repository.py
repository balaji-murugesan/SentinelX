"""RiskRepository contract suite (add-risk-engine task 2.3; risk-gate, risk-limits, equity-floor).

Every test runs against the in-memory fake and, when TEST_DATABASE_URL points at a real Postgres
(CI provides one), against PostgresRiskRepository on a freshly migrated schema. Scenarios:
equity-floor "Out-of-order write", "Restart after a high peak" (at repository level), risk-limits
"Limits survive restart", and risk-gate "Why didn't it trade" (the audit record is stored).
"""

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from contracts.snapshot import HaltLevel
from engine.persistence import tables as t
from engine.persistence.migrate import alembic_config
from engine.persistence.postgres_risk import PostgresRiskRepository
from engine.persistence.repository import PersistenceError
from engine.risk.repository import RiskEvent, RiskRepository
from engine.risk.state import DayState
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_trading_repository import (
    IN_CI,
    TEST_DATABASE_URL,
    migrate,
    prepare_database,
)

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
TODAY = date(2026, 11, 16)


@dataclass
class Harness:
    repo: RiskRepository
    events: Callable[[], Awaitable[list[RiskEvent]]]  # reads the audit back, whichever the store


@pytest.fixture(params=["memory", "postgres"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    if request.param == "memory":
        memory = MemoryRiskRepository()

        async def memory_events() -> list[RiskEvent]:
            return list(memory.events)

        yield Harness(memory, memory_events)
        return
    if not TEST_DATABASE_URL:
        if IN_CI:
            pytest.fail("TEST_DATABASE_URL must be set in CI (a Postgres service is provided)")
        pytest.skip("TEST_DATABASE_URL not set: no real Postgres to test")
    await prepare_database(TEST_DATABASE_URL)
    # A roomier bound than the production default (3 s): the concurrency test queues 40 writers
    # behind one row lock, and a slow runner must fail on a lost update, not on a timeout.
    postgres = PostgresRiskRepository(TEST_DATABASE_URL, timeout_s=10)
    reader = create_async_engine(TEST_DATABASE_URL)

    async def postgres_events() -> list[RiskEvent]:
        async with reader.connect() as conn:
            rows = (
                (await conn.execute(sa.select(t.risk_event).order_by(t.risk_event.c.event_id)))
                .mappings()
                .all()
            )
        return [
            RiskEvent(
                at=r["at"],
                account_id=r["account_id"],
                rule=r["rule"],
                phase=r["phase"],
                decision=r["decision"],
                enforced=r["enforced"],
                latency_us=r["latency_us"],
                reason_code=r["reason_code"],
                inputs=dict(r["inputs"]),
            )  # fmt: skip
            for r in rows
        ]

    yield Harness(postgres, postgres_events)
    await postgres.close()
    await reader.dispose()


def day(**overrides: object) -> DayState:
    values: dict[str, object] = {"account_id": "main", "trade_date": TODAY, "updated_at": NOW}
    return DayState(**{**values, **overrides})  # type: ignore[arg-type]


# ---- limits -------------------------------------------------------------------------------


async def test_no_limits_until_they_are_saved(harness: Harness) -> None:
    assert await harness.repo.load_limits("main") is None


async def test_limits_round_trip_with_every_value_kind_and_survive_a_reload(
    harness: Harness,
) -> None:
    limits = {
        "per_trade_max_risk": "1000.00",  # money travels as text
        "max_trades_per_day": 10,
        "trail_enabled": True,
        "lock_at": None,  # a term that is not configured
        "mode.per_trade_risk": "shadow",
    }
    assert await harness.repo.save_limits("main", limits, NOW) == 1

    stored = await harness.repo.load_limits("main")  # "Limits survive restart"
    assert stored is not None
    assert stored.limits == limits and stored.version == 1 and stored.updated_at == NOW
    assert stored.limits["per_trade_max_risk"] == "1000.00"  # still text, never a float


async def test_saving_again_replaces_the_limits_and_raises_the_version(harness: Harness) -> None:
    await harness.repo.save_limits("main", {"max_trades_per_day": 10, "lock_at": "200"}, NOW)
    later = NOW + timedelta(hours=1)

    assert await harness.repo.save_limits("main", {"max_trades_per_day": 8}, later) == 2
    assert await harness.repo.save_limits("main", {"max_trades_per_day": 6}, later) == 3

    stored = await harness.repo.load_limits("main")
    assert stored is not None
    assert stored.limits == {"max_trades_per_day": 6}  # replaced, not merged
    assert stored.version == 3 and stored.updated_at == later


async def test_limits_are_per_account(harness: Harness) -> None:
    await harness.repo.save_limits("main", {"max_trades_per_day": 10}, NOW)

    assert await harness.repo.load_limits("other") is None
    assert await harness.repo.save_limits("other", {"max_trades_per_day": 3}, NOW) == 1
    stored = await harness.repo.load_limits("main")
    assert stored is not None and stored.limits == {"max_trades_per_day": 10}


# ---- day state ----------------------------------------------------------------------------


async def test_no_day_state_until_it_is_saved(harness: Harness) -> None:
    assert await harness.repo.load_day("main", TODAY) is None


async def test_first_save_is_stored_exactly_and_returned(harness: Harness) -> None:
    first = day(
        peak=Decimal("1200.50"), floor=Decimal("-1500"), halt_level=HaltLevel.KILL,
        halt_reason="equity floor breached", entries_today=4, loss_streak=2,
        cooldown_until=NOW + timedelta(minutes=30),
        close_marks={"NSE_FNO:53720|INTRADAY": "201.35"},
    )  # fmt: skip

    returned = await harness.repo.save_day(first)

    assert returned == first
    assert await harness.repo.load_day("main", TODAY) == first
    stored = await harness.repo.load_day("main", TODAY)
    assert stored is not None and stored.peak == Decimal("1200.50")  # exact decimals


async def test_exact_decimals_survive_in_the_peak_and_floor(harness: Harness) -> None:
    third = Decimal(1) / Decimal(3)  # 0.3333... must not be rounded to 2 places
    await harness.repo.save_day(day(peak=third, floor=-third))

    stored = await harness.repo.load_day("main", TODAY)
    assert stored is not None and stored.peak == third and stored.floor == -third


async def test_a_delayed_lower_floor_changes_nothing(harness: Harness) -> None:
    """Scenario "Out-of-order write"."""
    await harness.repo.save_day(day(floor=Decimal("-1500"), peak=Decimal("3000")))

    returned = await harness.repo.save_day(day(floor=Decimal("-3000"), peak=Decimal("100")))

    assert returned.floor == Decimal("-1500") and returned.peak == Decimal("3000")
    stored = await harness.repo.load_day("main", TODAY)
    assert stored is not None
    assert stored.floor == Decimal("-1500") and stored.peak == Decimal("3000")


async def test_a_higher_floor_and_peak_are_taken(harness: Harness) -> None:
    await harness.repo.save_day(day(floor=Decimal("-3000"), peak=Decimal("0")))

    returned = await harness.repo.save_day(day(floor=Decimal("-1500"), peak=Decimal("3000")))

    assert returned.floor == Decimal("-1500") and returned.peak == Decimal("3000")


async def test_an_unknown_floor_never_erases_a_known_one(harness: Harness) -> None:
    await harness.repo.save_day(day(floor=Decimal("-1500")))

    returned = await harness.repo.save_day(day(floor=None))

    assert returned.floor == Decimal("-1500")


async def test_a_floor_arriving_after_none_is_stored(harness: Harness) -> None:
    await harness.repo.save_day(day(floor=None))

    returned = await harness.repo.save_day(day(floor=Decimal("-3000")))

    assert returned.floor == Decimal("-3000")


async def test_halt_level_never_decreases_and_keeps_the_higher_reason(harness: Harness) -> None:
    await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason="equity floor breached"))

    stale = await harness.repo.save_day(
        day(halt_level=HaltLevel.MTM_BLIND, halt_reason="mtm blind")
    )
    cleared = await harness.repo.save_day(day(halt_level=HaltLevel.NONE, halt_reason=None))

    assert stale.halt_level is HaltLevel.KILL and stale.halt_reason == "equity floor breached"
    assert cleared.halt_level is HaltLevel.KILL and cleared.halt_reason == "equity floor breached"


async def test_a_higher_halt_replaces_the_level_and_the_reason(harness: Harness) -> None:
    await harness.repo.save_day(day(halt_level=HaltLevel.MTM_BLIND, halt_reason="mtm blind"))

    returned = await harness.repo.save_day(
        day(halt_level=HaltLevel.KILL, halt_reason="kill switch")
    )

    assert returned.halt_level is HaltLevel.KILL and returned.halt_reason == "kill switch"


async def test_a_repeat_at_the_same_level_without_a_reason_keeps_the_reason(
    harness: Harness,
) -> None:
    await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason="kill switch"))

    returned = await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason=None))

    assert returned.halt_level is HaltLevel.KILL and returned.halt_reason == "kill switch"


async def test_a_repeat_at_the_same_level_may_replace_the_reason(harness: Harness) -> None:
    await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason="a"))

    returned = await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason="b"))

    assert returned.halt_level is HaltLevel.KILL and returned.halt_reason == "b"


async def test_a_write_at_the_same_level_supplies_a_missing_reason(harness: Harness) -> None:
    await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason=None))

    returned = await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason="late"))

    assert returned.halt_reason == "late"


async def test_a_higher_level_without_a_reason_does_not_keep_the_old_label(
    harness: Harness,
) -> None:
    """A wrong label is worse than none: KILL must never be shown as "mtm blind"."""
    await harness.repo.save_day(day(halt_level=HaltLevel.MTM_BLIND, halt_reason="mtm blind"))

    returned = await harness.repo.save_day(day(halt_level=HaltLevel.KILL, halt_reason=None))

    assert returned.halt_level is HaltLevel.KILL and returned.halt_reason is None


async def test_entries_today_and_cooldown_never_decrease(harness: Harness) -> None:
    ends = NOW + timedelta(minutes=30)
    await harness.repo.save_day(day(entries_today=7, cooldown_until=ends))

    stale = await harness.repo.save_day(
        day(entries_today=3, cooldown_until=NOW + timedelta(minutes=5))
    )
    unset = await harness.repo.save_day(day(entries_today=0, cooldown_until=None))

    assert stale.entries_today == 7 and stale.cooldown_until == ends
    assert unset.entries_today == 7 and unset.cooldown_until == ends


async def test_a_later_cooldown_end_is_taken(harness: Harness) -> None:
    await harness.repo.save_day(day(cooldown_until=NOW + timedelta(minutes=5)))
    later = NOW + timedelta(minutes=30)

    returned = await harness.repo.save_day(day(cooldown_until=later))

    assert returned.cooldown_until == later


async def test_loss_streak_is_the_one_field_a_win_may_lower(harness: Harness) -> None:
    await harness.repo.save_day(day(loss_streak=2))

    returned = await harness.repo.save_day(day(loss_streak=0))

    assert returned.loss_streak == 0


async def test_close_marks_keep_the_first_marks_written(harness: Harness) -> None:
    first = {"NSE_FNO:53720|INTRADAY": "201.35"}
    await harness.repo.save_day(day())  # no marks yet
    await harness.repo.save_day(day(close_marks=first))

    later = await harness.repo.save_day(day(close_marks={"NSE_FNO:53720|INTRADAY": "150.00"}))
    none = await harness.repo.save_day(day(close_marks=None))

    assert later.close_marks == first and none.close_marks == first


async def test_each_date_and_account_has_its_own_row(harness: Harness) -> None:
    tomorrow = TODAY + timedelta(days=1)
    await harness.repo.save_day(day(peak=Decimal("500"), halt_level=HaltLevel.KILL))

    # The next day starts fresh: a kill does not carry over (the "post-close reset").
    assert await harness.repo.load_day("main", tomorrow) is None
    fresh = await harness.repo.save_day(day(trade_date=tomorrow))
    assert fresh.halt_level is HaltLevel.NONE and fresh.peak == Decimal("0")
    # Another account is untouched.
    assert await harness.repo.load_day("other", TODAY) is None
    other = await harness.repo.save_day(day(account_id="other"))
    assert other.halt_level is HaltLevel.NONE
    original = await harness.repo.load_day("main", TODAY)
    assert original is not None and original.halt_level is HaltLevel.KILL


async def test_updated_at_is_the_latest_write(harness: Harness) -> None:
    await harness.repo.save_day(day())
    later = NOW + timedelta(seconds=5)

    returned = await harness.repo.save_day(replace(day(), updated_at=later))

    assert returned.updated_at == later


async def test_concurrent_writes_never_lower_a_rising_column(harness: Harness) -> None:
    """``save_day`` is one atomic statement: whatever the interleaving, nothing rising is lost.

    A read-modify-write (load, merge, overwrite) would lose updates when two writes overlap, so
    this fires 40 distinct writes at once, in a different shuffled order each round, and checks
    the stored row holds the largest of every rising column.
    """
    levels = list(HaltLevel)
    for round_number in range(3):
        trade_date = TODAY + timedelta(days=round_number)  # a fresh row each round
        writes = [
            day(
                trade_date=trade_date,
                peak=Decimal(i), floor=Decimal(i - 100), entries_today=i,
                halt_level=levels[i % len(levels)], halt_reason=f"reason {i}",
                cooldown_until=NOW + timedelta(minutes=i), loss_streak=i % 4,
            )
            for i in range(40)
        ]  # fmt: skip
        random.Random(round_number).shuffle(writes)

        await asyncio.gather(*(harness.repo.save_day(w) for w in writes))

        stored = await harness.repo.load_day("main", trade_date)
        assert stored is not None
        assert stored.peak == Decimal(39) and stored.floor == Decimal(-61)
        assert stored.entries_today == 39
        assert stored.halt_level is max(levels)
        assert stored.cooldown_until == NOW + timedelta(minutes=39)


# ---- audit --------------------------------------------------------------------------------


async def test_audit_events_are_stored_in_order_with_every_field(harness: Harness) -> None:
    """Scenario "Why didn't it trade": the rule, the code, the inputs and the limit survive."""
    rejected = RiskEvent(
        at=NOW, account_id="main", rule="per_trade_risk", phase="pre_trade", decision="REJECT",
        enforced=True, latency_us=87, reason_code="RISK_PER_TRADE",
        inputs={"risk": "1300.00", "limit": "1000.00", "order_id": "o1"},
    )  # fmt: skip
    verdict = RiskEvent(
        at=NOW + timedelta(seconds=1), account_id="main", rule="gate", phase="verdict",
        decision="ALLOW", enforced=True, latency_us=240,
    )  # fmt: skip
    shadow = RiskEvent(
        at=NOW + timedelta(seconds=2), account_id="main", rule="entry_cutoff", phase="pre_trade",
        decision="REJECT", enforced=False, latency_us=12, reason_code="RISK_ENTRY_CUTOFF",
        inputs={"cutoff": "15:00", "shadow": True, "note": None},
    )  # fmt: skip

    for event in (rejected, verdict, shadow):
        await harness.repo.append_event(event)

    assert await harness.events() == [rejected, verdict, shadow]


# ---- failure ------------------------------------------------------------------------------

# A refused or failed database call must say only what kind of failure it was: never a driver
# message, which can echo the connection string (CLAUDE.md safety rule 10).
ONLY_THE_TYPE = re.compile(r"database write failed \(\w+\)")


async def test_unreachable_database_raises_persistence_error_naming_only_the_type() -> None:
    repo = PostgresRiskRepository("postgresql+asyncpg://u:p@127.0.0.1:1/x_test", timeout_s=2)
    for call in (
        lambda: repo.load_limits("main"),
        lambda: repo.save_limits("main", {"a": 1}, NOW),
        lambda: repo.load_day("main", TODAY),
        lambda: repo.save_day(day()),
        lambda: repo.append_event(RiskEvent(NOW, "main", "gate", "verdict", "ALLOW", True, 1)),
    ):
        with pytest.raises(PersistenceError) as info:
            await call()  # type: ignore[operator]
        assert ONLY_THE_TYPE.fullmatch(str(info.value)), str(info.value)
    await repo.close()


async def test_a_database_that_never_answers_gives_up_after_the_timeout() -> None:
    """The bound matters: a hung database must not stall the "persist HALTED first" step."""
    release = asyncio.Event()

    async def accept_and_stay_silent(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await release.wait()
        writer.close()

    server = await asyncio.start_server(accept_and_stay_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    repo = PostgresRiskRepository(
        f"postgresql+asyncpg://u:p@127.0.0.1:{port}/x_test", timeout_s=0.5
    )
    try:
        started = time.monotonic()
        with pytest.raises(PersistenceError) as info:
            # wait_for is the test's own safety net: if the repository's bound were missing, this
            # raises a plain TimeoutError after 10 s (a failure) instead of hanging the suite.
            await asyncio.wait_for(repo.save_day(day()), timeout=10)
        assert time.monotonic() - started < 5
        assert re.fullmatch(r"database write failed \(TimeoutError\)", str(info.value))
    finally:
        release.set()
        await repo.close()
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
async def test_a_stored_halt_level_this_version_does_not_know_fails_closed() -> None:
    """The reserved level 20 (or damage) must never decode as "no halt": it raises."""
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(t.risk_day_state).values(
                account_id="main",
                trade_date=TODAY,
                peak=Decimal(0),
                halt_level=20,
                entries_today=0,
                loss_streak=0,
                updated_at=NOW,
            )  # fmt: skip
        )
    await engine.dispose()
    repo = PostgresRiskRepository(TEST_DATABASE_URL)
    try:
        with pytest.raises(PersistenceError) as info:
            await repo.load_day("main", TODAY)
        assert "stored risk data is invalid" in str(info.value)
        with pytest.raises(PersistenceError):  # the same row reached through a write
            await repo.save_day(day(halt_level=HaltLevel.MTM_BLIND))
    finally:
        await repo.close()


# ---- the migration is what this suite assumes ---------------------------------------------


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
async def test_the_migrations_and_the_table_definitions_do_not_drift() -> None:
    """Alembic's own ``check``: the migrated schema matches ``tables.py`` in every column type,
    nullability, key and index. It raises if autogenerate would produce any change."""
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    # The database may already be at head from an earlier test, which would make "upgrade head" a
    # no-op and check an old schema. Go back to the Stage 2 schema and apply the migrations again,
    # so the migration code itself is what gets checked. This reshapes the shared *_test
    # database mid-suite, so these tests must never run in parallel against one database.
    await asyncio.to_thread(migrate, TEST_DATABASE_URL, "0003")
    await asyncio.to_thread(migrate, TEST_DATABASE_URL, "head")
    await asyncio.to_thread(command.check, alembic_config(TEST_DATABASE_URL))
