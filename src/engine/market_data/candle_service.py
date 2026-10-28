"""Candles inside the engine: on-demand candle subscriptions, closing and publishing (add-candles
design §2, §9).

``CandleService`` feeds ticks from the TickBus into the pure ``MinuteBuilder``, closes candles
on a shared boundary timer, derives the longer intervals from completed 1-minute candles, keeps
them in a ``CandleStore`` and announces each completed candle on ``events`` (a ``Bus[Candle]``).
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta

import structlog

from contracts.market import InstrumentKey
from engine.market_data.bus import DEFAULT_QUEUE_SIZE, Bus, TickBus
from engine.market_data.candles import Candle, CandleStore, MinuteBuilder, combine
from engine.market_data.clock import interval_start, last_completed_minute, utc_now
from engine.market_data.provider import MarketDataProvider, Tick

log = structlog.get_logger("engine.market_data.candles")

ONE_MINUTE = timedelta(minutes=1)


class CandleService:
    def __init__(
        self,
        provider: MarketDataProvider,
        ticks: TickBus,
        *,
        intervals: tuple[int, ...],
        history_size: int,
        grace: timedelta,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._provider = provider
        self._intervals = intervals
        self._longer = tuple(m for m in intervals if m > 1)
        self._grace = grace
        self._now = now
        self._builder = MinuteBuilder()
        self._owners: dict[InstrumentKey, set[str]] = {}
        # Subscribe now, before any tick is published, so the builder never misses the first.
        self._ticks = ticks.subscribe("candles", DEFAULT_QUEUE_SIZE)
        # The 1-minute store holds enough to rebuild the longest interval from it.
        self.store = CandleStore(max(history_size, max(intervals)))
        self.events: Bus[Candle] = Bus()
        # Newest minute boundary whose closing work is done (None until the first timer run).
        self._done_until: datetime | None = None

    # ---- consumer interface ------------------------------------------------------------

    async def request(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        """Build candles for ``keys`` on behalf of ``owner`` (spec "Candle subscriptions on
        demand"). Raises the feed's SubscriptionLimitError over the cap, with nothing started."""
        wanted = list(dict.fromkeys(keys))
        await self._provider.subscribe(wanted, owner)
        since = self._now()
        for key in wanted:
            if key not in self._owners:
                self._builder.watch(key, since)
            self._owners.setdefault(key, set()).add(owner)

    async def release(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        """Stop building candles for ``owner``; a key lives on while another owner wants it."""
        wanted = list(dict.fromkeys(keys))
        mine = [k for k in wanted if owner in self._owners.get(k, set())]
        await self._provider.unsubscribe(mine, owner)
        for key in mine:
            owners = self._owners[key]
            owners.discard(owner)
            if not owners:
                del self._owners[key]
                self._builder.unwatch(key)
                self.store.drop(key)

    def last(self, key: InstrumentKey, minutes: int, count: int) -> list[Candle]:
        """Up to ``count`` most recent completed candles, oldest first. Never the forming one."""
        return self.store.last(key, minutes, count)

    # ---- running ----------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Consume ticks and run the boundary timer until ``stop`` is set."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._consume(stop))
            group.create_task(self._timer(stop))

    async def _consume(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(anext(self._ticks), timeout=0.5)
            except TimeoutError:
                continue
            if isinstance(item, Tick):
                self.on_tick(item)

    def on_tick(self, tick: Tick) -> None:
        """Fold one tick into its 1-minute candle (synchronous; tests call it directly)."""
        self._publish_minutes(self._builder.add(tick))

    async def _timer(self, stop: asyncio.Event) -> None:
        """Wake at each minute boundary + grace and close what is due (design §2)."""
        while not stop.is_set():
            # From now - grace: started inside a grace window (10:06:00-10:06:02), the 10:06
            # boundary is still pending, so wake at 10:06:02, not 10:07:02.
            wake = last_completed_minute(self._now() - self._grace) + ONE_MINUTE + self._grace
            try:
                await asyncio.wait_for(stop.wait(), (wake - self._now()).total_seconds())
            except TimeoutError:
                self.close_due(self._now())

    def close_due(self, now: datetime) -> None:
        """Close the 1-minute candles that are due, then every longer interval that ended at a
        boundary not yet handled. Public so tests can drive it with a fake clock."""
        self._publish_minutes(self._builder.close_due(now, self._grace))
        boundary = last_completed_minute(now - self._grace)
        start = boundary if self._done_until is None else self._done_until + ONE_MINUTE
        while start <= boundary:
            self._close_longer(start)
            start += ONE_MINUTE
        self._done_until = boundary

    def _publish_minutes(self, candles: list[Candle]) -> None:
        for candle in candles:
            self._store_and_announce(candle)

    def _close_longer(self, boundary: datetime) -> None:
        """Aggregate every longer interval that ends exactly at ``boundary``."""
        for minutes in self._longer:
            start = interval_start(boundary - ONE_MINUTE, minutes)
            if start is None or start + timedelta(minutes=minutes) != boundary:
                continue
            for key in self._builder.keys:
                parts = self.store.between(key, 1, start, boundary)
                if not parts:
                    continue  # no trade in the whole interval: no candle (spec)
                candle = combine(parts, minutes, start)
                seen_since = self._builder.seen_since(key)
                if seen_since is not None and seen_since > start and not candle.partial:
                    # Watching began inside the interval, so its early minutes are missing.
                    # dataclasses.replace: a modified copy, like C#'s `record with { ... }`.
                    candle = replace(candle, partial=True)
                self._store_and_announce(candle)

    def _store_and_announce(self, candle: Candle) -> None:
        """Keep every candle (1-minute ones feed the longer intervals); announce only the
        configured intervals."""
        self.store.add(candle)
        if candle.minutes in self._intervals:
            self.events.publish(candle)
