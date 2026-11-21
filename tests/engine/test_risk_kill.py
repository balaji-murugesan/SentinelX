"""The kill switch, the chase flattener and the own square-off (add-risk-engine tasks 10.1-10.3;
kill-switch spec and risk-rules "Own square-off").

A real ``OrderService`` on a fake clock with a broker double that records every place and cancel.
The flattener's ``sleep`` is a fake: it advances the clock and then runs the *broker script*, which
fills resting orders that the test's market price has crossed and confirms cancels the way the
broker's update stream would. Turning either off is how a test makes an exit rest or a cancel stay
unconfirmed.

Scenarios: "Carried delivery position is flattened", "Entry fills during cancel", "Exit not
filling", "Replacement waits for the cancel", "Not flat in time", "Stop after exit", "Kill twice",
"Resume does not help", "Next day", "No broker lock", "Floor breach kills without any client",
"Persistence down at a breach", "Mixed book at square-off" and "Not a halt"; and, for
add-risk-guards task 8.3, the flattener's positions filter and "Expiry square-off"; for task 9.2,
"A kill outranks the target" and that the recovery resume command does not clear a target halt.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from contracts.commands import KillSwitch as KillCommand
from contracts.commands import PlaceOrder, ResumeRecovery, SetRiskLimits
from contracts.market import InstrumentKey
from contracts.orders import CostBreakdown, Order, OrderSide, OrderState, OrderType, ProductType
from contracts.snapshot import HaltLevel
from engine.broker.adapter import (
    BrokerAck,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerRejected,
    BrokerUpdate,
    Cancelled,
    Filled,
    Rejected,
)
from engine.market_data.provider import Tick
from engine.orders.recovery import resume_recovery
from engine.persistence.repository import PersistenceError
from engine.positions.book import PositionId, position_id
from engine.risk.audit import RiskAudit
from engine.risk.checks.stale import FLOOR_BREACH, FloorBreach
from engine.risk.daybook import DayBook
from engine.risk.decision import Quote
from engine.risk.kill import ChaseFlattener, KillSwitch, SquareOff, aggressive_price
from engine.risk.limits_service import LimitsService
from engine.risk.monitor import RiskMonitor
from engine.risk.stops import SOURCE as STOP_SOURCE
from engine.risk.stops import CatastrophicStops
from tests.engine.risk_builders import (
    INSTRUMENTS,
    NOW,
    OPTION,
    STOCK,
    TODAY,
    instrument_of,
    position,
)
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build, command
from tests.engine.test_risk_limits import BASE
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
TOMORROW = NOW + timedelta(days=1)
SQUARE_OFF = datetime(2026, 11, 16, 9, 45, tzinfo=UTC)  # 15:15 IST, the configured square-off time
BEFORE_SQUARE_OFF = SQUARE_OFF - timedelta(minutes=5)
STEPS = int(BASE["max_chase_steps"])  # type: ignore[call-overload]
CHASE_S = int(BASE["chase_interval_s"])  # type: ignore[call-overload]


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Broker:
    """Acknowledges every order and remembers what it was asked to do."""

    def __init__(self) -> None:
        self.placed: list[Order] = []
        self.cancel_asked: list[str] = []  # engine order ids, in the order asked

    async def place(self, order: Order) -> BrokerAck:
        self.placed.append(order)
        return BrokerAck(f"b-{len(self.placed)}")

    async def cancel(self, order: Order) -> None:
        self.cancel_asked.append(order.order_id)

    async def order_book(self) -> list[BrokerOrder]:
        return []

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        return
        yield


async def until(condition: Callable[[], bool], *, limit: int = 5000) -> None:
    """Let the event loop run until ``condition()`` holds. Bounded, so a bug fails the test
    instead of hanging it."""
    for _ in range(limit):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never became true")


class World:
    def __init__(self, at: datetime = NOW, risk: MemoryRiskRepository | None = None) -> None:
        self.clock = Clock(at)
        self.broker = Broker()
        self.service, _, self.repo = build(adapter=self.broker, now=self.clock)
        self.risk = risk or MemoryRiskRepository()
        self.limits = LimitsService(
            account_id="main", repository=self.risk, results=MemoryRepository(),
            figures=lambda: (D(0), None), now=self.clock,
        )  # fmt: skip
        self.daybook = DayBook(
            account_id="main", repository=self.risk, limits=lambda: self.limits.current,
            now=self.clock,
        )  # fmt: skip
        self.prices: dict = {OPTION: D("100"), STOCK: D("200")}  # type: ignore[type-arg]
        self.market: dict = {OPTION: D("100"), STOCK: D("200")}  # type: ignore[type-arg]
        self.fills_on = True  # resting orders that the market has crossed are filled
        self.confirms_on = True  # asked-for cancels are confirmed
        self.slept = 0.0
        self.sleeps: list[float] = []
        self._fills = 0
        self.flattener = ChaseFlattener(
            account_id="main", service=self.service, limits=self.limits, quote=self.quote,
            instruments=instrument_of, now=self.clock, sleep=self.sleep, poll_s=0.5,
        )  # fmt: skip
        self.kill = KillSwitch(
            account_id="main", daybook=self.daybook, service=self.service,
            flattener=self.flattener, results=self.repo, now=self.clock,
        )  # fmt: skip

    def quote(self, key):  # type: ignore[no-untyped-def]
        price = self.prices.get(key)
        return None if price is None else Quote(price, timedelta(seconds=1))

    async def sleep(self, seconds: float) -> None:
        self.clock.at += timedelta(seconds=seconds)
        self.slept += seconds
        self.sleeps.append(seconds)
        await self.script()
        await asyncio.sleep(0)

    async def script(self) -> None:
        """What the broker does between two looks at the book."""
        for order in list(self.service.orders.values()):
            if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                continue
            broker_id = order.broker_order_id or ""
            if self.confirms_on and order.order_id in self.broker.cancel_asked:
                await self.service.on_update(Cancelled(broker_id, "cancelled", self.clock.at))
            elif self.fills_on and self.crossed(order):
                await self.fill(order)

    def crossed(self, order: Order) -> bool:
        market, limit = self.market.get(order.key), order.limit_price
        if market is None or limit is None or order.order_type is not OrderType.LIMIT:
            return False
        return limit <= market if order.side is SELL else limit >= market

    async def fill(self, order: Order) -> None:
        self._fills += 1
        remaining = order.quantity - order.filled_quantity
        await self.service.on_update(
            Filled(
                broker_order_id=order.broker_order_id or "", fill_id=f"f{self._fills}",
                quantity=remaining, price=order.limit_price or D("0"), at=self.clock.at,
                costs=CostBreakdown(),
            )
        )  # fmt: skip

    async def ready(self) -> "World":
        await self.limits.set_limits(
            SetRiskLimits(
                command_id="c", idempotency_key="k", account_id="main", source="t",
                sent_at=NOW, limits=BASE,
            )
        )  # fmt: skip
        assert await self.daybook.load()
        return self

    def hold(self, key, quantity: int, product=ProductType.INTRADAY, average="100.00"):  # type: ignore[no-untyped-def]
        held = position(key, quantity, average, product)
        self.service.positions[position_id(held)] = held

    def flat(self) -> bool:
        return all(p.quantity == 0 for p in self.service.positions.values())

    def orders(self, *, source: str | None = None) -> list[Order]:
        found = sorted(self.service.orders.values(), key=lambda o: o.created_at)
        return [o for o in found if source is None or o.source == source]

    async def flatten(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        await asyncio.wait_for(self.flattener.flatten("test", **kwargs), timeout=5)

    async def finish_flatten(self) -> None:
        assert self.kill.flattening is not None
        await asyncio.wait_for(self.kill.flattening, timeout=5)


async def world(at: datetime = NOW) -> World:
    return await World(at).ready()


# ---- the exit price --------------------------------------------------------------------------


def test_an_exit_is_priced_past_the_market_rounded_the_aggressive_way() -> None:
    tick = D("0.05")

    assert aggressive_price(SELL, D("100"), D("2"), tick) == D("98.00")
    assert aggressive_price(BUY, D("100"), D("2"), tick) == D("102.00")
    assert aggressive_price(SELL, D("100.03"), D("2"), tick) == D("98.00")  # 98.0294 down
    assert aggressive_price(BUY, D("100.03"), D("2"), tick) == D("102.05")  # 102.0306 up
    assert aggressive_price(SELL, D("0.05"), D("50"), tick) == D("0.05")  # never below a tick


# ---- the flattener ---------------------------------------------------------------------------


async def test_carried_delivery_position_is_flattened_with_the_intraday_one() -> None:
    """Spec "Carried delivery position is flattened"."""
    w = await world()
    w.hold(OPTION, 65)
    w.hold(STOCK, 100, ProductType.CNC)

    await w.flatten()

    assert w.flat()
    assert {o.product for o in w.orders()} == {ProductType.INTRADAY, ProductType.CNC}
    assert all(o.order_type is OrderType.LIMIT for o in w.orders())  # never a market order


async def test_a_short_position_is_exited_with_a_buy_priced_above_the_market() -> None:
    w = await world()
    w.hold(OPTION, -65)

    await w.flatten()

    [exit_] = w.orders()
    assert exit_.side is BUY and exit_.limit_price == D("102.00") and w.flat()


async def test_only_the_asked_for_products_are_exited() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.hold(STOCK, 100, ProductType.CNC)

    await w.flatten(products=frozenset({ProductType.INTRADAY}), kind="squareoff")

    held = {p.product: p.quantity for p in w.service.positions.values()}
    assert held[ProductType.INTRADAY] == 0 and held[ProductType.CNC] == 100
    assert {o.source for o in w.orders()} == {"risk-squareoff"}


async def test_an_exit_that_does_not_fill_is_cancelled_and_replaced_at_the_current_price() -> None:
    """Spec "Exit not filling": it rests for the chase interval, is cancelled, and the
    replacement is priced from the price at that moment."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")  # the sell limit at 98 is not marketable: it rests
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    assert w.orders()[0].limit_price == D("98.00")  # 100 less 2 %

    w.prices[OPTION] = D("91")  # the market moves: the replacement is priced from here
    await asyncio.wait_for(task, timeout=5)  # (89.15 is marketable at 90, so it then fills)

    first, second = w.orders()[:2]
    assert first.state is OrderState.CANCELLED
    assert second.limit_price == D("89.15")  # 91 less 2 % is 89.18, rounded down to the tick
    assert w.flat()


