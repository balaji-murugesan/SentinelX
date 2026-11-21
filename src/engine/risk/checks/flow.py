"""The order-flow guards (add-risk-guards task 5.2; order-flow-guards spec; design §5).

``EntryPacing`` (``entry_pacing``) slows a burst of entries, so a runaway signal cannot turn into
a storm of trades:

  * per minute  - an entry is refused with ``RISK_ENTRY_RATE`` when the entries the gate allowed
                  in the 60 seconds before it already number ``max_entries_per_minute``. An entry
                  exactly 60 s old still counts (the tight side of the edge); one 61 s old does
                  not. The times come from ``ctx.recent_entries`` (the day book's in-memory
                  window, so a restart forgets it: design, Risks).
  * minimum gap - an entry is refused with ``RISK_ENTRY_GAP`` when fewer than ``min_entry_gap_s``
                  seconds have passed since the last entry the gate allowed today
                  (``ctx.day.last_entry_at``, persisted with the day's state, so it survives a
                  restart). Exactly ``min_entry_gap_s`` seconds later is allowed.

Exits never count and are never refused: the gate does not run entry rules on them
(``applies_to_exits = False``) and only counts an allowed entry.

``RejectBreaker`` (``reject_breaker``, task 6.2) is a circuit breaker on broker rejections: when
``reject_breaker_n`` of them fall within the last 5 minutes (``ctx.broker_rejections``, fed by
``engine.risk.rejections.RejectionWindow``), new entries are refused with ``RISK_REJECT_BREAKER``
(severity HALT_NEW_ENTRIES, PRD R6). It does not latch: it clears by itself once fewer than
``reject_breaker_n`` rejections remain in the window (design, Risks). A rejection exactly 300 s old
still counts. It judges at both phases:

  * pre-trade  - so the entry right after the N-th rejection is refused at once, before the
                 monitor's next pass;
  * continuous - so the monitor holds it as a standing halt and logs the trip once, as critical,
                 when it starts (``RiskMonitor._audit_changes``), not on every pass.

Exits are never refused by it (order-flow-guards "Exit while tripped"): ``applies_to_exits`` is
False, and the standing halt the monitor holds is read by the gate for entries only. A rejected
exit still counts as a rejection ("Rejected exit counts").
"""

from datetime import timedelta
from typing import Final

from contracts.orders import Order
from engine.risk.decision import (
    ALLOW,
    Decision,
    RiskContext,
    RiskTrigger,
    halt_new_entries,
    reject,
)
from engine.risk.rejections import REJECTION_WINDOW

ENTRY_RATE: Final = "RISK_ENTRY_RATE"
ENTRY_GAP: Final = "RISK_ENTRY_GAP"
REJECT_BREAKER: Final = "RISK_REJECT_BREAKER"
PACING_WINDOW: Final = timedelta(seconds=60)


class EntryPacing:
    name = "entry_pacing"
    applies_to_exits = False  # order-flow-guards "An exit right after an entry"

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        limit = ctx.limits.max_entries_per_minute
        in_window = sum(1 for at in ctx.recent_entries if ctx.now - at <= PACING_WINDOW)
        if in_window >= limit:
            return reject(
                ENTRY_RATE,
                f"{in_window} entries allowed in the last 60 seconds, the maximum is {limit}",
                entries_last_minute=in_window,
                limit=limit,
            )
        last, gap = ctx.day.last_entry_at, timedelta(seconds=ctx.limits.min_entry_gap_s)
        if last is not None and ctx.now - last < gap:
            waited = ctx.now - last
            return reject(
                ENTRY_GAP,
                f"the last entry was allowed {waited.total_seconds():g} s ago, entries must be at "
                f"least {ctx.limits.min_entry_gap_s} s apart",
                last_entry_at=last.isoformat(),
                seconds_since=f"{waited.total_seconds():g}",
                limit=ctx.limits.min_entry_gap_s,
            )
        return ALLOW


class RejectBreaker:
    name = "reject_breaker"
    applies_to_exits = False  # order-flow-guards "Exit while tripped"

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        return self._judge(ctx)

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        return self._judge(ctx)

    @staticmethod
    def _judge(ctx: RiskContext) -> Decision:
        """Tripped while the rejections within 300 s of ``ctx.now`` number at least the limit.
        Counted here as well as in the window, so the edge is this rule's decision whatever the
        context was given."""
        limit = ctx.limits.reject_breaker_n
        in_window = sum(1 for at in ctx.broker_rejections if ctx.now - at <= REJECTION_WINDOW)
        if in_window >= limit:
            return halt_new_entries(
                REJECT_BREAKER,
                f"the broker rejected {in_window} orders in the last 5 minutes (the breaker trips "
                f"at {limit}): new entries are halted until fewer remain in the window",
                rejections_5m=in_window,
                limit=limit,
            )
        return ALLOW
