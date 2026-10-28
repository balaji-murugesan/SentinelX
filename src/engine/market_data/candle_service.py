"""Candles inside the engine: on-demand candle subscriptions, warm-up, closing, publishing and
refill (add-candles design §2, §6, §9).

``CandleService`` feeds ticks from the TickBus into the pure ``MinuteBuilder``, closes candles
on a shared boundary timer, derives the longer intervals from completed 1-minute candles, keeps
them in a ``CandleStore`` and announces each completed candle on ``events`` (a ``Bus[Candle]``).
History (Dhan or simulated) warms each new instrument up and refills gaps after a reconnect.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import structlog

from contracts.market import InstrumentKey
from engine.market_data.bus import DEFAULT_QUEUE_SIZE, Bus, TickBus
from engine.market_data.candles import Candle, CandleStore, MinuteBuilder, aggregate, combine
from engine.market_data.clock import interval_start, last_completed_minute, utc_now
from engine.market_data.history_source import HistorySource, HistoryUnavailable, lookback_start
from engine.market_data.provider import FeedState, FeedStatus, MarketDataProvider, Tick

log = structlog.get_logger("engine.market_data.candles")

ONE_MINUTE = timedelta(minutes=1)
# After the interval holding a reconnect completes, wait this long so Dhan's history has it.
REFILL_DELAY = timedelta(seconds=5)


class CandleService:
    def __init__(
        self,
        provider: MarketDataProvider,
        ticks: TickBus,
        history: HistorySource,
        *,
        intervals: tuple[int, ...],
        history_size: int,
        grace: timedelta,
        now: Callable[[], datetime] = utc_now,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._provider = provider
        self._history = history
        self._intervals = intervals
        self._longer = tuple(m for m in intervals if m > 1)
        self._history_size = history_size
        self._grace = grace
        self._now = now
        self._builder = MinuteBuilder()
        self._owners: dict[InstrumentKey, set[str]] = {}
        # Subscribe now, before any tick is published, so the builder never misses the first.
        self._ticks = ticks.subscribe("candles", queue_size)
        self._dropped_seen = 0
        # The 1-minute store holds enough to rebuild the longest interval from it.
        self.store = CandleStore(max(history_size, max(intervals)))
        self.events: Bus[Candle] = Bus()
        # Newest minute boundary whose closing work is done (None until the first timer run).
        self._done_until: datetime | None = None
        # Feed state seen so far: an outage is CONNECTED -> anything else -> CONNECTED.
        self._was_connected = False
        self._outage_since: datetime | None = None
        # Keys whose warm-up failed; retried at the next reconnect or request (spec).
        self._needs_warmup: set[InstrumentKey] = set()
        self._history_off: str | None = None  # set once history is permanently unavailable
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- consumer interface ------------------------------------------------------------

    async def request(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        """Build candles for ``keys`` on behalf of ``owner`` (spec "Candle subscriptions on
        demand"). Returns once history for new keys is installed ("Warm-up before first use").
        Raises the feed's SubscriptionLimitError over the cap, with nothing started."""
        wanted = list(dict.fromkeys(keys))
        await self._provider.subscribe(wanted, owner)
        since = self._now()
        new = [k for k in wanted if k not in self._owners]
        for key in wanted:
            if key not in self._owners:
                self._builder.watch(key, since)  # live ticks build from now (design §6)
            self._owners.setdefault(key, set()).add(owner)
        retry = [k for k in wanted if k in self._needs_warmup and k not in new]
        for key in new + retry:
            await self._warm_up(key, since)

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
                self._needs_warmup.discard(key)

    def last(self, key: InstrumentKey, minutes: int, count: int) -> list[Candle]:
        """Up to ``count`` most recent completed candles, oldest first. Never the forming one."""
        return self.store.last(key, minutes, count)

    # ---- warm-up and refill (design §6) -----------------------------------------------

    async def _warm_up(self, key: InstrumentKey, since: datetime) -> None:
        """Install history before the live candles: every interval that ended by the minute
        boundary at or before ``since``. The interval forming then is left to the live side."""
        end = last_completed_minute(since)
        start = lookback_start(end, self._history_size, max(self._intervals))
        minutes = await self._load(key, start, end)
        if key not in self._owners:
            return  # released while history was loading
        if minutes is None:
            self._needs_warmup.add(key)
            return
        self._needs_warmup.discard(key)
        self.store.add_many(minutes[-self._history_size :])
        for interval in self._longer:
            complete = [c for c in aggregate(minutes, interval) if c.end <= end]
            self.store.add_many(complete[-self._history_size :])

    async def _load(
        self, key: InstrumentKey, start: datetime, end: datetime
    ) -> list[Candle] | None:
        """History for one key, or None after logging why (spec "History failures are
        contained": live candles carry on without it)."""
        if self._history_off is not None:
            return None
        try:
            return await self._history.minutes(key, start, end)
        except HistoryUnavailable as exc:
            if exc.permanent:
                self._history_off = str(exc)
            log.error("candle history unavailable", instrument=str(key), reason=str(exc))
            return None

    async def refill(self, outage_since: datetime) -> None:
        """Reload history for every candle key from the outage start (spec "Refill after
        reconnect"): publish missed intervals and replace partial ones."""
        end = last_completed_minute(self._now())
        for key in list(self._owners):
            if key in self._needs_warmup:
                await self._warm_up(key, self._now())  # warm-up never succeeded: retry it
                continue
            first = interval_start(outage_since, max(self._intervals)) or outage_since
            minutes = await self._load(key, first, end)
            if minutes is None or key not in self._owners:
                continue
            changed = [c for c in minutes if self._fill(c)]
            if not changed:
                continue
            for interval in self._longer:
                for candle in aggregate(self.store.between(key, 1, first, end), interval):
                    if candle.end <= end:
                        self._fill(candle)

    def _fill(self, candle: Candle) -> bool:
        """Publish a missed candle, or replace a held one that refill changed. A complete live
        minute is kept as seen. Returns True if anything was published."""
        held = self.store.at(candle.key, candle.minutes, candle.start)
        if held is None:
            self._store_and_announce(candle)
            return True
        if (candle.minutes == 1 and not held.partial) or replace(held, replaced=False) == candle:
            return False
        self._store_and_announce(replace(candle, replaced=True))
        return True

    # ---- running ----------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Consume ticks and feed status and run the boundary timer until ``stop`` is set."""
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._consume(stop))
                group.create_task(self._timer(stop))
        finally:
            for task in self._tasks:
                task.cancel()

    async def _consume(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(anext(self._ticks), timeout=0.5)
            except TimeoutError:
                continue
            self._check_drops()
            if isinstance(item, Tick):
                self.on_tick(item)
            else:
                self.on_status(item)

    def on_tick(self, tick: Tick) -> None:
        """Fold one tick into its 1-minute candle (synchronous; tests call it directly)."""
        self._publish_minutes(self._builder.add(tick))

    def on_status(self, status: FeedStatus) -> None:
        """Feed outages make candles partial; the reconnect schedules a refill (design §6)."""
        if status.state is not FeedState.CONNECTED:
            self._builder.mark_partial()
            if self._was_connected and self._outage_since is None:
                self._outage_since = status.at
            return
        self._was_connected = True
        if self._outage_since is None:
            return
        outage_since, self._outage_since = self._outage_since, None
        self._builder.resync(status.at)
        # The interval holding the reconnect must complete before history can cover it.
        due = last_completed_minute(status.at) + ONE_MINUTE + max(self._grace, REFILL_DELAY)
        self._spawn(self._refill_at(due, outage_since))

    def _check_drops(self) -> None:
        """Ticks dropped from this consumer's queue: the forming candles miss trades."""
        if self._ticks.dropped > self._dropped_seen:
            self._dropped_seen = self._ticks.dropped
            self._builder.mark_partial()

    async def _refill_at(self, due: datetime, outage_since: datetime) -> None:
        delay = (due - self._now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        await self.refill(outage_since)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` in the background, logging (not raising) a failure."""

        async def contained() -> None:
            try:
                await work
            except Exception:
                log.exception("candle refill failed")

        task = asyncio.get_running_loop().create_task(contained())
        self._tasks.add(task)  # keep a reference so the task is not garbage-collected
        task.add_done_callback(self._tasks.discard)

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
