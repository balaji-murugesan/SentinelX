"""Restart recovery (task 8.1): trading-state "Mismatch after restart", order-commands "Duplicate
after restart", plus the in-flight and position-comparison rules of design §9.

A "restart" here is a brand-new ``OrderService`` built on the *same* ``MemoryRepository``: nothing
survives in memory, only what was written to the repository.
"""

import asyncio
from decimal import Decimal

import pytest

from contracts.commands import CancelOrder
from contracts.orders import CostBreakdown, OrderState
from engine.broker.adapter import BrokerOrder, Filled
from engine.orders.recovery import recover, recover_with_retry
from engine.orders.service import OrderService
from engine.persistence.repository import LoadedState, PersistenceError
from tests.engine.test_order_service import NOW, FakeAdapter, build, command
from tests.engine.trading_fakes import MemoryRepository


class BookAdapter(FakeAdapter):
    """A fake whose order book we control (the real one is the broker's view after the crash)."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[BrokerOrder] = []

    async def order_book(self) -> list[BrokerOrder]:
        return self.entries


async def restart(
    repo: MemoryRepository, adapter: BookAdapter | None = None
) -> tuple[OrderService, BookAdapter, list[str]]:
    adapter = adapter or BookAdapter()
    service, _, _ = build(adapter=adapter, repository=repo)
    differences = await recover(service, repo, adapter, account_id="main")
    return service, adapter, differences


async def open_order_before_crash() -> tuple[MemoryRepository, str]:
    """Place one resting limit order and return the repository plus its order id."""
    service, _, repo = build()
    result = await service.place(command("k1", limit="100"))
    assert result.accepted and result.order_id
    return repo, result.order_id


async def test_matching_state_is_reconciled_and_restored() -> None:
    repo, order_id = await open_order_before_crash()
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-1", order_id, OrderState.OPEN, 0, None)]

    service, _, differences = await restart(repo, adapter)

    assert differences == [] and service.reconciled
    assert service.orders[order_id].state is OrderState.OPEN


async def test_duplicate_after_restart_returns_the_original_result() -> None:
    repo, order_id = await open_order_before_crash()
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-1", order_id, OrderState.OPEN, 0, None)]
    service, _, _ = await restart(repo, adapter)

    again = await service.place(command("k1", limit="100"))

    assert again.accepted and again.order_id == order_id
    assert adapter.placed == [] and len(repo.orders) == 1  # nothing was sent a second time


async def test_order_missing_at_broker_blocks_new_orders_but_not_cancels() -> None:
    repo, order_id = await open_order_before_crash()

    service, adapter, differences = await restart(repo)  # broker's book is empty

    assert len(differences) == 1 and "not at the broker" in differences[0]
    assert not service.reconciled
    refused = await service.place(command("k2", limit="100"))
    assert not refused.accepted and "not reconciled" in (refused.reason or "")
    assert adapter.placed == []
    cancel = CancelOrder(
        command_id="c-x", idempotency_key="x1", account_id="main", source="cli",
        sent_at=NOW, order_id=order_id,
    )  # fmt: skip
    assert (await service.cancel(cancel)).accepted


async def test_state_mismatch_at_broker_is_reported() -> None:
    repo, order_id = await open_order_before_crash()
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-1", order_id, OrderState.PARTIALLY_FILLED, 20, None)]

    service, _, differences = await restart(repo, adapter)

    assert len(differences) == 1 and "broker PartiallyFilled filled 20" in differences[0]
    assert not service.reconciled


async def test_broker_unreachable_leaves_state_unreconciled() -> None:
    repo, _ = await open_order_before_crash()

    class Down(BookAdapter):
        async def order_book(self) -> list[BrokerOrder]:
            raise TimeoutError("no answer")

    service, _, differences = await restart(repo, Down())

    assert any("order book unavailable" in d for d in differences)
    assert not service.reconciled


async def test_requested_order_is_rejected_and_submitted_one_is_reconciled() -> None:
    """A crash between the writes: Requested never reached the broker; Submitted may have."""
    service, _, repo = build()
    first = await service.place(command("k1", limit="100"))
    second = await service.place(command("k2", limit="100"))
    assert first.order_id and second.order_id
    # Rewind the stored states as if the engine died mid-submission.
    repo.orders[first.order_id] = repo.orders[first.order_id].model_copy(
        update={"state": OrderState.REQUESTED}
    )
    repo.orders[second.order_id] = repo.orders[second.order_id].model_copy(
        update={"state": OrderState.SUBMITTED}
    )
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-2", second.order_id, OrderState.OPEN, 0, None)]

    restarted, adapter, differences = await restart(repo, adapter)

    assert repo.orders[first.order_id].state is OrderState.REJECTED
    assert restarted.orders[second.order_id].state is OrderState.OPEN
    assert adapter.placed == []  # rule 4: never resent
    assert differences == [] and restarted.reconciled


async def test_positions_rebuilt_from_fills_match_after_restart() -> None:
    service, _, repo = build()
    result = await service.place(command("k1"))
    order = repo.orders[result.order_id or ""]
    await service.on_update(
        Filled(order.broker_order_id or "", "f1", 65, Decimal("100"), NOW, CostBreakdown())
    )

    restarted, _, differences = await restart(repo)

    assert differences == [] and restarted.reconciled
    assert sum(p.quantity for p in restarted.positions.values()) == 65


async def test_position_that_disagrees_with_fills_blocks_new_orders() -> None:
    service, _, repo = build()
    result = await service.place(command("k1"))
    order = repo.orders[result.order_id or ""]
    await service.on_update(
        Filled(order.broker_order_id or "", "f1", 65, Decimal("100"), NOW, CostBreakdown())
    )
    for pid, position in list(repo.positions.items()):  # corrupt the stored position
        repo.positions[pid] = position.model_copy(update={"average_price": Decimal("999")})

    restarted, _, differences = await restart(repo)

    assert len(differences) == 1 and "rebuilt from fills" in differences[0]
    assert not restarted.reconciled


# ---- persisted halt (task 8.2) --------------------------------------------------------------


async def halted_repository() -> tuple[MemoryRepository, str]:
    """A repository whose engine restarted once against an empty broker book: HALTED, stored."""
    repo, order_id = await open_order_before_crash()
    _, _, differences = await restart(repo)  # broker's book is empty: a difference
    assert differences and repo.halts["main"].differences == tuple(differences)
    return repo, order_id


async def test_difference_stores_the_halt() -> None:
    repo, _ = await open_order_before_crash()

    service, _, differences = await restart(repo)

    assert repo.halts["main"].differences == tuple(differences)
    assert service.halt_differences == differences and not service.reconciled


async def test_halt_survives_restart_even_when_reconciliation_is_now_clean() -> None:
    repo, order_id = await halted_repository()
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-1", order_id, OrderState.OPEN, 0, None)]  # now it matches

    service, _, differences = await restart(repo, adapter)

    assert not service.reconciled and "not at the broker" in differences[0]  # the stored ones
    assert service.halt_differences == differences
    refused = await service.place(command("k2", limit="100"))
    assert not refused.accepted and "not reconciled" in (refused.reason or "")
    assert "main" in repo.halts  # still stored: only resume clears it


async def test_cleared_halt_lets_recovery_reconcile() -> None:
    repo, order_id = await halted_repository()
    await repo.clear_halt("main", NOW)  # what the resume command will do (task 8.3)
    adapter = BookAdapter()
    adapter.entries = [BrokerOrder("b-1", order_id, OrderState.OPEN, 0, None)]

    service, _, differences = await restart(repo, adapter)

    assert differences == [] and service.reconciled and service.halt_differences == []


async def test_halt_that_cannot_be_stored_still_halts() -> None:
    class NoHaltWrites(MemoryRepository):
        async def store_halt(self, account_id: str, differences: list[str], at: object) -> None:
            raise PersistenceError("database down")

    service, _, repo = build()
    await service.place(command("k1", limit="100"))
    broken = NoHaltWrites()
    broken.orders, broken.events, broken.results = repo.orders, repo.events, repo.results

    restarted, _, differences = await restart(broken)  # empty broker book: a difference

    assert differences and not restarted.reconciled


class FlakyLoad(MemoryRepository):
    """``load`` fails the first ``failures`` times, like Postgres still starting up."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.load_calls = 0

    async def load(self, account_id: str) -> LoadedState:
        self.load_calls += 1
        if self.load_calls <= self.failures:
            raise PersistenceError("database down")
        return await super().load(account_id)


