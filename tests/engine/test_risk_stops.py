"""Catastrophic stops (add-risk-engine tasks 9.2 and 9.3; catastrophic-stop spec).

A real ``OrderService`` with a broker double that records every place and cancel in order, so the
tests can see *when* things happen (place the new stop BEFORE cancelling the old). ``Filled`` and
``Cancelled`` updates are fed to the service by hand, like the broker's update stream.

Scenarios: "Stop after entry", "Short position", "Scale in", "Position closed by an exit", "Stop on
a halted day", "Broker rejects the stop", "Restart with an unprotected position", "Carried CNC
holding at the open"; no stop after 15:30; never two stops at once.

A stop rejected after acceptance (handle-late-broker-rejections task 3.1): "Stop rejected once",
"Broker keeps rejecting the stop", "A fill resets the delay", "Rejected stop on a flat book". Each
late rejection goes through a real acknowledgement, then a ``Rejected`` update on the stream.
"""

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from contracts.commands import PlaceOrder, SetRiskLimits
from contracts.orders import CostBreakdown, Order, OrderSide, OrderState, OrderType, ProductType
from contracts.snapshot import HaltLevel
from engine.broker.adapter import (
    BrokerAck,
    BrokerError,
    BrokerOrder,
    BrokerRejected,
    BrokerUpdate,
    Cancelled,
    Filled,
    Rejected,
)
from engine.orders.intent import Intent, classify
from engine.positions.book import PositionId, position_id
from engine.risk.checks.protection import UNPROTECTED, Unprotected
from engine.risk.decision import RiskTrigger, Severity
from engine.risk.limits_service import LimitsService
from engine.risk.stops import SOURCE, CatastrophicStops, stop_trigger
from tests.engine.risk_builders import NOW, OPTION, STOCK, context, instrument_of, position
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build
from tests.engine.test_risk_end_to_end import Engine
from tests.engine.test_risk_limits import BASE
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
OPEN_AT = datetime(2026, 11, 16, 3, 45, tzinfo=UTC)  # 09:15 IST on the Monday of NOW
BEFORE_OPEN = datetime(2026, 11, 16, 3, 30, tzinfo=UTC)  # 09:00 IST
AFTER_CLOSE = datetime(2026, 11, 16, 10, 5, tzinfo=UTC)  # 15:35 IST
PCT20 = {**BASE, "catastrophic_sl_pct": "20"}


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class StopAdapter:
    """Acknowledges every order and records, in order, what was placed and cancelled."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []  # ("place" | "cancel", engine order id)
        self.placed: list[Order] = []
        self.refuse_stops = False

    async def place(self, order: Order) -> BrokerAck:
        if self.refuse_stops and order.order_type is OrderType.STOP_MARKET:
            raise BrokerRejected("stops are not accepted today")
        self.events.append(("place", order.order_id))
        self.placed.append(order)
        return BrokerAck(f"b-{len(self.placed)}")

    async def cancel(self, order: Order) -> None:
        self.events.append(("cancel", order.order_id))

    async def order_book(self) -> list[BrokerOrder]:
        return []

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        return
        yield


class World:
    def __init__(self, at: datetime = NOW, limits: dict | None = None) -> None:  # type: ignore[type-arg]
        self.clock = Clock(at)
        self.adapter = StopAdapter()
        self.service, _, self.repo = build(adapter=self.adapter, now=self.clock)
        risk = MemoryRiskRepository()
        self.limits = LimitsService(
            account_id="main", repository=risk, results=MemoryRepository(),
            figures=lambda: (D(0), None), now=self.clock,
        )  # fmt: skip
        self.limit_values = limits or PCT20
        self.stops = CatastrophicStops(
            account_id="main", service=self.service, limits=self.limits,
            instruments=instrument_of, now=self.clock, sweep_every_s=5,
        )  # fmt: skip
        self.service.add_fill_listener(self.stops.on_fill)
        self.service.add_rejection_listener(self.stops.on_rejected)

    async def ready(self) -> "World":
        await self.limits.set_limits(
            SetRiskLimits(
                command_id="c", idempotency_key="k", account_id="main", source="t",
                sent_at=NOW, limits=self.limit_values,
            )
        )  # fmt: skip
        return self

    def hold(self, key, quantity: int, average: str = "200.00", product=ProductType.INTRADAY):  # type: ignore[no-untyped-def]
        held = position(key, quantity, average, product)
        self.service.positions[position_id(held)] = held
        return position_id(held)

    async def fill(self, side: OrderSide, quantity: int, price: str = "200.00", key=OPTION) -> None:  # type: ignore[no-untyped-def]
        """An entry or exit order placed and then filled, as the broker would report it."""
        n = len(self.service.orders) + 1
        result = await self.service.place(
            PlaceOrder(
                command_id=f"e{n}", idempotency_key=f"e{n}", account_id="main", source="cli",
                sent_at=NOW, key=key, side=side, quantity=quantity,
                product=ProductType.INTRADAY, order_type=OrderType.LIMIT, limit_price=D(price),
            )
        )  # fmt: skip
        assert result.accepted, result.reason
        broker_id = self.service.orders[result.order_id or ""].broker_order_id or ""
        await self.service.on_update(
            Filled(
                broker_order_id=broker_id, fill_id=f"f{n}", quantity=quantity, price=D(price),
                at=self.clock.at, costs=CostBreakdown(),
            )
        )  # fmt: skip

    async def settle(self) -> None:
        await self.stops.process_queue()
        await self.confirm_cancels()

    async def confirm_cancels(self) -> None:
        """The broker confirms every cancel it was asked for (the update stream)."""
        for kind, order_id in self.adapter.events:
            order = self.service.orders.get(order_id)
            if kind == "cancel" and order is not None and order.state is OrderState.OPEN:
                await self.service.on_update(
                    Cancelled(order.broker_order_id or "", "cancelled", self.clock.at)
                )

    def stop_orders(self, *states: OrderState) -> list[Order]:
        wanted = states or (OrderState.OPEN,)
        return [o for o in self.service.orders.values() if o.source == SOURCE and o.state in wanted]

    def stops_sent(self) -> int:
        """How many stops have reached the broker so far."""
        return sum(o.order_type is OrderType.STOP_MARKET for o in self.adapter.placed)

    async def reject_stop(self) -> Order:
        """The broker rejects the resting (acknowledged) stop on its update stream."""
        [stop] = self.stop_orders()
        assert stop.broker_order_id  # a real acknowledgement came first
        await self.service.on_update(
            Rejected(stop.broker_order_id, "RMS: stop rejected", self.clock.at)
        )
        assert self.service.orders[stop.order_id].state is OrderState.REJECTED
        return stop

    async def wait_for_next_stop(self, *, step_s: float = 0.5, limit_s: float = 60) -> float:
        """Sweep every ``step_s`` seconds of the fake clock until a new stop goes out; return how
        long that took. Every sweep before it placed nothing, or the wait would be shorter."""
        start, sent = self.clock.at, self.stops_sent()
        while self.stops_sent() == sent:
            assert self.clock.at - start < timedelta(seconds=limit_s), "no stop was placed"
            self.clock.at += timedelta(seconds=step_s)
            await self.stops.sweep()
            await self.stops.process_queue()
        await self.confirm_cancels()
        return (self.clock.at - start).total_seconds()


async def world(**kwargs: object) -> World:
    return await World(**kwargs).ready()  # type: ignore[arg-type]


# ---- the trigger -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "average", "pct", "expected"),
    [
        (65, "200.00", "20", "160.00"),  # a long: 20% below
        (65, "200.03", "20", "160.05"),  # 160.024 rounds UP, toward the entry: never wider
        (-100, "500.00", "10", "550.00"),  # a short: 10% above
        (-100, "500.03", "10", "550.00"),  # 550.033 rounds DOWN, toward the entry
    ],
)
def test_the_trigger_is_rounded_toward_the_entry_price(
    quantity: int, average: str, pct: str, expected: str
) -> None:
    held = position(OPTION, quantity, average)

    trigger = stop_trigger(held, D(pct), D("0.05"))

    assert trigger == D(expected)
    raw = D(average) * (1 - D(pct) / 100 if quantity > 0 else 1 + D(pct) / 100)
    assert (trigger >= raw) if quantity > 0 else (trigger <= raw)  # never wider than configured


# ---- placing, resizing and cancelling --------------------------------------------------------


async def test_stop_after_entry() -> None:
    """Spec "Stop after entry": a buy of 65 fills at 200.00; the catastrophic percentage is 20."""
    w = await world()

    await w.fill(BUY, 65)
    await w.settle()

    [stop] = w.stop_orders()
    assert (stop.side, stop.quantity, stop.trigger_price) == (SELL, 65, D("160.00"))
    assert stop.order_type is OrderType.STOP_MARKET and stop.product is ProductType.INTRADAY
    assert stop.source == SOURCE


async def test_short_position() -> None:
    """Spec "Short position": a sell entry of 100 fills at 500.00 and the percentage is 10."""
    w = await world(limits={**BASE, "catastrophic_sl_pct": "10"})

    await w.fill(SELL, 100, "500.00", key=STOCK)
    await w.settle()

    [stop] = w.stop_orders()
    assert (stop.side, stop.quantity, stop.trigger_price) == (BUY, 100, D("550.00"))


async def test_the_stop_is_placed_after_the_fill_never_before() -> None:
    w = await world()

    await w.fill(BUY, 65)
    assert w.stop_orders() == []  # nothing yet: the fill listener only queued the position
    await w.settle()

    assert [kind for kind, _ in w.adapter.events] == ["place", "place"]  # the entry, then the stop
    assert w.adapter.placed[0].order_type is OrderType.LIMIT
    assert w.adapter.placed[1].order_type is OrderType.STOP_MARKET


async def test_scale_in_places_the_new_stop_before_cancelling_the_old_one() -> None:
    """Spec "Scale in": 65 with a stop for 65 buys 65 more."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [old] = w.stop_orders()

    await w.fill(BUY, 65)
    await w.stops.process_queue()

    kinds = [(kind, order_id) for kind, order_id in w.adapter.events if order_id != old.order_id]
    new_stop = next(
        o for o in w.adapter.placed if o.order_type is OrderType.STOP_MARKET and o.quantity == 130
    )
    placed_at = w.adapter.events.index(("place", new_stop.order_id))
    cancelled_at = w.adapter.events.index(("cancel", old.order_id))
    assert placed_at < cancelled_at  # the position was never without a stop
    assert new_stop.quantity == 130 and kinds
    await w.confirm_cancels()
    assert [(o.quantity, o.trigger_price) for o in w.stop_orders()] == [(130, D("160.00"))]


