"""PaperBroker: simulated execution against live prices (paper-broker spec, design §6).

Fills are deliberately conservative so paper results are not better than live:
  * market orders fill in full at the latest price moved AGAINST the trader by
    PAPER_SLIPPAGE_TICKS (a buy pays more, a sell gets less), and are refused when the price
    is missing or stale;
  * limit orders rest until a later tick trades at or through the limit, then fill at the
    limit (a real limit order never gets a better price from a later trade); one that is
    marketable on arrival fills at once at the better of its limit and the latest price.
Accepted optimism (documented in the README): whole-quantity fills, no queue position.

It never makes a network call; the tick bus is its only input.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderState, OrderType
from engine.broker.adapter import (
    BrokerAck,
    BrokerError,
    BrokerOrder,
    BrokerRejected,
    BrokerUpdate,
    Cancelled,
    Filled,
)
from engine.broker.costs import category, fill_costs
from engine.market_data.bus import TickBus
from engine.market_data.clock import utc_now
from engine.market_data.provider import Tick


@dataclass(frozen=True)
class _Price:
    value: Decimal
    received_at: datetime


class PaperBroker:
    def __init__(
        self,
        ticks: TickBus,
        instrument: Callable[[InstrumentKey], Instrument | None],
        *,
        slippage_ticks: int,
        max_price_age: timedelta,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._instrument = instrument
        self._slippage_ticks = slippage_ticks
        self._max_price_age = max_price_age
        self._now = now
        self._ticks = ticks.subscribe("paper-broker")
        self._prices: dict[InstrumentKey, _Price] = {}
        self._resting: dict[str, Order] = {}  # broker order id -> resting limit order
        self._book: dict[str, BrokerOrder] = {}  # every order seen this session
        self._updates: asyncio.Queue[BrokerUpdate] = asyncio.Queue()

    # ---- BrokerAdapter ------------------------------------------------------------------

    async def place(self, order: Order) -> BrokerAck:
        instrument = self._instrument(order.key)
        if instrument is None:
            raise BrokerRejected(f"{order.key} is not in the instrument master")
        broker_id = f"paper-{uuid.uuid4().hex[:12]}"
        if order.order_type is OrderType.MARKET:
            price = self._fresh_price(order.key)
            if price is None:
                raise BrokerRejected(
                    f"no fresh price for {order.key}: the latest is missing or stale"
                )
            fill_at = market_fill_price(
                price, instrument.tick_size, self._slippage_ticks, order.side
            )
            self._remember(broker_id, order)
            self._fill(broker_id, order, fill_at, instrument)
            return BrokerAck(broker_id)
        assert order.limit_price is not None  # guaranteed by the Order model
        self._remember(broker_id, order)
        price = self._fresh_price(order.key)
        if price is not None and _marketable(order.side, order.limit_price, price):
            better = (
                min(price, order.limit_price)
                if order.side is OrderSide.BUY
                else max(price, order.limit_price)
            )
            self._fill(broker_id, order, better, instrument)
        else:
            self._resting[broker_id] = order
        return BrokerAck(broker_id)

    async def cancel(self, order: Order) -> None:
        broker_id = order.broker_order_id
        if broker_id is None or broker_id not in self._resting:
            raise BrokerError(f"order {order.order_id} is not open at the paper broker")
        del self._resting[broker_id]
        self._book[broker_id] = _book_entry(broker_id, order, OrderState.CANCELLED)
        self._updates.put_nowait(Cancelled(broker_id, "cancelled", self._now()))

    async def order_book(self) -> list[BrokerOrder]:
        return list(self._book.values())

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        while True:
            yield await self._updates.get()

    # ---- price input --------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Track prices and fill resting limits as ticks arrive, until ``stop`` is set."""
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(anext(self._ticks), timeout=0.5)
            except TimeoutError:
                continue
            if isinstance(item, Tick):
                self.on_tick(item)

    def on_tick(self, tick: Tick) -> None:
        """Record the price; fill every resting limit this tick trades through."""
        self._prices[tick.key] = _Price(tick.ltp, tick.received_at)
        for broker_id, order in list(self._resting.items()):
            assert order.limit_price is not None
            if order.key == tick.key and _marketable(order.side, order.limit_price, tick.ltp):
                instrument = self._instrument(order.key)
                if instrument is None:
                    continue  # cannot price costs without the instrument; try next tick
                del self._resting[broker_id]
                self._fill(broker_id, order, order.limit_price, instrument)

    def restore(self, orders: list[Order]) -> None:
        """After a restart, resume the resting limit orders the engine still has open."""
        for order in orders:
            if order.state is OrderState.OPEN and order.broker_order_id is not None:
                self._resting[order.broker_order_id] = order
                self._book[order.broker_order_id] = _book_entry(
                    order.broker_order_id, order, OrderState.OPEN
                )

    # ---- internals ------------------------------------------------------------------------

    def _fresh_price(self, key: InstrumentKey) -> Decimal | None:
        price = self._prices.get(key)
        if price is None or self._now() - price.received_at > self._max_price_age:
            return None
        return price.value

    def _remember(self, broker_id: str, order: Order) -> None:
        self._book[broker_id] = _book_entry(broker_id, order, OrderState.OPEN)

    def _fill(self, broker_id: str, order: Order, price: Decimal, instrument: Instrument) -> None:
        costs = fill_costs(
            category(order.key.segment, instrument.instrument_type, order.product),
            order.side,
            order.quantity,
            price,
        )
        self._book[broker_id] = BrokerOrder(
            broker_id, order.order_id, OrderState.FILLED, order.quantity, price
        )
        self._updates.put_nowait(
            Filled(
                broker_id,
                f"fill-{uuid.uuid4().hex[:12]}",
                order.quantity,
                price,
                self._now(),
                costs,
            )
        )


def market_fill_price(
    last: Decimal, tick: Decimal, slippage_ticks: int, side: OrderSide
) -> Decimal:
    """Where a paper market order fills: ``last`` moved ``slippage_ticks`` ticks against the
    trader (spec "Buy with one tick of slippage"), never below one tick."""
    slip = tick * slippage_ticks
    if side is OrderSide.BUY:
        return last + slip
    return max(last - slip, tick)


def _marketable(side: OrderSide, limit: Decimal, price: Decimal) -> bool:
    """A buy limit fills at or below its limit; a sell limit at or above it."""
    return price <= limit if side is OrderSide.BUY else price >= limit


def _book_entry(broker_id: str, order: Order, state: OrderState) -> BrokerOrder:
    return BrokerOrder(broker_id, order.order_id, state, 0, None)
