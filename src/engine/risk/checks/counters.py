"""The day-counter rules: entries per day and the consecutive-loss cooldown (add-risk-engine
task 6.4; risk-rules spec "Maximum trades per day" and "Consecutive-loss cooldown").

The counters themselves live in the day book (``engine.risk.daybook``), which persists them; these
checks only read ``ctx.day``. Exits are never touched by either rule.

  * ``MaxTrades`` (``max_trades``) - once the day's entry count has reached the limit, further
    entries are refused. The count includes entries the broker later rejected.
  * ``Cooldown`` (``cooldown``)    - after the configured number of consecutive losing trades,
    entries are refused until the cooldown end time has passed.
"""

from typing import Final

from contracts.orders import Order
from engine.risk.decision import ALLOW, Decision, RiskContext, reject

MAX_TRADES: Final = "RISK_MAX_TRADES"
COOLDOWN: Final = "RISK_COOLDOWN"


class MaxTrades:
    name = "max_trades"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        done, limit = ctx.day.entries_today, ctx.limits.max_trades_per_day
        if done >= limit:
            return reject(
                MAX_TRADES,
                f"{done} entries already allowed today, the maximum is {limit}",
                entries_today=done,
                limit=limit,
            )
        return ALLOW


class Cooldown:
    name = "cooldown"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        until = ctx.day.cooldown_until
        if until is not None and ctx.now < until:
            return reject(
                COOLDOWN,
                f"{ctx.day.loss_streak} losses in a row: no entries until {until.isoformat()}",
                loss_streak=ctx.day.loss_streak,
                until=until.isoformat(),
            )
        return ALLOW
