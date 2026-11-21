"""The lot caps (add-risk-guards task 5.1; lot-caps spec; expiry-day-profile "Entries in expiring
contracts use the expiry lot caps"; design §5).

``LotCaps`` (``lot_caps``) bounds how large the book can grow, measured in lots. An entry is
refused when, counting its own lots:

  * the open plus pending lots on its underlying exceed ``max_open_lots_per_underlying``
    (``RISK_LOTS_UNDERLYING``);
  * the open plus pending lots across all underlyings exceed ``max_open_lots_total``
    (``RISK_LOTS_TOTAL``);
  * the gross lots of every entry allowed today exceed ``max_gross_lots_per_day``
    (``RISK_LOTS_DAY``).

For an entry whose contract expires today the two open-lot caps are the expiry caps
(``engine.risk.expiry.effective_lot_caps``); the daily gross cap is the same on every day.

The caps count the lots of every instrument except cash equity and index rows (owner rulings,
2026-11-21): an equity's "lot" is one share, so a holding of shares would swamp a cap meant for
contracts. An entry in a cash equity is not judged by this rule, and cash positions and orders
count nothing. Every other segment is capped (F&O, currency, MCX commodity, and any segment
added later): the exemption is a deny-list (``common.lot_capped``), so a new segment that nobody
thought of is capped, not waved through. The segment comes from the instrument key, so an
equity the master does not know is still skipped, while an unknown contract in a capped segment
is refused.

Trading terms:
  * lot          - the exchange's trading unit for a contract, from the instrument master (a NIFTY
                   option lot is 65 or 75 units). Order and position quantities are in units, so
                   lots = |quantity| / lot size; a part lot counts as a whole one (the safe side;
                   validation lets only whole lots through anyway).
  * underlying   - what a contract is written on: every NIFTY option or future is on NIFTY.
  * open lots    - the lots of every open position, carried ones from earlier days included.
  * pending lots - the unfilled part of every resting *entry* order. It reserves its lots until
                   the order is final (filled, rejected or cancelled), so two entries in flight
                   cannot each pass the cap on their own. A resting order that only reduces a
                   position (an exit, a protective stop) reserves nothing: each resting order is
                   classified against the current positions with the same ``classify`` the gate
                   uses.
  * gross lots   - the sum of the lots of every entry the gate allowed today, whether or not it
                   filled; exits never add to it. The day book counts and persists it before an
                   entry is sent (``DayBook.entry_allowed``).
  * expiry caps  - optional tighter open-lot caps for contracts that expire today, when premiums
                   move fastest.

Fail closed (lot-caps "Lots are counted from the instrument master"): if the entry's capped
instrument, or any capped instrument it would have to count, has no master record, its lots (and
its underlying) cannot be known, so the entry is refused with ``RISK_LOTS_UNDERLYING``, the first
cap checked.
"""

from typing import Final

from contracts.market import InstrumentKey
from contracts.orders import Order
from engine.orders.intent import Intent, classify
from engine.risk.checks.common import lot_capped
from engine.risk.decision import ALLOW, Decision, RiskContext, reject
from engine.risk.expiry import effective_lot_caps, expires_today

LOTS_UNDERLYING: Final = "RISK_LOTS_UNDERLYING"
LOTS_TOTAL: Final = "RISK_LOTS_TOTAL"
LOTS_DAY: Final = "RISK_LOTS_DAY"


