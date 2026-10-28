"""History sources for candle-service tests (no network, no randomness)."""

from datetime import datetime

from contracts.market import InstrumentKey
from engine.market_data.candles import Candle
from engine.market_data.history_source import HistoryUnavailable


class NoHistory:
    """A source with nothing to offer: warm-up installs no candles."""

    async def minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        return []


class FakeHistory:
    """Serves preset 1-minute candles; ``fail`` makes the next calls raise."""

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self.candles = list(candles or [])
        self.calls: list[tuple[InstrumentKey, datetime, datetime]] = []
        self.fail: HistoryUnavailable | None = None

    async def minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        self.calls.append((key, start, end))
        if self.fail is not None:
            raise self.fail
        return [c for c in self.candles if c.key == key and start <= c.start < end]
