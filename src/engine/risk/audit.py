"""The risk audit trail: a write queue in front of ``RiskRepository.append_event`` (add-risk-engine
design §15; risk-gate spec "Every risk decision is audited").

Two promises, and they pull in opposite directions:

  * Nothing a decision depends on waits for the audit. ``record`` never raises: a database that is
    down, or an event that is malformed, is logged and the engine carries on. In particular a
    failed audit write can never block an exit (safety rule 9: keep managing exits).
  * Nothing is lost while the database is merely slow or briefly away. An event that cannot be
    written now goes on a queue and is replayed, oldest first, by ``run`` with backoff, so the
    table still reads in the order things happened.

The queue is bounded (``MAX_QUEUED``). If Postgres is away long enough to fill it, the oldest event
is dropped and a loud error is logged for each one: an unbounded queue would trade a lost audit
row for a lost engine (memory).

The same shape as the order service's pending-write queue (``OrderService._store``), without its
"refuse new orders while persistence is down" flag: the risk engine already refuses entries when
its state is unavailable, and the audit must not add a second switch.
"""

import asyncio
import contextlib
from collections import deque
from typing import Final

import structlog

from engine.persistence.repository import PersistenceError
from engine.risk.repository import RiskEvent, RiskRepository

log = structlog.get_logger("engine.risk.audit")

MAX_QUEUED: Final = 10_000
RETRY_MIN_S: Final = 0.5  # first wait before retrying; doubles up to RETRY_MAX_S
RETRY_MAX_S: Final = 30.0


class RiskAudit:
    def __init__(self, repository: RiskRepository, *, max_queued: int = MAX_QUEUED) -> None:
        self._repo = repository
        self._max_queued = max_queued
        self._queue: deque[RiskEvent] = deque()  # events not yet in the database, oldest first
        self._lock = asyncio.Lock()  # one writer at a time keeps the order of the rows
        self.dropped = 0  # events lost because the queue was full (see the module docstring)

    @property
    def pending(self) -> int:
        return len(self._queue)

    async def record(self, event: RiskEvent) -> None:
        """Write ``event`` now if nothing is queued ahead of it, else queue it. Never raises."""
        try:
            if self._lock.locked():
                # A write or a retry is in flight, and it may be waiting on a database that is
                # away. Queue behind it rather than wait for it: this is called inside the order
                # service's lock, so waiting here would hold up exits.
                self._enqueue(event)
                return
            async with self._lock:
                if self._queue:
                    self._enqueue(event)  # never jump ahead of an earlier, unwritten event
                    return
                try:
                    await self._repo.append_event(event)
                except PersistenceError as error:
                    log.error("risk_audit_queued", rule=event.rule, error=str(error))
                    # Events queued behind this one while it was in flight are younger than it:
                    # it goes back to the head, so the rows are still written in order.
                    self._enqueue(event, at_head=True)
        except Exception:  # an audit failure of any kind must not reach the order path
            log.error("risk_audit_failed", rule=event.rule, exc_info=True)

    def _enqueue(self, event: RiskEvent, *, at_head: bool = False) -> None:
        if at_head:
            if len(self._queue) >= self._max_queued:
                self._drop(event)  # it is the oldest event, so it is the one that is lost
            else:
                self._queue.appendleft(event)
            return
        if len(self._queue) >= self._max_queued:
            self._drop(self._queue.popleft())
        self._queue.append(event)

    def _drop(self, lost: RiskEvent) -> None:
        self.dropped += 1
        log.error("risk_audit_dropped", rule=lost.rule, decision=lost.decision, at=lost.at)

    async def drain(self) -> bool:
        """Replay queued events oldest first; True once the queue is empty. Stops at the first
        failure and keeps that event at the head."""
        async with self._lock:
            while self._queue:
                try:
                    await self._repo.append_event(self._queue[0])
                except PersistenceError as error:
                    log.error("risk_audit_retry_failed", queued=len(self._queue), error=str(error))
                    return False
                self._queue.popleft()
            return True

    async def run(self, stop: asyncio.Event) -> None:
        """Retry the queue with exponential backoff (0.5 s doubling to 30 s) until ``stop``."""
        delay = RETRY_MIN_S
        while not stop.is_set():
            if self._queue:
                delay = RETRY_MIN_S if await self.drain() else min(delay * 2, RETRY_MAX_S)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay if self._queue else RETRY_MIN_S)