async def test_a_replacement_waits_for_the_cancel_to_be_confirmed() -> None:
    """Spec "Replacement waits for the cancel": while the cancel is unconfirmed no new order goes
    out, however long that takes (replacing blind could leave two exits live)."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    w.confirms_on = False
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: w.slept >= 20)  # long past the chase interval

    assert len(w.orders()) == 1 and w.broker.cancel_asked == [w.orders()[0].order_id]
    w.confirms_on = True  # the broker confirms at last
    w.market[OPTION] = D("100")  # so the replacement can fill
    await asyncio.wait_for(task, timeout=5)

    assert len(w.orders()) == 2  # the replacement went out only after the cancel was final
    assert w.flat()


async def test_replacements_stop_at_the_configured_number_of_steps() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("50")  # never marketable: the exit keeps resting
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: w.slept >= 10 * CHASE_S * STEPS)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(w.orders()) == STEPS + 1  # the first exit plus max_chase_steps re-prices


async def test_an_entry_that_fills_while_its_cancel_is_unconfirmed_is_exited_too() -> None:
    """Spec "Entry fills during cancel": the kill cancels a resting entry, which fills before the
    cancel is confirmed. The flatten re-reads the book, so the larger position is exited."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")  # exits rest, and so does the entry below
    w.confirms_on = False
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert entry.accepted

    await w.kill.trigger("test", "cli")  # cancels the entry (unconfirmed) and starts the flatten
    await until(lambda: len(w.orders(source="risk-kill")) == 1)
    await w.fill(w.service.orders[entry.order_id or ""])  # the entry filled before its cancel
    assert next(iter(w.service.positions.values())).quantity == 130
    # The flatten sees a position of 130 under an exit of 65 and cancels that exit at once, not
    # after waiting out the chase interval ...
    await until(lambda: w.orders(source="risk-kill")[0].order_id in w.broker.cancel_asked)
    assert w.slept < CHASE_S
    w.confirms_on = True  # ... which is confirmed, and the replacement goes out
    await until(lambda: len(w.orders(source="risk-kill")) == 2)
    (chase,) = w.flattener._chases.values()  # type: ignore[attr-defined]
    assert chase.replacements == 0  # a size change is not a re-price: it must not use up the cap
    w.market[OPTION] = D("100")  # and now it can fill
    await w.finish_flatten()

    assert w.flat()
    exits = [o for o in w.orders(source="risk-kill") if o.side is SELL]
    assert sum(o.filled_quantity for o in exits) == 130
    # The replacement went out as soon as the cancel was confirmed, not a chase interval later.
    assert (exits[1].created_at - exits[0].created_at).total_seconds() < CHASE_S


async def test_a_flatten_that_is_not_finished_in_time_raises_a_critical_alert_and_goes_on() -> None:
    """Spec "Not flat in time": a critical log after ``exit_escalate_s``, and it keeps trying."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("50")
    task = asyncio.create_task(w.flattener.flatten("test"))

    with capture_logs() as logs:
        await until(lambda: w.slept >= 40)  # past the 30 s escalation time
    alerts = [e for e in logs if e["log_level"] == "critical" and "not flat" in e["event"]]

    assert len(alerts) == 1  # once, not every pass
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_position_that_cannot_be_priced_is_alerted_and_never_sold_at_market() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.prices.clear()  # nothing to price an exit from
    task = asyncio.create_task(w.flattener.flatten("test"))

    with capture_logs() as logs:
        await until(lambda: w.slept >= 5)

    assert w.orders() == []
    alerts = [e for e in logs if e["log_level"] == "critical" and "cannot price" in e["event"]]
    assert len(alerts) == 1  # once for the stretch, not on every look (five a second)
    w.prices[OPTION] = D("100")  # a tick arrives
    await asyncio.wait_for(task, timeout=5)
    assert w.flat()


async def test_without_limits_the_flatten_waits_and_alerts_instead_of_guessing_a_buffer() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.limits._current = None  # type: ignore[attr-defined]
    task = asyncio.create_task(w.flattener.flatten("test"))

    with capture_logs() as logs:
        await until(lambda: w.slept >= 5)

    assert w.orders() == []
    assert sum("needs the limits" in e["event"] for e in logs) == 1  # once, not on every look
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_catastrophic_stop_is_cancelled_after_the_kills_exit_fills() -> None:
    """Spec "Stop after exit": the flattener exits the position and the stop manager (fed by the
    fill listener) cancels the stop that is left resting on a flat book (rule 14). The flattener
    never touches the stop itself: exit first, then cancel (rule 7)."""
    w = await world()
    w.hold(OPTION, 65)
    stops = CatastrophicStops(
        account_id="main", service=w.service, limits=w.limits, instruments=instrument_of,
        now=w.clock, sweep_every_s=5,
    )  # fmt: skip
    w.service.add_fill_listener(stops.on_fill)
    await stops.sweep()
    await stops.process_queue()
    [stop] = [o for o in w.orders() if o.source == STOP_SOURCE]
    assert stop.state is OrderState.OPEN

    await w.kill.trigger("test", "cli")
    await w.finish_flatten()
    assert w.flat() and w.service.orders[stop.order_id].state is OrderState.OPEN  # not yet
    await stops.process_queue()  # the manager's next turn
    await w.script()  # the broker confirms the cancel

    assert w.service.orders[stop.order_id].state is OrderState.CANCELLED


async def test_two_callers_flattening_one_position_share_one_exit() -> None:
    """The kill and the square-off use one flattener: they must not each place an exit for the
    same position (two exits that both filled would flip it)."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")  # the exit rests while both callers look at it
    place = w.broker.place

    async def slow_place(order: Order) -> BrokerAck:
        await asyncio.sleep(0)  # a real broker call suspends: the other caller runs meanwhile
        return await place(order)

    w.broker.place = slow_place  # type: ignore[method-assign]
    first = asyncio.create_task(w.flattener.flatten("kill"))
    second = asyncio.create_task(w.flattener.flatten("square-off"))
    await until(lambda: w.slept >= 1)
    assert len(w.orders()) == 1
    w.market[OPTION] = D("100")

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert len(w.orders()) == 1 and w.flat()


