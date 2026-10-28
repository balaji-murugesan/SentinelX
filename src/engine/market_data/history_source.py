"""Where warm-up candles come from (add-candles design §5).

``HistorySource`` is a ``Protocol`` beside ``MarketDataProvider`` (D-023): DhanHistory in
``engine.broker.dhan`` and SimulatedHistory satisfy it, and the candle service depends on it
only, never on ``engine.broker``.
"""

import math
from datetime import datetime, timedelta
from typing import Protocol

from contracts.market import InstrumentKey
from engine.market_data.candles import Candle
from engine.market_data.clock import SESSION_MINUTES

HOLIDAY_MARGIN_DAYS = 4


class HistoryUnavailable(Exception):
    """History could not be loaded. ``permanent`` is True when no retry can help before a
    restart (rejected token or data plan): the candle service then stops asking."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class HistorySource(Protocol):
    async def minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        """Completed 1-minute candles for ``key`` with start in [start, end), oldest first,
        within 09:15-15:30 IST only. Raises HistoryUnavailable."""
        ...


def lookback_start(end: datetime, history: int, longest_interval: int) -> datetime:
    """Where a warm-up fetch must begin to yield ``history`` candles of the longest interval.

    sessions = ceil(history x interval / 375 minutes); calendar days = sessions x 7/5 for
    weekends, plus a margin for holidays (no holiday calendar yet). Defaults: 200 x 15 min ->
    8 sessions -> 16 days, well inside Dhan's 90-day limit for one request.
    """
    sessions = math.ceil(history * longest_interval / SESSION_MINUTES)
    days = math.ceil(sessions * 7 / 5) + HOLIDAY_MARGIN_DAYS
    return end - timedelta(days=days)
