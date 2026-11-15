"""Is an order an entry or an exit? (add-risk-engine task 5.1; risk-gate spec "Entries and exits
are told apart by position effect"; design §5.)

The answer decides almost everything the risk engine does: entry rules (limits, cutoffs, caps,
cooldowns, halts) apply only to entries, exits are never blocked by them, and the rate limiter
gives exits a reserved share. So one pure function, shared by the order service, the gate and the
limiter, decides it from the position alone: no clock, no I/O, nothing the caller can configure.

The rule: an order is an **exit** only if it is on the opposite side of the account's position in
that same instrument *and product* and its quantity is at most the position's size. Anything else
is an **entry**:
  * the order opens a position (there is none), or grows one (same side);
  * the order flips through zero (a sell of 130 against a long of 65): the 65 that would open the
    short make the whole order an entry, so every entry rule applies to it;
  * the order is in a different product (a long 65 INTRADAY is not closed by a MARGIN sell).

Resting exit orders are *not* netted against the position: two exits of 65 against a long of 65
would each classify as an exit, and if both filled the position would flip. That is accepted here
(design §5): the catastrophic stop is cancelled when the position goes flat, and the exit manager
of Stage 4 sizes exits against the ones already resting.
"""

from collections.abc import Mapping
from enum import StrEnum

from contracts.commands import PlaceOrder
from contracts.orders import Order, OrderSide, Position
from engine.positions.book import PositionId


class Intent(StrEnum):
    """``StrEnum`` is a C# ``enum`` whose members are also strings (they log and serialise as
    their text)."""

    ENTRY = "ENTRY"  # opens a position, grows one, or flips through zero
    EXIT = "EXIT"  # only reduces an existing position, by at most its size


def classify(order: Order | PlaceOrder, positions: Mapping[PositionId, Position]) -> Intent:
    """The intent of ``order`` given the account's current ``positions``. A ``PlaceOrder`` command
    is accepted as well as an ``Order``: both carry the five fields it reads (account, instrument,
    product, side, quantity), so the order service can classify a command before it builds the
    order (``A | B`` is a union type: either one).

    ``positions`` maps ``(account_id, instrument, product)`` to the position, the same mapping
    ``OrderService.positions`` holds. A position that is missing or flat means the order opens
    one: an entry.
    """
    position = positions.get((order.account_id, order.key, order.product))
    held = 0 if position is None else position.quantity
    if held > 0 and order.side is OrderSide.SELL and order.quantity <= held:
        return Intent.EXIT  # reduces a long (a sell of the whole position is still an exit)
    if held < 0 and order.side is OrderSide.BUY and order.quantity <= -held:
        return Intent.EXIT  # reduces a short
    return Intent.ENTRY
