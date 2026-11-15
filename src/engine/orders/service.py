"""The single owner of order state: the place path (orders spec; design §2, §3, §5).

``OrderService.place`` is the one door every order source goes through (CLI today, strategies
and exits later). Its fixed sequence is:

    idempotency lookup -> fail-closed flags -> validation -> RiskGate -> persist -> submit

Concepts (C# analogies):
  * ``asyncio.Lock`` + ``async with`` is ``SemaphoreSlim(1)`` + ``await WaitAsync()`` in a
    ``try/finally``. Every ``await`` lets another coroutine run, so the lock keeps each
    operation atomic (design §2).
  * ``asyncio.timeout(s)`` is a ``CancellationTokenSource(TimeSpan)`` wrapped around a block;
    on expiry the awaited call is cancelled and ``TimeoutError`` is raised.
  * ``with tracer.start_as_current_span(...)`` is ``using var a = source.StartActivity(...)``.

Safety rules in play: the gate is a required constructor argument, so no order skips it
(rule 2); a broker timeout makes the order ``Unknown`` and is never retried (rule 4); the
command result is stored in the same transaction as the order, so a repeat key returns the
first answer (rule 5).

Entries are refused while persistence is down or reconciliation is pending, but an *exit* is not
(add-risk-engine design §4, safety rule 9: keep managing exits): it is accepted, held in memory,
its create-write queued behind the earlier ones, and sent to the broker. If the engine then
crashes, the broker holds an order the database never saw; restart reconciliation treats that as
a difference and halts entries, which is the safe outcome. This is the one place where the
"persist before send" rule of Stage 2 is relaxed, and only for exits.

After submission the broker reports back on its update stream. ``on_update`` applies fills
(exactly once each, in order), cancellations and rejections under the same lock;
``reconcile_unknown`` resolves ``Unknown`` orders from the broker's order book without ever
resending; ``cancel`` and ``expire_at_close`` end open orders. The pending-write queue that
retries failed writes (below).

Failing closed (spec "Fail closed when persistence is unavailable"): a write that fails is put
on a pending-write queue and ``persistence_ok`` goes false, so new orders are refused with
"trading state cannot be saved". Fills and cancels for existing orders keep applying in
memory and queue behind the earlier writes, so the database sees them in the original order.
``drain_pending`` (run by ``run_pending_writes`` with backoff) replays the queue and, once it
is empty and the database answers, sets ``persistence_ok`` back to true.
"""

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

import structlog
from opentelemetry.trace import Span, Tracer

from contracts.commands import CancelOrder, CommandResult, PlaceOrder
from contracts.orders import Fill, Order, OrderSide, OrderState, Position, ProductType
from engine.broker.adapter import (
    BrokerAdapter,
    BrokerError,
    BrokerRejected,
    BrokerUpdate,
    Cancelled,
    Filled,
    Rejected,
)
from engine.market_data.clock import ist_date, market_close_utc, utc_now
from engine.market_data.instruments import InstrumentMaster
from engine.market_data.provider import SubscriptionLimitError
from engine.orders.gate import RiskGate
from engine.orders.intent import Intent, classify
from engine.orders.machine import InvalidTransition, transition
from engine.orders.validation import NO_HOLDINGS, Holdings, validate
from engine.persistence.repository import LoadedState, PersistenceError, TradingRepository
from engine.positions.book import PositionId, apply, flat, position_id
from engine.positions.feed import PositionFeed

log = structlog.get_logger()

type Write = Callable[[], Awaitable[None]]  # a deferred database write, replayable later
# Called with each fill and the position it produced; see ``OrderService.add_fill_listener``.
type FillListener = Callable[[Fill, Position], Awaitable[None]]
CANNOT_SAVE = "trading state cannot be saved"
PROBE_KEY = "persistence-probe"  # a read that proves the database answers again
RETRY_MIN_S = 0.5  # first wait before retrying failed writes; doubles up to RETRY_MAX_S
RETRY_MAX_S = 30.0