async def test_an_exit_left_resting_after_the_position_closed_another_way_is_cancelled() -> None:
    """The owner closes the position in the broker's app while the flattener's exit rests. A
    resting order on a flat book would open a position, so it is cancelled before the flatten
    returns."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)

    w.hold(OPTION, 0, average="0")  # closed elsewhere and reconciled into the book
    await asyncio.wait_for(task, timeout=5)

    assert w.orders()[0].state is OrderState.CANCELLED


async def test_an_exit_the_broker_rejects_is_placed_again_under_a_new_key() -> None:
    w = await world()
    w.hold(OPTION, 65)
    place = w.broker.place
    calls: list[str] = []

    async def flaky(order: Order) -> BrokerAck:
        calls.append(order.order_id)
        if len(calls) == 1:
            raise BrokerRejected("try again")
        return await place(order)

    w.broker.place = flaky  # type: ignore[method-assign]

    await w.flatten()

    first, second = w.orders()
    assert first.state is OrderState.REJECTED and second.state is OrderState.FILLED and w.flat()


async def test_an_exit_the_broker_keeps_rejecting_is_retried_after_the_chase_interval() -> None:
    """A broker that refuses every exit must not be hit with a new order on every look (twice a
    second): retries wait out the chase interval."""
    w = await world()
    w.hold(OPTION, 65)

    async def refusing(order: Order) -> BrokerAck:
        raise BrokerRejected("not accepting orders")

    w.broker.place = refusing  # type: ignore[method-assign]
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: w.slept >= 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    attempts = len(w.orders())
    assert 4 <= attempts <= 6  # about one every 2 s over 10 s, not about twenty


async def test_an_exit_the_engine_refuses_is_retried_after_the_chase_interval() -> None:
    w = await world()
    w.hold(OPTION, 70)  # not a whole number of 65-unit lots: validation refuses every exit
    task = asyncio.create_task(w.flattener.flatten("test"))

    with capture_logs() as logs:
        await until(lambda: w.slept >= 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    refused = [e for e in logs if e["event"] == "exit refused"]
    assert 4 <= len(refused) <= 6 and w.orders() == []


async def test_a_flatten_stops_with_an_alert_when_the_market_closes_with_the_book_not_flat() -> (
    None
):
    """Nothing can be sold after 15:30: it must not send orders the broker will refuse all night."""
    almost_closed = datetime(2026, 11, 16, 9, 59, 50, tzinfo=UTC)  # 15:29:50 IST
    w = await world(almost_closed)
    w.hold(OPTION, 65)
    w.market[OPTION] = D("50")  # never fills
    task = asyncio.create_task(w.flattener.flatten("test"))

    with capture_logs() as logs:
        await asyncio.wait_for(task, timeout=5)  # it ends by itself once 15:30 passes

    alerts = [e for e in logs if e["log_level"] == "critical" and "market is closed" in e["event"]]
    assert len(alerts) == 1 and alerts[0]["open"] == [str(OPTION)]
    assert not w.flat()


async def test_a_resting_exit_on_a_flat_book_is_still_cancelled_after_the_close() -> None:
    """The position was closed elsewhere just before 15:30 and the cancel is only confirmed after
    it: the flatten stays until the exit is final, because a resting order on a flat book would
    open a position."""
    almost_closed = datetime(2026, 11, 16, 9, 59, 50, tzinfo=UTC)  # 15:29:50 IST
    w = await world(almost_closed)
    w.hold(OPTION, 65)
    w.market[OPTION] = D("50")
    w.confirms_on = False
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    w.hold(OPTION, 0, average="0")  # closed elsewhere
    await until(lambda: w.slept >= 15)  # now past 15:30

    assert not task.done() and w.broker.cancel_asked == [w.orders()[0].order_id]
    w.confirms_on = True
    await asyncio.wait_for(task, timeout=5)

    assert w.orders()[0].state is OrderState.CANCELLED


async def test_no_flatten_is_started_while_the_market_is_closed() -> None:
    after_close = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)  # 16:00 IST
    w = await world(after_close)
    w.hold(OPTION, 65)
    w.daybook.raise_halt(HaltLevel.KILL, "locked earlier")

    w.kill.ensure_flattening()  # the monitor asks on every pass until midnight

    assert w.kill.flattening is None and w.orders() == []


async def test_a_kill_before_the_open_locks_the_day_and_exits_when_the_market_opens() -> None:
    before_open = datetime(2026, 11, 16, 3, 40, tzinfo=UTC)  # 09:10 IST
    w = await world(before_open)
    w.hold(OPTION, 65)

    await w.kill.trigger("test", "cli")
    assert w.daybook.day().halt_level is HaltLevel.KILL and w.kill.flattening is None

    w.clock.at = datetime(2026, 11, 16, 3, 46, tzinfo=UTC)  # 09:16 IST, the market is open
    w.kill.ensure_flattening()  # the monitor's next pass
    await w.finish_flatten()

    assert w.flat()


async def test_an_exit_is_placed_under_a_key_with_its_date_instrument_episode_and_step() -> None:
    w = await world()
    w.hold(OPTION, 65)

    await w.flatten()

    [exit_] = w.orders()
    prefix = f"risk:flatten:{TODAY}:{OPTION}:{ProductType.INTRADAY.value}:"
    [key] = [k for (_, k), r in w.repo.results.items() if r.order_id == exit_.order_id]
    episode, step = key.removeprefix(prefix).split(":")
    assert key.startswith(prefix) and len(episode) == 8 and step == "1"


# ---- the kill switch -------------------------------------------------------------------------


async def test_the_kill_persists_halted_before_the_first_exit_goes_out() -> None:
    w = await world()
    w.hold(OPTION, 65)
    seen: list[HaltLevel | None] = []
    place = w.broker.place

    async def spy(order: Order) -> BrokerAck:
        stored = await w.risk.load_day("main", TODAY)
        seen.append(None if stored is None else stored.halt_level)
        return await place(order)

    w.broker.place = spy  # type: ignore[method-assign]

    refusal = await w.kill.trigger("floor breached", "risk")
    await w.finish_flatten()

    assert refusal is None and seen == [HaltLevel.KILL]  # stored before the exit was sent
    stored = await w.risk.load_day("main", TODAY)
    assert stored is not None and "floor breached" in (stored.halt_reason or "")
    assert w.flat()


async def test_a_kill_cancels_pending_entries_and_leaves_exits_alone() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.confirms_on = False
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    resting_exit = await w.service.place(command("exit", side=SELL, quantity=65, limit="120.00"))
    assert entry.accepted and resting_exit.accepted

    await w.kill.trigger("test", "cli")

    assert entry.order_id in w.broker.cancel_asked
    assert resting_exit.order_id not in w.broker.cancel_asked  # an exit is not an entry


async def test_a_kill_does_not_cancel_an_entry_the_broker_rejected_after_its_ack() -> None:
    """handle-late-broker-rejections task 1.3: an acknowledged entry (``Open``) the broker then
    rejects on its update stream is final, so the kill has nothing to cancel there; the entry
    still resting is cancelled as before."""
    w = await world()
    w.confirms_on = False
    w.market[OPTION] = D("90")
    rejected = await w.service.place(command("late", side=BUY, quantity=65, limit="85.00"))
    resting = await w.service.place(command("resting", side=BUY, quantity=65, limit="85.00"))
    late = w.service.orders[rejected.order_id or ""]
    assert late.state is OrderState.OPEN and late.broker_order_id  # acknowledged the normal way
    await w.service.on_update(
        Rejected(late.broker_order_id, "RMS: insufficient margin", w.clock.at)
    )
    assert w.service.orders[late.order_id].state is OrderState.REJECTED

    await w.kill.trigger("test", "cli")

    assert w.broker.cancel_asked == [resting.order_id]


async def test_a_second_kill_places_and_cancels_nothing() -> None:
    """Spec "Kill twice": the answer says the day is already stopped."""
    w = await world()
    w.hold(OPTION, 65)
    await w.kill.trigger("first", "cli")
    await w.finish_flatten()
    placed, cancelled = len(w.orders()), len(w.broker.cancel_asked)

    again = await w.kill.trigger("second", "cli")

    assert again is not None and "already stopped" in again and "first" in again
    assert (len(w.orders()), len(w.broker.cancel_asked)) == (placed, cancelled)


async def test_the_kill_command_answers_once_per_idempotency_key() -> None:
    w = await world()
    w.hold(OPTION, 65)
    cmd = KillCommand(
        command_id="k1", idempotency_key="kill-1", account_id="main", source="cli", sent_at=NOW
    )

    first = await w.kill.handle(cmd)
    await w.finish_flatten()
    second = await w.kill.handle(cmd)
    third = await w.kill.handle(cmd.model_copy(update={"idempotency_key": "kill-2"}))

    assert first.accepted and first.reason is None
    assert second == first  # the same key: the first answer, nothing else happens
    assert third.accepted and "already stopped" in (third.reason or "")  # a new key, day stopped


async def test_a_kill_command_is_answered_even_when_the_database_cannot_be_asked() -> None:
    w = await world()
    w.hold(OPTION, 65)

    async def down(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PersistenceError("db down")

    w.repo.find_result = down  # type: ignore[method-assign]
    w.repo.save_result = down  # type: ignore[method-assign]
    cmd = KillCommand(
        command_id="k1", idempotency_key="kill-1", account_id="main", source="cli", sent_at=NOW
    )

    result = await w.kill.handle(cmd)
    await w.finish_flatten()

    assert result.accepted and w.flat()  # the kill never waits for the database


async def test_the_kill_still_flattens_when_the_day_state_cannot_be_written() -> None:
    """Spec "Persistence down at a breach"."""
    w = await world()
    w.hold(OPTION, 65)

    async def down(state):  # type: ignore[no-untyped-def]
        raise PersistenceError("db down")

    w.risk.save_day = down  # type: ignore[method-assign]

    await w.kill.trigger("test", "risk")
    await w.finish_flatten()

    assert w.flat() and w.daybook.day().halt_level is HaltLevel.KILL
    assert w.daybook.dirty  # in memory, and the monitor's timer will write it when it can


async def test_a_kill_works_while_the_orders_database_is_down() -> None:
    """Task 10.1: ``OrderService.cancel`` must not fail closed on the idempotency lookup: an entry
    that cannot be cancelled during an outage is the exposure the kill exists to remove."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert entry.accepted

    async def down(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PersistenceError("db down")

    w.repo.find_result = down  # type: ignore[method-assign]
    w.repo.create_order = down  # type: ignore[method-assign]
    w.repo.update_order = down  # type: ignore[method-assign]
    w.repo.save_result = down  # type: ignore[method-assign]
    w.service.persistence_ok = False  # what the first failed write would have set
    w.market[OPTION] = D("100")  # the kill's exits are marketable

    await w.kill.trigger("test", "risk")
    await w.finish_flatten()

    assert entry.order_id in w.broker.cancel_asked  # the entry was cancelled despite the outage
    assert w.flat()


async def test_the_day_stays_locked_after_a_restart_and_the_flatten_resumes() -> None:
    """Spec "Crash mid-flatten" at unit level: the day row says KILL and a position is still open.
    After reconciliation the monitor calls ``ensure_flattening`` and the flatten starts again."""
    first = await world()
    first.hold(OPTION, 65)
    await first.kill.trigger("floor breached", "risk")
    dying = first.kill.flattening
    assert dying is not None
    dying.cancel()  # the process dies mid-flatten
    with pytest.raises(asyncio.CancelledError):
        await dying

    second = await World(NOW, risk=first.risk).ready()  # a new process, the same database
    second.hold(OPTION, 65)  # the broker still shows the position after reconciliation

    assert second.daybook.day().halt_level is HaltLevel.KILL  # the lock survived the restart
    assert "floor breached" in (second.daybook.day().halt_reason or "")
    assert second.kill.flattening is None  # nothing starts by itself ...
    second.kill.ensure_flattening()  # ... until the monitor's pass asks
    await second.finish_flatten()

    assert second.flat()


async def test_nothing_is_flattened_on_an_unlocked_day() -> None:
    w = await world()
    w.hold(OPTION, 65)

    w.kill.ensure_flattening()

    assert w.kill.flattening is None and not w.flat()


async def test_a_kill_requested_from_inside_the_order_service_lock_does_not_deadlock() -> None:
    """The gate runs inside the service's lock and the kill's cancels need it: ``request`` only
    schedules, and the kill completes once the lock is free."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert entry.order_id

    async with w.service._lock:  # what the gate holds while it decides
        w.kill.request("pre-trade KILL", "risk")  # returns at once
        for _ in range(3):
            await asyncio.sleep(0)
        assert entry.order_id not in w.broker.cancel_asked  # waiting for the lock, not hung
    await until(lambda: entry.order_id in w.broker.cancel_asked)

    assert w.daybook.day().halt_level is HaltLevel.KILL


async def test_a_requested_kill_that_fails_is_logged_and_forgotten() -> None:
    w = await world()

    async def broken(reason: str, source: str) -> str | None:
        raise RuntimeError("kill bug")

    w.kill.trigger = broken  # type: ignore[method-assign]
    with capture_logs() as logs:
        w.kill.request("x", "risk")
        await until(lambda: not w.kill._requests)  # type: ignore[attr-defined]

    assert any("a requested kill failed" in e["event"] for e in logs)


async def test_a_blind_book_lock_starts_the_flatten_too() -> None:
    """The monitor's ``ensure_flat`` is the same call for a blind-book lock as for a kill."""
    w = await world()
    w.hold(OPTION, 65)
    w.daybook.raise_halt(HaltLevel.MTM_BLIND, "book is blind")

    w.kill.ensure_flattening()
    await w.finish_flatten()

    assert w.flat()


async def test_a_kill_with_a_flat_book_starts_no_flatten() -> None:
    w = await world()

    refusal = await w.kill.trigger("test", "cli")

    assert refusal is None and w.kill.flattening is None
    assert w.daybook.day().halt_level is HaltLevel.KILL  # the day is locked all the same


async def test_ensure_flattening_before_the_day_is_loaded_does_nothing() -> None:
    w = World()  # not ready: the day book has not been loaded
    w.hold(OPTION, 65)

    w.kill.ensure_flattening()

    assert w.kill.flattening is None


async def test_ensure_flattening_does_not_start_a_second_flatten() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    await w.kill.trigger("test", "cli")
    first = w.kill.flattening

    for _ in range(3):
        w.kill.ensure_flattening()  # the monitor asks every pass

    assert w.kill.flattening is first
    w.market[OPTION] = D("100")
    await w.finish_flatten()


async def test_a_flatten_that_crashed_is_started_again() -> None:
    w = await world()
    w.hold(OPTION, 65)
    boom = RuntimeError("flattener bug")

    async def crash(reason: str, products=None, kind: str = "kill") -> None:  # type: ignore[no-untyped-def]
        raise boom

    real = w.kill._flattener  # type: ignore[attr-defined]
    w.kill._flattener = type("Crash", (), {"flatten": staticmethod(crash)})()  # type: ignore[attr-defined]
    await w.kill.trigger("test", "cli")
    with pytest.raises(RuntimeError):
        await w.finish_flatten()

    w.kill._flattener = real  # type: ignore[attr-defined]
    with capture_logs() as logs:
        w.kill.ensure_flattening()  # the monitor's next pass
    await w.finish_flatten()

    assert any("flatten task failed" in e["event"] for e in logs) and w.flat()


async def test_resume_cannot_clear_the_day_lock() -> None:
    """Spec "Resume does not help": the lock lives in the day book, not in the recovery state, so
    the recovery resume command (which clears only a reconciliation halt) cannot touch it."""
    w = await world()
    await w.kill.trigger("test", "cli")
    # The engine is ALSO in a reconciliation halt (the thing the resume command is for).
    await w.repo.store_halt("main", ["seeded difference"], NOW)
    w.service.reconciled, w.service.halt_differences = False, ["seeded difference"]
    resume = ResumeRecovery(
        command_id="r1", idempotency_key="resume-1", account_id="main", source="cli", sent_at=NOW
    )

    answer = await resume_recovery(w.service, w.repo, w.broker, resume, account_id="main")

    assert answer.accepted and w.service.reconciled  # the reconciliation halt is cleared ...
    assert w.daybook.day().halt_level is HaltLevel.KILL  # ... and the day lock is not
    stored = await w.risk.load_day("main", TODAY)
    assert stored is not None and stored.halt_level is HaltLevel.KILL


async def target_halted(w: World) -> None:
    """Lock the day at TARGET_HALT the way the monitor does (add-risk-guards task 9.2)."""
    w.daybook.raise_halt(HaltLevel.TARGET_HALT, "RISK_TARGET_HALT: banked")
    assert await w.daybook.flush()


async def test_resume_cannot_clear_a_target_halt() -> None:
    """profit-target "The target halt holds for the rest of the day": the recovery resume command
    clears a reconciliation halt and leaves the day's TARGET_HALT, in memory and stored."""
    w = await world()
    await target_halted(w)
    await w.repo.store_halt("main", ["seeded difference"], NOW)
    w.service.reconciled, w.service.halt_differences = False, ["seeded difference"]
    resume = ResumeRecovery(
        command_id="r1", idempotency_key="resume-1", account_id="main", source="cli", sent_at=NOW
    )

    answer = await resume_recovery(w.service, w.repo, w.broker, resume, account_id="main")

    assert answer.accepted and w.service.reconciled  # the reconciliation halt is cleared ...
    assert w.daybook.day().halt_level is HaltLevel.TARGET_HALT  # ... and the day lock is not
    stored = await w.risk.load_day("main", TODAY)
    assert stored is not None and stored.halt_level is HaltLevel.TARGET_HALT


async def test_a_kill_outranks_the_target() -> None:
    """Spec "A kill outranks the target": on a day locked at TARGET_HALT a kill is not "already
    stopped" (that is only at KILL): it raises the day to KILL, stored, and keeps flattening."""
    w = await world()
    w.hold(OPTION, 65)
    await target_halted(w)

    refusal = await w.kill.trigger("floor breached", "risk")
    await w.finish_flatten()

    assert refusal is None  # this call did it
    assert w.daybook.day().halt_level is HaltLevel.KILL
    assert "floor breached" in (w.daybook.day().halt_reason or "")
    stored = await w.risk.load_day("main", TODAY)
    assert stored is not None and stored.halt_level is HaltLevel.KILL
    assert w.flat()


async def test_a_target_halt_starts_the_flatten_and_cancels_resting_entries() -> None:
    """The monitor's ``ensure_flat`` is the same call for a target halt as for a kill: the day is
    locked (TARGET_HALT is above MTM_BLIND), so a resting entry is cancelled and the book is
    flattened."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")  # the entry below rests
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert entry.order_id
    w.market[OPTION] = D("100")
    await target_halted(w)

    await w.kill.ensure()
    await w.finish_flatten()

    assert entry.order_id in w.broker.cancel_asked
    assert w.flat() and w.daybook.day().halt_level is HaltLevel.TARGET_HALT


async def test_next_day_the_lock_is_gone() -> None:
    """Spec "Next day": the new IST date has a fresh row; nothing was cleared in between."""
    w = await world()
    await w.kill.trigger("test", "cli")
    w.clock.at = TOMORROW

    assert await w.daybook.roll_over_if_new_day()

    assert w.daybook.day().halt_level is HaltLevel.NONE
    assert w.daybook.day().trade_date == TODAY + timedelta(days=1)


async def test_the_kill_places_only_ordinary_orders_no_broker_lock_and_no_exit_all() -> None:
    """Spec "No broker lock": the engine's kill is its own cancels and exit orders. The broker
    adapter has no lock or Exit All method for it to call (they are optional capabilities that
    stage 7 adds), and every order it places is a plain limit exit."""
    w = await world()
    w.hold(OPTION, 65)

    await w.kill.trigger("test", "cli")
    await w.finish_flatten()

    assert [o.order_type for o in w.broker.placed] == [OrderType.LIMIT]
    # The broker interface has no lock or Exit All call to make (they are optional capabilities
    # that stage 7 adds): only these four.
    assert {name for name in vars(BrokerAdapter) if not name.startswith("_")} == {
        "place", "cancel", "order_book", "updates",
    }  # fmt: skip


# ---- the floor breach reaches the kill with no client ----------------------------------------


async def test_a_floor_breach_kills_and_flattens_with_no_client_connected() -> None:
    """Spec "Floor breach kills without any client": the monitor calls the kill switch directly,
    so no API, Redis consumer or dashboard is involved."""
    w = await world()
    w.hold(OPTION, 65, average="100.00")
    audit = RiskAudit(w.risk)
    monitor = RiskMonitor(
        account_id="main", limits=w.limits, daybook=w.daybook, repository=w.risk, audit=audit,
        instruments=instrument_of, checks=[FloorBreach()], subscribed=lambda: True,
        interval_s=1.0, persist_interval_s=10.0,
        on_kill=lambda d: w.kill.on_kill(d.reason() or FLOOR_BREACH),
        ensure_flat=lambda reason: _ensure(w.kill), now=w.clock,
    )  # fmt: skip
    monitor.bind(w.service)
    await monitor.step()
    monitor.on_tick(Tick(OPTION, D("101"), exchange_time=w.clock.at, received_at=w.clock.at))
    await monitor.step()
    w.clock.at += timedelta(seconds=1)
    monitor.on_tick(Tick(OPTION, D("53"), exchange_time=w.clock.at, received_at=w.clock.at))
    w.prices[OPTION] = D("53")
    w.market[OPTION] = D("53")

    await monitor.step()  # -3,055 against a floor of -3,000
    await w.finish_flatten()
    await audit.drain()

    assert w.daybook.day().halt_level is HaltLevel.KILL and w.flat()
    assert any(e.reason_code == FLOOR_BREACH for e in w.risk.events)


async def _ensure(kill: KillSwitch) -> None:
    kill.ensure_flattening()


# ---- the own square-off ----------------------------------------------------------------------


async def test_the_square_off_waits_for_its_time() -> None:
    w = await world(BEFORE_SQUARE_OFF)
    w.hold(OPTION, 65)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)

    await square.maybe_run(w.clock.at)

    assert square.running is None and w.orders() == []


async def test_a_book_of_only_delivery_positions_is_not_squared_off() -> None:
    w = await world(SQUARE_OFF)
    w.hold(STOCK, 100, ProductType.CNC)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)

    await square.maybe_run(w.clock.at)

    assert square.running is None and w.orders() == []


async def test_the_square_off_starts_exactly_at_its_time() -> None:
    w = await world(SQUARE_OFF)
    w.hold(OPTION, 65)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)

    await square.maybe_run(SQUARE_OFF - timedelta(seconds=1))
    assert square.running is None
    await square.maybe_run(SQUARE_OFF)

    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)


async def test_the_square_off_exits_intraday_positions_and_leaves_delivery_alone() -> None:
    """Spec "Mixed book at square-off" and "Not a halt"."""
    w = await world(SQUARE_OFF)
    w.hold(OPTION, 65)
    w.hold(STOCK, 100, ProductType.CNC)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)

    await square.maybe_run(w.clock.at)
    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)

    held = {p.product: p.quantity for p in w.service.positions.values()}
    assert held == {ProductType.INTRADAY: 0, ProductType.CNC: 100}
    assert w.daybook.day().halt_level is HaltLevel.NONE  # the day stays open
    assert {o.source for o in w.orders()} == {"risk-squareoff"}


async def test_the_square_off_is_asked_every_pass_but_runs_one_flatten_at_a_time() -> None:
    w = await world(SQUARE_OFF)
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)

    await square.maybe_run(w.clock.at)
    first = square.running
    await square.maybe_run(w.clock.at)

    assert square.running is first
    w.market[OPTION] = D("100")
    assert first is not None
    await asyncio.wait_for(first, timeout=5)


async def test_a_late_intraday_position_is_squared_off_after_the_first_flatten_ended() -> None:
    w = await world(SQUARE_OFF)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)
    w.hold(OPTION, 65)
    await square.maybe_run(w.clock.at)
    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)

    w.hold(STOCK, 10)  # an entry that filled late
    await square.maybe_run(w.clock.at)
    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)

    assert w.flat()


async def test_there_is_no_square_off_outside_market_hours_or_without_limits() -> None:
    after_close = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)
    w = await world(after_close)
    w.hold(OPTION, 65)
    square = SquareOff(limits=w.limits, service=w.service, flattener=w.flattener, now=w.clock)
    await square.maybe_run(after_close)
    assert square.running is None

    w.limits._current = None  # type: ignore[attr-defined]
    await square.maybe_run(SQUARE_OFF)
    assert square.running is None


# ---- failing cancels, fresh chases and adoption (review round 1) ------------------------------


def failing_cancels(w: World, times: int) -> list[str]:
    """Make the broker refuse the next ``times`` cancels; return the list of cancel attempts."""
    attempts: list[str] = []
    real = w.broker.cancel

    async def flaky(order: Order) -> None:
        attempts.append(order.order_id)
        if len(attempts) <= times:
            raise BrokerError("the broker cannot cancel just now")
        await real(order)

    w.broker.cancel = flaky  # type: ignore[method-assign]
    return attempts


async def test_a_cancel_the_broker_refused_is_asked_again_and_the_flatten_completes() -> None:
    """The first cancel fails. A fixed key would return that stored refusal for ever and leave the
    exit resting at a stale price; each attempt has a fresh key, so the retry reaches the broker."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")  # the exit rests
    attempts = failing_cancels(w, 1)
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: len(attempts) >= 2)  # asked again after the first refusal
    w.market[OPTION] = D("100")  # so the replacement can fill
    await asyncio.wait_for(task, timeout=5)

    assert w.flat() and w.orders()[0].state is OrderState.CANCELLED


