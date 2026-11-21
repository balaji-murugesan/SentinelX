"""Small pure helpers the pre-trade checks share: the price an entry is expected at, and its risk.

Trading terms:
  * entry price   - where the order is expected to fill. For a limit order that is its limit price;
                    for a stop-market order its trigger; for a paper market order the latest price
                    (the paper broker fills a market order at the latest price, plus slippage).
  * initial stop  - where the trader accepts being wrong. The distance from the entry price to it,
                    times the quantity, is the trade's *risk*: what is lost if the stop is hit.
  * cash segment  - where shares trade (NSE_EQ, BSE_EQ). An equity's "lot" is one share. Index
                    rows (IDX_I) are prices only and cannot be traded. Every other segment holds
                    contracts that trade in whole lots: F&O, currency, commodity (MCX).
"""

from decimal import Decimal
from typing import Final

from contracts.market import ExchangeSegment, InstrumentKey
from contracts.orders import Order, OrderSide, OrderType
from engine.risk.decision import RiskContext

# The segments the lot caps do NOT count: cash equity and index rows only. This is a deny-list on
# purpose (owner ruling 2026-11-21): a segment added later (a commodity or currency segment, a
# second broker's) is capped unless someone exempts it here, so forgetting to update this set
# fails closed, never open. Decided from the instrument key alone, so an instrument the master
# does not know still has a known segment.
LOT_CAP_EXEMPT_SEGMENTS: Final = frozenset(
    {ExchangeSegment.NSE_EQ, ExchangeSegment.BSE_EQ, ExchangeSegment.IDX_I}
)


def lot_capped(key: InstrumentKey) -> bool:
    """Do the lot caps count ``key``? True for every segment except cash equity and index."""
    return key.segment not in LOT_CAP_EXEMPT_SEGMENTS


def money(value: Decimal) -> str:
    """Exact text for an audit row or a reason (never a float, never an exponent: ``1E+3``)."""
    return format(value, "f")


def entry_price(order: Order, ctx: RiskContext) -> Decimal | None:
    """The price ``order`` is expected at, or None when there is none to read (a market order in
    an instrument with no latest price: the stale-data rule refuses that entry)."""
    match order.order_type:
        case OrderType.LIMIT:
            return order.limit_price
        case OrderType.STOP_MARKET:
            return order.trigger_price
        case OrderType.MARKET:
            quote = ctx.quote(order.key)
            return None if quote is None else quote.price


def stop_on_loss_side(side: OrderSide, price: Decimal, stop: Decimal) -> bool:
    """A buy loses when the price falls, so its stop is below the entry price; a sell loses when
    it rises, so its stop is above."""
    return stop < price if side is OrderSide.BUY else stop > price


def risk_per_unit(price: Decimal, stop: Decimal) -> Decimal:
    return abs(price - stop)
