"""The price rules: fat-finger protection and stale quotes (add-risk-engine task 6.2; risk-rules
spec "Price sanity"; stale-data-guard spec "Stale quotes block entries").

  * ``PriceSanity`` (``price_sanity``) - a *limit* order whose price is more than the configured
    percentage away from the latest price is refused (a "fat finger": a mistyped price). This is the
    one rule that also judges exits, because it is about the order itself, not about opening risk.
    With no fresh latest price it cannot judge: it returns ALLOW with a notice code, which the gate
    audits ("an exit with no fresh latest price is accepted and the skipped check audited"). An
    entry in that state is refused by ``StaleQuote``, not here.
  * ``StaleQuote`` (``stale_data``) - an entry in an instrument whose latest tick is older than the
    stale threshold, or that has none, is refused. Exits are never blocked by it. It cannot be run
    in shadow mode.
"""

from datetime import timedelta
from typing import Final

from contracts.orders import Order, OrderType
from engine.risk.checks.common import money
from engine.risk.decision import ALLOW, Decision, RiskContext, Severity, reject

PRICE_SANITY: Final = "RISK_PRICE_SANITY"
PRICE_UNVERIFIED: Final = "RISK_PRICE_UNVERIFIED"  # a notice, not a refusal
STALE_QUOTE: Final = "RISK_STALE_QUOTE"


class PriceSanity:
    name = "price_sanity"
    applies_to_exits = True

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        if order.order_type is not OrderType.LIMIT or order.limit_price is None:
            return ALLOW  # a market or stop order has no price of its own to check
        quote = ctx.quote(order.key)
        if quote is None or quote.age > timedelta(seconds=ctx.limits.stale_s):
            return Decision(
                Severity.ALLOW,
                PRICE_UNVERIFIED,
                "no fresh latest price: the limit price could not be checked",
                {"limit_price": money(order.limit_price), "has_quote": quote is not None},
            )
        band = ctx.limits.price_band_pct
        distance = abs(order.limit_price - quote.price)
        if distance * 100 > band * quote.price:  # distance / latest > band / 100, no division
            return reject(
                PRICE_SANITY,
                f"limit price {money(order.limit_price)} is more than {money(band)}% from the "
                f"latest price {money(quote.price)}",
                limit_price=money(order.limit_price),
                latest=money(quote.price),
                band_pct=money(band),
            )
        return ALLOW


class StaleQuote:
    name = "stale_data"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        quote = ctx.quote(order.key)
        if quote is None:
            return reject(STALE_QUOTE, f"no tick has been received for {order.key}")
        limit = timedelta(seconds=ctx.limits.stale_s)
        if quote.age > limit:
            return reject(
                STALE_QUOTE,
                f"the latest tick for {order.key} is {quote.age.total_seconds():.1f}s old "
                f"(threshold {ctx.limits.stale_s}s)",
                age_s=int(quote.age.total_seconds()),
                threshold_s=ctx.limits.stale_s,
            )
        return ALLOW