class OrderService:
    def __init__(
        self,
        *,
        account_id: str,
        paper: bool,
        gate: RiskGate,  # required, no default: there is no way to build a service without one
        adapter: BrokerAdapter,
        repository: TradingRepository,
        instruments: InstrumentMaster,
        position_feed: PositionFeed,
        tracer: Tracer,
        ack_timeout_s: float,
        now: Callable[[], datetime] = utc_now,
        retry_min_s: float = RETRY_MIN_S,
        retry_max_s: float = RETRY_MAX_S,
    ) -> None:
        self._account_id = account_id
        self._paper = paper
        self._gate = gate
        self._adapter = adapter
        self._repo = repository
        self._instruments = instruments
        self._position_feed = position_feed
        self._tracer = tracer
        self._ack_timeout_s = ack_timeout_s
        self._now = now
        self._retry_min_s = retry_min_s
        self._retry_max_s = retry_max_s
        self._pending: deque[Write] = deque()  # writes not yet in the database, oldest first
        self._fill_listeners: list[FillListener] = []
        self._lock = asyncio.Lock()
        self.orders: dict[str, Order] = {}
        self.positions: dict[PositionId, Position] = {}
        self._fill_ids: set[str] = set()  # fills already applied: each applies exactly once
        self._held_updates: list[BrokerUpdate] = []  # updates for orders we cannot match yet
        # Fills that changed no position (order already final): kept for reconciliation.
        self.unapplied_fills: list[Fill] = []
        # Fail-closed flags (spec "fail closed"). 8.1 sets reconciled after recovery.
        self.persistence_ok = True
        self.reconciled = True
        # What recovery found (or the stored halt holds); empty unless HALTED. Shown in the
        # snapshot and banner so the owner can see why new orders are refused (design §9).
        self.halt_differences: list[str] = []

    async def place(self, command: PlaceOrder) -> CommandResult:
        """Run one place command to its first stable state and return the answer."""
        with self._tracer.start_as_current_span("order.place") as span:
            span.set_attribute("account_id", self._account_id)
            span.set_attribute("idempotency_key", command.idempotency_key)
            async with self._lock:
                return await self._place(command, span)

    async def _place(self, command: PlaceOrder, span: Span) -> CommandResult:
        # An exit only reduces a position: it keeps working when the engine is degraded (below).
        # The command is classified, not an Order: building the Order comes after validation.
        is_exit = classify(command, self.positions) is Intent.EXIT
        if is_exit and (not self.persistence_ok or self._pending):
            # Writes are queued, so the database may not hold this key's result yet even if it is
            # up again: an exit placed during the outage is only known to this process. Ask the
            # orders held in memory first (rule 5), and do not spend a database timeout per exit
            # while the database is known to be away (a kill sends many).
            if (known := self._known_in_memory(command)) is not None:
                return known
            stored = None
        else:
            try:
                stored = await self._repo.find_result(command.account_id, command.idempotency_key)
            except PersistenceError as error:
                if not is_exit:
                    return self._refuse(
                        command.command_id, command.idempotency_key, f"{CANNOT_SAVE}: {error}"
                    )
                # The database has only just failed and cannot say whether this key was seen.
                # The orders held in memory can, at least for this run; beyond that an exit must
                # not wait for the database.
                if (known := self._known_in_memory(command)) is not None:
                    return known
                log.warning("exit_idempotency_unchecked", key=command.idempotency_key)
                stored = None
        if stored is not None:
            return stored  # same key twice: the first answer, nothing else happens (rule 5)

        if not is_exit:
            if not self.persistence_ok:
                return await self._reject(
                    command, f"{CANNOT_SAVE}: new orders are refused until it recovers"
                )
            if not self.reconciled:
                return await self._reject(command, "state is not reconciled with the broker")

        with self._tracer.start_as_current_span("order.validate"):
            reason = validate(
                command,
                self._instruments.get(command.key),
                master_loaded=self._instruments.available,
                holdings=self._holdings(command),
            )
        if reason:
            return await self._reject(command, reason)

        order = self._new_order(command)
        span.set_attribute("order_id", order.order_id)

        with self._tracer.start_as_current_span("order.gate"):
            refusal = await self._gate.check(order)
        if refusal:
            # Final: the order is stored as Rejected and never reaches the broker.
            rejected = transition(order, OrderState.REJECTED, at=self._now(), reason=refusal)
            result = self._result(command, accepted=False, order_id=order.order_id, reason=refusal)
            return await self._persist_new(rejected, result, queue_if_down=is_exit)

        result = self._result(command, accepted=True, order_id=order.order_id)
        with self._tracer.start_as_current_span("order.persist"):
            created = await self._persist_new(order, result, queue_if_down=is_exit)
        if created is not result:  # the write failed: nothing was stored, nothing is sent
            return created

        await self._submit(order, span)
        return result

    async def _submit(self, order: Order, span: Span) -> None:
        """Requested -> Submitted -> Open / Rejected / Unknown. Never resends (rule 4)."""
        order = await self._advance(order, OrderState.SUBMITTED)
        started = time.perf_counter()
        try:
            with self._tracer.start_as_current_span("broker.place") as broker_span:
                try:
                    async with asyncio.timeout(self._ack_timeout_s):
                        ack = await self._adapter.place(order)
                finally:
                    latency_ms = (time.perf_counter() - started) * 1000
                    broker_span.set_attribute("broker.latency_ms", latency_ms)
                    span.set_attribute("broker.latency_ms", latency_ms)
        except BrokerRejected as error:
            await self._advance(order, OrderState.REJECTED, reason=str(error))
        except TimeoutError:
            # The broker may or may not have the order. 6.2 reconciles against the order book.
            log.warning("order_ack_timeout", order_id=order.order_id)
            await self._advance(
                order, OrderState.UNKNOWN, reason="broker acknowledgement timed out"
            )
        else:
            await self._advance(order, OrderState.OPEN, broker_order_id=ack.broker_order_id)

    async def _advance(
        self, order: Order, target: OrderState, reason: str | None = None, **changes: str
    ) -> Order:
        """Move the order in memory, then store it. A failed write flips ``persistence_ok``
        (new orders are refused) but the in-memory order still advances."""
        moved = transition(order, target, at=self._now(), reason=reason, **changes)
        self.orders[moved.order_id] = moved
        await self._store(lambda: self._repo.update_order(moved), f"order {moved.order_id}")
        return moved

    async def _store(self, write: Write, what: str) -> None:
        """Run ``write`` now, or queue it. A write never jumps ahead of an earlier queued one,
        because the database must see each order's states in the order they happened."""
        if not self._pending:
            try:
                await write()
                return
            except PersistenceError as error:
                self.persistence_ok = False
                log.error("write_failed_queued", what=what, error=str(error))
        self._pending.append(write)

    async def drain_pending(self) -> bool:
        """Replay queued writes oldest first; True once the database is fully caught up.

        Stops at the first failure and keeps that write at the head of the queue. When the queue
        is empty it also probes the database with a read, because a failed order creation
        (which is refused, not queued) leaves ``persistence_ok`` false with nothing to replay.
        """
        while self._pending:
            try:
                await self._pending[0]()
            except PersistenceError as error:
                log.error("pending_write_failed", queued=len(self._pending), error=str(error))
                return False
            self._pending.popleft()
        if not self.persistence_ok:
            try:
                await self._repo.find_result(self._account_id, PROBE_KEY)
            except PersistenceError:
                return False
            self.persistence_ok = True
            log.info("persistence_restored")
        return True

    async def run_pending_writes(self, stop: asyncio.Event) -> None:
        """Retry failed writes with exponential backoff (0.5 s doubling to 30 s) until ``stop``."""
        delay = self._retry_min_s
        while not stop.is_set():
            if self._pending or not self.persistence_ok:
                delay = (
                    self._retry_min_s
                    if await self.drain_pending()
                    else min(delay * 2, self._retry_max_s)
                )
            wait = delay if self._pending or not self.persistence_ok else self._retry_min_s
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait)

    async def _persist_new(
        self, order: Order, result: CommandResult, *, queue_if_down: bool = False
    ) -> CommandResult:
        """Create the order and its result in one transaction; on failure refuse the command.

        ``queue_if_down`` is for exits: when the database is down (or earlier writes are still
        queued, because this one must not jump ahead of them), keep the order in memory and queue
        its create-write instead of refusing. The order is then sent, and stored when the
        database returns."""
        if queue_if_down and (not self.persistence_ok or self._pending):
            return self._queue_new(order, result, "the database is down or behind")
        try:
            await self._repo.create_order(order, result)
        except PersistenceError as error:
            self.persistence_ok = False
            if queue_if_down:
                return self._queue_new(order, result, str(error))
            log.error("order_not_saved", order_id=order.order_id, error=str(error))
            return self._refuse(
                result.command_id, result.idempotency_key, f"{CANNOT_SAVE}: {error}"
            )
        self.orders[order.order_id] = order
        return result

    def _queue_new(self, order: Order, result: CommandResult, why: str) -> CommandResult:
        """Hold an exit in memory with its create-write queued (the first of its writes)."""
        self.persistence_ok = False
        self.orders[order.order_id] = order
        self._pending.append(lambda: self._replay_create(order, result))
        log.warning("exit_not_stored_yet", order_id=order.order_id, why=why)
        return result

    async def _replay_create(self, order: Order, result: CommandResult) -> None:
        """Store a queued exit. The first attempt may have committed and then reported a failure
        (a timeout after COMMIT), so a failed insert is not final: if the result for this key is
        in the database the order is stored, and the queue moves on instead of failing on the
        duplicate key at its head for ever."""
        try:
            await self._repo.create_order(order, result)
        except PersistenceError:
            stored = await self._repo.find_result(order.account_id, result.idempotency_key)
            if stored is None or stored.order_id != order.order_id:
                # Not stored (or the key belongs to another order): the failure stands, the queue
                # keeps this write at its head and retries, and entries stay refused meanwhile.
                raise

    def _known_in_memory(self, command: PlaceOrder) -> CommandResult | None:
        """The answer for a repeat of ``command`` while the database cannot be asked: the order
        this run already created for its key, as an accepted result. None if there is none."""
        for held in self.orders.values():
            if held.idempotency_key == command.idempotency_key:
                return self._result(
                    command,
                    accepted=held.state is not OrderState.REJECTED,
                    order_id=held.order_id,
                    reason=held.reason if held.state is OrderState.REJECTED else None,
                )
        return None

    async def _reject(self, command: PlaceOrder | CancelOrder, reason: str) -> CommandResult:
        """Answer a command that created no order, and remember the answer for repeats."""
        result = self._result(command, accepted=False, reason=reason)
        await self._store(lambda: self._repo.save_result(result), f"result {command.command_id}")
        return result

    def _refuse(self, command_id: str, idempotency_key: str, reason: str) -> CommandResult:
        """A rejection that could not be stored: still answered, but a repeat is re-evaluated."""
        return CommandResult(
            command_id=command_id,
            idempotency_key=idempotency_key,
            account_id=self._account_id,
            accepted=False,
            reason=reason,
            decided_at=self._now(),
        )

    def _result(
        self,
        command: PlaceOrder | CancelOrder,
        *,
        accepted: bool,
        order_id: str | None = None,
        reason: str | None = None,
    ) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=self._account_id,
            accepted=accepted,
            order_id=order_id,
            reason=reason,
            decided_at=self._now(),
        )

    def _new_order(self, command: PlaceOrder) -> Order:
        now = self._now()
        return Order(
            order_id=uuid.uuid4().hex,
            account_id=self._account_id,
            key=command.key,
            side=command.side,
            quantity=command.quantity,
            product=command.product,
            order_type=command.order_type,
            limit_price=command.limit_price,
            initial_stop=command.initial_stop,
            idempotency_key=command.idempotency_key,
            source=command.source,
            paper=self._paper,
            created_at=now,
            updated_at=now,
        )

    # ---- broker updates (fills, cancels, rejections) ---------------------------------------

    async def run_updates(self) -> None:
        """Apply the broker's update stream until cancelled (the engine supervises this task)."""
        async for update in self._adapter.updates():
            await self.on_update(update)

    async def on_update(self, update: BrokerUpdate) -> None:
        async with self._lock:
            await self._apply_update(update)

    async def _apply_update(self, update: BrokerUpdate) -> None:
        order = next(
            (o for o in self.orders.values() if o.broker_order_id == update.broker_order_id), None
        )
        if order is None:
            # Perhaps an Unknown order we have no broker id for yet: replay after reconciling.
            log.warning("update_for_unmatched_order", broker_order_id=update.broker_order_id)
            self._held_updates.append(update)
            return
        try:
            match update:
                case Filled():
                    await self._on_fill(order, update)
                case Cancelled():
                    if order.state is not OrderState.CANCELLED:  # expiry may have got there first
                        await self._advance(order, OrderState.CANCELLED, reason=update.reason)
                case Rejected():
                    await self._advance(order, OrderState.REJECTED, reason=update.reason)
        except InvalidTransition:
            # The order stays as it was (spec "Update after a final state").
            log.error(
                "invalid_transition", order_id=order.order_id, state=order.state.value,
                update=type(update).__name__,
            )  # fmt: skip

    async def _on_fill(self, order: Order, update: Filled) -> None:
        if update.fill_id in self._fill_ids:
            return  # a repeat of a fill already applied
        fill = Fill(
            fill_id=update.fill_id, order_id=order.order_id, account_id=order.account_id,
            key=order.key, product=order.product, side=order.side, quantity=update.quantity,
            price=update.price, filled_at=update.at, costs=update.costs, paper=order.paper,
        )  # fmt: skip
        total = order.filled_quantity + update.quantity
        if order.state.is_final or total > order.quantity:
            # Recorded for reconciliation, but no position changes.
            log.error("fill_not_applied", order_id=order.order_id, state=order.state.value)
            self.unapplied_fills.append(fill)
            return
        if order.state is OrderState.UNKNOWN:
            order = await self._advance(order, OrderState.OPEN)  # a fill proves it is live
        average = (
            (order.average_fill_price or Decimal(0)) * order.filled_quantity
            + update.price * update.quantity
        ) / total
        target = OrderState.FILLED if total == order.quantity else OrderState.PARTIALLY_FILLED
        moved = transition(
            order, target, at=update.at, filled_quantity=total, average_fill_price=average
        )
        pid = position_id(fill)
        position, _ = apply(
            self.positions.get(pid) or flat(order.account_id, order.key, order.product, update.at),
            fill,
        )
        self._fill_ids.add(fill.fill_id)
        self.orders[moved.order_id] = moved
        self.positions[pid] = position
        await self._store(
            lambda: self._repo.record_fill(moved, fill, position), f"fill {fill.fill_id}"
        )
        try:
            await self._position_feed.update(position)
        except SubscriptionLimitError as error:
            log.error("position_not_subscribed", key=str(order.key), error=str(error))
        for listener in self._fill_listeners:
            try:
                await listener(fill, position)
            except Exception:
                # A listener is a client of the fill, never part of applying it: the fill and
                # the position are already stored, so a failing listener cannot undo them.
                log.error("fill_listener_failed", fill_id=fill.fill_id, exc_info=True)

    def add_fill_listener(self, listener: FillListener) -> None:
        """Call ``listener(fill, position)`` after every applied fill, in registration order.

        The listener runs *inside* the service lock, right after the fill and the position it
        produced are in memory and queued for storage. So it sees a consistent book, and it
        must not call ``place`` or ``cancel`` itself: those take the same (non-reentrant)
        ``asyncio.Lock`` and would wait on it forever, like re-entering a C# ``SemaphoreSlim(1)``.
        A listener that has orders to send puts the work on a queue and returns.
        """
        self._fill_listeners.append(listener)

    # ---- restart recovery (task 8.1; ``engine.orders.recovery`` drives these) -----------------

    async def restore(self, state: LoadedState) -> None:
        """Take over the stored open orders, positions and applied fill ids after a restart."""
        async with self._lock:
            self.orders = {o.order_id: o for o in state.open_orders}
            self.positions = {position_id(p): p for p in state.positions}
            self._fill_ids = {f.fill_id for f in state.fills}
            for position in state.positions:
                try:
                    await self._position_feed.update(position)
                except SubscriptionLimitError as error:
                    log.error("position_not_subscribed", key=str(position.key), error=str(error))

    async def settle_in_flight(self) -> None:
        """Resolve orders caught mid-submission by the crash. ``Requested`` was stored but never
        sent (``Submitted`` is stored *before* the broker call), so it is safely ``Rejected``.
        ``Submitted`` may or may not have reached the broker, so it becomes ``Unknown`` and is
        reconciled against the order book: never resent (rule 4)."""
        async with self._lock:
            for order in list(self.orders.values()):
                if order.state is OrderState.REQUESTED:
                    await self._advance(
                        order, OrderState.REJECTED, reason="engine restarted before sending"
                    )
                elif order.state is OrderState.SUBMITTED:
                    await self._advance(
                        order, OrderState.UNKNOWN, reason="engine restarted while submitting"
                    )

    # ---- Unknown orders --------------------------------------------------------------------

    async def reconcile_unknown(self) -> None:
        """Resolve ``Unknown`` orders from the broker's order book. Never resubmits (rule 4).

        Found and live -> Open (its fills arrive on the update stream); found cancelled or
        rejected, or not found at all -> Rejected. The transition table has no Unknown ->
        Cancelled, so a broker-cancelled order is recorded as Rejected with the reason.
        """
        async with self._lock:
            unknown = [o for o in self.orders.values() if o.state is OrderState.UNKNOWN]
            if not unknown:
                return
            book = {b.engine_order_id: b for b in await self._adapter.order_book()}
            for order in unknown:
                entry = book.get(order.order_id)
                if entry is None:
                    await self._advance(
                        order, OrderState.REJECTED, reason="broker has no such order"
                    )
                elif entry.state in (OrderState.REJECTED, OrderState.CANCELLED):
                    await self._advance(
                        order, OrderState.REJECTED, reason=f"broker reports {entry.state.value}"
                    )
                else:
                    await self._advance(
                        order, OrderState.OPEN, broker_order_id=entry.broker_order_id
                    )
            held, self._held_updates = self._held_updates, []
            for update in held:
                await self._apply_update(update)

    # ---- cancel and expiry -----------------------------------------------------------------

    async def cancel(self, command: CancelOrder) -> CommandResult:
        """Ask the broker to cancel. The order becomes Cancelled when the broker confirms on
        the update stream, so a fill that beats the cancel still applies."""
        with self._tracer.start_as_current_span("order.cancel") as span:
            span.set_attribute("order_id", command.order_id)
            async with self._lock:
                return await self._cancel(command)

    async def _cancel(self, command: CancelOrder) -> CommandResult:
        try:
            stored = await self._repo.find_result(command.account_id, command.idempotency_key)
        except PersistenceError as error:
            return self._refuse(
                command.command_id, command.idempotency_key, f"{CANNOT_SAVE}: {error}"
            )
        if stored is not None:
            return stored
        order = self.orders.get(command.order_id)
        if order is None:
            return await self._reject(command, f"unknown order id {command.order_id}")
        if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            return await self._reject(
                command, f"order is {order.state.value}: only open orders can be cancelled"
            )
        try:
            await self._broker_cancel(order)
        except BrokerError as error:
            return await self._reject(command, str(error))
        except TimeoutError:
            return await self._reject(command, "broker did not answer the cancel")
        result = self._result(command, accepted=True, order_id=order.order_id)
        await self._store(lambda: self._repo.save_result(result), f"result {command.command_id}")
        return result

    async def expire_at_close(self) -> int:
        """Cancel every open order once the 15:30 IST close has passed; returns how many.

        Orders the broker will not cancel (for example it just filled) are left for their
        update to resolve. The caller (the engine's timer, task 8.2) invokes this at close.
        """
        now = self._now()
        if now < market_close_utc(ist_date(now)):
            return 0
        expired = 0
        async with self._lock:
            for order in list(self.orders.values()):
                if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                    continue
                try:
                    await self._broker_cancel(order)
                except (BrokerError, TimeoutError) as error:
                    log.warning("expiry_cancel_failed", order_id=order.order_id, error=str(error))
                    continue
                await self._advance(order, OrderState.CANCELLED, reason="expired at close")
                expired += 1
        return expired

    async def _broker_cancel(self, order: Order) -> None:
        async with asyncio.timeout(self._ack_timeout_s):
            await self._adapter.cancel(order)

    def _holdings(self, command: PlaceOrder) -> Holdings:
        """What a CNC (delivery) sell may draw on; other orders need no holdings."""
        if command.product is not ProductType.CNC or command.side is not OrderSide.SELL:
            return NO_HOLDINGS
        position = self.positions.get((self._account_id, command.key, ProductType.CNC))
        # A negative CNC quantity is not a holding, so it never counts as negative "held".
        held = max(0, position.quantity) if position else 0
        # Unknown orders count too: they may be live at the broker, so their units are spoken for.
        selling = sum(
            o.quantity - o.filled_quantity
            for o in self.orders.values()
            if o.key == command.key
            and o.product is ProductType.CNC
            and o.side is OrderSide.SELL
            and not o.state.is_final
        )
        return Holdings(held=held, selling=selling)
