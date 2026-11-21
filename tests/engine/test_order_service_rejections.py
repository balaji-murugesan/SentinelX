"""The order service's rejection listener (add-risk-guards task 6.1; design §4; order-flow-guards
spec "Only rejections by the broker count").

The listener fires for the two ways the broker refuses an order the engine sent: the adapter
raising ``BrokerRejected`` at placement, and a broker ``Rejected`` update. Entries and exits alike.
It does not fire for a gate refusal, for the engine's own rate limiter (``RateLimited``), or for
the orders restart recovery marks rejected (``settle_in_flight``, ``reconcile_unknown``). The
order's own handling is the same either way: the existing rejection tests cover that, and the
tests here also check the order still ends ``Rejected`` with its reason.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from contracts.orders import Order, OrderSide, OrderState, Position, ProductType
from engine.broker.adapter import BrokerOrder, BrokerRejected, RateLimited, Rejected
from engine.orders.service import OrderService
from engine.persistence.repository import LoadedState
from engine.positions.book import position_id
from engine.risk.ratelimit import RATE_LIMITED, RateLimitedAdapter
from tests.engine.test_order_service import NOW, OPTION, FakeAdapter, build, command
from tests.engine.test_order_service_admission import AdmittingAdapter
from tests.engine.test_order_updates import OrderBook

SELL = OrderSide.SELL


class Seen:
    """A rejection listener that records the orders it was given."""

    def __init__(self) -> None:
        self.orders: list[Order] = []

    def __call__(self, order: Order) -> None:
        self.orders.append(order)


def listening(service: OrderService) -> Seen:
    seen = Seen()
    service.add_rejection_listener(seen)
    return seen


class RefusingGate:
    async def check(self, order: Order) -> str | None:
        return "RISK_TEST: refused by the gate"


# ---- the two broker paths --------------------------------------------------------------------


async def test_the_listener_fires_when_the_broker_refuses_at_placement() -> None:
    service, adapter, repo = build(adapter=FakeAdapter(refuse="insufficient margin"))
    seen = listening(service)

    result = await service.place(command())

    stored = repo.orders[result.order_id or ""]
    assert stored.state is OrderState.REJECTED and stored.reason == "insufficient margin"
    assert [o.order_id for o in seen.orders] == [stored.order_id]
    assert seen.orders[0].state is OrderState.REJECTED  # it sees the order already rejected
    assert seen.orders[0].updated_at == NOW  # stamped with the engine's clock
    assert len(adapter.placed) == 1  # and never resent (safety rule 4)


async def awaiting_update(service: OrderService) -> Order:
    """An ``Unknown`` order carrying a broker id: a state the engine never produces today (an
    ``Unknown`` order gets its broker id only by reconciling, which moves it to ``Open``). It is
    built by hand because the state machine lets a ``Rejected`` update apply only to a
    ``Submitted`` or ``Unknown`` order, and an acknowledged order is ``Open``. So the tests that use
    it check only the call site in ``_apply_update``, not a path production can reach: see
    ``test_an_acknowledged_order_rejected_by_the_broker_counts`` (xfail) for the real one."""
    order_id = (await service.place(command())).order_id or ""
    assert service.orders[order_id].state is OrderState.UNKNOWN
    order = service.orders[order_id].model_copy(update={"broker_order_id": "b-9"})
    service.orders[order_id] = order
    return order


async def test_the_listener_fires_when_a_broker_rejected_update_is_applied() -> None:
    service, _, _ = build(adapter=FakeAdapter(hang=True), timeout_s=0.05)
    order = await awaiting_update(service)
    seen = listening(service)

    await service.on_update(Rejected("b-9", "price band", NOW))

    rejected = service.orders[order.order_id]
    assert rejected.state is OrderState.REJECTED and rejected.reason == "price band"
    assert seen.orders == [rejected]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="no Open -> Rejected transition yet: a broker rejection after the acknowledgement is "
    "not applied. Fixed by the separate change in add-risk-guards tasks.md, Workflow follow-up.",
)
async def test_an_acknowledged_order_rejected_by_the_broker_counts() -> None:
    """The production path: the broker acknowledges the order (``Open``) and its risk system then
    rejects it on the update stream. ``strict``: once the transition exists this passes, and the
    xfail marker must be removed (an unexpected pass fails the run)."""
    service, _, _ = build()
    seen = listening(service)
    order_id = (await service.place(command())).order_id or ""
    order = service.orders[order_id]
    assert order.state is OrderState.OPEN and order.broker_order_id == "b-1"

    await service.on_update(Rejected("b-1", "RMS: insufficient margin", NOW))

    rejected = service.orders[order_id]
    assert rejected.state is OrderState.REJECTED
    assert seen.orders == [rejected]


async def test_a_rejected_exit_counts_too() -> None:
    """Spec "Rejected exit counts": the broker refuses an exit, the listener hears of it."""
    service, _, _ = build(adapter=FakeAdapter(refuse="exchange closed"))
    held = Position(
        account_id="main", key=OPTION, product=ProductType.INTRADAY, quantity=65,
        average_price=Decimal("100"), updated_at=NOW,
    )  # fmt: skip
    service.positions[position_id(held)] = held
    seen = listening(service)

    result = await service.place(command(side=SELL))

    assert result.accepted  # the exit was accepted by the engine and sent; the broker refused it
    assert [o.order_id for o in seen.orders] == [result.order_id]


async def test_an_update_for_an_order_already_final_reports_nothing() -> None:
    """A ``Rejected`` update that cannot apply (spec "Update after a final state") changes nothing,
    so it is not a rejection to count."""
    service, _, _ = build(adapter=FakeAdapter(refuse="no margin"))
    seen = listening(service)
    result = await service.place(command())
    assert len(seen.orders) == 1
    order = service.orders[result.order_id or ""]
    # The broker's own echo of the rejection, for an order with a broker id, after it is final.
    service.orders[order.order_id] = order.model_copy(update={"broker_order_id": "b-1"})

    await service.on_update(Rejected("b-1", "no margin", NOW))

    assert len(seen.orders) == 1


async def test_every_listener_is_called_and_a_failing_one_changes_nothing() -> None:
    service, _, repo = build(adapter=FakeAdapter(refuse="no margin"))

    def broken(order: Order) -> None:
        raise RuntimeError("listener bug")

    service.add_rejection_listener(broken)
    seen = listening(service)

    result = await service.place(command())

    assert repo.orders[result.order_id or ""].state is OrderState.REJECTED
    assert len(seen.orders) == 1  # the one registered after the broken one still heard


# ---- what is not a broker rejection ----------------------------------------------------------


async def test_a_gate_refusal_reports_nothing() -> None:
    """Spec "Gate refusals do not count": five refused entries, none reported."""
    service, adapter, repo = build(gate=RefusingGate())
    seen = listening(service)

    results = [await service.place(command(f"k{n}")) for n in range(5)]

    assert all(repo.orders[r.order_id or ""].state is OrderState.REJECTED for r in results)
    assert seen.orders == [] and adapter.placed == []


async def test_the_rate_limiter_raises_rate_limited_which_is_still_a_broker_rejected() -> None:
    """Every ``except BrokerRejected`` still catches the limiter's refusal; only the type is new."""
    limiter = RateLimitedAdapter(
        AdmittingAdapter(), per_s=2, per_min=100, exit_share=0.4, positions=dict,
        clock=lambda: 1000.0,
    )  # fmt: skip
    service, _, _ = build()
    order = service._new_order(command(limit="100.00"))  # type: ignore[attr-defined]
    await limiter.admit_place(order)  # 2 a second, 1 reserved: the first entry takes the other

    with pytest.raises(RateLimited, match=RATE_LIMITED) as info:
        await limiter.admit_place(order)
    assert isinstance(info.value, BrokerRejected)