async def test_a_resting_stop_that_is_already_right_is_left_alone() -> None:
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    before = list(w.adapter.events)

    await w.stops.sweep()
    await w.settle()

    assert w.adapter.events == before


async def test_position_closed_by_an_exit_cancels_its_stop() -> None:
    """Spec "Position closed by an exit": a stop never rests on a flat book."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [stop] = w.stop_orders()

    await w.fill(SELL, 65)  # the exit fills: the position is flat
    await w.stops.process_queue()

    assert ("cancel", stop.order_id) in w.adapter.events
    await w.confirm_cancels()
    assert w.stop_orders() == []


async def test_a_stop_left_on_a_flat_book_is_found_and_cancelled_by_the_sweep() -> None:
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [stop] = w.stop_orders()
    w.service.positions[next(iter(w.service.positions))] = position(
        OPTION, 0, "0"
    )  # closed elsewhere

    await w.stops.sweep()
    await w.stops.process_queue()

    assert ("cancel", stop.order_id) in w.adapter.events


async def test_a_stale_extra_stop_is_cancelled_once_a_matching_one_exists() -> None:
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    w.hold(OPTION, 65, "200.00")
    [good] = w.stop_orders()
    # A second, wrong-sized stop is left over (an earlier resize that never finished).
    stray = await w.service.place(
        PlaceOrder(
            command_id="stray", idempotency_key="stray", account_id="main", source=SOURCE,
            sent_at=NOW, key=OPTION, side=SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.STOP_MARKET, trigger_price=D("150.00"),
        )
    )  # fmt: skip

    await w.stops.sweep()
    await w.stops.process_queue()

    assert ("cancel", stray.order_id) in w.adapter.events
    assert ("cancel", good.order_id) not in w.adapter.events


# ---- through the gate, as an exit ------------------------------------------------------------


async def test_a_stop_is_an_exit_to_the_gate() -> None:
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [stop] = w.stop_orders()

    assert classify(stop, w.service.positions) is Intent.EXIT


async def test_stop_on_a_halted_day_is_accepted() -> None:
    """Spec "Stop on a halted day": the real gate, the day locked, a stop that must be placed."""
    engine = await Engine().start()
    engine.daybook.raise_halt(HaltLevel.KILL, "equity floor breached")
    engine.hold()
    stops = CatastrophicStops(
        account_id="main", service=engine.service, limits=engine.limits,
        instruments=instrument_of, now=engine.clock,
    )  # fmt: skip

    await stops.sync(next(iter(engine.service.positions)))

    placed = [o for o in engine.adapter.placed if o.order_type is OrderType.STOP_MARKET]
    assert len(placed) == 1 and placed[0].side is SELL and placed[0].source == SOURCE
    assert engine.service.orders[placed[0].order_id].state is OrderState.OPEN  # taken, not refused


# ---- failing: the broker rejects the stop ----------------------------------------------------


async def test_broker_rejects_the_stop_so_the_position_is_unprotected_and_entries_halt() -> None:
    """Spec "Broker rejects the stop": a critical alert, entries rejected, the stop retried."""
    w = await world()
    w.adapter.refuse_stops = True
    pid = w.hold(OPTION, 65)

    with capture_logs() as logs:
        await w.stops.sweep()
        await w.stops.process_queue()

    assert w.stops.unprotected() == frozenset({pid})
    assert any(e["log_level"] == "critical" for e in logs)
    decision = Unprotected().continuous(
        context_with(unprotected=w.stops.unprotected()), RiskTrigger.TIMER
    )
    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == UNPROTECTED


async def test_the_failed_stop_is_retried_with_backoff_and_succeeds_when_the_broker_does() -> None:
    w = await world()
    w.adapter.refuse_stops = True
    w.hold(OPTION, 65)
    await w.stops.sweep()
    await w.stops.process_queue()  # fails: backoff 1 s
    attempts = len(w.service.orders)

    await w.stops.sweep()
    await w.stops.process_queue()  # inside the backoff: no new attempt
    assert len(w.service.orders) == attempts

    w.adapter.refuse_stops = False
    w.clock.at += timedelta(seconds=1.5)
    await w.stops.process_queue()  # the retry is due

    assert w.stops.unprotected() == frozenset()
    assert len(w.stop_orders()) == 1


async def test_the_backoff_doubles_and_is_capped() -> None:
    w = await world()
    w.adapter.refuse_stops = True
    pid = w.hold(OPTION, 65)
    delays: list[float] = []

    for _ in range(8):
        await w.stops.sync(pid)
        delays.append(w.stops._delay[pid])
        w.clock.at += timedelta(seconds=40)  # past any backoff

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


async def test_a_new_fill_ends_the_backoff() -> None:
    w = await world()
    w.adapter.refuse_stops = True
    await w.fill(BUY, 65)
    await w.settle()  # fails, backs off
    w.adapter.refuse_stops = False

    await w.fill(BUY, 65)  # a fill changes what the stop should be
    await w.stops.process_queue()

    assert [o.quantity for o in w.stop_orders()] == [130]


async def test_an_ambiguous_stop_is_never_doubled() -> None:
    """A stop that timed out (state Unknown) may be live at the broker: no second one is placed."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    [stop] = w.stop_orders()
    placed = len(w.adapter.placed)
    # The first stop turns out to be of unknown state (as after a broker timeout).
    unknown = stop.model_copy(update={"state": OrderState.UNKNOWN})
    w.service.orders[stop.order_id] = unknown

    await w.stops.sweep()
    await w.stops.process_queue()

    assert len(w.adapter.placed) == placed  # nothing new went out


