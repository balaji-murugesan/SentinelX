"""Restart recovery (trading-state "Restart recovery"; design §9). Runs before any command is read.

Steps, in order:
  1. load open orders, fills, positions and applied fill ids from the database;
  2. rebuild the paper broker's resting orders from the open orders (``restore_broker``);
  3. settle orders the crash caught mid-submission, then reconcile ``Unknown`` ones;
  4. compare every open order with the broker's order book;
  5. rebuild positions from fills and compare with the stored positions.

Any difference sets ``service.reconciled = False``: new orders are then rejected ("state is not
reconciled with the broker") while fills and cancels for existing orders are still processed.
Clearing it is manual in this stage: the owner fixes the data and restarts. Each difference is
logged (rule 10: ids and numbers only, no broker bodies).
"""

from collections.abc import Callable, Iterable
from decimal import Decimal

import structlog

from contracts.orders import Fill, Order, OrderState, Position
from engine.broker.adapter import BrokerAdapter, BrokerError, BrokerOrder
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
) -> list[str]:
    """Rebuild in-memory state and return the differences found (empty = reconciled)."""
    try:
        state = await repository.load(account_id)
    except PersistenceError as error:
        # Cannot even read our own records: refuse new orders until the owner restarts us.
        service.persistence_ok = False
        service.reconciled = False
        log.error("recovery_cannot_load", error=str(error))
        return [f"stored state could not be loaded: {error}"]

    await service.restore(state)
    if restore_broker is not None:
        restore_broker(state.open_orders)

    differences: list[str] = []
    try:
        await service.settle_in_flight()
        await service.reconcile_unknown()
        book = {b.engine_order_id: b for b in await adapter.order_book()}
    except (BrokerError, TimeoutError, OSError) as error:
        differences.append(f"broker order book unavailable: {error}")
        book = {}
    else:
        differences += _order_differences(service.orders.values(), book)
    differences += _position_differences(service, state.fills)

    service.reconciled = not differences
    for difference in differences:
        log.error("reconciliation_difference", difference=difference)
    if not differences:
        log.info("recovery_reconciled", open_orders=len(service.orders))
    return differences


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
