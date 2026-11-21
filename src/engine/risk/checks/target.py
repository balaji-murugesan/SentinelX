"""The daily profit target (add-risk-guards task 9.1; profit-target spec; design §3).

Trading terms:
  * MTM_day             - "mark to market" for the day: the account's profit or loss today, with
                          open positions valued at their latest price (realised plus unrealised,
                          net of charges). The monitor computes it on every pass; None means it is
                          unknown (a held instrument has no price, say), never zero.
  * profit target       - ``daily_profit_target``, in rupees of MTM_day. Optional: unset, there is
                          no target and nothing here ever fires. Once it is reached the day is
                          banked: the book is flattened and entries stay locked until the next IST
                          trading date (``TARGET_HALT``), even if MTM_day falls back afterwards.
  * confirmation passes - ``target_confirm_ticks``: how many *consecutive* monitor passes must see
                          MTM_day at or above the target before it counts as reached, so one
                          spike in the price cannot stop a good day by itself. A pass below the
                          target, a pass with MTM_day unknown, or no target set resets the count
                          to zero. The count lives in the monitor's memory only, so it does not
                          survive a restart (the halt itself does: it is in the day's row).

Two pieces, both pure:
  * ``next_confirmations`` - the counting rule above. The monitor holds the number and calls this
                             once per pass, before it runs the checks.
  * ``ProfitTarget``       - the continuous check (``profit_target``): once
                             ``ctx.target_confirmations`` has reached ``target_confirm_ticks`` it
                             returns ``RISK_TARGET_HALT`` at severity ``TARGET_HALT``, which ranks
                             above every entry halt and below a KILL. The monitor acts on it
                             (``RiskMonitor._act``). It is continuous only: an order cannot reach
                             the target, so the gate does not run it and keeps the context's
                             default count of zero.

Shadow mode (``mode.profit_target = "shadow"``) runs the check and logs its verdict as a warning,
and the monitor neither halts nor flattens: a shadow result's effective severity is ALLOW.
"""

from decimal import Decimal
from typing import Final

from engine.risk.checks.common import money
from engine.risk.decision import ALLOW, Decision, RiskContext, RiskTrigger, Severity

TARGET_HALT: Final = "RISK_TARGET_HALT"


def next_confirmations(count: int, mtm_day: Decimal | None, target: Decimal | None) -> int:
    """The confirmation count after one more monitor pass: one more when MTM_day is known and at
    or above the target, otherwise zero (a dip, an unknown MTM_day, or no target set)."""
    if target is None or mtm_day is None or mtm_day < target:
        return 0
    return count + 1


class ProfitTarget:
    name = "profit_target"

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        target, needed = ctx.limits.daily_profit_target, ctx.limits.target_confirm_ticks
        if target is None or needed is None:
            return ALLOW  # profit-target "No target set" (a target always has a count: limits)
        if ctx.target_confirmations < needed:
            return ALLOW
        mtm = "unknown" if ctx.mtm_day is None else money(ctx.mtm_day)
        return Decision(
            Severity.TARGET_HALT,
            TARGET_HALT,
            f"MTM_day {mtm} has been at or above the profit target {money(target)} on "
            f"{ctx.target_confirmations} consecutive passes (needed {needed}): the day is banked",
            {
                "mtm_day": mtm,
                "target": money(target),
                "confirmations": ctx.target_confirmations,
                "needed": needed,
            },
        )
