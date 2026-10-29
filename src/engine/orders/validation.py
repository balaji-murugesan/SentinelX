"""Order validation against instrument facts (orders spec; add-order-position-core task 3.1).

These are not risk limits (S3): they are what the exchange itself would refuse, taken from
the instrument master. A failing check returns the reason text the order is rejected with.

Trading terms:
  * lot size     - F&O trades in whole lots; quantity is in units, so it must be a multiple.
  * freeze qty   - the exchange freezes (refuses) any single order at or above it.
  * tick size    - the price grid; a limit price off the grid is refused.
"""

from dataclasses import dataclass

from contracts.commands import PlaceOrder
from contracts.market import ExchangeSegment, Instrument
from contracts.orders import OrderSide, OrderType, ProductType

# Which product types each segment accepts (spec "Product type allowed for the segment").
# CNC is equity delivery; MARGIN carries F&O overnight; INTRADAY works for both.
ALLOWED_PRODUCTS: dict[ExchangeSegment, frozenset[ProductType]] = {
    ExchangeSegment.NSE_EQ: frozenset({ProductType.INTRADAY, ProductType.CNC}),
    ExchangeSegment.NSE_FNO: frozenset({ProductType.INTRADAY, ProductType.MARGIN}),
}


@dataclass(frozen=True)
class Holdings:
    """What a CNC sell may draw on: shares held under CNC, minus those already being sold by
    open CNC sell orders (spec "Delivery sell only against holdings")."""

    held: int = 0
    selling: int = 0


NO_HOLDINGS = Holdings()  # a frozen singleton, safe to share as a default


def validate(
    command: PlaceOrder,
    instrument: Instrument | None,
    *,
    master_loaded: bool,
    holdings: Holdings = NO_HOLDINGS,
) -> str | None:
    """The reason to reject ``command``, or None if it passes every instrument check."""
    if not master_loaded:
        return "instrument data is unavailable: the instrument master has not loaded"
    segment = command.key.segment
    if segment is ExchangeSegment.IDX_I:
        return f"{command.key} is an index: indices cannot be ordered"
    if instrument is None:
        return f"{command.key} is not in the instrument master"
    allowed = ALLOWED_PRODUCTS.get(segment)
    if allowed is None:
        return f"segment {segment.value} is not supported for orders"
    if command.product not in allowed:
        names = ", ".join(sorted(p.value for p in allowed))
        return f"product {command.product.value} is not allowed for {segment.value} (use {names})"
    if command.quantity % instrument.lot_size:
        return (
            f"quantity {command.quantity} is not a whole number of lots "
            f"(lot size {instrument.lot_size})"
        )
    if reason := _freeze_reason(command, instrument):
        return reason
    if command.order_type is OrderType.LIMIT:
        assert command.limit_price is not None  # guaranteed by the PlaceOrder model
        if command.limit_price % instrument.tick_size:
            return (
                f"limit price {command.limit_price} is not on the tick grid "
                f"(tick size {instrument.tick_size})"
            )
    if command.product is ProductType.CNC and command.side is OrderSide.SELL:
        available = holdings.held - holdings.selling
        if command.quantity > available:
            return (
                f"CNC sell of {command.quantity} exceeds held quantity: {holdings.held} held, "
                f"{holdings.selling} already being sold; cash-equity shorts need INTRADAY"
            )
    return None


def _freeze_reason(command: PlaceOrder, instrument: Instrument) -> str | None:
    if instrument.freeze_qty is None:
        if command.key.segment is ExchangeSegment.NSE_FNO:
            # Fail closed: without the limit we cannot know the exchange will accept it.
            return f"freeze quantity for {command.key} is unknown: F&O orders need it"
        return None  # cash equity has no exchange freeze limit in Dhan's file
    if command.quantity >= instrument.freeze_qty:
        return (
            f"quantity {command.quantity} is at or above the freeze quantity "
            f"{instrument.freeze_qty}: the exchange would freeze it"
        )
    return None
