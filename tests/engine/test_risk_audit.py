"""The risk audit queue (add-risk-engine task 5.3; design §15): a failed audit write is queued and
replayed in order, never raised, and never blocks anything.
"""

import asyncio

import pytest

from engine.persistence.repository import PersistenceError
from engine.risk import audit as audit_module
from engine.risk.audit import RiskAudit
from engine.risk.repository import RiskEvent
from tests.engine.risk_builders import NOW
from tests.engine.risk_fakes import MemoryRiskRepository


def event(n: int) -> RiskEvent:
    return RiskEvent(
        at=NOW, account_id="main", rule="gate", phase="verdict", decision="ALLOW", enforced=True,
        latency_us=1, inputs={"n": n},
    )  # fmt: skip


class Switchable(MemoryRiskRepository):
    """Fails every append while ``down``."""

    def __init__(self, *, down: bool = False, error: Exception | None = None) -> None:
        super().__init__()
        self.down = down
        self.error = error or PersistenceError("db down")
        self.two_written = asyncio.Event()

    async def append_event(self, event: RiskEvent) -> None:
        if self.down:
            raise self.error
        await super().append_event(event)
        if len(self.events) >= 2:
            self.two_written.set()


def numbers(repo: MemoryRiskRepository) -> list[object]:
    return [e.inputs["n"] for e in repo.events]


async def test_an_event_is_written_at_once_when_the_database_is_up() -> None:
    repo = Switchable()
    audit = RiskAudit(repo)

    await audit.record(event(1))

    assert numbers(repo) == [1] and audit.pending == 0


async def test_a_failed_write_is_queued_not_raised_and_later_events_do_not_jump_ahead() -> None:
    repo = Switchable(down=True)
    audit = RiskAudit(repo)

    await audit.record(event(1))
    repo.down = False  # the database is back, but event 1 is still queued
    await audit.record(event(2))

    assert numbers(repo) == [] and audit.pending == 2  # 2 waits behind 1: the order is kept
    assert await audit.drain() is True
    assert numbers(repo) == [1, 2] and audit.pending == 0


async def test_drain_stops_at_the_first_failure_and_keeps_that_event_at_the_head() -> None:
    repo = Switchable(down=True)
    audit = RiskAudit(repo)
    for n in (1, 2, 3):
        await audit.record(event(n))

    assert await audit.drain() is False and audit.pending == 3
    repo.down = False
    assert await audit.drain() is True
    assert numbers(repo) == [1, 2, 3]


async def test_the_queue_is_bounded_and_drops_the_oldest_loudly() -> None:
    repo = Switchable(down=True)
    audit = RiskAudit(repo, max_queued=3)

    for n in range(1, 6):
        await audit.record(event(n))

    assert audit.pending == 3 and audit.dropped == 2
    repo.down = False
    await audit.drain()
    assert numbers(repo) == [3, 4, 5]  # the two oldest were lost, the order of the rest kept


@pytest.mark.parametrize("error", [RuntimeError("driver bug"), ValueError("bad row")])
async def test_record_never_raises_whatever_the_repository_does(error: Exception) -> None:
    audit = RiskAudit(Switchable(down=True, error=error))

    await audit.record(event(1))  # must not raise

    assert audit.pending == 0  # an unexpected error is logged, not queued: replaying a poison row
    # would block everything behind it


async def test_the_retry_loop_drains_the_queue_once_the_database_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "RETRY_MIN_S", 0.01)
    repo = Switchable(down=True)
    audit = RiskAudit(repo)
    await audit.record(event(1))
    await audit.record(event(2))
    stop = asyncio.Event()
    task = asyncio.create_task(audit.run(stop))

    await asyncio.sleep(0.05)
    assert audit.pending == 2  # still down
    repo.down = False
    await asyncio.wait_for(repo.two_written.wait(), timeout=2)
    stop.set()
    await task

    assert numbers(repo) == [1, 2]


async def test_record_never_waits_for_a_write_that_is_in_flight() -> None:
    """The gate calls ``record`` inside the order service's lock, so waiting on a database that
    is slow or away would hold up exits. A second event queues behind the first."""

    class Stuck(MemoryRiskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def append_event(self, event: RiskEvent) -> None:
            self.started.set()
            await self.release.wait()
            await super().append_event(event)

    repo = Stuck()
    audit = RiskAudit(repo)
    first = asyncio.create_task(audit.record(event(1)))
    await repo.started.wait()

    async with asyncio.timeout(1):
        await audit.record(event(2))  # returns at once: queued, not waiting for event 1

    assert audit.pending == 1
    repo.release.set()
    await first
    assert await audit.drain() is True
    assert numbers(repo) == [1, 2]


class FailsAfterWait(MemoryRiskRepository):
    """The first append waits to be released, then fails: an event in flight when others arrive."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_next = True

    async def append_event(self, event: RiskEvent) -> None:
        if self.fail_next:
            self.started.set()
            await self.release.wait()
            self.fail_next = False
            raise PersistenceError("db down")
        await super().append_event(event)


async def test_an_in_flight_event_that_fails_goes_back_ahead_of_the_ones_queued_behind_it() -> None:
    repo = FailsAfterWait()
    audit = RiskAudit(repo)
    first = asyncio.create_task(audit.record(event(1)))
    await repo.started.wait()
    await audit.record(event(2))  # queued behind the one in flight
    repo.release.set()
    await first

    assert await audit.drain() is True
    assert numbers(repo) == [1, 2]  # the rows read in the order things happened


async def test_with_a_full_queue_the_failed_in_flight_event_is_the_one_lost() -> None:
    """It is the oldest event, so when the queue has no room for it, it is the one dropped."""
    repo = FailsAfterWait()
    audit = RiskAudit(repo, max_queued=1)
    first = asyncio.create_task(audit.record(event(1)))
    await repo.started.wait()
    await audit.record(event(2))  # the queue (size 1) now holds it
    repo.release.set()
    await first

    assert audit.pending == 1 and audit.dropped == 1
    assert await audit.drain() is True
    assert numbers(repo) == [2]
