"""The broker rejections of the last five minutes (add-risk-guards task 6.2; order-flow-guards spec
"Broker rejections trip a breaker"; design §4).

Trading terms: a *broker rejection* is the broker refusing an order the engine sent, at placement
or later on its order-update stream (not enough margin, a price outside the exchange's band, the
broker's own risk system). One is routine; several in a few minutes usually mean something is
wrong on the broker's side or with the account, and each new entry would only be refused again.
The rejection breaker (``engine.risk.checks.flow.RejectBreaker``) reads this window to stop new
entries while that lasts, like an electrical circuit breaker that trips on a fault.

``RejectionWindow.record`` is registered as the order service's rejection listener
(``OrderService.add_rejection_listener``), which reports only real broker rejections: not the
gate's refusals, not the engine's own rate limiter, not orders marked rejected by restart
recovery. The window lives in memory only, so a restart forgets it (design, Risks).

The edge: a rejection exactly ``REJECTION_WINDOW`` (300 s) old still counts; one a moment older
does not. That is the tight side, and the same choice entry pacing makes for its 60 s window.
"""

from collections import deque
from datetime import datetime, timedelta
from typing import Final

from contracts.orders import Order

REJECTION_WINDOW: Final = timedelta(seconds=300)


class RejectionWindow:
    def __init__(self) -> None:
        # ``deque`` is a double-ended queue (like C#'s ``LinkedList<T>`` used as a queue).
        self._times: deque[datetime] = deque()

    def record(self, order: Order) -> None:
        """The order service's rejection listener: note when the broker rejected ``order``. The
        time is the order's ``updated_at``, which the service stamped with the engine's clock as it
        moved the order to ``Rejected``, so it is on the same clock the window is read with."""
        self._times.append(order.updated_at)

    def recent(self, now: datetime) -> tuple[datetime, ...]:
        """The rejection times within ``REJECTION_WINDOW`` of ``now``, oldest first; older ones are
        dropped here (pruned on read). Every entry is tested, not only the oldest, so a clock that
        stepped backwards between two rejections cannot leave a stale time behind a fresh one."""
        self._times = deque(at for at in self._times if now - at <= REJECTION_WINDOW)
        return tuple(sorted(self._times))
