"""Pure position arithmetic (positions spec; design §8).

No I/O and no clock: every function takes values and returns new ones, so the same fills
always give the same positions (spec "Rebuild from fills"). Quantities are in units (lots x
lot size), so P&L needs no lot multiplier.

Trading terms:
  * average price - quantity-weighted open price of the units still held.
  * realized P&L  - profit or loss locked in by closing units, gross of costs.
  * MTM           - mark-to-market: unrealized P&L of the open units at the last price.
  * flip          - one fill that closes the position and opens the other direction.

The average is NOT rounded to the tick: rounding would make a fill split into parts give a
different average than the same fill in one piece. ``Decimal`` keeps 28 significant digits.
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from contracts.market import InstrumentKey
from contracts.orders import Fill, OrderSide, Position, ProductType

type PositionId = tuple[str, InstrumentKey, ProductType]  # (account, instrument, product)


def position_id(item: Fill | Position) -> PositionId:
    return item.account_id, item.key, item.product


def flat(account_id: str, key: InstrumentKey, product: ProductType, at: datetime) -> Position:
    return Position(account_id=account_id, key=key, product=product, updated_at=at)


def net(
    quantity: int, average: Decimal, signed_fill: int, price: Decimal
) -> tuple[int, Decimal, Decimal]:
    """Net a signed fill (+ buy, - sell) into a signed position.

    Returns (new quantity, new average price, P&L realized by this fill). Three cases:
    increase (same direction or from flat), reduce (opposite, not larger), flip (opposite,
    larger: close everything, open the rest at ``price``). A flat result has average 0.
    """
    new_quantity = quantity + signed_fill
    if quantity == 0 or (quantity > 0) == (signed_fill > 0):
        # Increase: weighted average of the held units and the new ones. No P&L is locked in.
        average = (average * abs(quantity) + price * abs(signed_fill)) / abs(new_quantity)
        return new_quantity, average, Decimal(0)

    # Reduce or flip: the units closed are capped at what is held. They carry the position's
    # sign, so (price - average) x closed is positive for a winning long AND a winning short.
    closed = min(abs(signed_fill), abs(quantity)) * (1 if quantity > 0 else -1)
    realized = (price - average) * closed
    if new_quantity == 0:
        return 0, Decimal(0), realized
    if (new_quantity > 0) == (quantity > 0):
        return new_quantity, average, realized  # reduce: the remaining units keep their average
    return new_quantity, price, realized  # flip: the remainder opens at the fill price


def apply(position: Position, fill: Fill) -> tuple[Position, Decimal]:
    """The position after ``fill``, and the gross P&L that fill realized."""
    if position_id(position) != position_id(fill):
        raise ValueError(f"fill {fill.fill_id} is for another position")
    signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
    quantity, average, realized = net(position.quantity, position.average_price, signed, fill.price)
    # model_copy(update=...) returns a changed copy of a frozen model, like a C# `with` expression.
    updated = position.model_copy(
        update={
            "quantity": quantity,
            "average_price": average,
            "realized_pnl": position.realized_pnl + realized,
            "costs": position.costs + fill.costs.total,
            "updated_at": fill.filled_at,
        }
    )
    return updated, realized


def rebuild(fills: Iterable[Fill]) -> dict[PositionId, Position]:
    """Positions from fills applied in the given (fill) order, starting flat."""
    positions: dict[PositionId, Position] = {}
    for fill in fills:
        pid = position_id(fill)
        current = positions.get(pid) or flat(*pid, at=fill.filled_at)
        positions[pid], _ = apply(current, fill)
    return positions


def mtm(position: Position, last_price: Decimal) -> Decimal:
    """Unrealized P&L at ``last_price``. With a signed quantity one formula serves both sides:
    for a short, (last - average) x (negative qty) == (average - last) x |qty|."""
    return (last_price - position.average_price) * position.quantity


def net_pnl(position: Position, last_price: Decimal) -> Decimal:
    """Realized + MTM - costs: the figure the dashboard shows (no client computes it)."""
    return position.realized_pnl + mtm(position, last_price) - position.costs
