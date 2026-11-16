"""Client-side limit on the engine's calls to the broker's order API (add-risk-engine task 8.1;
order-rate-limiter spec; design §13; PRD §10).

``RateLimitedAdapter`` wraps a ``BrokerAdapter``. Every ``place`` and ``cancel`` takes a token
first; ``order_book`` and ``updates`` pass straight through (they are reads, not orders). Two
sliding windows are kept: ``per_s`` sends in any one second and ``per_min`` in any minute. The two
ceilings those caps may never exceed live in ``engine.risk.ceilings`` (code, not configuration).

A share of each window is **reserved** for exits and cancels:

  * An **entry** may be sent only while the window still has a token *outside* the reserved
    share. If it has none, the entry is refused at once with ``RISK_RATE_LIMITED`` (the order
    service turns that into a final ``Rejected`` order) and it **never waits**: an entry that
    cannot go now is not worth sending late.
  * An **exit** or a **cancel** may use any token. If there is none it **waits** for the next one,
    in the order it asked (an ``asyncio.Lock`` hands out turns first come, first served), and is
    **never dropped**. So a kill that flattens ten positions against a cap of three per second is
    simply paced: all ten go, spread over a few seconds.

The reserved share is ``cap * percent // 100`` whole tokens (integer maths, so the split never
depends on float rounding), then clamped to at least one token and at most ``cap - 1``: exits
always have a token of their own and entries always keep one, whatever share is configured. That
is why a cap below 2 is refused (one token cannot be both). Intent comes from
``engine.orders.intent.classify``, the same function the gate uses.

**Waiting for a turn must not look like a broker that did not answer.** The order service gives
the broker ``ORDER_ACK_TIMEOUT_S`` to answer a call, but an exit or cancel may legitimately wait
for a token (up to a minute when the per-minute window is full). So the limiter has two steps the
service calls separately, ``admit_*`` (take the token, wait if need be) and ``send_*`` (the call to
the broker), and the service starts its timeout only for the second. ``place`` and ``cancel`` do
both, for a caller that does not care (the tests, a direct user of the adapter).

Trading terms: SEBI threshold - since the retail-algo framework, a self-built algo needs exchange
registration above 10 orders per second per exchange; the ceilings keep the engine well below it.
Dhan's published limits are 10 per second and 250 per minute (to be re-verified before live).
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Final

import structlog

from contracts.orders import Order, Position
from engine.broker.adapter import (
    BrokerAck,
    BrokerAdapter,
    BrokerOrder,
    BrokerRejected,
    BrokerUpdate,
)
from engine.orders.intent import Intent, classify
from engine.positions.book import PositionId

# Re-exported: ``engine.config`` reads them from the leaf module (no import cycle), and older
# imports of them from here keep working. ``X as X`` is how a re-export is spelled for mypy.
from engine.risk.ceilings import ORDERS_PER_MIN_CEILING as ORDERS_PER_MIN_CEILING
from engine.risk.ceilings import ORDERS_PER_S_CEILING as ORDERS_PER_S_CEILING

log = structlog.get_logger("engine.risk.ratelimit")

RATE_LIMITED: Final = "RISK_RATE_LIMITED"
SECOND: Final = 1.0
MINUTE: Final = 60.0


MIN_CAP: Final = 2  # one token cannot be both reserved for exits and left for entries


def reserved_tokens(cap: int, share: float) -> int:
    """Tokens of a window of ``cap`` kept for exits and cancels: ``share`` of it rounded down, but
    never fewer than one nor more than ``cap - 1``. ``share`` is a fraction (0.4); it is turned
    into a whole percent first so the product is an integer calculation."""
    if cap < MIN_CAP:
        raise ValueError(f"a window needs at least {MIN_CAP} tokens to reserve one for exits")
    percent = round(share * 100)
    return max(1, min(cap - 1, cap * percent // 100))


class _Window:
    """Send times (seconds on a monotonic clock) inside one sliding window."""

    def __init__(self, length_s: float, cap: int, reserved: int) -> None:
        self.length = length_s
        self.cap = cap  # every send, entry or exit, is counted against this
        self.reserved = reserved  # of which exits and cancels alone may use these last tokens
        self._sent: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._sent and self._sent[0] <= now - self.length:
            self._sent.popleft()

    def has_entry_token(self, now: float) -> bool:
        """May an entry go now? Only while a token outside the reserved share is left."""
        self._prune(now)
        return len(self._sent) < self.cap - self.reserved

    def wait_for_exit(self, now: float) -> float:
        """Seconds until an exit would find a token here (0 if it has one already)."""
        self._prune(now)
        if len(self._sent) < self.cap:
            return 0.0
        # The send that has to age out is the one that leaves cap - 1 inside the window.
        return self._sent[len(self._sent) - self.cap] + self.length - now

    def record(self, now: float) -> None:
        self._sent.append(now)


class RateLimitedAdapter:
    def __init__(
        self,
        inner: BrokerAdapter,
        *,
        per_s: int,
        per_min: int,
        exit_share: float,
        positions: Callable[[], Mapping[PositionId, Position]],
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``clock`` returns monotonic seconds and ``sleep`` waits that long: both are injected
        so a test drives time by hand. ``positions`` is read at each call to classify the order
        (pass ``lambda: service.positions``)."""
        if not MIN_CAP <= per_s <= ORDERS_PER_S_CEILING:
            raise ValueError(f"per_s must be from {MIN_CAP} to the ceiling {ORDERS_PER_S_CEILING}")
        if not MIN_CAP <= per_min <= ORDERS_PER_MIN_CEILING:
            raise ValueError(
                f"per_min must be from {MIN_CAP} to the ceiling {ORDERS_PER_MIN_CEILING}"
            )
        if not 0 < exit_share < 1:
            raise ValueError("exit_share must be above 0 and below 1")
        self._inner = inner
        self._windows = (
            _Window(SECOND, per_s, reserved_tokens(per_s, exit_share)),
            _Window(MINUTE, per_min, reserved_tokens(per_min, exit_share)),
        )
        self._positions = positions
        self._clock = clock
        self._sleep = sleep
        self._turn = asyncio.Lock()  # exits and cancels queue on this, first come first served

    # ---- the two calls that are orders -------------------------------------------------------

    async def admit_place(self, order: Order) -> None:
        """Take the token for placing ``order``: an exit waits for one, an entry that finds none
        is refused at once (``BrokerRejected``, ``RISK_RATE_LIMITED``)."""
        if classify(order, self._positions()) is Intent.EXIT:
            await self._take_priority_token("exit")
        else:
            self._take_entry_token(order)

    async def admit_cancel(self, order: Order) -> None:
        """Take the token for cancelling ``order``: every cancel has exit priority and waits."""
        await self._take_priority_token("cancel")

    async def send_place(self, order: Order) -> BrokerAck:
        return await self._inner.place(order)  # no token: ``admit_place`` already took it

    async def send_cancel(self, order: Order) -> None:
        await self._inner.cancel(order)

    async def place(self, order: Order) -> BrokerAck:
        await self.admit_place(order)
        return await self.send_place(order)

    async def cancel(self, order: Order) -> None:
        await self.admit_cancel(order)
        await self.send_cancel(order)

    # ---- tokens ------------------------------------------------------------------------------

    def _take_entry_token(self, order: Order) -> None:
        now = self._clock()
        if not all(w.has_entry_token(now) for w in self._windows):
            log.warning("entry refused by the rate limiter", order_id=order.order_id)
            raise BrokerRejected(
                f"{RATE_LIMITED}: too many orders just now (entries leave the last tokens of "
                f"each window to exits and cancels)"
            )
        self._record(now)

    async def _take_priority_token(self, kind: str) -> None:
        async with self._turn:  # the lock is fair: waiters are served in the order they came
            while True:
                now = self._clock()
                wait = max(w.wait_for_exit(now) for w in self._windows)
                if wait <= 0:
                    self._record(now)
                    return
                log.info("rate limiter pacing", kind=kind, wait_s=round(wait, 3))
                await self._sleep(wait)

    def _record(self, now: float) -> None:
        for window in self._windows:
            window.record(now)

    # ---- reads pass through ------------------------------------------------------------------

    async def order_book(self) -> list[BrokerOrder]:
        return await self._inner.order_book()

    def updates(self) -> AsyncIterator[BrokerUpdate]:
        return self._inner.updates()
