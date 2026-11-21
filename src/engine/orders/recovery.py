"""Restart recovery (trading-state "Restart recovery"; design §9). Runs before any command is read.

Steps, in order:
  1. load open orders, fills, positions and applied fill ids from the database;
  2. rebuild the paper broker's resting orders from the open orders (``restore_broker``);
  3. settle orders the crash caught mid-submission, then reconcile ``Unknown`` ones;
  4. apply rejections the broker's order book shows for ``Open``, unfilled orders (missed while
     the engine was down), then compare every open order with that book;
  5. rebuild positions from fills and compare with the stored positions.

Any difference HALTS the engine: ``service.reconciled = False``, so new orders are rejected
("state is not reconciled with the broker") while fills and cancels for existing orders are still
processed. The halt is stored in Postgres and is sticky: a later restart that finds no
differences does not clear it. Only the resume command does (design §9). Each difference is
logged (rule 10: ids and numbers only, no broker bodies).
"""

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

import structlog

from contracts.commands import CommandResult, ResumeRecovery
from contracts.orders import Fill, Order, OrderState, Position
from engine.broker.adapter import BrokerAdapter, BrokerError, BrokerOrder
from engine.market_data.clock import utc_now
from engine.orders.service import OrderService
from engine.persistence.repository import LoadedState, PersistenceError, TradingRepository
from engine.positions.book import PositionId, position_id, rebuild

log = structlog.get_logger("engine.orders.recovery")

