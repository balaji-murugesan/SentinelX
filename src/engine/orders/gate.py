"""The pre-trade gate every order must pass (orders spec "Mandatory pre-trade gate").

``RiskGate`` is a ``typing.Protocol``: any class with a matching ``check`` method satisfies
it, the way a C# class satisfies an interface, but without declaring ``: IRiskGate``. The
order service takes one as a required constructor argument, so there is no order path that
skips it and no flag to switch it off (CLAUDE.md safety rule 2).

The one implementation is ``engine.risk.gate.RiskEngine`` (add-risk-engine): it refuses every
order in live mode until a live broker exists, and runs every risk rule on the rest. Tests that
are not about risk use a permissive stand-in that lives in the tests
(``tests.engine.trading_fakes``), never in the engine.
"""

from typing import Protocol

from contracts.orders import Order


class RiskGate(Protocol):
    async def check(self, order: Order) -> str | None:
        """None to let ``order`` through, else the reason it is rejected (final)."""
        ...
