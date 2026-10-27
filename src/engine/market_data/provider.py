"""The market-data interface (D-023) and the values it produces: ticks and feed status.

``MarketDataProvider`` is a ``typing.Protocol``: structural typing, close to a C# interface
except that a class satisfies it simply by having matching methods; it never declares
``: IMarketDataProvider``. DhanFeed and SimulatedFeed both satisfy it, and consumers depend on
it (and on TickBus) only, never on engine.broker.

Ticks stay inside the engine process, so they are lightweight frozen dataclasses rather than
pydantic models (no per-tick validation cost on the 5 ms tick-to-exit budget, PRD 9.1).
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from contracts.market import InstrumentKey


# frozen=True: immutable (assigning a field raises). slots=True: fixed attribute layout with no
# per-instance __dict__, smaller and faster, roughly a C# `readonly record struct`.
@dataclass(frozen=True, slots=True)
class Tick:
    """One trade update for one instrument.

    ltp    - last traded price, exact Decimal (rupees).
    ltq    - last traded quantity (size of that last trade), if the source provides it.
    volume - cumulative traded quantity for the day so far, if provided.
    """

    key: InstrumentKey
    ltp: Decimal
    exchange_time: datetime  # when the exchange says the trade happened (aware, UTC)
    received_at: datetime  # when the engine received it (aware, UTC)
    ltq: int | None = None
    volume: int | None = None


class FeedState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DOWN = "down"  # stopped for good until restart (e.g. token expired)


@dataclass(frozen=True, slots=True)
class FeedStatus:
    state: FeedState
    reason: str
    at: datetime  # aware, UTC


class SubscriptionLimitError(Exception):
    """A subscribe request would exceed FEED_MAX_SUBSCRIPTIONS. Nothing was subscribed."""


class MarketDataProvider(Protocol):
    """A tick source. Implementations publish Tick and FeedStatus values to a TickBus."""

    async def run(self, stop: asyncio.Event) -> None:
        """Produce ticks until ``stop`` is set. Must never raise for feed/network problems."""
        ...

    async def subscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        """Start streaming ``keys`` for ``owner``. Raises SubscriptionLimitError over the cap."""
        ...

    async def unsubscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        """Stop streaming ``keys`` for ``owner``; a key stays live while another owner wants it."""
        ...


class Subscriptions:
    """Who wants which instrument, with the FEED_MAX_SUBSCRIPTIONS cap. Shared by all feeds.

    Each key keeps the set of owners that asked for it (e.g. "prefill", later "positions"), so
    one owner unsubscribing never cuts off another. Methods return only the keys whose
    feed-level state changed, which is exactly what a feed must send to the broker.
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._owners: dict[InstrumentKey, set[str]] = {}

    @property
    def keys(self) -> list[InstrumentKey]:
        return list(self._owners)

    def __contains__(self, key: object) -> bool:
        return key in self._owners

    def add(self, keys: Iterable[InstrumentKey], owner: str) -> list[InstrumentKey]:
        """Register ``owner`` for ``keys``; return the keys that are new to the feed.

        All or nothing: if the new total would exceed the cap, nothing changes.
        """
        wanted = list(dict.fromkeys(keys))
        new = [k for k in wanted if k not in self._owners]
        if len(self._owners) + len(new) > self._cap:
            raise SubscriptionLimitError(
                f"subscribing {len(new)} more instrument(s) would exceed "
                f"FEED_MAX_SUBSCRIPTIONS={self._cap} (currently {len(self._owners)})"
            )
        for key in wanted:
            self._owners.setdefault(key, set()).add(owner)
        return new

    def remove(self, keys: Iterable[InstrumentKey], owner: str) -> list[InstrumentKey]:
        """Unregister ``owner``; return the keys nobody wants any more."""
        gone = []
        for key in dict.fromkeys(keys):
            owners = self._owners.get(key)
            if owners is None or owner not in owners:
                continue
            owners.discard(owner)
            if not owners:
                del self._owners[key]
                gone.append(key)
        return gone
