"""The order service and an adapter that makes calls wait for a turn (the rate limiter).

The point (add-risk-engine 8.1, review): the service gives the broker ``ack_timeout_s`` to answer
a call, but an exit or cancel may legitimately wait a long time for a token. If that wait were
inside the timeout, a busy minute would turn an exit into an ``Unknown`` order that is never
sent. So the service takes the turn first (``admit_*``) and starts the timeout only for the call
itself (``send_*``).
"""

import asyncio
from collections.abc import AsyncIterator

from contracts.commands import CancelOrder
from contracts.orders import Order, OrderState
from engine.broker.adapter import BrokerAck, BrokerOrder, BrokerUpdate, RateLimited
from engine.risk.ratelimit import RATE_LIMITED
from tests.engine.test_order_service import NOW, build, command

WAIT_S = 0.2  # a wait for a turn that is much longer than the acknowledgement timeout ...
TIMEOUT_S = 0.05  # ... which is this


class AdmittingAdapter:
    """An adapter shaped like the rate limiter: a turn to take, then the call."""

    def __init__(self, *, admit_wait_s: float = 0.0, send_wait_s: float = 0.0) -> None:
        self.admit_wait_s = admit_wait_s
        self.send_wait_s = send_wait_s
        self.refuse_entries = False
        self.placed: list[Order] = []
        self.cancelled: list[Order] = []
        self.log: list[tuple[str, str]] = []  # (step, order id), in the order they happened

    async def admit_place(self, order: Order) -> None:
        self.log.append(("admit_place", order.order_id))
        if self.refuse_entries:
            raise RateLimited(f"{RATE_LIMITED}: too many orders just now")  # as the real limiter
        await asyncio.sleep(self.admit_wait_s)

    async def admit_cancel(self, order: Order) -> None:
        self.log.append(("admit_cancel", order.order_id))
        await asyncio.sleep(self.admit_wait_s)

    async def send_place(self, order: Order) -> BrokerAck:
        self.log.append(("send_place", order.order_id))
        await asyncio.sleep(self.send_wait_s)
        self.placed.append(order)
        return BrokerAck(f"b-{len(self.placed)}")

    async def send_cancel(self, order: Order) -> None:
        self.log.append(("send_cancel", order.order_id))
        await asyncio.sleep(self.send_wait_s)
        self.cancelled.append(order)

    async def place(self, order: Order) -> BrokerAck:  # the plain interface, for other callers
        await self.admit_place(order)
        return await self.send_place(order)

    async def cancel(self, order: Order) -> None:
        await self.admit_cancel(order)
        await self.send_cancel(order)

    async def order_book(self) -> list[BrokerOrder]:
        return []

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        return
        yield


async def test_a_long_wait_for_a_turn_does_not_make_the_order_unknown() -> None:
    adapter = AdmittingAdapter(admit_wait_s=WAIT_S)
    service, _, repo = build(adapter=adapter, timeout_s=TIMEOUT_S)

    result = await service.place(command("k1", limit="100.00"))

    assert result.accepted and result.order_id
    assert repo.orders[result.order_id].state is OrderState.OPEN  # not UNKNOWN
    assert len(adapter.placed) == 1  # and it was sent


async def test_a_broker_that_does_not_answer_after_the_turn_is_still_unknown() -> None:
    """The timeout is only moved, not removed: once the call is made, silence still means Unknown
    (and the order is never resent, safety rule 4)."""
    adapter = AdmittingAdapter(send_wait_s=WAIT_S)
    service, _, repo = build(adapter=adapter, timeout_s=TIMEOUT_S)

    result = await service.place(command("k1", limit="100.00"))

    assert result.order_id and repo.orders[result.order_id].state is OrderState.UNKNOWN


async def test_an_entry_with_no_token_is_rejected_with_the_limiters_code() -> None:
    adapter = AdmittingAdapter()
    adapter.refuse_entries = True
    service, _, repo = build(adapter=adapter, timeout_s=TIMEOUT_S)

    result = await service.place(command("k1", limit="100.00"))

    assert result.order_id  # the order exists, as a final Rejected order
    stored = repo.orders[result.order_id]
    assert stored.state is OrderState.REJECTED and RATE_LIMITED in (stored.reason or "")
    assert adapter.placed == []  # and nothing was sent


async def test_a_long_wait_for_a_turn_does_not_fail_a_cancel() -> None:
    adapter = AdmittingAdapter()
    service, _, _ = build(adapter=adapter, timeout_s=TIMEOUT_S)
    placed = await service.place(command("k1", limit="100.00"))
    assert placed.order_id
    adapter.admit_wait_s = WAIT_S

    result = await service.cancel(
        CancelOrder(
            command_id="c1", idempotency_key="cancel-1", account_id="main", source="cli",
            sent_at=NOW, order_id=placed.order_id,
        )
    )  # fmt: skip

    assert result.accepted, result.reason
    assert len(adapter.cancelled) == 1


async def test_an_adapter_without_turns_works_as_before() -> None:
    """The plain interface (the paper broker, a future Dhan adapter) is unchanged."""
    service, adapter, repo = build(timeout_s=TIMEOUT_S)

    result = await service.place(command("k1", limit="100.00"))

    assert result.order_id and repo.orders[result.order_id].state is OrderState.OPEN
    assert len(adapter.placed) == 1


async def test_every_place_and_every_cancel_takes_a_turn_before_it_is_sent() -> None:
    """No order path bypasses the limiter: one admit per place and per cancel, before the send."""
    adapter = AdmittingAdapter()
    service, _, _ = build(adapter=adapter, timeout_s=TIMEOUT_S)
    placed = await service.place(command("k1", limit="100.00"))
    assert placed.order_id

    await service.cancel(
        CancelOrder(
            command_id="c1", idempotency_key="cancel-1", account_id="main", source="cli",
            sent_at=NOW, order_id=placed.order_id,
        )
    )  # fmt: skip

    order_id = placed.order_id
    assert adapter.log == [
        ("admit_place", order_id), ("send_place", order_id),
        ("admit_cancel", order_id), ("send_cancel", order_id),
    ]  # fmt: skip


async def test_the_real_limiter_behind_the_service_refuses_an_entry_over_its_share() -> None:
    """The real ``RateLimitedAdapter`` wired in as the engine wires it: with 3 tokens a second and
    one reserved, the third entry in a second is a final Rejected order, and a cancel then takes
    a (reserved) token."""
    from engine.risk.ratelimit import RateLimitedAdapter

    class Inner(AdmittingAdapter):
        pass

    inner = Inner()
    # As engine.main builds it: the limiter reads ``service.positions`` through a closure over the
    # service, which is built after it.
    limiter = RateLimitedAdapter(
        inner, per_s=3, per_min=100, exit_share=0.4, positions=lambda: service.positions,
        clock=lambda: 1000.0,
    )  # fmt: skip
    service, _, repo = build(adapter=limiter, timeout_s=TIMEOUT_S)

    results = [await service.place(command(f"k{n}", limit="100.00")) for n in range(3)]

    states = [repo.orders[r.order_id or ""].state for r in results]
    assert states == [OrderState.OPEN, OrderState.OPEN, OrderState.REJECTED]
    assert RATE_LIMITED in (repo.orders[results[2].order_id or ""].reason or "")
    cancel = await service.cancel(
        CancelOrder(
            command_id="c1", idempotency_key="cancel-1", account_id="main", source="cli",
            sent_at=NOW, order_id=results[0].order_id or "",
        )
    )  # fmt: skip
    assert cancel.accepted  # the reserved token is there for it
