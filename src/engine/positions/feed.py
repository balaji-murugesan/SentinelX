"""Keeps every instrument with an open position on the feed (positions spec "Open positions
stay subscribed"; design §8).

MTM needs a live price for as long as units are held, even if no other part of the engine
wants that instrument. The feed already tracks *who* wants each key (``Subscriptions`` owner
sets), so this class only has to say "positions wants it" and later "positions is done".
"""

from contracts.market import InstrumentKey
from contracts.orders import Position
from engine.market_data.provider import MarketDataProvider
from engine.positions.book import PositionId, position_id

POSITIONS_OWNER = "positions"  # owner name in the feed's subscription registry


class PositionFeed:
    """Subscribes a key on the first open position for it and releases it when none is left.

    One key can back several positions (the same instrument under INTRADAY and CNC), so the
    release waits for all of them to be flat.
    """

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider
        self._open: dict[InstrumentKey, set[PositionId]] = {}  # key -> its open positions

    @property
    def keys(self) -> list[InstrumentKey]:
        return list(self._open)

    async def update(self, position: Position) -> None:
        """Call after every fill with the resulting position; safe to repeat.

        Raises SubscriptionLimitError (from the provider) if the feed is at its cap; the position
        is then not recorded as tracked, so the next call retries.
        """
        pid = position_id(position)
        holders = self._open.get(position.key, set())
        if position.quantity != 0:
            if not holders:
                await self._provider.subscribe([position.key], POSITIONS_OWNER)
            self._open.setdefault(position.key, set()).add(pid)
        elif pid in holders:
            holders.discard(pid)
            if not holders:
                del self._open[position.key]
                # Only the positions owner lets go: another owner's hold on the key stays.
                await self._provider.unsubscribe([position.key], POSITIONS_OWNER)
