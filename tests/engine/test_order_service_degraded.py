"""The two ``OrderService`` hooks of add-risk-engine task 5.4: exits keep working when the engine is
degraded, and fill listeners.

Scenarios (risk-gate spec): "Kill with Postgres down" and "Entry in the same state"; plus an exit
during a recovery halt, a repeat of an exit while the database cannot be asked, and a gate refusal
of an exit while the database is down. Every S2 service test (``test_order_service`` and friends)
still passes unchanged: entries behave exactly as before.
"""

from decimal import Decimal
from typing import Any

import pytest

from contracts.commands import CancelOrder, CommandResult
from contracts.orders import CostBreakdown, Fill, OrderSide, OrderState, Position
from engine.broker.adapter import Filled
from engine.orders.service import OrderService
from engine.persistence.repository import PersistenceError
from engine.positions.book import position_id
from tests.engine.risk_builders import position
from tests.engine.test_order_service import NOW, build, command
from tests.engine.trading_fakes import MemoryRepository

BUY, SELL = OrderSide.BUY, OrderSide.SELL


class FailingRepository(MemoryRepository):
    """A database that can be taken down: while ``down`` every call fails."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise PersistenceError("database down")

    async def find_result(self, account_id: str, idempotency_key: str) -> Any:
        self._check()
        return await super().find_result(account_id, idempotency_key)

    async def save_result(self, result: Any) -> None:
        self._check()
        await super().save_result(result)

    async def create_order(self, order: Any, result: Any) -> None:
        self._check()
        await super().create_order(order, result)

    async def update_order(self, order: Any) -> None:
        self._check()
        await super().update_order(order)

    async def record_fill(self, order: Any, fill: Any, position: Any) -> None:
        self._check()
        await super().record_fill(order, fill, position)


def long_65(service: OrderService) -> None:
    held = position(quantity=65)
    service.positions[position_id(held)] = held


def states(repo: MemoryRepository) -> list[str]:
    return [state for _, state, _ in repo.events]


# ---- exits when the database is down -----------------------------------------------------------


async def test_kill_with_postgres_down_the_exit_is_sent_and_its_write_queued() -> None:
    """Spec "Kill with Postgres down"."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    long_65(service)
    repo.down = True

    result = await service.place(command("exit", side=SELL, quantity=65))

    assert result.accepted and result.order_id
    assert len(adapter.placed) == 1 and adapter.placed[0].side is SELL  # sent to the broker
    assert repo.orders == {} and not service.persistence_ok  # not stored yet
    held = service.orders[result.order_id]
    assert held.state is OrderState.OPEN  # advanced in memory

    repo.down = False
    assert await service.drain_pending() is True

    assert service.persistence_ok
    assert states(repo) == ["Requested", "Submitted", "Open"]  # stored in the order they happened
    assert repo.results[("main", "exit")] == result and repo.orders[result.order_id] == held


def cancel_of(order_id: str, key: str = "cancel-1") -> CancelOrder:
    return CancelOrder(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", source="risk-kill",
        sent_at=NOW, order_id=order_id,
    )  # fmt: skip


async def test_a_cancel_still_reaches_the_broker_when_the_database_is_down() -> None:
    """Task 10.1: the kill must be able to cancel an entry during an outage. A cancel only removes
    risk, so like an exit it does not wait for the idempotency lookup; its result is queued."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    assert resting.accepted and resting.order_id
    asked: list[str] = []

    async def cancel(order: Any) -> None:
        asked.append(order.order_id)

    adapter.cancel = cancel  # type: ignore[method-assign]
    repo.down = True  # the database fails just now: persistence_ok is not yet known to be false

    first = await service.cancel(cancel_of(resting.order_id))

    assert first.accepted and asked == [resting.order_id]
    assert ("main", "cancel-1") not in repo.results  # queued, not stored

    repo.down = False
    assert await service.drain_pending() is True
    assert repo.results[("main", "cancel-1")] == first  # stored once the database is back


async def test_a_cancel_does_not_spend_a_database_call_while_the_database_is_known_to_be_down() -> (
    None
):
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    assert resting.order_id
    service.persistence_ok = False  # an earlier write failed
    lookups: list[str] = []
    real = repo.find_result

    async def counting(account_id: str, key: str) -> Any:
        lookups.append(key)
        return await real(account_id, key)

    repo.find_result = counting  # type: ignore[method-assign]

    result = await service.cancel(cancel_of(resting.order_id))

    assert result.accepted and lookups == []


async def test_a_repeated_cancel_key_still_returns_the_first_answer_when_the_database_is_up() -> (
    None
):
    service, adapter, repo = build()
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    assert resting.order_id
    asked: list[str] = []

    async def cancel(order: Any) -> None:
        asked.append(order.order_id)

    adapter.cancel = cancel  # type: ignore[method-assign]
    first = await service.cancel(cancel_of(resting.order_id))

    second = await service.cancel(cancel_of(resting.order_id))

    assert second == first and asked == [resting.order_id]  # the broker was asked once


async def test_an_entry_in_the_same_state_is_rejected_with_the_persistence_reason() -> None:
    """Spec "Entry in the same state"."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    long_65(service)
    repo.down = True

    result = await service.place(command("entry", side=BUY, quantity=65))

    assert not result.accepted and "cannot be saved" in (result.reason or "")
    assert adapter.placed == [] and service.orders == {}


