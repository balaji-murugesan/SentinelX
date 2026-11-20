"""The day's P&L the risk engine watches: ``MTM_day``, net of charges (equity-floor spec).

Trading terms:
  * MTM          - mark-to-market: profit or loss at current prices, not yet locked in.
  * previous close - the price a carried position (held overnight) closed at yesterday. A
                   carried position's day P&L is measured from it, so the floor tracks *today's*
                   change only (owner decision Q13, 2026-11-15), not the whole holding period.
  * net of charges - brokerage, STT and fees already paid on today's fills are subtracted, and
                   so is the estimated cost of closing every open position at its current price
                   (owner decision Q18), using the same cost model as the paper broker.

Computed from cash flows, not from average prices, so it is right for carried, scaled and flipped
positions alike. For one position:

    qty_open  = qty_now - (sum of today's signed fills)     # what it held at the start of the day
    day_gross = (cash from today's fills: sells in, buys out)
              + qty_now  x last_price
              - qty_open x reference_price                   # the previous close

and ``MTM_day = sum of day_gross - costs paid today - estimated exit costs``. For a position
opened today ``qty_open`` is 0, so ``day_gross`` is exactly realized plus unrealized P&L.

No I/O and no clock: values in, values out. ``day_pnl`` returns None, "unknown", never a guessed
number, when it cannot value the day (the caller must fail closed):
  * an open position has no price, or its instrument is unknown; or
  * a carried position was fully closed today and has no previous-close reference. A flat position
    keeps no average price, so the price the quantity it opened the day with was worth cannot be
    recovered, and any stand-in (such as 0) would turn the whole sale into profit.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Fill, OrderSide, Position
from engine.broker.costs import category, fill_costs
from engine.positions.book import PositionId, position_id


@dataclass(frozen=True)
class DayPnl:
    gross: Decimal  # the day's P&L before any charges
    costs_paid: Decimal  # charges on today's fills
    estimated_exit_costs: Decimal  # estimated charges to close every open position now
    # Positions whose previous-close reference was missing, so their average price stood in
    # (recorded as RISK_REFERENCE_MISSING by the caller).
    reference_missing: tuple[PositionId, ...] = ()

    # ``@property`` is a read-only computed property, like a C# expression-bodied ``=>`` getter.
    @property
    def net(self) -> Decimal:
        """``MTM_day``: gross, less charges already paid, less the estimated cost to close."""
        return self.gross - self.costs_paid - self.estimated_exit_costs


def estimate_exit_cost(position: Position, last_price: Decimal, instrument: Instrument) -> Decimal:
    """The estimated charges to close ``position`` in full at ``last_price`` (0 if flat)."""
    if position.quantity == 0:
        return Decimal(0)
    closing_side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
    cost_category = category(position.key.segment, instrument.instrument_type, position.product)
    return fill_costs(cost_category, closing_side, abs(position.quantity), last_price).total


# ``Iterable`` is a C# ``IEnumerable``, ``Mapping`` an ``IReadOnlyDictionary``, and
# ``Callable[[InstrumentKey], Instrument | None]`` a ``Func<InstrumentKey, Instrument?>``.
def day_pnl(
    positions: Iterable[Position],
    fills_today: Iterable[Fill],
    prices: Mapping[InstrumentKey, Decimal],
    references: Mapping[PositionId, Decimal],
    instruments: Callable[[InstrumentKey], Instrument | None],
) -> DayPnl | None:
    """The day's P&L, or None when it cannot be known (see the module docstring).

    ``positions`` are the account's current positions (flat ones too). ``fills_today`` are the
    fills since the IST day began. ``prices`` are the last prices to value open positions at, and
    ``references`` the previous-close price of each position carried into today.

    Raises ValueError if two positions share an id, or a fill belongs to no listed position: the
    state is inconsistent and must not be turned into a number. A caller that catches it must
    treat the day's MTM as unknown, because the floor check is blind while it raises.
    """
    book: dict[PositionId, Position] = {}
    for held in positions:
        pid = position_id(held)
        if pid in book:
            raise ValueError(f"two positions share the id {pid}")
        book[pid] = held

    signed_today: dict[PositionId, int] = {}
    cash = Decimal(0)
    costs_paid = Decimal(0)
    for fill in fills_today:
        pid = position_id(fill)
        if pid not in book:
            raise ValueError(f"fill {fill.fill_id} belongs to no listed position")
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        signed_today[pid] = signed_today.get(pid, 0) + signed
        cash -= signed * fill.price  # a buy spends cash, a sell brings it in
        costs_paid += fill.costs.total

    value_now = Decimal(0)  # open positions at their last price
    value_at_open = Decimal(0)  # what each position was worth at the previous close
    estimated_exit = Decimal(0)
    missing: list[PositionId] = []
    for pid, position in book.items():
        if position.quantity != 0:
            price = prices.get(position.key)
            instrument = instruments(position.key)
            if price is None or instrument is None:
                return None  # an open position we cannot value: MTM is unknown
            value_now += position.quantity * price
            estimated_exit += estimate_exit_cost(position, price, instrument)
        quantity_open = position.quantity - signed_today.get(pid, 0)
        if quantity_open != 0:
            reference = references.get(pid)
            if reference is None:
                if position.quantity == 0:
                    return None  # closed today, no reference: its opening value is unrecoverable
                reference = position.average_price  # the spec's fallback: measure from entry
                missing.append(pid)
            value_at_open += quantity_open * reference

    return DayPnl(
        gross=cash + value_now - value_at_open,
        costs_paid=costs_paid,
        estimated_exit_costs=estimated_exit,
        reference_missing=tuple(missing),
    )
