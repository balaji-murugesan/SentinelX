"""The pre-trade gate every order must pass (orders spec "Mandatory pre-trade gate").

``RiskGate`` is a ``typing.Protocol``: any class with a matching ``check`` method satisfies
it, the way a C# class satisfies an interface, but without declaring ``: IRiskGate``. The
order service takes one as a required constructor argument, so there is no order path that
skips it and no flag to switch it off (CLAUDE.md safety rule 2).

``PaperOnlyGate`` is the stand-in until the risk engine (S3) replaces it behind the same
Protocol: it lets paper orders through and rejects every order in live mode, because no
risk rule exists yet to protect real money.
"""

from typing import Protocol

from contracts.mode import TradingMode
from contracts.orders import Order

LIVE_NOT_READY = "risk checks are not yet available: live orders are refused until Stage 3"


class RiskGate(Protocol):
    async def check(self, order: Order) -> str | None:
        """None to let ``order`` through, else the reason it is rejected (final)."""
        ...


class PaperOnlyGate:
    def __init__(self, mode: TradingMode) -> None:
        self._mode = mode

    async def check(self, order: Order) -> str | None:
        if self._mode is TradingMode.PAPER and order.paper:
            return None
        return LIVE_NOT_READY
