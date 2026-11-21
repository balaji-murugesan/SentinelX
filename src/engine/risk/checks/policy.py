"""The instrument policy (add-risk-guards task 7.4; instrument-policy spec; expiry-day-profile
"Option entries in expiring contracts use the higher minimum premium"; design §7).

``InstrumentPolicy`` (``instrument_policy``) decides which instruments may be *entered* at all. It
judges in this order and returns the first refusal, so the audit names one reason:

  1. deny list       - the underlying is in ``denied_underlyings``: ``RISK_DENIED``.
  2. allow list      - an allow list is set and the underlying is not in it: ``RISK_NOT_ALLOWED``.
  3. F&O ban list    - only for ``NSE_FNO`` and ``BSE_FNO`` entries (``BAN_LIST_SEGMENTS``): no
                       list loaded for today, or the stored lists unread, fails closed with
                       ``RISK_BAN_LIST_MISSING``; an underlying on today's list: ``RISK_FNO_BAN``.
  4. min premium     - an option whose price is below the minimum: ``RISK_MIN_PREMIUM``.
  5. strike distance - an option whose strike is too far from the underlying's fresh price:
                       ``RISK_STRIKE_DISTANCE``; no fresh price for the underlying:
                       ``RISK_UNDERLYING_UNPRICED``.

It never refuses an exit, a missing ban list included (instrument-policy "Instrument policy never
applies to exits"): ``applies_to_exits = False``, so the gate does not run it on one.

Trading terms:
  * underlying      - what a contract is written on (every NIFTY option is on NIFTY); for a cash
                      equity, the share itself.
  * F&O ban list    - underlyings the exchange bars from new futures and options positions today,
                      because open interest passed 95% of the MWPL (market-wide position limit).
                      See ``engine.risk.banlist``. It does not cover commodity or currency
                      contracts, and a cash-equity entry in a banned name is not refused by it.
  * option          - an instrument with an ``option_type`` (CE: call, PE: put) in the master.
  * premium         - an option's price. A very cheap option is far out of the money and rarely
                      pays; ``min_premium`` keeps entries at or above a floor price. On a contract
                      that expires today the higher of ``min_premium`` and ``min_premium_expiry``
                      applies (``engine.risk.expiry.effective_min_premium``). The price judged is
                      the order's limit price, or the latest price for an order without one. A
                      price equal to the minimum is allowed; one paisa below is refused.
  * strike distance - how far an option's strike is from the underlying's latest price, as a
                      percent of that price: |strike - spot| / spot x 100. A strike far from the
                      money needs a large move to pay. ``max_strike_distance_pct`` bounds it.
  * spot            - the underlying's own price: its index row (NIFTY) or its equity row (a
                      stock option's share), found by ``InstrumentMaster.spot_of`` and read from
                      ``ctx.spot_key``. Its latest tick must be no older than ``stale_s`` (the
                      same threshold as ``StaleQuote``: exactly ``stale_s`` old is still fresh).
                      The index must be subscribed (``FEED_INSTRUMENTS``) to have a price.

Fail closed: an instrument the master does not know has no known underlying or option type, so
each step that needs one (because its limit is set, or the segment is a ban-list segment) refuses
with its own code rather than skip.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Final

from contracts.market import Instrument
from contracts.orders import Order
from engine.risk.banlist import BAN_LIST_SEGMENTS
from engine.risk.checks.common import money
from engine.risk.decision import ALLOW, Decision, RiskContext, reject
from engine.risk.expiry import effective_min_premium, expires_today

DENIED: Final = "RISK_DENIED"
NOT_ALLOWED: Final = "RISK_NOT_ALLOWED"
FNO_BAN: Final = "RISK_FNO_BAN"
BAN_LIST_MISSING: Final = "RISK_BAN_LIST_MISSING"
MIN_PREMIUM: Final = "RISK_MIN_PREMIUM"
STRIKE_DISTANCE: Final = "RISK_STRIKE_DISTANCE"
UNDERLYING_UNPRICED: Final = "RISK_UNDERLYING_UNPRICED"


def _unknown(order: Order) -> str:
    return f"{order.key} is not in the instrument master"


class InstrumentPolicy:
    name = "instrument_policy"
    applies_to_exits = False  # instrument-policy "Instrument policy never applies to exits"

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        instrument = ctx.instruments(order.key)
        # Each step returns a refusal or None (go on). ``or`` returns its first truthy operand,
        # like C#'s ``??`` chained over the steps: the first refusal wins.
        return (
            self._lists(ctx, order, instrument)
            or self._ban(ctx, order, instrument)
            or self._premium(ctx, order, instrument)
            or self._strike(ctx, order, instrument)
            or ALLOW
        )

    @staticmethod
    def _lists(ctx: RiskContext, order: Order, instrument: Instrument | None) -> Decision | None:
        denied, allowed = ctx.limits.denied_underlyings, ctx.limits.allowed_underlyings
        if denied is None and allowed is None:
            return None
        if instrument is None:
            code = DENIED if denied is not None else NOT_ALLOWED
            return reject(code, f"{_unknown(order)}, so its underlying cannot be checked")
        underlying = instrument.underlying_symbol.upper()
        if denied is not None and underlying in denied:
            return reject(
                DENIED, f"{underlying} is in the denied underlyings", underlying=underlying
            )
        if allowed is not None and underlying not in allowed:
            return reject(
                NOT_ALLOWED,
                f"{underlying} is not in the allowed underlyings",
                underlying=underlying,
            )
        return None

    @staticmethod
    def _ban(ctx: RiskContext, order: Order, instrument: Instrument | None) -> Decision | None:
        if order.key.segment not in BAN_LIST_SEGMENTS:
            return None  # cash equity, index, commodity, currency: no exchange F&O ban applies
        if ctx.ban_list is None:
            return reject(
                BAN_LIST_MISSING,
                "no F&O ban list is loaded for today (load it with `risk set-ban-list`)",
                instrument=str(order.key),
            )
        if instrument is None:
            return reject(FNO_BAN, f"{_unknown(order)}, so the ban list cannot be checked")
        underlying = instrument.underlying_symbol.upper()
        if underlying in ctx.ban_list:
            return reject(
                FNO_BAN, f"{underlying} is on today's F&O ban list", underlying=underlying
            )
        return None

    @staticmethod
    def _premium(ctx: RiskContext, order: Order, instrument: Instrument | None) -> Decision | None:
        if instrument is None:
            # Not known: it may be an option, and may expire today, so the higher minimum binds.
            if effective_min_premium(ctx.limits, expiring=True) is None:
                return None
            return reject(MIN_PREMIUM, f"{_unknown(order)}, so its premium cannot be checked")
        if instrument.option_type is None:
            return None  # only options have a premium
        expiring = expires_today(instrument, ctx.now)
        minimum = effective_min_premium(ctx.limits, expiring)
        if minimum is None:
            return None
        price: Decimal | None = order.limit_price
        if price is None:
            quote = ctx.quote(order.key)
            price = None if quote is None else quote.price
        if price is None:
            return reject(
                MIN_PREMIUM,
                f"{order.key} has no limit price and no latest price, so its premium cannot be "
                "checked",
                minimum=money(minimum),
            )
        if price < minimum:
            return reject(
                MIN_PREMIUM,
                f"premium {money(price)} is below the {'expiry-day ' if expiring else ''}minimum "
                f"of {money(minimum)}",
                price=money(price),
                minimum=money(minimum),
                expiring=expiring,
            )
        return None

    @staticmethod
    def _strike(ctx: RiskContext, order: Order, instrument: Instrument | None) -> Decision | None:
        limit = ctx.limits.max_strike_distance_pct
        if limit is None:
            return None
        if instrument is None:
            return reject(STRIKE_DISTANCE, f"{_unknown(order)}, so its strike cannot be checked")
        if instrument.option_type is None:
            return None  # futures and equity have no strike
        if instrument.strike is None:
            return reject(STRIKE_DISTANCE, f"{order.key} is an option with no strike in the master")
        underlying = instrument.underlying_symbol
        spot = ctx.spot_key(underlying)
        quote = None if spot is None else ctx.quote(spot)
        stale_after = timedelta(seconds=ctx.limits.stale_s)
        why: str | None = None
        if spot is None:
            why = "has no spot instrument in the master"
        elif quote is None:
            why = f"has no price for {spot}"
        elif quote.age > stale_after:
            why = (
                f"price for {spot} is {quote.age.total_seconds():.1f}s old "
                f"(threshold {ctx.limits.stale_s}s)"
            )
        if why is not None or quote is None:
            return reject(
                UNDERLYING_UNPRICED,
                f"the underlying {underlying} {why}, so the strike distance cannot be checked "
                "(subscribe its index or stock in FEED_INSTRUMENTS)",
                underlying=underlying,
            )
        distance = abs(instrument.strike - quote.price)
        if distance * 100 > limit * quote.price:  # distance / spot > limit / 100, no division
            return reject(
                STRIKE_DISTANCE,
                f"strike {money(instrument.strike)} is more than {money(limit)}% from "
                f"{underlying} at {money(quote.price)}",
                strike=money(instrument.strike),
                spot=money(quote.price),
                limit_pct=money(limit),
            )
        return None