async def test_refused_cancels_do_not_use_up_the_chase_steps() -> None:
    """Only a re-price that happened counts toward ``max_chase_steps``, not every cancel tried."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    attempts = failing_cancels(w, 2 * STEPS)  # twice the step budget of refusals
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: len(attempts) > 2 * STEPS)  # the cancel finally got through
    w.market[OPTION] = D("100")
    await asyncio.wait_for(task, timeout=10)

    assert w.flat() and len(w.orders()) == 2  # and the exit WAS replaced, then filled


async def test_an_exit_that_is_unknown_for_a_while_can_be_cancelled_once_it_is_open() -> None:
    """A cancel refused because the order was not open yet (``Unknown`` after a timeout) must not
    be stored for good: when reconciliation makes the order open, the next attempt works."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    exit_id = w.orders()[0].order_id
    w.service.orders[exit_id] = w.orders()[0].model_copy(update={"state": OrderState.UNKNOWN})

    await until(lambda: w.slept >= 3 * CHASE_S)  # refused every interval while it is Unknown
    assert exit_id not in w.broker.cancel_asked
    w.service.orders[exit_id] = w.orders()[0].model_copy(update={"state": OrderState.OPEN})
    await until(lambda: exit_id in w.broker.cancel_asked)  # reconciled: the cancel goes through
    w.market[OPTION] = D("100")
    await asyncio.wait_for(task, timeout=5)

    assert w.flat()


