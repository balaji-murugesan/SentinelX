"""OrderService broker updates, reconciliation, cancel and expiry (task 6.2).

Scenarios: orders "Submission times out" (with reconciliation), "Cancel a resting limit order",
"Limit order never reached", "Update after a final state"; positions "Fills update positions
exactly once" and "Open positions stay subscribed" (through the service).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from opentelemetry.sdk.trace import TracerProvider

from contracts.commands import CancelOrder
from contracts.mode import TradingMode
from contracts.orders import CostBreakdown, OrderSide, OrderState
from engine.broker.adapter import BrokerOrder, Cancelled, Filled
from engine.broker.paper import PaperBroker
from engine.market_data.bus import TickBus
from engine.market_data.provider import Subscriptions, Tick
from engine.market_data.simulated import SimulatedFeed
from engine.orders.gate import PaperOnlyGate
from engine.orders.service import OrderService
from engine.positions.feed import PositionFeed
from tests.engine.test_order_service import NOW, OPTION, FakeAdapter, build, command, master
from tests.engine.trading_fakes import MemoryRepository

CLOSE = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)  # 15:30 IST


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Rig(NamedTuple):
    service: OrderService
    broker: PaperBroker
    repo: MemoryRepository
    clock: Clock
    subscriptions: Subscriptions


def paper_rig() -> Rig:
    clock = Clock()
    subscriptions = Subscriptions(10)
    broker = PaperBroker(
        TickBus(), master().get, slippage_ticks=1, max_price_age=timedelta(seconds=5), now=clock
    )
    repo = MemoryRepository()
    service = OrderService(
        account_id="main", paper=True, gate=PaperOnlyGate(TradingMode.PAPER), adapter=broker,
        repository=repo, instruments=master(), ack_timeout_s=5, now=clock,
        position_feed=PositionFeed(SimulatedFeed(TickBus(), subscriptions, script=[])),
        tracer=TracerProvider().get_tracer("test"),
    )  # fmt: skip
    broker.on_tick(Tick(OPTION, Decimal("100"), exchange_time=NOW, received_at=NOW))
    return Rig(service, broker, repo, clock, subscriptions)


async def pump(rig: Rig, count: int = 1) -> None:
    """Deliver the next ``count`` broker updates to the service, like run_updates would."""
    updates = rig.broker.updates()
    for _ in range(count):
        await rig.service.on_update(await anext(updates))


def cancel(order_id: str, key: str = "x1") -> CancelOrder:
    return CancelOrder(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", source="cli",
        sent_at=NOW, order_id=order_id,
    )  # fmt: skip


async def test_fill_updates_order_position_and_subscription_once() -> None:
    rig = paper_rig()
    result = await rig.service.place(command())
    order_id = result.order_id or ""

    await pump(rig)

    order = rig.service.orders[order_id]
    assert order.state is OrderState.FILLED and order.filled_quantity == 65
    assert order.average_fill_price == Decimal("100.05")  # one tick of slippage on a buy
    position = next(iter(rig.service.positions.values()))
    assert position.quantity == 65 and position.average_price == Decimal("100.05")
    assert position.costs > 0  # the fill's costs reach the position
    assert len(rig.repo.fills) == 1 and rig.repo.orders[order_id].state is OrderState.FILLED
    assert OPTION in rig.subscriptions  # open position stays on the feed


async def test_a_repeated_fill_changes_nothing() -> None:
    rig = paper_rig()
    await rig.service.place(command())
    updates = rig.broker.updates()
    fill = await anext(updates)

    await rig.service.on_update(fill)
    before = dict(rig.service.positions)
    await rig.service.on_update(fill)  # redelivered

    assert rig.service.positions == before and len(rig.repo.fills) == 1


async def test_closing_fill_releases_the_subscription() -> None:
    rig = paper_rig()
    await rig.service.place(command("k1"))
    await pump(rig)
    await rig.service.place(command("k2", side=OrderSide.SELL))
    await pump(rig)

    position = next(iter(rig.service.positions.values()))
    assert position.quantity == 0
    assert OPTION not in rig.subscriptions


async def test_fill_after_a_final_state_changes_no_position() -> None:
    rig = paper_rig()
    result = await rig.service.place(command(limit="90"))  # resting: price is 100
    order = rig.service.orders[result.order_id or ""]
    assert order.state is OrderState.OPEN
    await rig.service.cancel(cancel(order.order_id))
    await pump(rig)  # the broker's Cancelled confirmation
    assert rig.service.orders[order.order_id].state is OrderState.CANCELLED

    late = Filled(order.broker_order_id or "", "late-1", 65, Decimal("90"), NOW, CostBreakdown())
    await rig.service.on_update(late)

    assert rig.service.orders[order.order_id].state is OrderState.CANCELLED
    assert rig.service.positions == {} and rig.repo.fills == []
    assert [f.fill_id for f in rig.service.unapplied_fills] == ["late-1"]


async def test_cancel_a_resting_limit_order() -> None:
    rig = paper_rig()
    result = await rig.service.place(command(limit="90"))
    order_id = result.order_id or ""

    answer = await rig.service.cancel(cancel(order_id))
    assert answer.accepted
    await pump(rig)

    assert rig.service.orders[order_id].state is OrderState.CANCELLED
    assert rig.repo.orders[order_id].state is OrderState.CANCELLED
    rig.broker.on_tick(Tick(OPTION, Decimal("80"), exchange_time=NOW, received_at=NOW))
    assert rig.service.positions == {}  # trading through the old limit fills nothing


async def test_cancel_refuses_unknown_and_final_orders() -> None:
    rig = paper_rig()
    nothing = await rig.service.cancel(cancel("nope", "x1"))
    assert not nothing.accepted and "unknown order" in (nothing.reason or "")

    result = await rig.service.place(command())  # market: fills at once
    await pump(rig)
    final = await rig.service.cancel(cancel(result.order_id or "", "x2"))
    assert not final.accepted and "Filled" in (final.reason or "")


async def test_limit_order_never_reached_expires_at_close() -> None:
    rig = paper_rig()
    result = await rig.service.place(command(limit="90"))
    order_id = result.order_id or ""

    rig.clock.now = CLOSE - timedelta(minutes=1)
    assert await rig.service.expire_at_close() == 0  # market still open
    assert rig.service.orders[order_id].state is OrderState.OPEN

    rig.clock.now = CLOSE
    assert await rig.service.expire_at_close() == 1

    order = rig.service.orders[order_id]
    assert order.state is OrderState.CANCELLED and order.reason == "expired at close"
    assert rig.repo.orders[order_id].reason == "expired at close"
    await pump(rig)  # the broker's own Cancelled confirmation is a harmless repeat
    assert rig.service.orders[order_id].reason == "expired at close"


class OrderBook(FakeAdapter):
    """Never acks the placement (so it goes Unknown) but has a configurable order book."""

    def __init__(self, entries: list[BrokerOrder]) -> None:
        super().__init__(hang=True)
        self.entries = entries

    async def order_book(self) -> list[BrokerOrder]:
        return self.entries


async def unknown_order() -> tuple[OrderService, str, OrderBook]:
    adapter = OrderBook([])
    service, _, _ = build(adapter=adapter, timeout_s=0.05)
    result = await service.place(command())
    order_id = result.order_id or ""
    assert service.orders[order_id].state is OrderState.UNKNOWN
    return service, order_id, adapter


async def test_unknown_order_found_at_broker_becomes_open_and_its_fill_applies() -> None:
    service, order_id, adapter = await unknown_order()
    adapter.entries = [BrokerOrder("b-9", order_id, OrderState.OPEN, 0, None)]
    # The fill arrives before reconciliation: it cannot be matched yet, so it is held.
    await service.on_update(Filled("b-9", "f1", 65, Decimal("100"), NOW, CostBreakdown()))
    assert service.positions == {}

    await service.reconcile_unknown()

    order = service.orders[order_id]
    assert order.state is OrderState.FILLED and order.broker_order_id == "b-9"
    assert len(adapter.placed) == 1  # still exactly one submission
    assert next(iter(service.positions.values())).quantity == 65


async def test_unknown_order_missing_at_broker_is_rejected() -> None:
    service, order_id, adapter = await unknown_order()

    await service.reconcile_unknown()

    order = service.orders[order_id]
    assert order.state is OrderState.REJECTED and order.reason == "broker has no such order"
    assert len(adapter.placed) == 1  # never resubmitted


async def test_broker_cancelled_unknown_order_is_recorded_rejected() -> None:
    service, order_id, adapter = await unknown_order()
    adapter.entries = [BrokerOrder("b-9", order_id, OrderState.CANCELLED, 0, None)]

    await service.reconcile_unknown()

    order = service.orders[order_id]
    assert order.state is OrderState.REJECTED and order.reason == "broker reports Cancelled"


async def test_cancelled_update_after_cancel_confirms_order() -> None:
    rig = paper_rig()
    result = await rig.service.place(command(limit="90"))
    order = rig.service.orders[result.order_id or ""]
    await rig.service.on_update(Cancelled(order.broker_order_id or "", "cancelled", NOW))
    assert rig.service.orders[order.order_id].state is OrderState.CANCELLED