async def test_the_fill_listener_only_queues_so_it_cannot_deadlock_the_order_path() -> None:
    w = await world()

    await w.fill(BUY, 65)  # the fill is applied inside the service lock, with the listener called

    assert w.stop_orders() == []  # not placed from inside the listener
    await w.stops.process_queue()  # placed by the worker, outside the lock
    assert len(w.stop_orders()) == 1


# ---- when stops are placed -------------------------------------------------------------------


async def test_restart_with_an_unprotected_position_gets_its_stop_before_entries() -> None:
    """Spec "Restart with an unprotected position": its stop was lost; reconciliation is clean."""
    w = await world()
    pid = w.hold(OPTION, 65)
    assert w.stops.unprotected() == frozenset({pid})  # entries are blocked until it is placed

    await w.stops.sweep()
    await w.stops.process_queue()

    assert w.stops.unprotected() == frozenset()
    assert len(w.stop_orders()) == 1


async def test_carried_cnc_holding_gets_a_stop_at_the_open_not_before() -> None:
    """Spec "Carried CNC holding at the open": a stop is placed for the holding."""
    w = await world(at=BEFORE_OPEN)
    pid = w.hold(STOCK, 100, "200.00", ProductType.CNC)

    await w.stops.sweep()
    await w.stops.process_queue()
    assert w.stop_orders() == [] and w.stops.unprotected() == frozenset()  # 09:00: not yet

    w.clock.at = OPEN_AT
    assert w.stops.unprotected() == frozenset({pid})  # the open: no stop, so entries wait
    await w.stops.sweep()
    await w.stops.process_queue()

    [stop] = w.stop_orders()
    assert stop.product is ProductType.CNC and stop.side is SELL and stop.quantity == 100
    assert w.stops.unprotected() == frozenset()