async def test_a_second_flatten_of_a_position_starts_a_fresh_chase_with_its_own_keys() -> None:
    """A square-off, then later a kill, for the same position: the second must place its own exit
    (a repeated key would return the first order's stored answer and place nothing)."""
    w = await world()
    w.hold(OPTION, 65)
    await w.flatten()
    assert w.flat() and len(w.orders()) == 1
    assert w.flattener._chases == {}  # type: ignore[attr-defined]  # the finished chase is forgotten

    w.hold(OPTION, 65)  # a new position in the same instrument
    await w.flatten()

    assert w.flat() and len(w.orders()) == 2
    keys = [k for (_, k) in w.repo.results if k.startswith("risk:flatten:")]
    assert len(keys) == len(set(keys)) == 2


async def test_a_flatten_adopts_the_resting_exit_of_a_flatten_that_was_cut_short() -> None:
    """After a restart (or a square-off that ran first) a new flattener finds the engine's own
    resting exit and works it, instead of placing a second: two exits that both filled would flip
    the position."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    first = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    first.cancel()  # the process dies mid-flatten
    with pytest.raises(asyncio.CancelledError):
        await first
    second_flattener = ChaseFlattener(
        account_id="main", service=w.service, limits=w.limits, quote=w.quote,
        instruments=instrument_of, now=w.clock, sleep=w.sleep, poll_s=0.5,
    )  # fmt: skip

    second = asyncio.create_task(second_flattener.flatten("test"))
    slept_before = w.slept
    await until(lambda: w.slept >= slept_before + 1)
    assert len(w.orders()) == 1  # adopted, not duplicated
    w.market[OPTION] = D("100")  # the adopted exit becomes marketable
    await asyncio.wait_for(second, timeout=5)

    assert w.flat() and len(w.orders()) == 1 and w.orders()[0].filled_quantity == 65


async def test_extra_resting_exits_of_our_own_are_cancelled_when_one_is_adopted() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    placed = []
    for n in range(2):
        placed.append(
            await w.service.place(
                PlaceOrder(
                    command_id=f"x{n}",
                    idempotency_key=f"x{n}",
                    account_id="main",
                    source="risk-kill",
                    sent_at=NOW,
                    key=OPTION,
                    side=SELL,
                    quantity=65,
                    product=ProductType.INTRADAY,
                    order_type=OrderType.LIMIT,
                    limit_price=D("120.00"),
                )
            )  # fmt: skip
        )
        w.clock.at += timedelta(seconds=1)  # so one is older than the other
    assert all(r.accepted for r in placed)
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: bool(w.broker.cancel_asked))
    w.market[OPTION] = D("125")  # the adopted (older) one is marketable
    await asyncio.wait_for(task, timeout=5)

    assert w.broker.cancel_asked[0] == placed[1].order_id  # the newer duplicate went first
    assert w.flat()


async def test_the_kill_asked_while_todays_state_is_unavailable_still_cancels_and_flattens() -> (
    None
):
    """The database was down at start: the day is not loaded. A kill must not wait for it."""
    w = World()
    await w.limits.set_limits(
        SetRiskLimits(
            command_id="c", idempotency_key="k", account_id="main", source="t", sent_at=NOW,
            limits=BASE,
        )
    )  # fmt: skip
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert w.daybook.unavailable(w.clock.at) is not None  # not loaded

    refusal = await w.kill.trigger("floor breached", "risk")
    await until(lambda: entry.order_id in w.broker.cancel_asked)  # the entry is cancelled
    assert refusal is None and w.kill.flattening is not None  # and the flatten runs

    again = await w.kill.trigger("second", "cli")  # asked twice: nothing more is started
    assert again is not None and "already stopped" in again

    assert await w.daybook.load()  # the database is back: the day loads ...
    await w.kill.ensure()  # ... and the monitor's next pass records the lock on it
    assert w.daybook.day().halt_level is HaltLevel.KILL
    stored = await w.risk.load_day("main", TODAY)  # written at once, not at the next persist
    assert stored is not None and stored.halt_level is HaltLevel.KILL
    assert "floor breached" in (w.daybook.day().halt_reason or "")
    w.market[OPTION] = D("100")
    await w.finish_flatten()
    assert w.flat()


async def test_a_kill_with_the_market_closed_is_loud_and_says_so_in_its_answer() -> None:
    after_close = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)  # 16:00 IST
    w = await world(after_close)
    w.hold(OPTION, 65)
    cmd = KillCommand(
        command_id="k1", idempotency_key="kill-1", account_id="main", source="cli", sent_at=NOW
    )

    with capture_logs() as logs:
        result = await w.kill.handle(cmd)

    assert result.accepted and "market is closed" in (result.reason or "")
    assert any(e["log_level"] == "critical" and "market closed" in e["event"] for e in logs)
    assert w.daybook.day().halt_level is HaltLevel.KILL  # the day is locked all the same
    assert w.orders() == []  # and nothing was sent


async def test_the_kill_persists_halted_before_it_cancels_entries() -> None:
    w = await world()
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    assert entry.accepted
    seen: list[HaltLevel | None] = []
    cancel = w.broker.cancel

    async def spy(order: Order) -> None:
        stored = await w.risk.load_day("main", TODAY)
        seen.append(None if stored is None else stored.halt_level)
        await cancel(order)

    w.broker.cancel = spy  # type: ignore[method-assign]

    await w.kill.trigger("test", "cli")

    assert seen == [HaltLevel.KILL]  # stored before the first cancel went out


async def test_a_second_kill_restarts_a_flatten_that_was_cut_short() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    await w.kill.trigger("first", "cli")
    dying = w.kill.flattening
    assert dying is not None
    dying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dying

    again = await w.kill.trigger("second", "cli")  # "already stopped", but the flatten restarts

    assert again is not None and "already stopped" in again
    assert w.kill.flattening is not None and w.kill.flattening is not dying
    w.market[OPTION] = D("100")
    await w.finish_flatten()


async def test_an_entry_whose_cancel_was_refused_is_asked_again_by_the_next_pass() -> None:
    w = await world()
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    attempts = failing_cancels(w, 1)

    with capture_logs() as logs:
        await w.kill.trigger("test", "cli")  # the first cancel is refused
    assert attempts == [entry.order_id]
    assert any("could not cancel an entry" in e["event"] for e in logs)

    w.clock.at += timedelta(seconds=3)  # past the short wait after a refusal
    await w.kill.ensure()  # the monitor's next pass

    assert attempts == [entry.order_id, entry.order_id]  # asked again, and this time it went


async def test_a_resting_entry_left_on_a_locked_day_is_cancelled_by_the_monitors_pass() -> None:
    """The lock was stored but the process died before the cancels went out: on restart the
    day is locked and the entry still rests. The pass finds it and cancels it, once (not on
    every pass)."""
    w = await world()
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    w.daybook.raise_halt(HaltLevel.KILL, "from before the restart")
    attempts = failing_cancels(w, 0)

    for _ in range(3):
        await w.kill.ensure()

    assert attempts == [entry.order_id]  # asked once; the retry waits out ENTRY_CANCEL_RETRY_S


async def test_a_refused_cancel_is_asked_again_after_the_chase_interval_not_on_every_look() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    attempts = failing_cancels(w, 1000)  # the broker refuses every cancel
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: w.slept >= 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert 2 <= len(attempts) <= 6  # about one every 2 s over 10 s, not one every half second


# ---- round 2 of the review: the pending kill, retried extras, backoff, sources -----------------


async def test_a_kill_taken_while_the_day_cannot_be_read_is_recorded_by_the_monitors_own_pass() -> (
    None
):
    """The production path, not a direct call: the monitor loads the day itself and, because a kill
    is pending, asks ``ensure`` even though the freshly loaded day is not locked."""
    w = World()
    await w.limits.set_limits(
        SetRiskLimits(
            command_id="c", idempotency_key="k", account_id="main", source="t", sent_at=NOW,
            limits=BASE,
        )
    )  # fmt: skip
    w.hold(OPTION, 65)
    await w.kill.trigger("floor breached", "risk")  # the day is not loaded: the kill is pending
    assert w.kill.pending
    monitor = RiskMonitor(
        account_id="main", limits=w.limits, daybook=w.daybook, repository=w.risk,
        audit=RiskAudit(w.risk), instruments=instrument_of, checks=[], subscribed=lambda: True,
        interval_s=1.0, persist_interval_s=10.0, ensure_flat=lambda reason: w.kill.ensure(),
        lock_pending=lambda: w.kill.pending, now=w.clock,
    )  # fmt: skip
    monitor.bind(w.service)

    await monitor.step()  # loads the day, then records the kill on it

    assert w.daybook.day().halt_level is HaltLevel.KILL
    assert "floor breached" in (w.daybook.day().halt_reason or "")
    assert not w.kill.pending
    await w.daybook.flush()
    stored = await w.risk.load_day("main", TODAY)
    assert stored is not None and stored.halt_level is HaltLevel.KILL  # and it is stored


async def test_an_extra_exit_the_broker_refused_to_cancel_is_asked_again() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    placed = []
    for n in range(2):
        placed.append(
            await w.service.place(
                PlaceOrder(
                    command_id=f"x{n}",
                    idempotency_key=f"x{n}",
                    account_id="main",
                    source="risk-kill",
                    sent_at=NOW,
                    key=OPTION,
                    side=SELL,
                    quantity=65,
                    product=ProductType.INTRADAY,
                    order_type=OrderType.LIMIT,
                    limit_price=D("120.00"),
                )
            )  # fmt: skip
        )
        w.clock.at += timedelta(seconds=1)
    attempts = failing_cancels(w, 1)  # the first cancel, of the newer duplicate, is refused
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: placed[1].order_id in w.broker.cancel_asked)  # asked again, and taken
    w.market[OPTION] = D("125")
    await asyncio.wait_for(task, timeout=5)

    assert attempts.count(placed[1].order_id) == 2  # refused once, then taken
    assert w.flat()


async def test_a_resting_exit_from_another_source_is_neither_adopted_nor_cancelled() -> None:
    """The owner's own take-profit: the flatten places its own exit beside it and leaves it be
    (design Risks: an owner decision whether the kill should cancel it)."""
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    manual = await w.service.place(
        command("take-profit", side=SELL, quantity=65, limit="120.00")  # source "cli"
    )
    assert manual.accepted
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: len(w.orders(source="risk-kill")) == 1)  # its own exit went out
    w.market[OPTION] = D("100")
    await asyncio.wait_for(task, timeout=5)

    assert manual.order_id not in w.broker.cancel_asked  # not cancelled
    assert w.flat()


async def test_an_entry_cancel_the_broker_keeps_refusing_is_not_asked_on_every_pass() -> None:
    w = await world()
    w.market[OPTION] = D("90")
    entry = await w.service.place(command("entry", side=BUY, quantity=65, limit="85.00"))
    attempts = failing_cancels(w, 1000)
    w.daybook.raise_halt(HaltLevel.KILL, "locked")

    for _ in range(5):  # five monitor passes within the same second
        await w.kill.ensure()
    assert attempts == [entry.order_id]  # asked once, then it waits

    w.clock.at += timedelta(seconds=3)
    await w.kill.ensure()

    assert attempts == [entry.order_id, entry.order_id]  # and again after the short backoff


async def test_an_extra_exit_the_broker_keeps_refusing_is_not_asked_on_every_look() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    placed = []
    for n in range(2):
        placed.append(
            await w.service.place(
                PlaceOrder(
                    command_id=f"x{n}",
                    idempotency_key=f"x{n}",
                    account_id="main",
                    source="risk-kill",
                    sent_at=NOW,
                    key=OPTION,
                    side=SELL,
                    quantity=65,
                    product=ProductType.INTRADAY,
                    order_type=OrderType.LIMIT,
                    limit_price=D("120.00"),
                )
            )  # fmt: skip
        )
        w.clock.at += timedelta(seconds=1)
    attempts = failing_cancels(w, 1000)  # every cancel is refused
    task = asyncio.create_task(w.flattener.flatten("test"))

    slept_before = w.slept
    await until(lambda: w.slept >= slept_before + 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert 2 <= attempts.count(placed[1].order_id) <= 6  # about one per 2 s, not one per look


async def test_an_extra_exit_that_has_ended_is_no_longer_asked_to_cancel() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.market[OPTION] = D("90")
    placed = []
    for n in range(2):
        placed.append(
            await w.service.place(
                PlaceOrder(
                    command_id=f"x{n}",
                    idempotency_key=f"x{n}",
                    account_id="main",
                    source="risk-kill",
                    sent_at=NOW,
                    key=OPTION,
                    side=SELL,
                    quantity=65,
                    product=ProductType.INTRADAY,
                    order_type=OrderType.LIMIT,
                    limit_price=D("120.00"),
                )
            )  # fmt: skip
        )
        w.clock.at += timedelta(seconds=1)
    failing_cancels(w, 1000)  # every cancel is refused
    extra_id = placed[1].order_id or ""

    def asks() -> int:  # every ask is answered and stored under its own key, even a refusal
        prefix = f"risk:flatten-cancel:{extra_id}:"
        return sum(1 for (_, key) in w.repo.results if key.startswith(prefix))

    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: asks() == 1)  # asked once, refused

    ended = w.service.orders[extra_id].model_copy(update={"state": OrderState.CANCELLED})
    w.service.orders[extra_id] = ended  # the owner (or the broker) ended it meanwhile
    slept_before = w.slept
    await until(lambda: w.slept >= slept_before + 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert asks() == 1  # never asked again: there is nothing left to cancel


# ---- the positions filter and the expiry square-off (add-risk-guards task 8.3) ----------------

AT_1430 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)  # 14:30 IST: the expiry square-off time
AT_1510 = datetime(2026, 11, 16, 9, 40, tzinfo=UTC)  # 15:10 IST: the base square-off time
EXPIRING_KEY = InstrumentKey.parse("NSE_FNO:53721")
NEXT_WEEK_KEY = InstrumentKey.parse("NSE_FNO:53722")
UNKNOWN_KEY = InstrumentKey.parse("NSE_FNO:99999")  # no master record
KNOWN = {
    **INSTRUMENTS,
    EXPIRING_KEY: INSTRUMENTS[OPTION].model_copy(update={"key": EXPIRING_KEY, "expiry": TODAY}),
    NEXT_WEEK_KEY: INSTRUMENTS[OPTION].model_copy(
        update={"key": NEXT_WEEK_KEY, "expiry": TODAY + timedelta(days=7)}
    ),
}
EXPIRY_SQUARE_OFF = {
    **BASE, "square_off_time": "15:10", "square_off_time_expiry": "14:30",
    "entry_cutoff_expiry": "14:00",
}  # fmt: skip


def pid(key: InstrumentKey, product: ProductType = ProductType.INTRADAY) -> PositionId:
    return ("main", key, product)


class Recorder:
    """A flattener that records what it was asked to exit and returns when released."""

    def __init__(self) -> None:
        self.calls: list[frozenset[PositionId] | None] = []
        self.release = asyncio.Event()

    async def flatten(
        self,
        reason: str,
        products: frozenset[ProductType] | None = None,
        kind: str = "kill",
        positions: frozenset[PositionId] | None = None,
    ) -> None:
        assert products == frozenset({ProductType.INTRADAY}) and kind == "squareoff"
        self.calls.append(positions)
        await self.release.wait()


async def expiry_world(at: datetime, limits: dict | None = None) -> World:  # type: ignore[type-arg]
    """A world whose flattener and prices know the expiring and next week's contracts."""
    w = World(at)
    # The order service validates every exit against its own master: teach it the two contracts.
    w.service._instruments.replace(list(KNOWN.values()), date(2026, 10, 29))  # type: ignore[attr-defined]
    w.flattener = ChaseFlattener(
        account_id="main", service=w.service, limits=w.limits, quote=w.quote,
        instruments=KNOWN.get, now=w.clock, sleep=w.sleep, poll_s=0.5,
    )  # fmt: skip
    for key in (EXPIRING_KEY, NEXT_WEEK_KEY):
        w.prices[key] = w.market[key] = D("100")
    await w.limits.set_limits(
        SetRiskLimits(
            command_id="c", idempotency_key="k", account_id="main", source="t",
            sent_at=NOW, limits=limits or EXPIRY_SQUARE_OFF,
        )
    )  # fmt: skip
    assert await w.daybook.load()
    return w