async def test_a_rate_limited_entry_is_rejected_as_before_and_reports_nothing() -> None:
    """Spec "Rate-limited entry does not count", with the real limiter behind the service: the
    third entry in a second is refused with ``RISK_RATE_LIMITED`` and is not reported."""
    inner = AdmittingAdapter()
    limiter = RateLimitedAdapter(
        inner, per_s=3, per_min=100, exit_share=0.4, positions=lambda: service.positions,
        clock=lambda: 1000.0,
    )  # fmt: skip
    service, _, repo = build(adapter=limiter, timeout_s=0.05)
    seen = listening(service)

    results = [await service.place(command(f"k{n}", limit="100.00")) for n in range(3)]

    third = repo.orders[results[2].order_id or ""]
    assert third.state is OrderState.REJECTED and RATE_LIMITED in (third.reason or "")
    assert seen.orders == [] and len(inner.placed) == 2


async def test_a_rate_limited_refusal_from_any_admitting_adapter_reports_nothing() -> None:
    adapter = AdmittingAdapter()
    adapter.refuse_entries = True
    service, _, repo = build(adapter=adapter, timeout_s=0.05)
    seen = listening(service)

    result = await service.place(command(limit="100.00"))

    assert repo.orders[result.order_id or ""].state is OrderState.REJECTED
    assert seen.orders == []


