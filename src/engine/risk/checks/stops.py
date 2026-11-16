"""The stop rules: every entry carries a sane initial stop, whose risk is within limits (add-risk-
engine task 6.1; risk-rules spec "Every entry carries an initial stop on the loss side", "Per-trade
maximum risk" and "Initial stop inside the catastrophic stop").

Three checks, each a pure function of the order and the ``RiskContext``:

  * ``InitialStop`` (``initial_stop``)   - an entry needs a stop, on the loss side, on the tick
                                           grid. It cannot be run in shadow mode: without a stop no
                                           risk can be measured at all.
  * ``PerTradeRisk`` (``per_trade_risk``) - distance to the stop x quantity must not exceed the
                                           per-trade maximum.
  * ``StopDistance`` (``stop_distance``)  - the stop must be strictly closer than the catastrophic
                                           stop, which has to stay the wider backstop.

When the entry price cannot be read (a market order with no latest price) the price-dependent
checks have no opinion and say ALLOW: the stale-data rule refuses that entry, and it cannot be
shadowed, so an entry with no price never gets through.
"""

from typing import Final

from contracts.orders import Order
from engine.risk.checks.common import entry_price, money, risk_per_unit, stop_on_loss_side
from engine.risk.decision import ALLOW, Decision, RiskContext, reject

NO_STOP: Final = "RISK_NO_STOP"
PER_TRADE: Final = "RISK_PER_TRADE"
STOP_BEYOND_CATASTROPHIC: Final = "RISK_STOP_BEYOND_CATASTROPHIC"


class InitialStop:
    name = "initial_stop"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        stop = order.initial_stop
        if stop is None:
            return reject(NO_STOP, "an entry must carry an initial stop")
        instrument = ctx.instruments(order.key)
        if instrument is None:
            # Validation refuses an unknown instrument before the gate; failing closed anyway.
            return reject(
                NO_STOP, "instrument facts are unavailable, so the stop cannot be checked"
            )
        if stop % instrument.tick_size:
            return reject(
                NO_STOP,
                f"initial stop {money(stop)} is not on the tick grid (tick size "
                f"{money(instrument.tick_size)})",
                stop=money(stop),
                tick_size=money(instrument.tick_size),
            )
        price = entry_price(order, ctx)
        if price is not None and not stop_on_loss_side(order.side, price, stop):
            side = "below" if order.side.value == "BUY" else "above"
            return reject(
                NO_STOP,
                f"initial stop {money(stop)} must be {side} the entry price {money(price)}",
                stop=money(stop),
                entry=money(price),
                side=order.side.value,
            )
        return ALLOW


class PerTradeRisk:
    name = "per_trade_risk"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        price, stop = entry_price(order, ctx), order.initial_stop
        if price is None or stop is None:
            return ALLOW  # no stop or no price: the other rules refuse that entry
        risk = risk_per_unit(price, stop) * order.quantity
        limit = ctx.limits.per_trade_max_risk
        if risk > limit:
            return reject(
                PER_TRADE,
                f"risk {money(risk)} exceeds the per-trade maximum {money(limit)}",
                risk=money(risk),
                limit=money(limit),
                entry=money(price),
                stop=money(stop),
                quantity=order.quantity,
            )
        return ALLOW


class StopDistance:
    name = "stop_distance"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        price, stop = entry_price(order, ctx), order.initial_stop
        if price is None or stop is None:
            return ALLOW
        # distance / price >= pct / 100, cross-multiplied: no division, so no rounding.
        pct = ctx.limits.catastrophic_sl_pct
        if risk_per_unit(price, stop) * 100 >= pct * price:
            return reject(
                STOP_BEYOND_CATASTROPHIC,
                f"initial stop {money(stop)} is as far from {money(price)} as the catastrophic "
                f"stop ({money(pct)}%) or farther: it must stay the wider backstop",
                stop=money(stop),
                entry=money(price),
                catastrophic_pct=money(pct),
            )
        return ALLOW