async def recorded_square_off(
    at: datetime,
    *keys: InstrumentKey,
    limits: dict | None = None,  # type: ignore[type-arg]
) -> tuple[World, SquareOff, Recorder]:
    """A square-off over intraday positions in ``keys`` whose flattener only records."""
    w = await expiry_world(at, limits)
    for key in keys:
        w.hold(key, 65)
    recorder = Recorder()
    square = SquareOff(
        limits=w.limits, service=w.service, flattener=recorder, instruments=KNOWN.get,
        now=w.clock,
    )  # fmt: skip
    return w, square, recorder


async def ask(square: SquareOff, at: datetime) -> None:
    """One pass of the square-off, then a turn of the event loop so that a flatten it started
    begins (and the recorder sees the call)."""
    await square.maybe_run(at)
    await asyncio.sleep(0)


async def test_the_flattener_exits_only_the_positions_it_is_given() -> None:
    w = await world()
    w.hold(OPTION, 65)
    w.hold(STOCK, 100)  # intraday too: only the positions filter tells them apart

    await w.flatten(positions=frozenset({pid(OPTION)}), kind="squareoff")

    held = {p.key: p.quantity for p in w.service.positions.values()}
    assert held == {OPTION: 0, STOCK: 100}
    assert all(o.order_type is OrderType.LIMIT for o in w.orders())  # never a market order