async def test_no_stop_is_placed_after_the_close_and_its_expiry_is_not_unprotected() -> None:
    w = await world(at=AFTER_CLOSE)
    w.hold(OPTION, 65)

    await w.stops.sweep()
    await w.stops.process_queue()

    assert w.stop_orders() == [] and w.adapter.placed == []
    assert w.stops.unprotected() == frozenset()  # the close expired the stops: not a failure


async def test_run_sweeps_on_its_own_and_stops_when_asked() -> None:
    import asyncio

    w = await world()
    w.hold(OPTION, 65)
    stop = asyncio.Event()
    task = asyncio.create_task(w.stops.run(stop))

    for _ in range(50):
        await asyncio.sleep(0.01)
        if w.stop_orders():
            break
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(w.stop_orders()) == 1


async def test_without_limits_there_is_nothing_to_judge_and_nothing_is_placed() -> None:
    w = await World().ready()
    w.limits._current = None  # type: ignore[attr-defined]
    pid = w.hold(OPTION, 65)

    await w.stops.sync(pid)

    assert w.adapter.placed == [] and w.stops.unprotected() == frozenset()


def context_with(**fields: object):  # type: ignore[no-untyped-def]
    ctx = context()
    from dataclasses import replace

    return replace(ctx, **fields)  # type: ignore[arg-type]


def test_the_unprotected_check_is_silent_when_every_position_has_its_stop() -> None:
    decision = Unprotected().continuous(context_with(unprotected=frozenset()), RiskTrigger.TIMER)

    assert decision.severity is Severity.ALLOW and decision.code is None


def test_the_unprotected_check_names_every_position_without_a_stop() -> None:
    pids = frozenset({position_id(position(OPTION, 65)), position_id(position(STOCK, 10, "50"))})

    decision = Unprotected().continuous(context_with(unprotected=pids), RiskTrigger.TIMER)

    assert decision.code == UNPROTECTED
    assert str(OPTION) in decision.detail and str(STOCK) in decision.detail