async def test_orders_settled_after_a_restart_report_nothing() -> None:
    """``settle_in_flight``: a stored ``Requested`` order becomes ``Rejected`` (it was never sent),
    a ``Submitted`` one ``Unknown``. Neither is the broker refusing anything."""
    service, _, _ = build()
    placed = service.orders[(await service.place(command())).order_id or ""]
    requested = placed.model_copy(
        update={"order_id": "r1", "state": OrderState.REQUESTED, "broker_order_id": None}
    )
    submitted = placed.model_copy(
        update={"order_id": "s1", "state": OrderState.SUBMITTED, "broker_order_id": None}
    )
    await service.restore(LoadedState(open_orders=[requested, submitted], fills=[], positions=[]))
    seen = listening(service)

    await service.settle_in_flight()

    assert service.orders["r1"].state is OrderState.REJECTED
    assert service.orders["s1"].state is OrderState.UNKNOWN
    assert seen.orders == []


@pytest.mark.parametrize(
    ("book_state", "reason"),
    [
        (None, "broker has no such order"),
        (OrderState.REJECTED, "broker reports Rejected"),
        (OrderState.CANCELLED, "broker reports Cancelled"),
    ],
)
async def test_unknown_orders_resolved_as_rejected_by_reconciliation_report_nothing(
    book_state: OrderState | None, reason: str
) -> None:
    adapter = OrderBook([])
    service, _, _ = build(adapter=adapter, timeout_s=0.05)
    order_id = (await service.place(command())).order_id or ""
    assert service.orders[order_id].state is OrderState.UNKNOWN
    if book_state is not None:
        adapter.entries = [BrokerOrder("b-9", order_id, book_state, 0, None)]
    seen = listening(service)

    await service.reconcile_unknown()

    order = service.orders[order_id]
    assert order.state is OrderState.REJECTED and order.reason == reason
    assert seen.orders == []


async def test_the_listener_is_called_inside_the_service_lock() -> None:
    """It runs while the lock is held, so it sees a consistent book (design §4)."""
    adapter = FakeAdapter(hang=True)
    service, _, _ = build(adapter=adapter, timeout_s=0.05)
    await awaiting_update(service)
    held: list[bool] = []
    service.add_rejection_listener(lambda order: held.append(service._lock.locked()))  # type: ignore[attr-defined]

    await service.on_update(Rejected("b-9", "price band", NOW + timedelta(seconds=1)))
    adapter._hang, adapter._refuse = False, "no margin"  # type: ignore[attr-defined]
    await service.place(command("k2"))

    assert held == [True, True]  # the update path, then the placement path