async def test_expiry_square_off() -> None:
    """Spec "Expiry square-off": square-off 15:10, expiry square-off 14:30. At 14:30 only the
    intraday position in the contract expiring today is exited; the one in next week's contract
    stays open until 15:10. Every exit is a limit order from the square-off."""
    w = await expiry_world(AT_1430 - timedelta(minutes=1))
    w.hold(EXPIRING_KEY, 65)
    w.hold(NEXT_WEEK_KEY, 65)
    square = SquareOff(
        limits=w.limits, service=w.service, flattener=w.flattener, instruments=KNOWN.get,
        now=w.clock,
    )  # fmt: skip

    await square.maybe_run(w.clock.at)
    assert square.running is None  # 14:29: nothing is due yet
    w.clock.at = AT_1430
    await square.maybe_run(w.clock.at)
    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)

    held = {p.key: p.quantity for p in w.service.positions.values()}
    assert held == {EXPIRING_KEY: 0, NEXT_WEEK_KEY: 65}
    w.clock.at = AT_1510 - timedelta(seconds=1)
    await square.maybe_run(w.clock.at)
    assert square.running is None and all(o.key == EXPIRING_KEY for o in w.orders())

    w.clock.at = AT_1510
    await square.maybe_run(w.clock.at)
    assert square.running is not None
    await asyncio.wait_for(square.running, timeout=5)

    assert w.flat()
    assert {o.key for o in w.orders()} == {EXPIRING_KEY, NEXT_WEEK_KEY}
    assert {o.source for o in w.orders()} == {"risk-squareoff"}
    assert all(o.order_type is OrderType.LIMIT for o in w.orders())