async def test_a_tightened_catastrophic_percentage_replaces_the_stop_at_the_new_trigger() -> None:
    """Same side and size, wrong trigger: the owner tightens the percentage from 20 to 15 mid-day,
    so the resting stop at 160.00 is no longer what is wanted (170.00). The new one goes out first,
    then the old one is cancelled."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [old] = w.stop_orders()
    assert old.trigger_price == D("160.00")

    edit = await w.limits.set_limits(
        SetRiskLimits(
            command_id="tighten", idempotency_key="tighten", account_id="main", source="t",
            sent_at=NOW, limits={**PCT20, "catastrophic_sl_pct": "15"},
        )
    )  # fmt: skip
    assert edit.accepted, edit.reason
    assert w.stops.unprotected() == frozenset(position_id(p) for p in w.service.positions.values())

    await w.stops.sweep()
    await w.stops.process_queue()

    new = next(o for o in w.adapter.placed if o.trigger_price == D("170.00"))
    assert w.adapter.events.index(("place", new.order_id)) < w.adapter.events.index(
        ("cancel", old.order_id)
    )
    await w.confirm_cancels()
    assert [o.trigger_price for o in w.stop_orders()] == [D("170.00")]


# ---- delivery holdings, failing cancels and in-flight states (review round 1) -----------------


async def test_a_carried_cnc_holding_is_re_protected_after_a_scale_in() -> None:
    """The resting stop reserves the holding at the broker, so it must not count as "units
    already being sold": otherwise the replacement stop for 130 is refused (100 held, 100 spoken
    for) and entries stay halted until someone cancels the old stop by hand."""
    w = await world()
    w.hold(STOCK, 100, "200.00", ProductType.CNC)
    await w.stops.sweep()
    await w.stops.process_queue()
    [old] = w.stop_orders()
    assert old.quantity == 100

    w.hold(STOCK, 130, "200.00", ProductType.CNC)  # 30 more shares bought and delivered
    await w.stops.sweep()
    await w.stops.process_queue()

    new = [o for o in w.stop_orders() if o.quantity == 130]
    assert len(new) == 1 and w.stops.unprotected() == frozenset()  # accepted, and protected
    placed_at = w.adapter.events.index(("place", new[0].order_id))
    assert placed_at < w.adapter.events.index(("cancel", old.order_id))  # new first, then old


async def test_a_cnc_exit_is_accepted_while_the_stop_rests() -> None:
    """Exit before cancel (rule 7): the kill's exit for a delivery holding must go out while its
    catastrophic stop still rests, so the stop cannot count against what may be sold."""
    w = await world()
    w.hold(STOCK, 100, "200.00", ProductType.CNC)
    await w.stops.sweep()
    await w.stops.process_queue()
    assert len(w.stop_orders()) == 1

    result = await w.service.place(
        PlaceOrder(
            command_id="x1", idempotency_key="x1", account_id="main", source="risk-kill",
            sent_at=NOW, key=STOCK, side=SELL, quantity=100, product=ProductType.CNC,
            order_type=OrderType.LIMIT, limit_price=D("199.00"),
        )
    )  # fmt: skip

    assert result.accepted, result.reason


async def test_other_resting_cnc_sells_still_count_against_the_holding() -> None:
    """Only the protective stop is exempt: two limit sells of the same shares are still refused."""
    w = await world()
    w.hold(STOCK, 100, "200.00", ProductType.CNC)

    def sell(n: int) -> PlaceOrder:
        return PlaceOrder(
            command_id=f"s{n}", idempotency_key=f"s{n}", account_id="main", source="cli",
            sent_at=NOW, key=STOCK, side=SELL, quantity=100, product=ProductType.CNC,
            order_type=OrderType.LIMIT, limit_price=D("300.00"),
        )  # fmt: skip

    first = await w.service.place(sell(1))
    second = await w.service.place(sell(2))

    assert first.accepted and not second.accepted


async def test_a_stop_cancel_the_broker_refused_is_tried_again_by_the_next_sweep() -> None:
    """The first cancel of a stop that sits on a flat book fails. A fixed key would return that
    stored refusal for ever and leave the stop there, where it would OPEN a position if it
    triggered. Each attempt has its own key, and a refusal is loud."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [stop] = w.stop_orders()
    w.service.positions[next(iter(w.service.positions))] = position(OPTION, 0, "0")  # flat
    real, attempts = w.adapter.cancel, []

    async def flaky(order: Order) -> None:
        attempts.append(order.order_id)
        if len(attempts) == 1:
            raise BrokerError("the broker cannot cancel just now")
        await real(order)

    w.adapter.cancel = flaky  # type: ignore[method-assign]

    with capture_logs() as logs:
        await w.stops.sweep()
        await w.stops.process_queue()  # refused
    assert ("cancel", stop.order_id) not in w.adapter.events
    assert any(
        e["log_level"] == "critical" and "could not be cancelled" in e["event"] for e in logs
    )

    await w.stops.sweep()
    await w.stops.process_queue()  # asked again

    assert ("cancel", stop.order_id) in w.adapter.events


@pytest.mark.parametrize("state", [OrderState.REQUESTED, OrderState.SUBMITTED, OrderState.UNKNOWN])
async def test_a_stop_that_is_neither_open_nor_final_is_never_doubled(state: OrderState) -> None:
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    [stop] = w.stop_orders()
    placed = len(w.adapter.placed)
    w.service.orders[stop.order_id] = stop.model_copy(update={"state": state})

    await w.stops.sweep()
    await w.stops.process_queue()

    assert len(w.adapter.placed) == placed  # nothing new went out while it may be live


async def test_a_position_with_no_instrument_backs_off_once_per_try_not_per_sweep() -> None:
    w = await world()
    pid = w.hold(OPTION, 65)
    w.stops._instruments = lambda key: None  # type: ignore[attr-defined,assignment]

    with capture_logs() as logs:
        for _ in range(5):
            await w.stops.sync(pid)  # five sweeps within the first backoff

    alerts = [e for e in logs if e["log_level"] == "critical" and "unprotected" in e["event"]]
    assert len(alerts) == 1  # the alert and the delay grow once a try, not on every sweep


