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
(``applies_to_exits = False``) and only counts an allowed entry. The rejection breaker joins this
module in task 6.2.
"""

from datetime import timedelta
from typing import Final

from contracts.orders import Order
from engine.risk.decision import ALLOW, Decision, RiskContext, reject

ENTRY_RATE: Final = "RISK_ENTRY_RATE"
ENTRY_GAP: Final = "RISK_ENTRY_GAP"
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
