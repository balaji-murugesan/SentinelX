"""The kill switch, the flattener and the own square-off (add-risk-engine tasks 10.1-10.3;
kill-switch spec; risk-rules "Own square-off"; design §11).

``KillSwitch.trigger`` is the one action that stops all trading for the day. In this order it:

  1. **persists HALTED first**: the day's halt level goes to KILL in the day book and is flushed;
     a failed write is retried by the monitor's timer and the kill goes on regardless;
  2. **cancels every pending entry order**;
  3. makes sure the **flattener** is running: it exits every open position, whatever its product
     (carried ``CNC`` included), as its own task, until the book is flat.

The day lock has no intraday undo. It lives in the day book (``risk_day_state``), not in
``recovery_state``, so the ``ResumeRecovery`` command, which clears only a reconciliation halt,
cannot touch it by construction; a restart keeps it; the next IST date has a fresh row. No broker
lock and no Exit All are called here (stage 7).

``ChaseFlattener`` is the simple stage-3 implementation behind the ``Flattener`` Protocol (stage 4's
exit manager replaces it). It never sends a market order: each exit is a limit order priced past
the latest price by ``exit_buffer_pct`` (a sell below it, a buy above it, rounded to the tick in the
aggressive direction). If an exit has not filled after ``chase_interval_s`` it is cancelled, the
engine **waits for the cancel to be confirmed** (an order is replaced only once it is final, never
blind), and a new exit goes out at the current price, up to ``max_chase_steps`` replacements. It
loops until every position is flat, re-reading the book each pass, so an entry that fills while it
is being cancelled is caught and exited too. After ``exit_escalate_s`` without a flat book it logs a
critical alert and keeps going. An exit the broker rejects (or the engine refuses) is tried again
after the chase interval, not on every look. Outside market hours (09:15-15:30 IST) nothing can be
sold, so it places nothing: if the market closes with the book not flat it logs a critical alert and
stops (the day's lock ends at midnight, so nothing carries the intent over), and
``ensure_flattening`` starts no flatten while the market is closed.

One flattener serves both the kill and the square-off. Its per-position state lives on the instance
and each position's step runs under its own lock, so two callers working on the same position share
one resting exit instead of each placing their own (two exits that both filled would flip it).

Every order carries an idempotency key ``risk:flatten:{date}:{instrument}:{product}:{episode}:
{step}``; ``episode`` is random and new for each chase, so a key can never repeat one from an
earlier flatten (a square-off, then a kill; or a restart) and silently return its stored answer. A
cancel's key gets a fresh suffix for every attempt, for the same reason: a failed cancel must be
able to try again. When a chase starts and the engine already has a resting exit of its own for
the position (a restart in the middle of a flatten, or a square-off that ran first), it **adopts**
that exit instead of placing a second one: two exits that both filled would flip the position.

``ensure_flattening`` is how a flatten that was cut short resumes: the monitor calls it on every
pass, and it starts the flatten whenever the day is locked and a position is open. That covers a
restart in the middle of a flatten (the day row says KILL; after reconciliation the monitor starts),
an entry that fills late, and a flatten task that crashed.

Trading terms: flatten - exit every position; chase - re-price an unfilled exit towards the market;
aggressive limit - a limit priced so that it fills at once; square-off - the engine's own scheduled
exit of intraday positions (not a halt: the day stays open for the next day's trading).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final, Protocol

import structlog

from contracts.commands import CancelOrder, CommandResult, PlaceOrder
from contracts.commands import KillSwitch as KillCommand
from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderState, OrderType, Position, ProductType
from contracts.snapshot import HaltLevel
from engine.market_data.clock import IST, is_market_hours, ist_date, utc_now
from engine.orders.intent import Intent, classify
from engine.orders.service import OrderService
from engine.persistence.repository import PersistenceError, TradingRepository
from engine.positions.book import PositionId
from engine.risk.daybook import DayBook
from engine.risk.decision import Quote
from engine.risk.expiry import effective_square_off, treated_as_expiring
from engine.risk.limits import RiskLimits
from engine.risk.limits_service import LimitsService

log = structlog.get_logger("engine.risk.kill")

POLL_S: Final = 0.2  # how often the flattener re-reads the book
ENTRY_CANCEL_RETRY_S: Final = 30.0  # how long before the kill asks again to cancel an entry
ENTRY_CANCEL_REFUSED_RETRY_S: Final = 2.0  # ... and after the broker REFUSED the cancel
OWN_SOURCES: Final = frozenset({"risk-kill", "risk-squareoff"})  # who places the flattener's exits


class Flattener(Protocol):
    async def flatten(
        self,
        reason: str,
        products: frozenset[ProductType] | None = None,
        kind: str = "kill",
        positions: frozenset[PositionId] | None = None,
    ) -> None:
        """Exit every open position and return once they are flat. Never sends a market order.

        ``products`` and ``positions`` narrow it: only positions of those products, and only those
        positions (add-risk-guards task 8.3: the square-off asks for the positions whose own time
        has come). Both None, which is what the kill passes, means everything. Stage 4's exit
        manager replaces this flattener and must keep both parameters."""
        ...


class _Chase:
    """The exit currently working for one position."""

    def __init__(self) -> None:
        self.order_id: str | None = None
        self.episode = uuid.uuid4().hex[:8]  # makes this chase's keys differ from any earlier one
        self.step = 0  # how many orders have gone out for this position (key suffix)
        self.replacements = 0  # re-prices done: capped by ``max_chase_steps``
        self.placed_at: datetime | None = None
        self.cancelling = False
        self.cancel_for: str | None = None  # why the cancel in flight was asked: "due" or "size"
        self.cancel_asked_at: datetime | None = None
        self.extras: list[str] = []  # other resting exits of ours for this position, to cancel
        self.extras_asked_at: datetime | None = None
        self.unpriced_alerted = False  # the "cannot price" alert was given for this stretch


def aggressive_price(side: OrderSide, last: Decimal, buffer_pct: Decimal, tick: Decimal) -> Decimal:
    """The limit price of an exit: ``last`` moved past the market by ``buffer_pct`` percent,
    rounded to the tick grid in the direction that makes the order MORE aggressive (down for a
    sell, up for a buy), and never below one tick."""
    if side is OrderSide.SELL:
        raw = last * (1 - buffer_pct / 100)
        return max((raw / tick).to_integral_value(ROUND_FLOOR) * tick, tick)
    raw = last * (1 + buffer_pct / 100)
    return (raw / tick).to_integral_value(ROUND_CEILING) * tick


class ChaseFlattener:
    def __init__(
        self,
        *,
        account_id: str,
        service: OrderService,
        limits: LimitsService,
        quote: Callable[[InstrumentKey], Quote | None],
        instruments: Callable[[InstrumentKey], Instrument | None],
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_s: float = POLL_S,
    ) -> None:
        self._account_id = account_id
        self._service = service
        self._limits = limits
        self._quote = quote
        self._instruments = instruments
        self._now = now
        self._sleep = sleep
        self._poll_s = poll_s
        self._chases: dict[PositionId, _Chase] = {}
        self._locks: dict[PositionId, asyncio.Lock] = {}

    async def flatten(
        self,
        reason: str,
        products: frozenset[ProductType] | None = None,
        kind: str = "kill",
        positions: frozenset[PositionId] | None = None,
    ) -> None:
        started = self._now()
        escalated = False
        limits_alerted = False
        while True:
            limits = self._limits.current
            if limits is None:
                if not limits_alerted:  # once per stretch, not on every look
                    log.critical(
                        "flatten needs the limits for its buffer and timings", reason=reason
                    )
                    limits_alerted = True
                await self._sleep(self._poll_s)
                continue
            limits_alerted = False
            open_positions = {
                pid: p
                for pid, p in self._service.positions.items()
                if p.quantity != 0 and self._in_scope(pid, products, positions)
            }
            # A chase that has finished (its position is flat and its order final) is forgotten, so
            # the next one for that position starts fresh: its own step count and its own keys.
            for pid in [
                pid
                for pid, chase in self._chases.items()
                if self._in_scope(pid, products, positions)
                and pid not in open_positions
                and not self._alive(chase)
            ]:
                del self._chases[pid]
            working = [
                pid
                for pid, chase in self._chases.items()
                if self._in_scope(pid, products, positions) and self._alive(chase)
            ]
            if not open_positions and not working:
                return
            if open_positions and not is_market_hours(self._now()):
                # Nothing can be sold now. Stop rather than send orders the broker will refuse
                # all night: the orders still resting expire at the close, and the next day starts
                # without this lock. The owner is told, because positions remain.
                log.critical(
                    "the market is closed and the book is not flat: the flatten stops",
                    reason=reason,
                    open=[str(pid[1]) for pid in open_positions],
                )
                return
            for pid, position in open_positions.items():
                async with self._lock(pid):
                    await self._work(pid, position, self._chase(pid), limits, kind)
            # A chase whose position has gone flat finishes when its order is final: cancel any
            # exit still resting on a flat book so it cannot open a position (safety rule 14).
            for pid in working:
                if pid not in open_positions:
                    async with self._lock(pid):
                        await self._cancel(pid, self._chase(pid), kind)
            elapsed = (self._now() - started).total_seconds()
            if not escalated and elapsed >= limits.exit_escalate_s:
                escalated = True
                log.critical(
                    "book is not flat after the escalation time: still flattening",
                    reason=reason,
                    open=[str(pid[1]) for pid in open_positions],
                    after_s=int(elapsed),
                )
            await self._sleep(self._poll_s)

    # ---- one position ----------------------------------------------------------------------

    @staticmethod
    def _in_scope(
        pid: PositionId,
        products: frozenset[ProductType] | None,
        positions: frozenset[PositionId] | None,
    ) -> bool:
        """Is ``pid`` one this flatten works on? No filter (None) lets everything through."""
        return (products is None or pid[2] in products) and (positions is None or pid in positions)

    def _lock(self, pid: PositionId) -> asyncio.Lock:
        return self._locks.setdefault(pid, asyncio.Lock())

    def _chase(self, pid: PositionId) -> _Chase:
        chase = self._chases.get(pid)
        if chase is None:
            chase = self._chases[pid] = _Chase()
            self._adopt(pid, chase)
        return chase

    def _adopt(self, pid: PositionId, chase: _Chase) -> None:
        """Take over the engine's own resting exit for ``pid``, if there is one, instead of
        placing a second (see the module docstring). The oldest is adopted; any others are
        cancelled."""
        held = self._service.positions.get(pid)
        if held is None or held.quantity == 0:
            return
        side = OrderSide.SELL if held.quantity > 0 else OrderSide.BUY
        own = sorted(
            (
                o
                for o in self._service.orders.values()
                if (o.account_id, o.key, o.product) == pid
                and o.source in OWN_SOURCES
                and o.side is side
                and o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
            ),
            key=lambda o: o.created_at,
        )
        if own:
            chase.order_id, chase.placed_at = own[0].order_id, own[0].created_at
            chase.extras = [o.order_id for o in own[1:]]
            log.warning("flatten adopts a resting exit of its own", key=str(pid[1]), n=len(own))

    def _alive(self, chase: _Chase) -> bool:
        order = self._order(chase)
        return order is not None and not order.state.is_final

    def _order(self, chase: _Chase) -> Order | None:
        return None if chase.order_id is None else self._service.orders.get(chase.order_id)

    async def _work(
        self, pid: PositionId, position: Position, chase: _Chase, limits: RiskLimits, kind: str
    ) -> None:
        # An extra whose order has ended (it filled, or was cancelled meanwhile) needs no cancel.
        chase.extras = [
            e
            for e in chase.extras
            if (o := self._service.orders.get(e)) is not None and not o.state.is_final
        ]
        if chase.extras and (
            chase.extras_asked_at is None
            or (self._now() - chase.extras_asked_at).total_seconds() >= limits.chase_interval_s
        ):
            # Other exits of ours for this position: only one may rest. One that the broker
            # refuses to cancel stays on the list and is asked again after the chase interval.
            chase.extras_asked_at = self._now()
            for extra in list(chase.extras):
                if await self._cancel_order(extra, kind):
                    chase.extras.remove(extra)
        order = self._order(chase)
        if order is None or order.state.is_final:
            # An exit that was refused or rejected is tried again after the chase interval, not on
            # every look (0.5 s): a broker that refuses it would otherwise be hit with a new order
            # twice a second. One replaced after a confirmed cancel goes out at once.
            refused = order is None or order.state is OrderState.REJECTED
            if refused and chase.placed_at is not None and self._within_interval(chase, limits):
                return
            if (
                order is not None
                and order.state is OrderState.CANCELLED
                and chase.cancel_for == "due"
            ):
                chase.replacements += 1  # a re-price that has happened, not a cancel attempted
            chase.order_id, chase.cancelling, chase.cancel_for = None, False, None
            await self._place(pid, position, chase, limits, kind)
            return
        wanted = abs(position.quantity)
        resting = order.quantity - order.filled_quantity
        due = chase.placed_at is not None and not self._within_interval(chase, limits)
        wrong_size = resting != wanted  # the position changed under the order (an entry filled)
        out_of_steps = chase.replacements >= limits.max_chase_steps
        retry_wait = chase.cancel_asked_at is not None and (
            (self._now() - chase.cancel_asked_at).total_seconds() < limits.chase_interval_s
        )
        if ((due and not out_of_steps) or wrong_size) and not chase.cancelling and not retry_wait:
            await self._cancel(pid, chase, kind, "size" if wrong_size else "due")
        # While the cancel is not confirmed the order is not final, so nothing is placed: the
        # next pass finds it final and places the replacement (never blind).

    def _within_interval(self, chase: _Chase, limits: RiskLimits) -> bool:
        """True while the last exit was placed less than ``chase_interval_s`` ago."""
        assert chase.placed_at is not None
        return (self._now() - chase.placed_at).total_seconds() < limits.chase_interval_s

    async def _place(
        self, pid: PositionId, position: Position, chase: _Chase, limits: RiskLimits, kind: str
    ) -> None:
        _, key, product = pid
        quote, instrument = self._quote(key), self._instruments(key)
        if quote is None or instrument is None:
            if not chase.unpriced_alerted:  # once per stretch, not on every look
                log.critical("cannot price an exit: no latest price", instrument=str(key))
                chase.unpriced_alerted = True
            return
        chase.unpriced_alerted = False
        side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
        price = aggressive_price(side, quote.price, limits.exit_buffer_pct, instrument.tick_size)
        chase.step += 1
        result = await self._service.place(
            PlaceOrder(
                command_id=uuid.uuid4().hex,
                idempotency_key=self._key(key, product, chase),
                account_id=self._account_id,
                source=f"risk-{kind}",
                sent_at=self._now(),
                key=key,
                side=side,
                quantity=abs(position.quantity),
                product=product,
                order_type=OrderType.LIMIT,
                limit_price=price,
            )
        )
        chase.placed_at = self._now()
        chase.order_id = result.order_id if result.accepted else None
        if not result.accepted:
            log.error("exit refused", instrument=str(key), reason=result.reason, step=chase.step)

    async def _cancel(
        self, pid: PositionId, chase: _Chase, kind: str, why: str | None = None
    ) -> None:
        if chase.order_id is None or chase.cancelling:
            return
        chase.cancelling, chase.cancel_for = True, why
        chase.cancel_asked_at = self._now()
        accepted = await self._cancel_order(chase.order_id, kind)
        if not accepted:
            # Not open any more (it just filled?) or the broker did not take the cancel: the next
            # pass sees the order's real state, or asks again after the chase interval.
            chase.cancelling, chase.cancel_for = False, None

    async def _cancel_order(self, order_id: str, kind: str) -> bool:
        """Ask for one cancel. A fresh key each time: a failed cancel is stored under its key, and
        a repeat of that key would only return the stored refusal."""
        result = await self._service.cancel(
            CancelOrder(
                command_id=uuid.uuid4().hex,
                idempotency_key=f"risk:flatten-cancel:{order_id}:{uuid.uuid4().hex[:8]}",
                account_id=self._account_id,
                source=f"risk-{kind}",
                sent_at=self._now(),
                order_id=order_id,
            )
        )
        if not result.accepted:
            log.error("exit cancel not taken", order_id=order_id, reason=result.reason)
        return result.accepted

    def _key(self, key: InstrumentKey, product: ProductType, chase: _Chase) -> str:
        return (
            f"risk:flatten:{ist_date(self._now())}:{key}:{product.value}:"
            f"{chase.episode}:{chase.step}"
        )


class KillSwitch:
    def __init__(
        self,
        *,
        account_id: str,
        daybook: DayBook,
        service: OrderService,
        flattener: Flattener,
        results: TradingRepository,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._account_id = account_id
        self._daybook = daybook
        self._service = service
        self._flattener = flattener
        self._results = results
        self._now = now
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._requests: set[asyncio.Task[str | None]] = set()
        # A kill asked for while today's risk state could not be read (the database was down at
        # start, or the state is for an earlier day): the text to record once it can be.
        self._pending: str | None = None
        self._entry_cancel_at: dict[str, datetime] = {}  # when each entry's cancel was asked

    async def trigger(self, reason: str, source: str) -> str | None:
        """Stop the day. None if this call did it; otherwise why nothing new was started (the day
        is already stopped). Idempotent per account and day: a second trigger places nothing."""
        async with self._lock:
            text = f"{source}: {reason}"
            if self._daybook.unavailable(self._now()) is None:
                state = self._daybook.day()
                if state.halt_level >= HaltLevel.KILL:
                    self.ensure_flattening()  # also resumes a flatten that was cut short
                    return f"already stopped today: {state.halt_reason}"
                log.critical("kill switch", reason=reason, source=source)
                self._daybook.raise_halt(HaltLevel.KILL, text)
                await self._daybook.flush()  # persist before anything else; a failure is retried
            elif self._pending is not None:
                self.ensure_flattening()
                return f"already stopped today: {self._pending}"
            else:
                # The kill must still cancel and flatten (it cannot wait for the database); the
                # lock is recorded on the day the moment it can be read (``ensure_flattening``).
                log.critical(
                    "kill switch while today's risk state is unavailable",
                    reason=reason,
                    source=source,
                )
                self._pending = text
            await self._cancel_entries()
            self.ensure_flattening()
            self._alert_if_closed()
            return None

    def request(self, reason: str, source: str) -> None:
        """Ask for the kill from code that cannot await it: the gate runs inside the order
        service's lock, which the kill's own cancels need, so awaiting ``trigger`` there would
        wait on that same lock for ever. This schedules it as a task and returns at once."""
        task = asyncio.create_task(self.trigger(reason, source))
        self._requests.add(task)  # keep a reference, or the loop may drop the task unfinished
        task.add_done_callback(self._request_done)

    def _request_done(self, task: asyncio.Task[str | None]) -> None:
        self._requests.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.critical("a requested kill failed", exc_info=task.exception())

    def ensure_flattening(self) -> None:
        """Start the flatten if the day is locked and a position is open and none is running. The
        monitor calls this on every pass (see the module docstring)."""
        now = self._now()
        if self._pending is not None and self._daybook.unavailable(now) is None:
            # A kill was asked for while the state could not be read: record the lock now (the
            # monitor's timer writes it).
            if self._daybook.day().halt_level < HaltLevel.KILL:
                self._daybook.raise_halt(HaltLevel.KILL, self._pending)
            self._pending = None
        if not self._locked():
            return
        if not is_market_hours(now):
            return  # nothing can be sold: the monitor's next pass inside market hours starts it
        if self._task is not None and self._task.done():
            if not self._task.cancelled() and self._task.exception() is not None:
                log.error("flatten task failed", exc_info=self._task.exception())
            self._task = None
        has_position = any(p.quantity != 0 for p in self._service.positions.values())
        if has_position and self._task is None:
            self._task = asyncio.create_task(self._flattener.flatten("day locked"))

    @property
    def pending(self) -> bool:
        """Is a kill waiting to be recorded on the day (it was asked for while the day's state
        could not be read)? The monitor asks ``ensure`` while this is true."""
        return self._pending is not None

    def _locked(self) -> bool:
        """Is the day locked (a kill, a blind book, the profit target: any level from
        ``MTM_BLIND`` up), or a kill waiting to be recorded?"""
        if self._pending is not None:
            return True
        if self._daybook.unavailable(self._now()) is not None:
            return False
        return self._daybook.day().halt_level >= HaltLevel.MTM_BLIND

    async def ensure(self) -> None:
        """What the monitor asks for on every pass: while the day is locked, keep the flatten
        running and keep asking for any resting entry to be cancelled (one that survived a crash
        between the lock being stored and the cancels, or whose cancel failed)."""
        recording = self._pending is not None
        self.ensure_flattening()
        if recording and self._pending is None:
            # A kill that was waiting for the day to load has just been recorded on it: write it
            # now, not at the monitor's next persist (a crash in between would lose the lock).
            await self._daybook.flush()
        if self._locked():
            await self._cancel_entries()

    def open_while_closed(self) -> list[str]:
        """The positions still open when the market is closed (they cannot be exited now)."""
        if is_market_hours(self._now()):
            return []
        return [str(p.key) for p in self._service.positions.values() if p.quantity != 0]

    def _alert_if_closed(self) -> None:
        if open_ := self.open_while_closed():
            log.critical(
                "kill with the market closed: these positions cannot be exited until it opens",
                open=open_,
            )

    @property
    def flattening(self) -> asyncio.Task[None] | None:
        """The running flatten, if any (tests await it)."""
        return self._task

    async def _cancel_entries(self) -> None:
        """Ask for every resting entry to be cancelled (once per ``ENTRY_CANCEL_RETRY_S``, so the
        monitor can call this every pass). A refused cancel is loud and is tried again after
        ``ENTRY_CANCEL_REFUSED_RETRY_S``; the key is fresh each time because a refusal is stored
        under its key."""
        now = self._now()
        for order in list(self._service.orders.values()):
            if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                continue
            if classify(order, self._service.positions) is not Intent.ENTRY:
                continue
            asked = self._entry_cancel_at.get(order.order_id)
            if asked is not None and (now - asked).total_seconds() < ENTRY_CANCEL_RETRY_S:
                continue
            self._entry_cancel_at[order.order_id] = now
            result = await self._service.cancel(
                CancelOrder(
                    command_id=uuid.uuid4().hex,
                    idempotency_key=(
                        f"risk:kill:{ist_date(now)}:cancel-entry:{order.order_id}:"
                        f"{uuid.uuid4().hex[:8]}"
                    ),
                    account_id=self._account_id,
                    source="risk-kill",
                    sent_at=now,
                    order_id=order.order_id,
                )
            )
            if not result.accepted:
                log.error(
                    "the kill could not cancel an entry: trying again",
                    order_id=order.order_id,
                    reason=result.reason,
                )
                # Asked again soon, but not on every pass of the monitor (about once a
                # second): a broker that keeps refusing would be hit at that rate.
                self._entry_cancel_at[order.order_id] = now - timedelta(
                    seconds=ENTRY_CANCEL_RETRY_S - ENTRY_CANCEL_REFUSED_RETRY_S
                )

    # ---- the commands ----------------------------------------------------------------------

    async def on_kill(self, reason: str) -> None:
        """The monitor's hook for a KILL decision (a floor breach)."""
        await self.trigger(reason, "risk")

    async def handle(self, command: KillCommand) -> CommandResult:
        """The ``KillSwitch`` command from the CLI. The same key twice returns the first answer."""
        try:
            first = await self._results.find_result(command.account_id, command.idempotency_key)
        except PersistenceError:
            first = None  # the kill must not wait for the database: act, and answer below
        if first is not None:
            return first
        refusal = await self.trigger("kill command", command.source)
        note = refusal
        if note is None and (open_ := self.open_while_closed()):
            # Accepted, but not the whole story: say so rather than let the owner think it is flat.
            note = f"the market is closed: {len(open_)} open position(s) cannot be exited now"
        result = CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=self._account_id,
            accepted=True,
            reason=note,  # None the first time; "already stopped today: ..." after that
            decided_at=self._now(),
        )
        try:
            await self._results.save_result(result)
        except PersistenceError:
            log.warning("kill result not stored", key=command.idempotency_key)
        return result