async def test_a_position_with_no_master_record_is_squared_off_at_the_earlier_time() -> None:
    """Spec: an intraday position whose instrument has no master record is squared off at the
    earlier of the two times."""
    _, square, recorder = await recorded_square_off(
        AT_1430, EXPIRING_KEY, UNKNOWN_KEY, NEXT_WEEK_KEY
    )

    await ask(square, AT_1430)

    assert recorder.calls == [frozenset({pid(EXPIRING_KEY), pid(UNKNOWN_KEY)})]
    recorder.release.set()


async def test_a_slow_expiry_exit_does_not_hold_up_the_base_square_off() -> None:
    """The flatten started at 14:30 is still running at 15:10: next week's position gets a flatten
    of its own then, and no position is asked for twice."""
    _, square, recorder = await recorded_square_off(AT_1430, EXPIRING_KEY, NEXT_WEEK_KEY)

    await ask(square, AT_1430)
    await ask(square, AT_1510)
    await ask(square, AT_1510 + timedelta(seconds=1))

    assert recorder.calls == [frozenset({pid(EXPIRING_KEY)}), frozenset({pid(NEXT_WEEK_KEY)})]
    recorder.release.set()


async def test_without_an_expiry_square_off_every_intraday_position_goes_at_the_base_time() -> None:
    base_only = {**BASE, "square_off_time": "15:10"}
    _, square, recorder = await recorded_square_off(
        AT_1430, EXPIRING_KEY, UNKNOWN_KEY, NEXT_WEEK_KEY, limits=base_only
    )

    await ask(square, AT_1430)
    assert recorder.calls == []
    await ask(square, AT_1510)

    assert recorder.calls == [frozenset({pid(EXPIRING_KEY), pid(UNKNOWN_KEY), pid(NEXT_WEEK_KEY)})]
    recorder.release.set()


async def test_an_expiring_delivery_position_is_left_to_the_owner() -> None:
    """The square-off is for intraday positions only, expiring or not."""
    w, square, recorder = await recorded_square_off(AT_1430)
    w.hold(EXPIRING_KEY, 65, ProductType.CNC)

    await ask(square, AT_1430)

    assert recorder.calls == [] and square.running is None


async def test_a_square_off_built_without_the_master_treats_every_position_as_expiring() -> None:
    """The fail-safe default: with no instrument lookup every position gets the earlier time."""
    w, _, recorder = await recorded_square_off(AT_1430, EXPIRING_KEY, NEXT_WEEK_KEY)
    square = SquareOff(limits=w.limits, service=w.service, flattener=recorder, now=w.clock)

    await ask(square, AT_1430)

    assert recorder.calls == [frozenset({pid(EXPIRING_KEY), pid(NEXT_WEEK_KEY)})]
    recorder.release.set()
