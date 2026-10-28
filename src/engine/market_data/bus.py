"""In-process fan-out of ticks and feed status (and, since add-candles, completed candles) to
every engine consumer (add-market-data design §5, add-candles design §7).

Each consumer gets its own bounded ``asyncio.Queue``. ``publish`` is synchronous and never
waits: if a consumer's queue is full, its oldest item is dropped and counted. So the feed
reader is never slowed by a consumer, and one stuck consumer cannot starve the others (the
exit check, S4, must always see fresh prices).

Consumers read with ``async for item in subscription:``, the async counterpart of iterating an
``IAsyncEnumerable<T>`` with ``await foreach`` in C#.

``Bus[T]`` and ``Subscription[T]`` are generic classes using Python 3.12's type-parameter
syntax: ``class Bus[T]:`` is the C# ``class Bus<T>``, and ``Bus[Candle]`` is ``Bus<Candle>``.
The parameter is checked by mypy only; at runtime every ``Bus`` is the same class.
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


class Subscription[T]:
    """One consumer's view of the bus. Iterate it to receive items in arrival order."""

    def __init__(self, bus: "Bus[T]", name: str, maxsize: int) -> None:
        self.name = name
        self.dropped = 0
        self._bus = bus
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize)
        self._last_warning = float("-inf")
        self._dropped_at_warning = 0

    def _offer(self, item: T) -> None:
        if self._queue.full():
            self._queue.get_nowait()  # drop the oldest: for ticks only the latest prices matter
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
    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await self._queue.get()

    def get_nowait(self) -> T | None:
        """Next item if one is waiting, else None (handy in tests)."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None


class Bus[T]:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription[T]] = []

    def subscribe(self, name: str, maxsize: int = DEFAULT_QUEUE_SIZE) -> Subscription[T]:
        subscription = Subscription(self, name, maxsize)
        self._subscriptions.append(subscription)
        return subscription

    def _remove(self, subscription: Subscription[T]) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def publish(self, item: T) -> None:
        """Deliver to every consumer. Synchronous and non-blocking by design."""
        for subscription in self._subscriptions:
            subscription._offer(item)


class TickBus(Bus[Item]):
    """The market-data bus: ticks and feed status, plus the latest status and tick times."""

    def __init__(self) -> None:
        super().__init__()
        self._last_tick_at: dict[InstrumentKey, datetime] = {}
        self._status: FeedStatus | None = None

    def publish(self, item: Item) -> None:
        if isinstance(item, Tick):
            self._last_tick_at[item.key] = item.received_at
        else:
            self._status = item
        super().publish(item)

    @property
    def status(self) -> FeedStatus | None:
        """The latest feed status, or None before the feed has reported any."""
        return self._status

    def last_tick_time(self, key: InstrumentKey) -> datetime | None:
        """Receive time of the latest tick for ``key``, or None if none has arrived."""
        return self._last_tick_at.get(key)


async def consume[T](subscription: Subscription[T], handle: Callable[[T], Awaitable[None]]) -> None:
    """Feed every item to ``handle``; an exception in ``handle`` is logged and skipped.

    A consumer bug must never stop the feed or other consumers (spec "Consumer error"). Runs
    until the calling task is cancelled.
    """
    async for item in subscription:
        try:
            await handle(item)
        except Exception:
            log.exception("market-data consumer failed on an item", consumer=subscription.name)