class SquareOff:
    """The engine's own exit of ``INTRADAY`` positions at the configured square-off time (IST).

    It uses the kill switch's flattener but is **not a halt**: the day stays open, no broker lock
    is requested, and positions held under ``CNC`` or ``MARGIN`` (and their stops) are left alone.
    It is called on every monitor pass.

    Each intraday position has its own time (add-risk-guards task 8.3; expiry-day-profile
    "Intraday positions in expiring contracts are squared off earlier"): ``square_off_time_expiry``
    when its contract expires today, or when it has no master record (its expiry cannot be read,
    so the earlier time is the fail-safe side), and ``square_off_time`` otherwise
    (``engine.risk.expiry.effective_square_off``). Once a position's time has come it is handed to
    the flattener by its id, so an expiring contract is exited at 14:30 while next week's stays
    open until 15:10. A position whose time has come and that no running flatten covers gets a
    flatten of its own at once: one that appears later is exited too, and a slow exit of an
    expiring position cannot hold up the others at the base time. Two flattens never share a
    position here, and the flattener would let them share one exit if they did.

    Trading terms: square-off - closing intraday (MIS) positions before the broker's own cut-off
    does it at its own price; expiry day - the date a contract expires, when its premium moves
    fastest, so the owner may square it off earlier.
    """

    def __init__(
        self,
        *,
        limits: LimitsService,
        service: OrderService,
        flattener: Flattener,
        instruments: Callable[[InstrumentKey], Instrument | None] = lambda key: None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``instruments`` is the instrument master lookup, for each position's expiry date. The
        default knows none, so every position is treated as expiring: the earlier time, the
        fail-safe side (``RiskRuntime`` passes the real master)."""
        self._limits = limits
        self._service = service
        self._flattener = flattener
        self._instruments = instruments
        self._now = now
        # The flattens started, each with the positions it was asked for (newest last).
        self._runs: list[tuple[frozenset[PositionId], asyncio.Task[None]]] = []

    def _due(self, limits: RiskLimits, now: datetime) -> frozenset[PositionId]:
        """The open intraday positions whose own square-off time has come."""
        local = now.astimezone(IST).time()
        return frozenset(
            pid
            for pid, p in self._service.positions.items()
            if p.quantity != 0
            and p.product is ProductType.INTRADAY
            and local
            >= effective_square_off(limits, treated_as_expiring(self._instruments(p.key), now))
        )

    async def maybe_run(self, now: datetime) -> None:
        limits = self._limits.current
        if limits is None or not is_market_hours(now):
            return
        self._runs = [(scope, task) for scope, task in self._runs if not self._ended(task)]
        covered = frozenset(pid for scope, _ in self._runs for pid in scope)
        new = self._due(limits, now) - covered
        if not new:
            return
        log.warning(
            "square-off time: exiting intraday positions", positions=[str(p[1]) for p in new]
        )
        task = asyncio.create_task(
            self._flattener.flatten(
                "square-off", frozenset({ProductType.INTRADAY}), kind="squareoff", positions=new
            )
        )
        self._runs.append((new, task))

    @staticmethod
    def _ended(task: asyncio.Task[None]) -> bool:
        """True once ``task`` has finished; a failure is logged (the next pass starts again)."""
        if not task.done():
            return False
        if not task.cancelled() and task.exception() is not None:
            log.error("square-off task failed", exc_info=task.exception())
        return True

    @property
    def running(self) -> asyncio.Task[None] | None:
        """The newest flatten started (tests await it), until a later pass sees it finished."""
        return self._runs[-1][1] if self._runs else None
