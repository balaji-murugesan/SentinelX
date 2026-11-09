"""Restart recovery (trading-state "Restart recovery"; design §9). Runs before any command is read.

Steps, in order:
  1. load open orders, fills, positions and applied fill ids from the database;
  2. rebuild the paper broker's resting orders from the open orders (``restore_broker``);
  3. settle orders the crash caught mid-submission, then reconcile ``Unknown`` ones;
  4. compare every open order with the broker's order book;
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

from contracts.orders import Fill, Order, OrderState, Position
from engine.broker.adapter import BrokerAdapter, BrokerError, BrokerOrder
from engine.market_data.clock import utc_now
from engine.orders.service import OrderService
from engine.persistence.repository import PersistenceError, TradingRepository
from engine.positions.book import rebuild

log = structlog.get_logger("engine.orders.recovery")

_LIVE = (OrderState.OPEN, OrderState.PARTIALLY_FILLED)


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
        found += _order_differences(service.orders.values(), book)
    found += _position_differences(service, state.fills)

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
    retry_min_s: float = 0.5,
    retry_max_s: float = 30.0,
) -> list[str]:
    """``recover`` until the stored state loads (spec "Recovery completes before trading").

    Until it succeeds nothing is loaded, so ``reconciled`` stays False and every new order is
    refused. Backoff doubles from ``retry_min_s`` up to ``retry_max_s``. Stops when ``stop`` is
    set. ``asyncio.wait_for(stop.wait(), delay)`` is an interruptible sleep: like awaiting
    ``Task.Delay(delay, cancellationToken)`` in C#.
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
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except TimeoutError:
            delay = min(delay * 2, retry_max_s)
        else:
            return ["stopped before recovery completed"]


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


def _position_differences(service: OrderService, fills: list[Fill]) -> list[str]:
    rebuilt = rebuild(fills)
    stored = service.positions
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
