"""The execution interface (D-023): what the order service needs from a broker (design §5).

``BrokerAdapter`` is a ``Protocol`` (a structural interface, like a C# interface that classes
satisfy by shape). ``PaperBroker`` implements it now; ``DhanBroker`` (read side in
add-dhan-readonly, order placement in S7) will too. The shape mirrors Dhan's API:
request/response for place and cancel, plus a separate stream of updates (fills,
cancellations, rejections) like Dhan's order-update WebSocket.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from contracts.orders import CostBreakdown, Order, OrderState


class BrokerRejected(Exception):
    """The broker refused the order outright (it never became open)."""


class BrokerError(Exception):
    """A broker call failed for another reason (for example cancelling an order not open)."""


@dataclass(frozen=True)
class BrokerAck:
    broker_order_id: str


@dataclass(frozen=True)
class Filled:
    broker_order_id: str
    fill_id: str
    quantity: int
    price: Decimal
    at: datetime
    costs: CostBreakdown


@dataclass(frozen=True)
class Cancelled:
    broker_order_id: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class Rejected:
    broker_order_id: str
    reason: str
    at: datetime


BrokerUpdate = Filled | Cancelled | Rejected


@dataclass(frozen=True)
class BrokerOrder:
    """One entry of the broker's order book, for reconciliation (spec "Unknown orders...")."""

    broker_order_id: str
    engine_order_id: str  # the id the engine sent with the order (Dhan: correlationId)
    state: OrderState
    filled_quantity: int
    average_price: Decimal | None


class BrokerAdapter(Protocol):
    async def place(self, order: Order) -> BrokerAck:
        """Send ``order``; return once the broker has acknowledged it. Raises BrokerRejected
        if refused outright. Fills arrive later on ``updates()``."""
        ...

    async def cancel(self, order: Order) -> None:
        """Ask the broker to cancel; the confirmation arrives on ``updates()``. Raises
        BrokerError if the order is not open at the broker."""
        ...

    async def order_book(self) -> list[BrokerOrder]:
        """Today's orders as the broker sees them."""
        ...

    def updates(self) -> AsyncIterator[BrokerUpdate]:
        """Fills, cancellations and rejections, in the order the broker reports them."""
        ...
