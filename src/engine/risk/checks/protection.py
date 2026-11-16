"""An unprotected position blocks new entries (add-risk-engine task 9.2; catastrophic-stop spec "An
unprotected position blocks new entries").

``Unprotected`` is a continuous check: while any open position has no catastrophic stop resting at
the broker (``engine.risk.stops`` works that out from the live book, in market hours), new entries
are halted. The halt clears by itself on the first pass where every open position has its stop.
Exits are never blocked by it, and the stop manager keeps retrying with backoff.
"""

from typing import Final

from engine.risk.decision import ALLOW, Decision, RiskContext, RiskTrigger, halt_new_entries

UNPROTECTED: Final = "RISK_UNPROTECTED"


class Unprotected:
    name = "catastrophic_stop"

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        if not ctx.unprotected:
            return ALLOW
        names = sorted(f"{key}|{product.value}" for _, key, product in ctx.unprotected)
        return halt_new_entries(
            UNPROTECTED,
            f"{len(names)} open position(s) have no catastrophic stop: {', '.join(names)}",
            positions=", ".join(names),
        )
