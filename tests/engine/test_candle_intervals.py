"""Candle type and IST interval helpers (add-candles task 1.2).

Scenarios from spec candles "Session-aligned intervals": "15-minute alignment", "Received late,
traded on time", "Outside the session".
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.market import InstrumentKey
from engine.market_data.candles import Candle
from engine.market_data.clock import IST, interval_start, last_completed_minute


def ist(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 10, 28, hour, minute, second, micro, tzinfo=IST)


def test_fifteen_minute_alignment() -> None:
    tick = ist(9, 31, 5)
    assert interval_start(tick, 15) == ist(9, 30).astimezone(UTC)
    assert interval_start(tick, 5) == ist(9, 30).astimezone(UTC)
    assert interval_start(tick, 3) == ist(9, 30).astimezone(UTC)
    assert interval_start(tick, 1) == ist(9, 31).astimezone(UTC)


def test_anchored_to_open_not_clock_hour() -> None:
    # 15-minute candles run 09:15, 09:30, 09:45, 10:00 ... and 3-minute ones 09:15, 09:18 ...
    assert interval_start(ist(10, 14, 59), 15) == ist(10, 0).astimezone(UTC)
    assert interval_start(ist(9, 20, 30), 3) == ist(9, 18).astimezone(UTC)


def test_traded_on_time_received_late() -> None:
    # Bucketing uses exchange time only; 09:29:59 stays in the 09:29 minute however late it lands.
    assert interval_start(ist(9, 29, 59), 1) == ist(9, 29).astimezone(UTC)


@pytest.mark.parametrize(
    "instant",
    [ist(9, 8), ist(9, 14, 59, 999_999), ist(15, 30), ist(15, 40), ist(8, 0)],
)
def test_outside_the_session(instant: datetime) -> None:
    assert interval_start(instant, 1) is None


@pytest.mark.parametrize(("minutes", "last_start"), [(1, (15, 29)), (3, (15, 27)), (15, (15, 15))])
def test_session_edges(minutes: int, last_start: tuple[int, int]) -> None:
    assert interval_start(ist(9, 15), minutes) == ist(9, 15).astimezone(UTC)
    assert interval_start(ist(15, 29, 59), minutes) == ist(*last_start).astimezone(UTC)


def test_input_time_zone_does_not_matter() -> None:
    assert interval_start(ist(9, 31, 5).astimezone(UTC), 5) == ist(9, 30).astimezone(UTC)


def test_last_completed_minute() -> None:
    assert last_completed_minute(ist(10, 6, 1, 500)) == ist(10, 6).astimezone(UTC)
    assert last_completed_minute(ist(10, 6)) == ist(10, 6).astimezone(UTC)


def test_candle_is_immutable_and_knows_its_end() -> None:
    candle = Candle(
        key=InstrumentKey.parse("IDX_I:13"),
        minutes=5,
        start=ist(9, 30).astimezone(UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=0,
    )
    assert candle.end - candle.start == timedelta(minutes=5)
    assert not candle.partial and not candle.replaced
    with pytest.raises(dataclasses.FrozenInstanceError):
        candle.close = Decimal("1")  # type: ignore[misc]
