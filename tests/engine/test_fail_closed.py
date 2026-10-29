"""Fail-closed persistence (task 6.3).

Scenarios: trading-state "Postgres down" and "Postgres back", plus: fills for existing orders
keep applying while new orders are rejected, queued writes replay in their original order,
and the retry loop recovers by itself.
"""

import asyncio
from decimal import Decimal

import structlog

from contracts.commands import CommandResult
from contracts.orders import CostBreakdown, Fill, Order, OrderState, Position
from engine.broker.adapter import Filled
from engine.persistence.repository import LoadedState, PersistenceError
from tests.engine.test_order_service import NOW, build, command
from tests.engine.trading_fakes import MemoryRepository


class FailingRepository(MemoryRepository):
    """A MemoryRepository whose reads and writes can each be switched off, like Postgres down."""

    def __init__(self) -> None:
        super().__init__()
        self.reads_down = False
        self.writes_down = False

    def _check(self, down: bool) -> None:
        if down:
            raise PersistenceError("database down")

    async def find_result(self, account_id: str, idempotency_key: str) -> CommandResult | None:
        self._check(self.reads_down)
        return await super().find_result(account_id, idempotency_key)

    async def save_result(self, result: CommandResult) -> None:
        self._check(self.writes_down)
        await super().save_result(result)

    async def create_order(self, order: Order, result: CommandResult) -> None:
        self._check(self.writes_down)
        await super().create_order(order, result)

    async def update_order(self, order: Order) -> None:
        self._check(self.writes_down)
        await super().update_order(order)

    async def record_fill(self, order: Order, fill: Fill, position: Position) -> None:
        self._check(self.writes_down)
        await super().record_fill(order, fill, position)

    async def load(self, account_id: str) -> LoadedState:
        self._check(self.reads_down)
        return await super().load(account_id)

    def go_down(self) -> None:
        self.reads_down = self.writes_down = True

    def come_back(self) -> None:
        self.reads_down = self.writes_down = False


def fill(fill_id: str, quantity: int = 65) -> Filled:
    return Filled("b-1", fill_id, quantity, Decimal("100"), NOW, CostBreakdown())


async def test_postgres_down_refuses_the_order_without_a_broker_call() -> None:
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    repo.writes_down = True  # reads still work: the failure is on create_order

    with structlog.testing.capture_logs() as logs:
        result = await service.place(command())

    assert not result.accepted and "trading state cannot be saved" in (result.reason or "")
    assert adapter.placed == [] and not service.persistence_ok
    assert any(entry["log_level"] == "error" for entry in logs)  # spec: an error is logged
    again = await service.place(command("k2"))  # still refused while it is down
    assert not again.accepted and adapter.placed == []


async def test_postgres_back_accepts_the_next_order() -> None:
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    repo.writes_down = True
    await service.place(command())
    assert not service.persistence_ok

    repo.come_back()
    assert await service.drain_pending()  # nothing queued; a read probe proves it is back

    assert service.persistence_ok
    result = await service.place(command("k2"))
    assert result.accepted and len(adapter.placed) == 1


async def test_fills_keep_applying_while_new_orders_are_refused() -> None:
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    placed = await service.place(command(quantity=65))
    repo.go_down()

    await service.on_update(fill("f1"))  # a fill for an existing order: applied in memory

    order = service.orders[placed.order_id or ""]
    assert order.state is OrderState.FILLED
    assert next(iter(service.positions.values())).quantity == 65
    assert repo.fills == [] and not service.persistence_ok  # queued, not yet stored
    assert not (await service.place(command("k2"))).accepted
    assert len(adapter.placed) == 1  # the refused order never reached the broker

    repo.come_back()
    assert await service.drain_pending()
    assert [f.fill_id for f in repo.fills] == ["f1"]
    assert repo.orders[order.order_id].state is OrderState.FILLED
    assert (await service.place(command("k3"))).accepted


async def test_queued_writes_replay_in_their_original_order() -> None:
    repo = FailingRepository()
    service, _, _ = build(repository=repo)
    placed = await service.place(command(quantity=130))
    repo.go_down()

    await service.on_update(fill("f1"))
    await service.on_update(fill("f2"))
    repo.come_back()
    assert await service.drain_pending()

    assert [f.fill_id for f in repo.fills] == ["f1", "f2"]
    assert repo.orders[placed.order_id or ""].state is OrderState.FILLED
    assert [e[1] for e in repo.events if e[0] == placed.order_id][-2:] == [
        "PartiallyFilled",
        "Filled",
    ]


async def test_drain_stops_at_the_first_failure_and_keeps_the_queue() -> None:
    repo = FailingRepository()
    service, _, _ = build(repository=repo)
    await service.place(command(quantity=130))
    repo.go_down()
    await service.on_update(fill("f1"))
    await service.on_update(fill("f2"))

    assert not await service.drain_pending()  # still down
    repo.come_back()
    assert await service.drain_pending()
    assert len(repo.fills) == 2  # nothing lost or doubled by the failed attempt


async def test_retry_loop_recovers_by_itself() -> None:
    repo = FailingRepository()
    service, _, _ = build(repository=repo, retry_min_s=0.01)
    await service.place(command())
    repo.go_down()
    await service.on_update(fill("f1"))
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_pending_writes(stop))

    await asyncio.sleep(0.05)  # a few failed retries at 10 ms, 20 ms, ...
    assert not service.persistence_ok
    repo.come_back()
    async with asyncio.timeout(2):
        while not service.persistence_ok:  # noqa: ASYNC110 - the service has no event to await
            await asyncio.sleep(0.01)

    stop.set()
    await task
    assert [f.fill_id for f in repo.fills] == ["f1"]