async def test_a_users_own_stop_order_still_counts_against_the_holding() -> None:
    """Only the catastrophic stop (by its source) is exempt, not every stop-market order: a
    stop the owner placed by hand reserves the shares like any other resting sell."""
    w = await world()
    w.hold(STOCK, 100, "200.00", ProductType.CNC)
    own_stop = await w.service.place(
        PlaceOrder(
            command_id="u1", idempotency_key="u1", account_id="main", source="cli", sent_at=NOW,
            key=STOCK, side=SELL, quantity=100, product=ProductType.CNC,
            order_type=OrderType.STOP_MARKET, trigger_price=D("150.00"),
        )
    )  # fmt: skip
    assert own_stop.accepted, own_stop.reason

    second = await w.service.place(
        PlaceOrder(
            command_id="u2", idempotency_key="u2", account_id="main", source="cli", sent_at=NOW,
            key=STOCK, side=SELL, quantity=100, product=ProductType.CNC,
            order_type=OrderType.LIMIT, limit_price=D("300.00"),
        )
    )  # fmt: skip

    assert not second.accepted  # the shares are already spoken for


# ---- a stop rejected after acceptance (handle-late-broker-rejections task 3.1) ----------------

LATE = "the broker rejected the catastrophic stop"  # the start of on_rejected's critical alert


def late_alerts(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["log_level"] == "critical" and str(e["event"]).startswith(LATE)]


async def test_stop_rejected_once() -> None:
    """Spec "Stop rejected once": a long of 65 with a resting stop; the broker rejects the stop.
    The position is unprotected, entries are refused, a critical alert is logged, and the new stop
    goes out no earlier than 1 s later."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    [pid] = w.service.positions
    sent = w.stops_sent()

    with capture_logs() as logs:
        await w.reject_stop()

    assert w.stops.unprotected() == frozenset({pid})
    decision = Unprotected().continuous(
        context_with(unprotected=w.stops.unprotected()), RiskTrigger.TIMER
    )
    assert decision.severity is Severity.HALT_NEW_ENTRIES and decision.code == UNPROTECTED
    assert len(late_alerts(logs)) == 1
    assert w.stops_sent() == sent  # nothing placed from inside the listener

    w.clock.at += timedelta(seconds=0.9)
    await w.stops.sweep()
    await w.stops.process_queue()
    assert w.stops_sent() == sent  # too early

    w.clock.at += timedelta(seconds=0.1)
    await w.stops.sweep()
    await w.stops.process_queue()
    [stop] = w.stop_orders()
    assert (stop.side, stop.quantity, stop.trigger_price) == (SELL, 65, D("160.00"))
    assert w.stops.unprotected() == frozenset()


async def test_broker_keeps_rejecting_the_stop() -> None:
    """Spec "Broker keeps rejecting the stop": the broker accepts and then rejects every new stop.
    The waits are 1, 2, 4, 8, 16, 30, 30 s: a successful placement does not reset the delay, and
    no stop goes out on the sweeps in between (``wait_for_next_stop`` sweeps every 0.5 s)."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    waits: list[float] = []

    for _ in range(7):
        await w.reject_stop()
        waits.append(await w.wait_for_next_stop())

    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


async def test_a_fill_resets_the_delay() -> None:
    """Spec "A fill resets the delay": the stop was rejected three times, so the next wait would be
    8 s; an entry for the position fills, its new stop is rejected, and the next wait is 1 s."""
    w = await world()
    await w.fill(BUY, 65)
    await w.settle()
    waits: list[float] = []
    for _ in range(3):
        await w.reject_stop()
        waits.append(await w.wait_for_next_stop())
    assert waits == [1.0, 2.0, 4.0]

    await w.fill(BUY, 65)  # the position changes: a stop for 130 replaces the one for 65
    await w.settle()
    assert [o.quantity for o in w.stop_orders()] == [130]
    await w.reject_stop()

    assert await w.wait_for_next_stop() == 1.0


async def test_going_flat_resets_the_delay() -> None:
    """The delay also resets when ``_sync`` finds the position flat (closed outside the engine,
    its stop still resting): a position opened later starts again at 1 s."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    for _ in range(3):
        await w.reject_stop()
        await w.wait_for_next_stop()
    w.hold(OPTION, 0, "0")  # flat, with the last stop still resting
    await w.stops.sweep()
    await w.stops.process_queue()  # the stop on the flat book is cancelled
    await w.confirm_cancels()
    assert w.stop_orders() == []

    w.hold(OPTION, 65)  # opened again
    await w.stops.sync(pid)
    await w.reject_stop()

    assert await w.wait_for_next_stop() == 1.0


async def test_rejected_stop_on_a_flat_book() -> None:
    """Spec "Rejected stop on a flat book": the position went flat before the broker rejected its
    stop. No new stop is placed, and no delay or alert is set up for a position that is gone."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    sent = w.stops_sent()
    w.hold(OPTION, 0, "0")

    with capture_logs() as logs:
        await w.reject_stop()
        for _ in range(4):
            w.clock.at += timedelta(seconds=10)
            await w.stops.sweep()
            await w.stops.process_queue()

    assert w.stops_sent() == sent and w.stop_orders() == []
    assert late_alerts(logs) == [] and pid not in w.stops._retry_at


