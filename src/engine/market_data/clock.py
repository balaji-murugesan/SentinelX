"""Market-time helpers: IST conversion, NSE trading hours, next daily run time.

All functions take an aware ``now`` instead of reading the system clock, so tests can pass
any instant (the engine passes ``utc_now()``). Times are stored UTC and converted to IST
(Asia/Kolkata, UTC+05:30, no daylight saving) only for market-hour logic (CLAUDE.md).

No holiday calendar yet: "market hours" means 09:15-15:30 IST, Monday to Friday.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def utc_now() -> datetime:
    return datetime.now(UTC)


def ist_date(now: datetime) -> date:
    """The IST calendar date of an aware instant (the trading date it belongs to)."""
    return now.astimezone(IST).date()


def is_market_hours(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() < MARKET_CLOSE


def market_close_utc(day: date) -> datetime:
    """15:30 IST on ``day``, as a UTC instant."""
    return datetime.combine(day, MARKET_CLOSE, tzinfo=IST).astimezone(UTC)


SESSION_MINUTES = 375  # 09:15-15:30 IST


def interval_start(at: datetime, minutes: int) -> datetime | None:
    """Start (UTC) of the ``minutes``-long candle interval containing ``at``, or None outside
    09:15-15:30 IST. Intervals are anchored to the 09:15 open, not to the clock hour."""
    local = at.astimezone(IST)
    if not MARKET_OPEN <= local.time() < MARKET_CLOSE:
        return None
    open_ = datetime.combine(local.date(), MARKET_OPEN, tzinfo=IST)
    elapsed = int((local - open_).total_seconds()) // 60
    return (open_ + timedelta(minutes=elapsed - elapsed % minutes)).astimezone(UTC)


def last_completed_minute(now: datetime) -> datetime:
    """The latest whole-minute boundary at or before ``now`` (UTC): every 1-minute interval
    that starts before it has ended."""
    return now.astimezone(UTC).replace(second=0, microsecond=0)


def next_ist_time(at: time, now: datetime) -> datetime:
    """The next instant (UTC) whose IST wall-clock time is ``at``; strictly after ``now``."""
    local = now.astimezone(IST)
    candidate = datetime.combine(local.date(), at, tzinfo=IST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