def lots_of(quantity: int, lot_size: int) -> int:
    """Whole lots in ``quantity`` units. ``-(-a // b)`` is ceiling division (``//`` floors, like
    C# ``Math.Floor`` on a division), so a part lot counts as a whole one."""
    return -(-abs(quantity) // lot_size)


def capped_lots(key: InstrumentKey, quantity: int, lot_size: int) -> int:
    """The lots the caps count for ``quantity`` units of ``key``: whole lots in a capped segment,
    0 in an exempt one (cash equity, index). The gate adds this to the day's gross lots, so the
    check and the count agree."""
    return lots_of(quantity, lot_size) if lot_capped(key) else 0


def book_lots(ctx: RiskContext, order: Order) -> dict[str, int] | InstrumentKey:
    """The open plus pending capped lots per underlying, not counting ``order`` itself. Returns
    the key of the first counted capped instrument that has no master record instead, when there
    is one: its lots and underlying cannot be known, so no cap can be judged. Positions and orders
    in exempt segments are skipped before any lookup, so an unknown equity changes nothing.

    The return type ``dict[str, int] | InstrumentKey`` is a union (either one); the caller tells
    them apart with ``isinstance``, like a C# ``is`` pattern."""
    lots: dict[str, int] = {}

    def count(key: InstrumentKey, quantity: int) -> InstrumentKey | None:
        if not lot_capped(key):
            return None  # cash equity and index rows count no lots
        instrument = ctx.instruments(key)
        if instrument is None:
            return key
        underlying = instrument.underlying_symbol
        lots[underlying] = lots.get(underlying, 0) + lots_of(quantity, instrument.lot_size)
        return None

    for held in ctx.positions.values():
        if held.quantity != 0 and (unknown := count(held.key, held.quantity)) is not None:
            return unknown
    for resting in ctx.orders.values():
        if resting.state.is_final or resting.order_id == order.order_id:
            continue
        # Only the unfilled part is pending: a partial fill is already in the position. It is
        # classified on that part too, so a partly filled exit is not mistaken for an entry
        # because its full quantity is now larger than what is left of the position.
        # ``model_copy(update=...)`` returns a changed copy of a frozen pydantic model, like a C#
        # ``record`` ``with`` expression.
        unfilled = abs(resting.quantity) - resting.filled_quantity
        if unfilled <= 0:
            continue
        rest = resting.model_copy(update={"quantity": unfilled})
        if classify(rest, ctx.positions) is not Intent.ENTRY:
            continue
        if (unknown := count(resting.key, unfilled)) is not None:
            return unknown
    return lots


class LotCaps:
    name = "lot_caps"
    applies_to_exits = False  # lot-caps "Lot caps never apply to exits"

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        if not lot_capped(order.key):
            return ALLOW  # an equity entry is not judged by the lot caps
        instrument = ctx.instruments(order.key)
        if instrument is None:
            return reject(
                LOTS_UNDERLYING,
                f"{order.key} is not in the instrument master, so its lots cannot be counted",
                instrument=str(order.key),
            )
        book = book_lots(ctx, order)
        if isinstance(book, InstrumentKey):
            return reject(
                LOTS_UNDERLYING,
                f"{book} is in the book but not in the instrument master, so its lots and "
                "underlying cannot be counted",
                instrument=str(book),
            )
        mine = lots_of(order.quantity, instrument.lot_size)
        expiring = expires_today(instrument, ctx.now)
        cap_underlying, cap_total = effective_lot_caps(ctx.limits, expiring)
        underlying = instrument.underlying_symbol
        on_underlying = book.get(underlying, 0)
        if on_underlying + mine > cap_underlying:
            return reject(
                LOTS_UNDERLYING,
                f"{on_underlying} lots open or pending on {underlying} plus this entry's {mine} "
                f"would exceed the {'expiry-day ' if expiring else ''}cap of {cap_underlying}",
                underlying=underlying,
                open_or_pending=on_underlying,
                lots=mine,
                limit=cap_underlying,
                expiring=expiring,
            )
        total = sum(book.values())
        if total + mine > cap_total:
            return reject(
                LOTS_TOTAL,
                f"{total} lots open or pending in total plus this entry's {mine} would exceed "
                f"the {'expiry-day ' if expiring else ''}cap of {cap_total}",
                open_or_pending=total,
                lots=mine,
                limit=cap_total,
                expiring=expiring,
            )
        today, cap_day = ctx.day.entry_lots_today, ctx.limits.max_gross_lots_per_day
        if today + mine > cap_day:
            return reject(
                LOTS_DAY,
                f"{today} lots already allowed today plus this entry's {mine} would exceed the "
                f"daily cap of {cap_day}",
                entry_lots_today=today,
                lots=mine,
                limit=cap_day,
            )
        return ALLOW