async def test_a_stop_refused_at_placement_is_backed_off_once_and_not_counted_as_late() -> None:
    """``BrokerRejected`` at placement: the order has no broker id. ``_sync`` backs it off (one
    alert, a 1 s wait) and ``on_rejected``, which the service also calls, ignores it. Had it been
    counted, the first late rejection afterwards would wait 2 s instead of 1 s."""
    w = await world()
    w.adapter.refuse_stops = True
    pid = w.hold(OPTION, 65)

    with capture_logs() as logs:
        await w.stops.sweep()
        await w.stops.process_queue()

    [refused] = w.stop_orders(OrderState.REJECTED)
    assert refused.broker_order_id is None
    retrying = [e for e in logs if e["log_level"] == "critical" and "retrying" in e["event"]]
    assert len(retrying) == 1 and late_alerts(logs) == []
    w.adapter.refuse_stops = False
    assert await w.wait_for_next_stop() == 1.0  # the placement backoff, once

    await w.reject_stop()

    assert await w.wait_for_next_stop() == 1.0  # the first LATE rejection of this position
    assert w.stops._delay.get(pid) is None  # the placement backoff ended with the success


async def test_a_users_own_stop_rejected_late_is_not_the_managers_business() -> None:
    """Only the manager's own stops count: a stop-market order the owner placed by hand, rejected
    after its acknowledgement, sets no delay and raises no alert of the manager's. (The manager has
    no stop of its own resting here, so only the source filter can tell the two apart.)"""
    w = await world()
    pid = w.hold(OPTION, 65)
    own = await w.service.place(
        PlaceOrder(
            command_id="u1", idempotency_key="u1", account_id="main", source="cli", sent_at=NOW,
            key=OPTION, side=SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.STOP_MARKET, trigger_price=D("150.00"),
        )
    )  # fmt: skip
    broker_id = w.service.orders[own.order_id or ""].broker_order_id or ""

    with capture_logs() as logs:
        await w.service.on_update(Rejected(broker_id, "RMS", w.clock.at))

    assert w.service.orders[own.order_id or ""].state is OrderState.REJECTED
    assert late_alerts(logs) == [] and pid not in w.stops._retry_at


async def test_the_rejection_listener_never_awaits_and_is_safe_inside_the_service_lock() -> None:
    """``add_rejection_listener`` requires a plain function: it runs inside the service lock, so an
    ``async`` one would never run (its coroutine is dropped) and one that placed a stop would wait
    on that lock for ever. The rejection is applied promptly, under the lock, and nothing is placed
    from inside it."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    sent = w.stops_sent()
    seen: list[tuple[bool, object]] = []

    def spy(order: Order) -> None:
        locked = w.service._lock.locked()  # type: ignore[attr-defined]
        returned: object = w.stops.on_rejected(order)  # type: ignore[func-returns-value]
        seen.append((locked, returned))

    w.service._rejection_listeners[:] = [spy]  # type: ignore[attr-defined]

    await asyncio.wait_for(w.reject_stop(), timeout=1)

    assert not inspect.iscoroutinefunction(w.stops.on_rejected)
    assert seen == [(True, None)]  # called under the lock, and returned (no coroutine) at once
    assert w.stops_sent() == sent and pid in w.stops._retry_at


async def test_a_late_rejection_of_a_stale_extra_stop_leaves_the_position_protected() -> None:
    """The broker rejects a leftover stop of the manager's (wrong trigger) while the correct one
    still rests: nothing is unprotected, so there is no count, no delay and no alert."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    stray = await w.service.place(
        PlaceOrder(
            command_id="stray", idempotency_key="stray", account_id="main", source=SOURCE,
            sent_at=NOW, key=OPTION, side=SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.STOP_MARKET, trigger_price=D("150.00"),
        )
    )  # fmt: skip
    broker_id = w.service.orders[stray.order_id or ""].broker_order_id or ""

    with capture_logs() as logs:
        await w.service.on_update(Rejected(broker_id, "RMS", w.clock.at))

    assert w.service.orders[stray.order_id or ""].state is OrderState.REJECTED
    assert late_alerts(logs) == [] and pid not in w.stops._retry_at
    assert w.stops.unprotected() == frozenset()


# The broker's Rejected for a stop the manager has just placed, landing while ``_sync`` is still
# awaiting (the ack, a cancel): review round 1 of group 3. Each test queues the update on the
# service lock the way the update stream would, and checks the old stop and the delay survive.


def rejection_of(w: World, order: Order) -> Rejected:
    return Rejected(w.service.orders[order.order_id].broker_order_id or "", "RMS", w.clock.at)


async def scale_in(w: World) -> tuple[Order, PositionId]:
    """A long of 65 with its stop resting; returns that stop and the position."""
    await w.fill(BUY, 65)
    await w.settle()
    [old] = w.stop_orders()
    [pid] = w.service.positions
    return old, pid


async def test_a_new_stop_rejected_before_the_old_one_is_cancelled_keeps_the_old_one() -> None:
    """The position grows to 130, so a stop for 130 goes out. The broker acknowledges it and its
    rejection follows on the stream while the ack is still being handled, so the update waits on
    the service lock. The old stop (65) must not be cancelled: it is the only protection left. The
    delay ``on_rejected`` set survives, and the next stop goes out 1 s later, not at once."""
    w = await world()
    old, pid = await scale_in(w)
    real_place, pending = w.adapter.place, []

    async def place_then_reject(order: Order) -> BrokerAck:
        ack = await real_place(order)
        if order.order_type is OrderType.STOP_MARKET and not pending:
            update = Rejected(ack.broker_order_id, "RMS", w.clock.at)
            pending.append(asyncio.create_task(w.service.on_update(update)))
            await asyncio.sleep(0)  # the update starts and queues on the lock the place holds
        return ack

    w.adapter.place = place_then_reject  # type: ignore[method-assign]
    await w.fill(BUY, 65)

    with capture_logs() as logs:
        await w.stops.process_queue()
    await asyncio.gather(*pending)

    [rejected] = w.stop_orders(OrderState.REJECTED)
    assert rejected.quantity == 130 and len(late_alerts(logs)) == 1
    assert ("cancel", old.order_id) not in w.adapter.events
    assert w.stop_orders() == [w.service.orders[old.order_id]]  # the 65 still rests
    assert w.stops._retry_at[pid] == w.clock.at + timedelta(seconds=1)
    assert w.stops.unprotected() == frozenset({pid})  # truly: no stop for 130 rests
    assert await w.wait_for_next_stop() == 1.0  # and no stop on the sweeps in between
    assert [o.quantity for o in w.stop_orders()] == [130]  # then the 65 is cancelled