async def test_an_entry_is_refused_while_persistence_is_known_to_be_down_and_an_exit_is_not() -> (
    None
):
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    long_65(service)
    service.persistence_ok = False  # an earlier write already failed

    entry = await service.place(command("entry", side=BUY, quantity=65))
    exit_ = await service.place(command("exit", side=SELL, quantity=65))

    assert not entry.accepted and "refused until it recovers" in (entry.reason or "")
    assert exit_.accepted and len(adapter.placed) == 1


async def test_exits_queue_behind_earlier_writes_and_replay_in_order() -> None:
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    held = position(quantity=130)
    service.positions[position_id(held)] = held
    repo.down = True

    first = await service.place(command("e1", side=SELL, quantity=65))
    repo.down = False  # back up, but the first exit's writes are still queued
    second = await service.place(command("e2", side=SELL, quantity=65))

    assert first.accepted and second.accepted and len(adapter.placed) == 2
    assert repo.orders == {}  # the second did not jump ahead of the first
    assert await service.drain_pending() is True
    assert list(repo.orders) == [first.order_id, second.order_id]
    assert service.persistence_ok


async def test_a_repeat_of_an_exit_while_the_database_cannot_be_asked_places_nothing_new() -> None:
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    long_65(service)
    repo.down = True

    first = await service.place(command("exit", side=SELL, quantity=65))
    again = await service.place(command("exit", side=SELL, quantity=65))

    assert again.accepted and again.order_id == first.order_id
    assert len(adapter.placed) == 1  # one exit, not two (rule 5: idempotency)


async def test_an_exit_the_gate_refuses_while_the_database_is_down_is_still_a_final_rejection() -> (
    None
):
    class Refuse:
        async def check(self, order: Any) -> str:
            return "RISK_PRICE_SANITY: too far"

    repo = FailingRepository()
    service, adapter, _ = build(repository=repo, gate=Refuse())
    long_65(service)
    repo.down = True

    result = await service.place(command("exit", side=SELL, quantity=65))

    assert not result.accepted and result.order_id
    assert adapter.placed == []
    assert service.orders[result.order_id].state is OrderState.REJECTED
    repo.down = False
    await service.drain_pending()
    assert repo.orders[result.order_id].state is OrderState.REJECTED  # recorded once it is back


# ---- exits while reconciliation is pending -------------------------------------------------------


async def test_an_exit_is_accepted_during_a_recovery_halt_and_an_entry_is_not() -> None:
    service, adapter, repo = build()
    long_65(service)
    service.reconciled = False

    entry = await service.place(command("entry", side=BUY, quantity=65))
    exit_ = await service.place(command("exit", side=SELL, quantity=65))

    assert not entry.accepted and "not reconciled" in (entry.reason or "")
    assert exit_.accepted and len(adapter.placed) == 1
    assert repo.orders[exit_.order_id or ""].state is OrderState.OPEN  # stored normally


async def test_a_flip_is_an_entry_so_it_is_refused_in_a_degraded_state() -> None:
    service, adapter, _ = build()
    long_65(service)
    service.reconciled = False

    result = await service.place(command("flip", side=SELL, quantity=130))

    assert not result.accepted and "not reconciled" in (result.reason or "")
    assert adapter.placed == []


async def test_the_other_products_position_does_not_make_an_order_an_exit() -> None:
    from contracts.orders import ProductType

    service, adapter, _ = build()
    long_65(service)  # INTRADAY
    service.reconciled = False

    result = await service.place(
        command("margin", side=SELL, quantity=65, product=ProductType.MARGIN)
    )

    assert not result.accepted and adapter.placed == []


# ---- fill listeners ------------------------------------------------------------------------------


