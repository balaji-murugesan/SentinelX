"""Candles (OHLCV bars) built from ticks and from 1-minute history (add-candles).

A candle summarises all trades in one interval: open (first trade), high, low, close (last
trade) and volume (quantity traded). Trailing-stop methods (S4, D-025) and their indicators
(ATR, EMA) read completed candles only, so a stop never jitters inside a forming candle.
"""

import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import structlog

from contracts.market import InstrumentKey
from engine.market_data.clock import interval_start
from engine.market_data.provider import Tick

log = structlog.get_logger("engine.market_data.candles")

LATE_WARNING_INTERVAL_S = 10.0


@dataclass(frozen=True, slots=True)
class Candle:
    """One completed candle for one instrument and interval.

    partial  - the engine did not see the whole interval (started mid-interval, feed outage,
               or ticks dropped), so OHLCV may be incomplete.
    replaced - published again by the refill after a reconnect, superseding an earlier
               candle with the same key, interval and start.
    """

    key: InstrumentKey
    minutes: int  # interval length
    start: datetime  # aware UTC; the interval's first instant
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    partial: bool = False
    replaced: bool = False

    @property
    def end(self) -> datetime:
        """The first instant after this candle's interval."""
        return self.start + timedelta(minutes=self.minutes)


@dataclass(slots=True)
class _Forming:
    """A 1-minute candle still collecting ticks (mutable; never shown to consumers)."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    partial: bool

    def freeze(self, key: InstrumentKey) -> Candle:
        return Candle(
            key,
            1,
            self.start,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.partial,
        )


class MinuteBuilder:
    """Builds 1-minute candles from ticks (design §1, §3, §4). Pure and synchronous.

    Every method returns the candles it closed, so the caller (CandleService) owns timing and
    publishing. Longer intervals are aggregated from these 1-minute candles with ``aggregate``.
    """

    def __init__(self) -> None:
        self._forming: dict[InstrumentKey, _Forming] = {}
        # When the builder started seeing every tick for a key: an interval that began earlier
        # was not seen whole, so its candle is partial.
        self._seen_since: dict[InstrumentKey, datetime] = {}
        self._last_volume: dict[InstrumentKey, int] = {}
        # End of the newest closed interval per key: ticks before it are late.
        self._closed_until: dict[InstrumentKey, datetime] = {}
        self.late = 0
        self._late_at_warning = 0
        self._last_warning = float("-inf")

    @property
    def keys(self) -> list[InstrumentKey]:
        return list(self._seen_since)

    def seen_since(self, key: InstrumentKey) -> datetime | None:
        """When the builder started seeing every tick for ``key`` (None if not watched)."""
        return self._seen_since.get(key)

    def watch(self, key: InstrumentKey, since: datetime) -> None:
        """Start building candles for ``key``; ticks are seen from ``since`` on."""
        self._seen_since.setdefault(key, since)

    def unwatch(self, key: InstrumentKey) -> None:
        self._forming.pop(key, None)
        self._seen_since.pop(key, None)
        self._last_volume.pop(key, None)
        self._closed_until.pop(key, None)

    def add(self, tick: Tick) -> list[Candle]:
        """Fold one tick in. Returns the candle it closed early, if the tick opened a new minute."""
        key = tick.key
        start = interval_start(tick.exchange_time, 1)
        if key not in self._seen_since or start is None:
            return []
        if start < self._closed_until.get(key, start):
            self._count_late(key)
            return []
        closed: list[Candle] = []
        forming = self._forming.get(key)
        if forming is not None and start > forming.start:
            closed.append(self._close(key, forming))
            forming = None
        delta, volume_ok = self._volume_delta(key, tick.volume)
        if forming is None:
            forming = _Forming(
                start, tick.ltp, tick.ltp, tick.ltp, tick.ltp, delta,
                partial=self._seen_since[key] > start or not volume_ok,
            )  # fmt: skip
            self._forming[key] = forming
        else:
            forming.high = max(forming.high, tick.ltp)
            forming.low = min(forming.low, tick.ltp)
            forming.close = tick.ltp
            forming.volume += delta
            forming.partial = forming.partial or not volume_ok
        return closed

    def close_due(self, now: datetime, grace: timedelta) -> list[Candle]:
        """Close every forming candle whose minute ended at least ``grace`` before ``now``."""
        due = [
            (k, f)
            for k, f in self._forming.items()
            if f.start + timedelta(minutes=1) + grace <= now
        ]
        return [self._close(key, forming) for key, forming in due]

    def mark_partial(self) -> None:
        """The feed went down or ticks were dropped: every forming candle is incomplete."""
        for forming in self._forming.values():
            forming.partial = True

    def resync(self, since: datetime) -> None:
        """The feed reconnected at ``since``: intervals that began earlier were not seen whole,
        and the cumulative volume jumped by the outage's trades, so drop the baselines."""
        self.mark_partial()
        for key in self._seen_since:
            self._seen_since[key] = since
        self._last_volume.clear()

    def _close(self, key: InstrumentKey, forming: _Forming) -> Candle:
        del self._forming[key]
        candle = forming.freeze(key)
        self._closed_until[key] = candle.end
        return candle

    def _volume_delta(self, key: InstrumentKey, volume: int | None) -> tuple[int, bool]:
        """Quantity traded since the previous tick, from the day's cumulative volume.

        Returns (delta, ok). No volume (indices) adds 0 and is fine. The first tick after start
        or reconnect has no baseline, and a drop in the cumulative total means the feed reset;
        both add 0 and make the candle partial.
        """
        if volume is None:
            return 0, True
        previous = self._last_volume.get(key)
        self._last_volume[key] = volume
        if previous is None or volume < previous:
            return 0, False
        return volume - previous, True

    def _count_late(self, key: InstrumentKey) -> None:
        self.late += 1
        now = time.monotonic()
        if now - self._last_warning >= LATE_WARNING_INTERVAL_S:
            log.warning(
                "late ticks ignored: their candle was already published",
                instrument=str(key),
                late_since_last=self.late - self._late_at_warning,
                late_total=self.late,
            )
            self._last_warning = now
            self._late_at_warning = self.late


