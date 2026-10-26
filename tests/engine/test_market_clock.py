"""IST and market-hours helpers."""

from datetime import UTC, date, datetime, time

import pytest

from engine.market_data.clock import (
    is_market_hours,
    ist_date,
    market_close_utc,
    next_ist_time,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_ist_date_crosses_midnight_before_utc() -> None:
    assert ist_date(utc(2026, 10, 25, 19, 0)) == date(2026, 10, 26)  # 00:30 IST next day


@pytest.mark.parametrize(
    ("instant", "open_"),
    [
        (utc(2026, 10, 26, 3, 44), False),  # Mon 09:14 IST
        (utc(2026, 10, 26, 3, 45), True),  # Mon 09:15 IST
        (utc(2026, 10, 26, 9, 59), True),  # Mon 15:29 IST
        (utc(2026, 10, 26, 10, 0), False),  # Mon 15:30 IST
        (utc(2026, 10, 25, 5, 30), False),  # Sun 11:00 IST
    ],
)
def test_market_hours(instant: datetime, open_: bool) -> None:
    assert is_market_hours(instant) is open_


def test_market_close_utc() -> None:
    assert market_close_utc(date(2026, 10, 26)) == utc(2026, 10, 26, 10, 0)


def test_next_ist_time_today_or_tomorrow() -> None:
    before = utc(2026, 10, 26, 2, 0)  # 07:30 IST
    after = utc(2026, 10, 26, 4, 0)  # 09:30 IST
    assert next_ist_time(time(8, 45), before) == utc(2026, 10, 26, 3, 15)
    assert next_ist_time(time(8, 45), after) == utc(2026, 10, 27, 3, 15)
