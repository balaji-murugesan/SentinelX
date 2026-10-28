"""Candles (OHLCV bars) built from ticks and from 1-minute history (add-candles).

A candle summarises all trades in one interval: open (first trade), high, low, close (last
trade) and volume (quantity traded). Trailing-stop methods (S4, D-025) and their indicators
(ATR, EMA) read completed candles only, so a stop never jitters inside a forming candle.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from contracts.market import InstrumentKey


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