async def test_a_new_stop_that_fills_in_the_ack_window_still_has_the_old_one_cancelled() -> None:
    """The new stop for 130 triggers and fills while its ack is still being handled. It did its
    job: the book is now flat, so the old stop (65) would OPEN a position if it triggered. It is
    cancelled in the same ``_sync``, not left for the next pass (safety rule 14)."""
    w = await world()
    old, pid = await scale_in(w)
    real_place, pending = w.adapter.place, []

    async def place_then_fill(order: Order) -> BrokerAck:
        ack = await real_place(order)
        if order.order_type is OrderType.STOP_MARKET and not pending:
            update = Filled(
                broker_order_id=ack.broker_order_id, fill_id="stop-fill", quantity=order.quantity,
                price=D("160.00"), at=w.clock.at, costs=CostBreakdown(),
            )  # fmt: skip
            pending.append(asyncio.create_task(w.service.on_update(update)))
            await asyncio.sleep(0)  # the fill starts and queues on the lock the place holds
        return ack

    w.adapter.place = place_then_fill  # type: ignore[method-assign]
    await w.fill(BUY, 65)

    await w.stops.sync(pid)  # one _sync only: the queue the fill listener fed is not drained
    await asyncio.gather(*pending)

    [filled] = w.stop_orders(OrderState.FILLED)
    assert filled.quantity == 130 and w.service.positions[pid].quantity == 0
    assert ("cancel", old.order_id) in w.adapter.events


async def test_a_rejection_that_lands_during_the_old_stops_cancel_keeps_its_delay() -> None:
    """The narrowest window: the manager has re-read the new stop (still open) and asks for the old
    one's cancel, but the broker's rejection of the new stop got onto the lock first. The cancel
    cannot be taken back, but the delay ``on_rejected`` set must not be forgotten afterwards, or
    the next sweep would place again at once. (The update is queued behind the manager's own
    re-read, the way a second lock holder would queue it.)"""
    w = await world()
    old, pid = await scale_in(w)
    real_settled, pending = w.service.settled, []

    async def settled_then_a_rejection_queues() -> None:
        lock = w.service._lock  # type: ignore[attr-defined]
        async with lock:  # what settled() does, with the broker's update queuing meanwhile
            if not pending:
                [new] = [o for o in w.stop_orders() if o.quantity == 130]
                pending.append(asyncio.create_task(w.service.on_update(rejection_of(w, new))))
                await asyncio.sleep(0)
        w.service.settled = real_settled  # type: ignore[method-assign]

    w.service.settled = settled_then_a_rejection_queues  # type: ignore[method-assign]
    await w.fill(BUY, 65)

    await w.stops.process_queue()
    await asyncio.gather(*pending)
    await w.confirm_cancels()

    assert ("cancel", old.order_id) in w.adapter.events  # asked for before the rejection landed
    assert w.stops._retry_at[pid] == w.clock.at + timedelta(seconds=1)
    assert w.stops.unprotected() == frozenset({pid}) and w.stop_orders() == []
    assert await w.wait_for_next_stop() == 1.0


async def test_the_kept_stop_rejected_while_a_stray_is_cancelled_keeps_its_delay() -> None:
    """The correct stop rests with a stray beside it. The sweep cancels the stray, and the broker's
    rejection of the correct stop is already queued on the lock: it lands during that cancel. The
    delay ``on_rejected`` set must survive, so the next stop goes out 1 s later, not at once."""
    w = await world()
    pid = w.hold(OPTION, 65)
    await w.stops.sync(pid)
    [kept] = w.stop_orders()
    stray = await w.service.place(
        PlaceOrder(
            command_id="stray", idempotency_key="stray", account_id="main", source=SOURCE,
            sent_at=NOW, key=OPTION, side=SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.STOP_MARKET, trigger_price=D("150.00"),
        )
    )  # fmt: skip
    lock = w.service._lock  # type: ignore[attr-defined]

    async with lock:  # an update is being applied as the sweep starts; the rejection queues
        pending = asyncio.create_task(w.service.on_update(rejection_of(w, kept)))
        await asyncio.sleep(0)
    await w.stops.sync(pid)  # reads the kept stop as open, then cancels the stray
    await pending
    await w.confirm_cancels()

    assert ("cancel", stray.order_id) in w.adapter.events
    assert w.service.orders[kept.order_id].state is OrderState.REJECTED
    assert w.stops._retry_at[pid] == w.clock.at + timedelta(seconds=1)
    assert w.stops.unprotected() == frozenset({pid}) and w.stop_orders() == []
    assert await w.wait_for_next_stop() == 1.0