async def place_and_fill(
    service: OrderService, *, side: OrderSide = BUY, quantity: int = 65, key: str = "k1"
) -> None:
    result = await service.place(command(key, side=side, quantity=quantity))
    assert result.accepted
    broker_id = service.orders[result.order_id or ""].broker_order_id
    await service.on_update(
        Filled(
            broker_order_id=broker_id or "",
            fill_id=f"f-{key}",
            quantity=quantity,
            price=Decimal("100"),
            at=NOW,
            costs=CostBreakdown(),
        )  # fmt: skip
    )


async def test_a_listener_gets_each_fill_with_the_position_it_produced() -> None:
    service, *_ = build()
    seen: list[tuple[Fill, Position]] = []

    async def listener(fill: Fill, position: Position) -> None:
        seen.append((fill, position))
        assert service.positions[position_id(position)] == position  # already applied

    service.add_fill_listener(listener)

    await place_and_fill(service, side=BUY, quantity=65, key="open")
    await place_and_fill(service, side=SELL, quantity=65, key="close")

    assert [(f.fill_id, f.side, p.quantity) for f, p in seen] == [
        ("f-open", BUY, 65),
        ("f-close", SELL, 0),  # the position went flat: this is how the risk engine notices
    ]


async def test_listeners_run_in_registration_order() -> None:
    service, *_ = build()
    order: list[str] = []

    async def make(name: str) -> None:
        order.append(name)

    service.add_fill_listener(lambda fill, position: make("first"))
    service.add_fill_listener(lambda fill, position: make("second"))

    await place_and_fill(service)

    assert order == ["first", "second"]


async def test_a_failing_listener_does_not_undo_the_fill_or_stop_the_others() -> None:
    service, _, repo = build()
    reached: list[str] = []

    async def broken(fill: Fill, position: Position) -> None:
        raise RuntimeError("listener bug")

    async def after(fill: Fill, position: Position) -> None:
        reached.append(fill.fill_id)

    service.add_fill_listener(broken)
    service.add_fill_listener(after)

    await place_and_fill(service)

    assert reached == ["f-k1"]
    assert [f.fill_id for f in repo.fills] == ["f-k1"]  # the fill is stored regardless
    [applied] = service.positions.values()
    assert applied.quantity == 65


async def test_a_repeated_fill_does_not_call_the_listener_twice() -> None:
    service, *_ = build()
    calls: list[str] = []

    async def listener(fill: Fill, position: Position) -> None:
        calls.append(fill.fill_id)

    service.add_fill_listener(listener)
    await place_and_fill(service)
    order = next(iter(service.orders.values()))
    await service.on_update(
        Filled(
            broker_order_id=order.broker_order_id or "",
            fill_id="f-k1",
            quantity=65,
            price=Decimal("100"),
            at=NOW,
            costs=CostBreakdown(),
        )  # fmt: skip
    )

    assert calls == ["f-k1"]


@pytest.mark.parametrize("flag", ["persistence_ok", "reconciled"])
async def test_a_listener_still_runs_when_the_service_is_degraded(flag: str) -> None:
    """Fills keep applying (and so keep reaching the risk engine) in a degraded state."""
    service, *_ = build()
    seen: list[str] = []

    async def listener(fill: Fill, position: Position) -> None:
        seen.append(fill.fill_id)

    service.add_fill_listener(listener)
    result = await service.place(command("k1", side=BUY, quantity=65))
    setattr(service, flag, False)
    broker_id = service.orders[result.order_id or ""].broker_order_id or ""

    await service.on_update(
        Filled(
            broker_order_id=broker_id,
            fill_id="f1",
            quantity=65,
            price=Decimal("100"),
            at=NOW,
            costs=CostBreakdown(),
        )  # fmt: skip
    )

    assert seen == ["f1"]


# ---- review round 1: queued exits and the database coming back ---------------------------------


