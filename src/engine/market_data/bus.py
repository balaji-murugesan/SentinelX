"""In-process fan-out of ticks and feed status to every engine consumer (design §5).

Each consumer gets its own bounded ``asyncio.Queue``. ``publish`` is synchronous and never
waits: if a consumer's queue is full, its oldest item is dropped and counted. So the feed
reader is never slowed by a consumer, and one stuck consumer cannot starve the others (the
exit check, S4, must always see fresh prices).

Consumers read with ``async for item in subscription:``, the async counterpart of iterating an
``IAsyncEnumerable<T>`` with ``await foreach`` in C#.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

import structlog

from contracts.market import InstrumentKey
from engine.market_data.provider import FeedStatus, Tick

log = structlog.get_logger("engine.market_data")

Item = Tick | FeedStatus
DEFAULT_QUEUE_SIZE = 10_000
DROP_WARNING_INTERVAL_S = 10.0


class Subscription:
    """One consumer's view of the bus. Iterate it to receive items in arrival order."""

    def __init__(self, bus: "TickBus", name: str, maxsize: int) -> None:
        self.name = name
        self.dropped = 0
        self._bus = bus
        self._queue: asyncio.Queue[Item] = asyncio.Queue(maxsize)
        self._last_warning = float("-inf")
        self._dropped_at_warning = 0

    def _offer(self, item: Item) -> None:
        if self._queue.full():
            self._queue.get_nowait()  # drop the oldest: only the latest prices matter
            self.dropped += 1
            self._warn_if_due()
        self._queue.put_nowait(item)

    def _warn_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_warning >= DROP_WARNING_INTERVAL_S:
            log.warning(
                "slow market-data consumer: dropped oldest ticks",
                consumer=self.name,
                dropped_since_last=self.dropped - self._dropped_at_warning,
                dropped_total=self.dropped,
            )
            self._last_warning = now
            self._dropped_at_warning = self.dropped

    def close(self) -> None:
        self._bus._remove(self)

    # __aiter__/__anext__ make this usable in `async for` (like implementing
    # IAsyncEnumerator<T>.MoveNextAsync in C#). It never ends on its own; cancel the reading
    # task or close() and stop iterating to finish.
    def __aiter__(self) -> AsyncIterator[Item]:
        return self

    async def __anext__(self) -> Item:
        return await self._queue.get()

    def get_nowait(self) -> Item | None:
        """Next item if one is waiting, else None (handy in tests)."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None


class TickBus:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._last_tick_at: dict[InstrumentKey, datetime] = {}
        self._status: FeedStatus | None = None

    def subscribe(self, name: str, maxsize: int = DEFAULT_QUEUE_SIZE) -> Subscription:
        subscription = Subscription(self, name, maxsize)
        self._subscriptions.append(subscription)
        return subscription

    def _remove(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def publish(self, item: Item) -> None:
        """Deliver to every consumer. Synchronous and non-blocking by design."""
        if isinstance(item, Tick):
            self._last_tick_at[item.key] = item.received_at
        else:
            self._status = item
        for subscription in self._subscriptions:
            subscription._offer(item)

    @property
    def status(self) -> FeedStatus | None:
        """The latest feed status, or None before the feed has reported any."""
        return self._status

    def last_tick_time(self, key: InstrumentKey) -> datetime | None:
        """Receive time of the latest tick for ``key``, or None if none has arrived."""
        return self._last_tick_at.get(key)


async def consume(subscription: Subscription, handle: Callable[[Item], Awaitable[None]]) -> None:
    """Feed every item to ``handle``; an exception in ``handle`` is logged and skipped.

    A consumer bug must never stop the feed or other consumers (spec "Consumer error"). Runs
    until the calling task is cancelled.
    """
    async for item in subscription:
        try:
            await handle(item)
        except Exception:
            log.exception("market-data consumer failed on an item", consumer=subscription.name)
