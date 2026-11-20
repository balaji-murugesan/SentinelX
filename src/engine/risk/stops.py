"""Catastrophic stops: one wide stop-loss resting at the broker for every open position (add-risk-
engine tasks 9.2 and 9.3; catastrophic-stop spec; design §12; safety rules 6, 7, 14).

The trade's real stop and its trailing live in the engine; this order is only the worst-case
backstop for when the engine, the network or the feed fails. It is placed once after the entry
fills, resized only on fills (never on ticks), and never modified in place:

  * **Place new, then cancel old.** When a position's quantity changes while it stays open, the new
    stop goes out first and the old one is cancelled only once the new one is open, so the position
    is never unprotected in between.
  * **Cancel first when flat.** A resting stop on a flat book would open a new position if it
    triggered, so when a position closes by any route its stop is cancelled before anything else.
  * **Trigger.** The average entry price moved against the position by ``catastrophic_sl_pct``,
    rounded to the tick grid *toward the entry price* so it is never wider than configured (a long's
    stop rounds up, a short's rounds down).
  * **Through the gate, as an exit.** A stop goes through ``OrderService.place`` like every order;
    the gate classifies it as an exit, so a halt, a day lock or an entry limit never blocks it, and
    the rate limiter gives it exit priority. Keys are ``risk:stop:{date}:{instrument}:{product}:
    {random suffix}`` for a placement and ``risk:stop-cancel:{order}:{random suffix}`` for a
    cancel. The suffix is random, not a counter, on purpose: a fixed key would return the stored
    answer of an earlier attempt (a failed cancel would then never be retried, and a counter that
    restarts at 1 after a restart would repeat a key from before it). What decides whether a stop
    is needed is the book (``_current_stops``), never a key.
  * **Market hours only.** Stops are placed between 09:15 and 15:30 IST. The order service expires
    every open order at the close, stops included, and that expiry is not a loss of protection: the
    manager does not re-place after the close.

How it is driven: the fill listener runs *inside* the order service's lock, so it must not call
``place`` or ``cancel`` itself (they would wait on that same lock for ever). ``on_fill`` therefore
only queues the position; ``process_queue`` (called by ``run``) does the work. ``run`` also
**sweeps** every few seconds: it looks at every open position and queues those whose stop is
missing or the wrong size. That one mechanism covers a carried position at each market open (the
stops expired at yesterday's close; ``CNC`` and ``MARGIN`` included), the pass after startup
reconciliation, a stop the broker lost, and a placement that failed and is retried with backoff.

**Unprotected** is computed from the book, not remembered: ``unprotected()`` is every open
position, in market hours, that has no resting stop of the right side, size and trigger. The
continuous check ``Unprotected`` turns that into a halt of new entries until every open position
has a stop, and a failed placement logs a critical alert and is retried. Exits are never blocked.

Trading terms: SL-M (stop-loss market) - a resting order that becomes a market order when the price
trades at or through its trigger; catastrophic - far wider than the trade's own stop.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final

import structlog

from contracts.commands import CancelOrder, PlaceOrder
from contracts.market import Instrument, InstrumentKey
from contracts.orders import Fill, Order, OrderSide, OrderState, OrderType, Position
from engine.market_data.clock import is_market_hours, ist_date, utc_now
from engine.orders.intent import PROTECTIVE_STOP_SOURCE
from engine.orders.service import OrderService
from engine.positions.book import PositionId, position_id
from engine.risk.limits import RiskLimits
from engine.risk.limits_service import LimitsService

log = structlog.get_logger("engine.risk.stops")

# The order type of the catastrophic stop, in ONE place. If Dhan converts or rejects SL-M under its
# market-protection limits (PRD §12 Q17), the change is a new ``STOP_LIMIT`` type and this constant.
STOP_ORDER_TYPE: Final = OrderType.STOP_MARKET
SOURCE: Final = PROTECTIVE_STOP_SOURCE
SWEEP_EVERY_S: Final = 5.0  # how often ``run`` looks for positions whose stop is missing
RETRY_MIN_S: Final = 1.0  # first wait before retrying a failed placement; doubles up to RETRY_MAX_S
RETRY_MAX_S: Final = 30.0


def stop_trigger(position: Position, pct: Decimal, tick: Decimal) -> Decimal:
    """The trigger of ``position``'s stop: its average price moved against it by ``pct`` percent,
    rounded to the tick grid toward the entry price (never wider than configured)."""
    if position.quantity > 0:  # a long loses when the price falls: stop below, round UP to the grid
        raw = position.average_price * (1 - pct / 100)
        return (raw / tick).to_integral_value(ROUND_CEILING) * tick
    raw = position.average_price * (1 + pct / 100)  # a short: stop above, round DOWN to the grid
    return (raw / tick).to_integral_value(ROUND_FLOOR) * tick


class CatastrophicStops:
    def __init__(
        self,
        *,
        account_id: str,
        service: OrderService,
        limits: LimitsService,
        instruments: Callable[[InstrumentKey], Instrument | None],
        now: Callable[[], datetime] = utc_now,
        sweep_every_s: float = SWEEP_EVERY_S,
    ) -> None:
        self._account_id = account_id
        self._service = service
        self._limits = limits
        self._instruments = instruments
        self._now = now
        self._sweep_every = timedelta(seconds=sweep_every_s)
        self._queue: asyncio.Queue[PositionId] = asyncio.Queue()
        self._retry_at: dict[PositionId, datetime] = {}
        self._delay: dict[PositionId, float] = {}
        self._next_sweep = now()

    # ---- what drives it --------------------------------------------------------------------

    async def on_fill(self, fill: Fill, position: Position) -> None:
        """The order service's fill listener: queue the position and return. Never places here.
        A fill changes what the stop should be, so any backoff from an earlier failure ends."""
        pid = position_id(position)
        self._forget(pid)
        self._queue.put_nowait(pid)

    async def sweep(self) -> None:
        """Queue every position whose stop is missing or wrong, and every position that is flat
        but still has a resting stop. Called by ``run`` and once after startup reconciliation."""
        for pid in self._needing_work():
            self._queue.put_nowait(pid)

    async def process_queue(self) -> None:
        """Reconcile every position queued so far (and the retries that are due)."""
        while not self._queue.empty():
            await self.sync(self._queue.get_nowait())
        now = self._now()
        for pid, at in list(self._retry_at.items()):
            if at <= now:
                del self._retry_at[pid]
                await self.sync(pid)

    async def run(self, stop: asyncio.Event) -> None:
        """Reconcile queued positions, and sweep every few seconds, until ``stop`` (supervised)."""
        while not stop.is_set():
            if self._now() >= self._next_sweep:
                self._next_sweep = self._now() + self._sweep_every
                await self.sweep()
            await self.process_queue()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.2)

    # ---- what is protected -----------------------------------------------------------------

    def unprotected(self) -> frozenset[PositionId]:
        """Open positions, in market hours, with no resting stop of the right side, size and
        trigger. Computed from the book each time, so it needs no clearing and cannot go stale."""
        if not is_market_hours(self._now()) or self._limits.current is None:
            return frozenset()
        return frozenset(
            pid
            for pid, position in self._service.positions.items()
            if position.quantity != 0 and not self._current_stops(pid, position)
        )

    def _needing_work(self) -> set[PositionId]:
        """Positions whose resting stops are not exactly one correct stop: a missing or wrong stop,
        a stray extra one (left by a resize that did not finish), or a stop on a flat book."""
        resting: dict[PositionId, int] = {}
        for stop in self._resting_stops():
            pid = (stop.account_id, stop.key, stop.product)
            resting[pid] = resting.get(pid, 0) + 1
        work: set[PositionId] = set()
        for pid, position in self._service.positions.items():
            if position.quantity != 0 and (
                not self._current_stops(pid, position) or resting.get(pid, 0) != 1
            ):
                work.add(pid)
        for pid in resting:
            held = self._service.positions.get(pid)
            if held is None or held.quantity == 0:
                work.add(pid)  # a stop left on a flat book
        return work

    def _resting_stops(self) -> list[Order]:
        return [
            o
            for o in self._service.orders.values()
            if o.source == SOURCE
            and o.order_type is STOP_ORDER_TYPE
            and o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        ]

    def _wanted(
        self, position: Position, limits: RiskLimits
    ) -> tuple[OrderSide, int, Decimal] | None:
        instrument = self._instruments(position.key)
        if instrument is None:
            return None
        side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
        trigger = stop_trigger(position, limits.catastrophic_sl_pct, instrument.tick_size)
        return side, abs(position.quantity), trigger

    def _in_flight(self, pid: PositionId) -> bool:
        """Is a stop for ``pid`` neither open nor final (submitted, or unknown after a timeout)?"""
        return any(
            o.source == SOURCE
            and o.order_type is STOP_ORDER_TYPE
            and (o.account_id, o.key, o.product) == pid
            and o.state in (OrderState.REQUESTED, OrderState.SUBMITTED, OrderState.UNKNOWN)
            for o in self._service.orders.values()
        )

    def _current_stops(self, pid: PositionId, position: Position) -> list[Order]:
        """The resting stops that are exactly what ``position`` needs (usually one)."""
        limits = self._limits.current
        wanted = None if limits is None else self._wanted(position, limits)
        if wanted is None:
            return []
        return [
            o
            for o in self._resting_stops()
            if (o.account_id, o.key, o.product) == pid
            and (o.side, o.quantity, o.trigger_price) == wanted
        ]

    # ---- one position ----------------------------------------------------------------------

    async def sync(self, pid: PositionId) -> None:
        """Bring ``pid``'s resting stop in line with its position. Never raises. Call it only from
        ``process_queue`` (the one ``run`` task): two concurrent calls for one position could both
        pass the in-flight check and place two stops."""
        try:
            await self._sync(pid)
        except Exception:
            log.critical(
                "catastrophic stop could not be maintained",
                key=str(pid[1]),
                product=pid[2].value,
                exc_info=True,
            )
            self._backoff(pid)

    async def _sync(self, pid: PositionId) -> None:
        position = self._service.positions.get(pid)
        resting = [o for o in self._resting_stops() if (o.account_id, o.key, o.product) == pid]
        if position is None or position.quantity == 0:
            for stop in resting:  # a stop on a flat book is an entry order: cancel it first
                await self._cancel(stop)
            self._forget(pid)
            return
        if not is_market_hours(self._now()):
            return  # the close expired the stops; they are placed again after the next open
        limits = self._limits.current
        wanted = None if limits is None else self._wanted(position, limits)
        if wanted is None:
            if self._retry_at.get(pid, self._now()) <= self._now():
                self._backoff(pid)  # (not on every sweep: the alert and the delay grow once a try)
            return
        side, quantity, trigger = wanted
        current = self._current_stops(pid, position)
        if current:
            for extra in resting:
                if extra is not current[0]:
                    await self._cancel(extra)  # a stale one left behind by an earlier resize
            self._forget(pid)
            return
        if self._retry_at.get(pid, self._now()) > self._now():
            return  # backing off after a failure
        if self._in_flight(pid):
            # An earlier stop is neither open nor final (submitted, or unknown after a timeout):
            # it may be live at the broker. Placing another could leave two stops that both
            # trigger and flip the position, so wait for reconciliation to settle it.
            log.warning("a stop is still in flight: not placing another", key=str(pid[1]))
            return
        if await self._place(pid, side, quantity, trigger) is None:
            self._backoff(pid)  # the old stop (if any) stays until a new one is open
            return
        for old in resting:
            await self._cancel(old)  # only now: the new stop is open, so never unprotected
        self._forget(pid)

    async def _place(
        self, pid: PositionId, side: OrderSide, quantity: int, trigger: Decimal
    ) -> Order | None:
        _, key, product = pid
        # A unique suffix, not a counter: a counter restarts at 1 after a restart and could repeat
        # a key from before it, which would silently return that earlier (stale) order. Whether a
        # stop is already there is decided by looking at the book (``_current_stops``), not by the
        # key; the key only makes one placement idempotent.
        result = await self._service.place(
            PlaceOrder(
                command_id=uuid.uuid4().hex,
                idempotency_key=(
                    f"risk:stop:{ist_date(self._now())}:{key}:{product.value}:{uuid.uuid4().hex[:8]}"
                ),
                account_id=self._account_id,
                source=SOURCE,
                sent_at=self._now(),
                key=key,
                side=side,
                quantity=quantity,
                product=product,
                order_type=STOP_ORDER_TYPE,
                trigger_price=trigger,
            )
        )
        order = self._service.orders.get(result.order_id or "")
        if not result.accepted or order is None or order.state is not OrderState.OPEN:
            log.critical(
                "the catastrophic stop was not taken",
                key=str(key),
                product=product.value,
                reason=result.reason or (order.reason if order else None),
            )
            return None
        return order

    async def _cancel(self, order: Order) -> None:
        """Ask for a stop to be cancelled. A refusal (the broker errored or did not answer) is
        loud and is tried again by the next sweep with a fresh key: a stop that stays on a flat
        book would open a position if it triggered (safety rule 14)."""
        result = await self._service.cancel(
            CancelOrder(
                command_id=uuid.uuid4().hex,
                idempotency_key=f"risk:stop-cancel:{order.order_id}:{uuid.uuid4().hex[:8]}",
                account_id=self._account_id,
                source=SOURCE,
                sent_at=self._now(),
                order_id=order.order_id,
            )
        )
        if not result.accepted:
            log.critical(
                "a catastrophic stop could not be cancelled: trying again at the next sweep",
                key=str(order.key),
                product=order.product.value,
                reason=result.reason,
            )

    # ---- retry with backoff ----------------------------------------------------------------

    def _backoff(self, pid: PositionId) -> None:
        delay = min(self._delay.get(pid, RETRY_MIN_S / 2) * 2, RETRY_MAX_S)
        self._delay[pid] = delay
        self._retry_at[pid] = self._now() + timedelta(seconds=delay)
        log.critical(
            "position is unprotected: retrying", key=str(pid[1]), product=pid[2].value, in_s=delay
        )

    def _forget(self, pid: PositionId) -> None:
        self._delay.pop(pid, None)
        self._retry_at.pop(pid, None)