async def test_a_repeat_exit_after_the_database_returns_but_before_the_drain_places_nothing() -> (
    None
):
    """The first exit's create-write and result are still queued, so the database does not know
    its key yet. A repeat must be answered from memory, not placed again (rule 5), and the drain
    must still be able to store everything (a second create for the key would wedge it)."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    long_65(service)
    repo.down = True
    first = await service.place(command("exit", side=SELL, quantity=65))
    repo.down = False  # back up, nothing drained yet

    again = await service.place(command("exit", side=SELL, quantity=65))

    assert again.accepted and again.order_id == first.order_id
    assert len(adapter.placed) == 1
    assert await service.drain_pending() is True and service.persistence_ok
    assert list(repo.orders) == [first.order_id]


async def test_exits_do_not_ask_a_database_that_is_known_to_be_away() -> None:
    """A kill sends many exits: each must not cost a database timeout."""

    class Counting(FailingRepository):
        lookups = 0

        async def find_result(self, account_id: str, idempotency_key: str) -> Any:
            self.lookups += 1
            return await super().find_result(account_id, idempotency_key)

    repo = Counting()
    service, adapter, _ = build(repository=repo)
    held = position(quantity=195)
    service.positions[position_id(held)] = held
    repo.down = True

    for n in range(3):
        assert (await service.place(command(f"x{n}", side=SELL, quantity=65))).accepted

    assert repo.lookups == 1  # only the first, which discovered the outage
    assert len(adapter.placed) == 3


async def test_a_queued_create_that_had_already_committed_does_not_wedge_the_queue() -> None:
    """A timeout after COMMIT: the insert is stored but reported as failed. The replay hits a
    duplicate key, finds the stored result, and carries on."""

    class CommitThenError(MemoryRepository):
        failed_once = False

        async def create_order(self, order: Any, result: Any) -> None:
            await super().create_order(order, result)
            if not self.failed_once:
                self.failed_once = True
                raise PersistenceError("timed out after commit")

    repo = CommitThenError()
    service, adapter, _ = build(repository=repo)
    long_65(service)

    result = await service.place(command("exit", side=SELL, quantity=65))

    assert result.accepted and len(adapter.placed) == 1 and not service.persistence_ok
    assert await service.drain_pending() is True and service.persistence_ok
    assert states(repo) == ["Requested", "Submitted", "Open"]  # each state stored exactly once
    assert repo.results[("main", "exit")] == result


async def test_a_queued_create_that_really_failed_stays_at_the_head_and_is_retried() -> None:
    repo = FailingRepository()
    service, _, _ = build(repository=repo)
    long_65(service)
    repo.down = True
    await service.place(command("exit", side=SELL, quantity=65))

    assert await service.drain_pending() is False  # still down: nothing is dropped
    repo.down = False
    assert await service.drain_pending() is True
    assert len(repo.orders) == 1


async def test_a_queued_backlog_is_respected_even_if_the_flag_says_the_database_is_fine() -> None:
    """``persistence_ok`` goes false with the first queued write, so "writes are queued" normally
    implies "the flag is false", and the backlog checks are defence in depth. If a bug or a
    recovery step ever set the flag back while writes were still queued, a new exit must still
    queue behind them (the database must see each order's writes in order), and a repeat of an
    earlier key must still be answered from memory."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    held = position(quantity=130)
    service.positions[position_id(held)] = held
    repo.down = True
    first = await service.place(command("e1", side=SELL, quantity=65))
    repo.down = False
    service.persistence_ok = True  # the flag now disagrees with the queue

    # The repeat first: queueing the second exit would set the flag false again and hide it.
    repeat = await service.place(command("e1", side=SELL, quantity=65))
    second = await service.place(command("e2", side=SELL, quantity=65))

    assert repo.orders == {}  # the second did not jump ahead of the first
    assert repeat.order_id == first.order_id and len(adapter.placed) == 2  # and e1 was not resent
    assert await service.drain_pending() is True
    assert list(repo.orders) == [first.order_id, second.order_id]


async def test_an_entry_repeat_gets_its_stored_answer_while_persistence_is_flagged_down() -> None:
    """Entries are refused while degraded, but a repeat of a key that already has an answer gets
    that answer (rule 5), not a fresh refusal."""
    service, adapter, repo = build()
    first = await service.place(command("entry", side=BUY, quantity=65))
    assert first.accepted
    service.persistence_ok = False  # the flag only: the database itself still answers

    again = await service.place(command("entry", side=BUY, quantity=65))

    assert again == first and len(adapter.placed) == 1


async def test_a_queued_create_that_fails_while_reads_work_stays_at_the_head() -> None:
    """The write fails but the database answers reads (no stored result for the key): the create
    is not stored, so it must not be dropped from the queue."""

    class WritesFail(MemoryRepository):
        down = False

        async def create_order(self, order: Any, result: Any) -> None:
            if self.down:
                raise PersistenceError("write failed")
            await super().create_order(order, result)

    repo = WritesFail()
    service, _, _ = build(repository=repo)
    long_65(service)
    repo.down = True
    await service.place(command("exit", side=SELL, quantity=65))
    queued = len(service._pending)  # the create and the order's later state writes

    assert await service.drain_pending() is False
    assert len(service._pending) == queued  # the create is still at the head, nothing was dropped
    repo.down = False
    assert await service.drain_pending() is True
    assert states(repo) == ["Requested", "Submitted", "Open"]


async def test_a_foreign_result_for_the_key_keeps_the_create_at_the_head() -> None:
    class ForeignResult(MemoryRepository):
        async def create_order(self, order: Any, result: Any) -> None:
            raise PersistenceError("duplicate idempotency key")

        async def find_result(self, account_id: str, idempotency_key: str) -> Any:
            if not self.results:  # let the service's own first lookup find nothing
                return None
            return CommandResult(
                command_id="other", idempotency_key=idempotency_key, account_id=account_id,
                accepted=True, order_id="someone-elses", decided_at=NOW,
            )  # fmt: skip

    repo = ForeignResult()
    service, _, _ = build(repository=repo)
    long_65(service)
    service.persistence_ok = False
    await service.place(command("exit", side=SELL, quantity=65))
    repo.results[("main", "exit")] = CommandResult(  # now the database holds a foreign answer
        command_id="other", idempotency_key="exit", account_id="main", accepted=True,
        order_id="someone-elses", decided_at=NOW,
    )  # fmt: skip
    queued = len(service._pending)

    assert await service.drain_pending() is False
    assert len(service._pending) == queued  # the create was not called "stored"


async def test_a_validation_rejection_is_still_the_answer_to_a_repeat_while_flagged_down() -> None:
    """An entry refused by validation leaves no order in memory, only its stored answer. A
    repeat while the flag says degraded must get that answer, not the degraded refusal."""
    service, adapter, _ = build()
    first = await service.place(command("bad", side=BUY, quantity=70))  # not a whole lot
    assert not first.accepted and "lot" in (first.reason or "")
    service.persistence_ok = False

    again = await service.place(command("bad", side=BUY, quantity=70))

    assert again == first and adapter.placed == []


async def test_a_repeated_cancel_key_during_an_outage_gets_the_first_answer_from_memory() -> None:
    """Rule 5 without the database: the key lookup is skipped, so the service remembers what it
    answered. A repeat must not ask the broker again."""
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    assert resting.order_id
    asked: list[str] = []

    async def cancel(order: Any) -> None:
        asked.append(order.order_id)

    adapter.cancel = cancel  # type: ignore[method-assign]
    repo.down = True

    first = await service.cancel(cancel_of(resting.order_id))
    second = await service.cancel(cancel_of(resting.order_id))

    assert first.accepted and second == first and asked == [resting.order_id]


async def test_a_failed_key_lookup_marks_persistence_down_so_the_next_cancel_pays_no_timeout() -> (
    None
):
    repo = FailingRepository()
    service, adapter, _ = build(repository=repo)
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    other = await service.place(command("rest2", side=BUY, quantity=65, limit="91.00"))
    assert resting.order_id and other.order_id
    lookups: list[str] = []
    real = repo.find_result

    async def counting(account_id: str, key: str) -> Any:
        lookups.append(key)
        return await real(account_id, key)

    repo.find_result = counting  # type: ignore[method-assign]
    repo.down = True

    await service.cancel(cancel_of(resting.order_id, "c-1"))
    assert not service.persistence_ok  # the failed lookup said so
    await service.cancel(cancel_of(other.order_id, "c-2"))

    assert lookups == ["c-1"]  # only the first cancel paid for a lookup


async def test_the_cancel_answers_kept_for_an_outage_are_dropped_when_the_database_catches_up() -> (
    None
):
    repo = FailingRepository()
    service, _, _ = build(repository=repo)
    resting = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    assert resting.order_id
    repo.down = True
    await service.cancel(cancel_of(resting.order_id))
    assert service._cancel_answers  # type: ignore[attr-defined]

    repo.down = False
    assert await service.drain_pending() is True

    assert service._cancel_answers == {}  # type: ignore[attr-defined]
    assert repo.results[("main", "cancel-1")].accepted  # the answer reached the database


async def test_a_failed_key_lookup_alone_marks_persistence_down() -> None:
    """Even if writes still work, a lookup that failed says the database is not answering: the
    next cancel must not pay for another one."""
    repo = FailingRepository()
    service, _, _ = build(repository=repo)
    first = await service.place(command("rest", side=BUY, quantity=65, limit="90.00"))
    second = await service.place(command("rest2", side=BUY, quantity=65, limit="91.00"))
    assert first.order_id and second.order_id
    lookups: list[str] = []

    async def failing_lookup(account_id: str, key: str) -> Any:
        lookups.append(key)
        raise PersistenceError("reads are timing out")

    repo.find_result = failing_lookup  # type: ignore[method-assign]  # writes still work

    await service.cancel(cancel_of(first.order_id, "c-1"))
    assert not service.persistence_ok
    await service.cancel(cancel_of(second.order_id, "c-2"))

    assert lookups == ["c-1"]