def aggregate(candles: Iterable[Candle], minutes: int) -> list[Candle]:
    """Group 1-minute candles (one instrument, time order) into ``minutes``-long candles
    anchored to 09:15 IST. The same function serves live candles and history (design §1)."""
    groups: dict[datetime, list[Candle]] = {}
    for candle in candles:
        start = interval_start(candle.start, minutes)
        if start is not None:
            groups.setdefault(start, []).append(candle)
    return [combine(group, minutes, start) for start, group in groups.items()]


def combine(minute_candles: Sequence[Candle], minutes: int, start: datetime) -> Candle:
    """One ``minutes``-long candle starting at ``start`` from its 1-minute candles (non-empty,
    time order, same instrument)."""
    return Candle(
        key=minute_candles[0].key,
        minutes=minutes,
        start=start,
        open=minute_candles[0].open,
        high=max(c.high for c in minute_candles),
        low=min(c.low for c in minute_candles),
        close=minute_candles[-1].close,
        volume=sum(c.volume for c in minute_candles),
        partial=any(c.partial for c in minute_candles),
    )


class CandleStore:
    """Completed candles per (instrument, interval), newest last, at most ``size`` each.

    ``deque(maxlen=n)`` is a fixed-size ring buffer: appending to a full deque silently drops
    the oldest item from the other end (like a bounded queue that discards from the front).
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._candles: dict[tuple[InstrumentKey, int], deque[Candle]] = {}

    def add(self, candle: Candle) -> None:
        """Append a newer candle, or replace the one with the same start (refill)."""
        held = self._candles.setdefault((candle.key, candle.minutes), deque(maxlen=self._size))
        if not held or candle.start > held[-1].start:
            held.append(candle)
            return
        merged = {c.start: c for c in held}
        merged[candle.start] = candle
        held.clear()
        held.extend(merged[start] for start in sorted(merged))  # maxlen keeps the newest

    def last(self, key: InstrumentKey, minutes: int, count: int) -> list[Candle]:
        """Up to ``count`` most recent completed candles, oldest first."""
        held = self._candles.get((key, minutes))
        if not held or count <= 0:
            return []
        return list(held)[-count:]

    def between(
        self, key: InstrumentKey, minutes: int, start: datetime, end: datetime
    ) -> list[Candle]:
        """Held candles whose start is in [start, end), oldest first."""
        return [c for c in self._candles.get((key, minutes), ()) if start <= c.start < end]

    def drop(self, key: InstrumentKey) -> None:
        for held_key in [k for k in self._candles if k[0] == key]:
            del self._candles[held_key]