async def test_recover_raises_and_refuses_orders_when_the_state_cannot_be_loaded() -> None:
    repo = FlakyLoad(failures=1)
    service, adapter, _ = build(repository=repo)

    with pytest.raises(PersistenceError):
        await recover(service, repo, adapter, account_id="main")

    assert not service.reconciled and not service.persistence_ok


async def test_recovery_retries_until_postgres_is_back_then_accepts_orders() -> None:
    repo = FlakyLoad(failures=2)
    adapter = BookAdapter()
    service, _, _ = build(adapter=adapter, repository=repo)
    task = asyncio.create_task(
        recover_with_retry(
            service,
            repo,
            adapter,
            account_id="main",
            stop=asyncio.Event(),
            retry_min_s=0.01,
            retry_max_s=0.02,
        )  # fmt: skip
    )
    await asyncio.sleep(0)  # let it make its first (failing) attempt
    assert not service.reconciled
    refused = await service.place(command("k1", limit="100"))  # while still loading
    assert not refused.accepted

    differences = await asyncio.wait_for(task, 2)

    assert differences == [] and service.reconciled and service.persistence_ok
    assert repo.load_calls == 3
    assert (await service.place(command("k2", limit="100"))).accepted


async def test_recovery_retry_stops_when_the_engine_is_stopping() -> None:
    repo = FlakyLoad(failures=10_000)
    adapter = BookAdapter()
    service, _, _ = build(adapter=adapter, repository=repo)
    stop = asyncio.Event()
    task = asyncio.create_task(
        recover_with_retry(
            service,
            repo,
            adapter,
            account_id="main",
            stop=stop,
            retry_min_s=0.01,
            retry_max_s=0.02,
        )  # fmt: skip
    )
    await asyncio.sleep(0.05)
    stop.set()

    assert await asyncio.wait_for(task, 2) == ["stopped before recovery completed"]
    assert not service.reconciled