_LIVE = (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
_RESUME_REFUSED = "trading state cannot be saved: resume is refused until it recovers"


async def recover(
    service: OrderService,
    repository: TradingRepository,
    adapter: BrokerAdapter,
    *,
    account_id: str,
    restore_broker: Callable[[list[Order]], None] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> list[str]:
    """Rebuild in-memory state and return why the engine is halted (empty = reconciled).

    Raises ``PersistenceError`` if the stored state cannot be loaded: nothing is known yet, so
    new orders stay refused; ``recover_with_retry`` keeps trying.
    """
    try:
        state = await repository.load(account_id)
    except PersistenceError as error:
        service.persistence_ok = False
        service.reconciled = False
        log.error("recovery_cannot_load", error=str(error))
        raise
    service.persistence_ok = True

    await service.restore(state)
    if restore_broker is not None:
        restore_broker(state.open_orders)

    found: list[str] = []
    try:
        await service.settle_in_flight()
        await service.reconcile_unknown()
        book = {b.engine_order_id: b for b in await adapter.order_book()}
    except (BrokerError, TimeoutError, OSError) as error:
        found.append(f"broker order book unavailable: {error}")
        book = {}
    else:
        # A rejection missed while the engine was down is applied, not reported (design §3).
        await service.apply_book_rejections(book)
        found += _order_differences(service.orders.values(), book)
    found += _position_differences(service.positions, state.fills)

    # Sticky halt: new differences replace the stored ones; with none, a stored halt stays.
    if found:
        try:
            await repository.store_halt(account_id, found, now())
        except PersistenceError as error:
            # Still halted in memory; a restart recomputes the differences from the same data.
            log.error("halt_not_stored", error=str(error))
    halted = found or (list(state.halt.differences) if state.halt else [])

    service.halt_differences = halted
    service.reconciled = not halted
    for difference in halted:
        log.error("reconciliation_difference", difference=difference)
    if halted:
        log.error("engine_halted", differences=len(halted), stored_halt=state.halt is not None)
    else:
        log.info("recovery_reconciled", open_orders=len(service.orders))
    return halted


async def recover_with_retry(
    service: OrderService,
    repository: TradingRepository,
    adapter: BrokerAdapter,
    *,
    account_id: str,
    stop: asyncio.Event,
    restore_broker: Callable[[list[Order]], None] | None = None,
    now: Callable[[], datetime] = utc_now,
    first_attempt: asyncio.Event | None = None,
    retry_min_s: float = 0.5,
    retry_max_s: float = 30.0,
) -> list[str]:
    """``recover`` until the stored state loads (spec "Recovery completes before trading").

    Until it succeeds nothing is loaded, so ``reconciled`` stays False and every new order is
    refused. Backoff doubles from ``retry_min_s`` up to ``retry_max_s``. Stops when ``stop`` is
    set. ``asyncio.wait_for(stop.wait(), delay)`` is an interruptible sleep: like awaiting
    ``Task.Delay(delay, cancellationToken)`` in C#.

    ``first_attempt`` is set once the first try has finished, whether it worked or not. The
    engine starts reading commands then: normally recovery is already done (spec "before
    consuming any command"); with Postgres down, commands are answered with a rejection instead
    of waiting unanswered until it comes back.
    """
    service.reconciled = False
    delay = retry_min_s
    while True:
        try:
            return await recover(
                service,
                repository,
                adapter,
                account_id=account_id,
                restore_broker=restore_broker,
                now=now,
            )
        except PersistenceError:
            log.warning("recovery_retry", in_s=delay)
        finally:
            if first_attempt is not None:
                first_attempt.set()
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except TimeoutError:
            delay = min(delay * 2, retry_max_s)
        else:
            return ["stopped before recovery completed"]


async def resume_recovery(
    service: OrderService,
    repository: TradingRepository,
    adapter: BrokerAdapter,
    command: ResumeRecovery,
    *,
    account_id: str,
    now: Callable[[], datetime] = utc_now,
) -> CommandResult:
    """Handle ``cli recovery resume``: re-check, and clear the halt only if nothing differs.

    Idempotent like every command: a repeated key returns the first answer. Before checking it
    settles the in-memory orders the broker's book decides: ``Unknown`` orders are reconciled
    and, while halted, ``Open`` unfilled orders the book reports ``Rejected`` become ``Rejected``
    (both stored). The check then compares the broker's order book and the *database*; it also
    refuses while memory and database disagree, which means the data was edited after this
    engine loaded it, so the owner must restart the engine first.
    """

    def result(accepted: bool, reason: str | None) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=command.account_id,
            accepted=accepted,
            reason=reason,
            decided_at=now(),
        )

    try:
        previous = await repository.find_result(command.account_id, command.idempotency_key)
    except PersistenceError as error:
        return result(False, f"trading state cannot be saved: {error}")
    if previous is not None:
        return previous
    answer = await _resume(service, repository, adapter, account_id, now, result)
    try:
        await repository.save_result(answer)
    except PersistenceError as error:  # the halt change above is already stored or refused
        log.error("resume_result_not_stored", error=str(error))
    return answer


async def _resume(
    service: OrderService,
    repository: TradingRepository,
    adapter: BrokerAdapter,
    account_id: str,
    now: Callable[[], datetime],
    result: Callable[[bool, str | None], CommandResult],
) -> CommandResult:
    if not service.persistence_ok:
        return result(False, _RESUME_REFUSED)
    found: list[str] = []
    try:
        await service.reconcile_unknown()
        book = {b.engine_order_id: b for b in await adapter.order_book()}
        if not service.reconciled:
            # Halted only: on a running engine a rejection in the book is still on its way down
            # the update stream, which must apply it so the breaker counts it. Before the load,
            # so the database compared below already holds the Rejected state (design §3).
            await service.apply_book_rejections(book)
        if not service.persistence_ok:
            # A write above was queued (the database is failing): the halt stays (fail closed).
            return result(False, _RESUME_REFUSED)
        state = await repository.load(account_id)
    except (BrokerError, TimeoutError, OSError) as error:
        return result(False, f"broker order book unavailable: {error}")
    except PersistenceError as error:
        return result(False, f"trading state cannot be saved: {error}")
    if state.halt is None and service.reconciled:
        return result(True, None)  # not halted: nothing to clear

    found += _order_differences(state.open_orders, book)
    found += _position_differences({position_id(p): p for p in state.positions}, state.fills)
    found += _stale_memory(service, state)
    try:
        if found:
            await repository.store_halt(account_id, found, now())
        else:
            await repository.clear_halt(account_id, now())
    except PersistenceError as error:
        return result(False, f"trading state cannot be saved: {error}")
    service.halt_differences = found
    service.reconciled = not found
    if found:
        for difference in found:
            log.error("reconciliation_difference", difference=difference)
        return result(False, "still halted: " + "; ".join(found))
    log.info("recovery_resumed")
    return result(True, None)


def _stale_memory(service: OrderService, state: LoadedState) -> list[str]:
    """Differences between the live in-memory book and what the database holds now."""
    out = []
    for stored in state.open_orders:
        mine = service.orders.get(stored.order_id)
        if mine is None or (mine.state, mine.filled_quantity) != (
            stored.state,
            stored.filled_quantity,
        ):
            out.append(
                f"order {stored.order_id} changed in the database since this engine loaded it: "
                "restart the engine, then resume"
            )
    in_db = {position_id(p): _figures(p) for p in state.positions}
    if in_db != {pid: _figures(p) for pid, p in service.positions.items()}:
        out.append(
            "positions changed in the database since this engine loaded them: "
            "restart the engine, then resume"
        )
    return out


def _order_differences(orders: Iterable[Order], book: dict[str, BrokerOrder]) -> list[str]:
    out = []
    for order in orders:
        if order.state not in _LIVE:
            continue
        entry = book.get(order.order_id)
        if entry is None:
            out.append(f"order {order.order_id} is {order.state.value} here but not at the broker")
        elif entry.state is not order.state or entry.filled_quantity != order.filled_quantity:
            out.append(
                f"order {order.order_id}: engine {order.state.value} filled "
                f"{order.filled_quantity}, broker {entry.state.value} filled "
                f"{entry.filled_quantity}"
            )
    return out


def _position_differences(stored: dict[PositionId, Position], fills: list[Fill]) -> list[str]:
    rebuilt = rebuild(fills)
    out = []
    for pid in sorted(set(rebuilt) | set(stored), key=str):
        mine, theirs = stored.get(pid), rebuilt.get(pid)
        if mine is None and theirs is not None and theirs.quantity == 0:
            continue  # a flat position rebuilt from fills but never stored: nothing to hold
        if _figures(mine) != _figures(theirs):
            out.append(
                f"position {pid[1]} {pid[2].value}: stored {_figures(mine)}, "
                f"rebuilt from fills {_figures(theirs)}"
            )
    return out


def _figures(p: Position | None) -> tuple[int, Decimal, Decimal, Decimal] | None:
    """The numbers that must survive a restart (not ``updated_at``)."""
    return None if p is None else (p.quantity, p.average_price, p.realized_pnl, p.costs)
