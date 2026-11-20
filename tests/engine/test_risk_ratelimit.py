"""The order rate limiter (add-risk-engine task 8.1; order-rate-limiter spec).

Scenarios: "Burst of entries", "Entries exhaust their share", "Exit during an entry burst",
"Ten positions"; plus the per-minute window, the sliding of both windows, cancels, first-come
first-served pacing, and the integer rounding of the reserved share. Time is a fake: ``sleep``
moves the clock, so nothing here waits for real.
"""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from contracts.orders import Order, OrderSide, OrderState
from engine.broker.adapter import Admitting, BrokerAck, BrokerOrder, BrokerRejected, BrokerUpdate
from engine.positions.book import position_id
from engine.risk.ceilings import ORDERS_PER_MIN_CEILING, ORDERS_PER_S_CEILING
from engine.risk.ratelimit import RATE_LIMITED, RateLimitedAdapter, reserved_tokens
from tests.engine.risk_builders import OPTION, STOCK, order, position

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


class FakeTime:
    """A clock and a sleep that moves it. ``gate`` holds every sleeper until it is set (so a test
    can look at the limiter while an exit is waiting); ``early`` makes the FIRST sleep end after
    only half the time asked, as a timer with jitter might."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.gate: asyncio.Event | None = None
        self.early = False

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.gate is not None:
            await self.gate.wait()
        self.now += seconds / 2 if self.early and len(self.slept) == 1 else seconds
        await asyncio.sleep(0)  # let other waiters run, as a real sleep would


class FakeAdapter:
    def __init__(self, time: FakeTime) -> None:
        self._time = time
        self.placed: list[tuple[str, float]] = []  # (order id, fake time it was sent)
        self.cancelled: list[tuple[str, float]] = []
        self.calls: list[str] = []  # every place and cancel in the order it reached the broker

    async def place(self, order: Order) -> BrokerAck:
        self.placed.append((order.order_id, self._time.now))
        self.calls.append(order.order_id)
        return BrokerAck(f"b-{order.order_id}")

    async def cancel(self, order: Order) -> None:
        self.cancelled.append((order.order_id, self._time.now))
        self.calls.append(order.order_id)

    async def order_book(self) -> list[BrokerOrder]:
        return [BrokerOrder("b1", "e1", OrderState.OPEN, 0, None)]

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        return
        yield


def limiter(
    *,
    per_s: int = 3,
    per_min: int = 100,
    share: float = 0.4,
    held: list | None = None,  # type: ignore[type-arg]
) -> tuple[RateLimitedAdapter, FakeAdapter, FakeTime]:
    time = FakeTime()
    inner = FakeAdapter(time)
    book = {position_id(p): p for p in held or []}
    return (
        RateLimitedAdapter(
            inner,
            per_s=per_s,
            per_min=per_min,
            exit_share=share,
            positions=lambda: book,
            clock=time.clock,
            sleep=time.sleep,
        ),
        inner,
        time,
    )


def entry(n: int) -> Order:
    return order(BUY, 65, order_id=f"e{n}")


def exit_(n: int, key=OPTION) -> Order:  # type: ignore[no-untyped-def]
    return order(SELL, 65, order_id=f"x{n}", key=key)


# ---- the reserved share ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "share", "reserved"),
    [
        (3, 0.4, 1),
        (5, 0.4, 2),
        (200, 0.4, 80),
        (120, 0.4, 48),
        (10, 0.29, 2),
        (5, 0.99, 4),
        # The clamp: a share that rounds to nothing still keeps one token for exits, and one that
        # rounds to everything still leaves one for entries.
        (2, 0.4, 1),
        (200, 0.004, 1),
        (3, 0.999, 2),
        (200, 0.999, 199),
    ],
)
def test_the_reserved_share_is_rounded_down_and_kept_between_one_and_all_but_one(
    cap: int, share: float, reserved: int
) -> None:
    assert reserved_tokens(cap, share) == reserved
    assert 1 <= reserved <= cap - 1  # exits always have a token of their own, and so do entries


def test_a_window_of_one_token_cannot_reserve_a_share() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        reserved_tokens(1, 0.4)


# ---- entries ---------------------------------------------------------------------------------


async def test_burst_of_entries() -> None:
    """Spec "Burst of entries": more entries in one second than the cap allows. With 3 per second
    and 1 reserved, entries get 2; the rest are refused and not sent."""
    adapter, inner, _ = limiter(per_s=3)

    sent, refused = 0, 0
    for n in range(5):
        try:
            await adapter.place(entry(n))
            sent += 1
        except BrokerRejected as error:
            refused += 1
            assert str(error).startswith(RATE_LIMITED)

    assert (sent, refused) == (2, 3) and len(inner.placed) == 2


async def test_entries_never_wait() -> None:
    adapter, _, time = limiter(per_s=3)
    await adapter.place(entry(0))
    await adapter.place(entry(1))

    with pytest.raises(BrokerRejected):
        await adapter.place(entry(2))

    assert time.slept == []  # refused at once, not delayed


async def test_the_window_slides_so_entries_flow_again_a_second_later() -> None:
    adapter, inner, time = limiter(per_s=3)
    await adapter.place(entry(0))
    await adapter.place(entry(1))
    with pytest.raises(BrokerRejected):
        await adapter.place(entry(2))

    time.now += 1.0
    await adapter.place(entry(3))

    assert [i for i, _ in inner.placed] == ["e0", "e1", "e3"]


async def test_the_per_minute_window_limits_entries_too() -> None:
    adapter, inner, time = limiter(per_s=5, per_min=4)  # reserved 1 per minute: entries get 3
    for n in range(3):
        await adapter.place(entry(n))
        time.now += 2.0  # well apart: the per-second window is never the limit
    with pytest.raises(BrokerRejected, match=RATE_LIMITED):
        await adapter.place(entry(3))

    assert len(inner.placed) == 3
    time.now += 60.0
    await adapter.place(entry(4))  # the minute has passed
    assert len(inner.placed) == 4


async def test_a_flip_is_an_entry_to_the_limiter() -> None:
    adapter, _, _ = limiter(per_s=3, held=[position(quantity=65)])
    await adapter.place(entry(0))
    await adapter.place(entry(1))

    with pytest.raises(BrokerRejected, match=RATE_LIMITED):
        await adapter.place(order(SELL, 130, order_id="flip"))  # closes 65, opens 65: an entry


# ---- exits and cancels -----------------------------------------------------------------------


async def test_entries_exhaust_their_share_and_an_exit_still_goes() -> None:
    """Specs "Entries exhaust their share" and "Exit during an entry burst"."""
    adapter, inner, time = limiter(per_s=3, held=[position(quantity=65)])
    await adapter.place(entry(0))
    await adapter.place(entry(1))
    with pytest.raises(BrokerRejected, match=RATE_LIMITED):
        await adapter.place(entry(2))  # one more entry: refused

    await adapter.place(exit_(0))  # the exit takes the reserved token

    assert [i for i, _ in inner.placed] == ["e0", "e1", "x0"] and time.slept == []


async def test_an_exit_with_no_token_waits_for_one_and_is_not_dropped() -> None:
    adapter, inner, time = limiter(per_s=3, held=[position(quantity=65 * 4)])
    for n in range(3):
        await adapter.place(exit_(n))  # uses all three tokens of the second, 0.3 s apart
        time.now += 0.3

    await adapter.place(exit_(3))  # the fourth finds none

    assert [i for i, _ in inner.placed] == ["x0", "x1", "x2", "x3"]
    # It waits for the FIRST send to age out of the window (t = 1000 + 1), not the last one.
    assert inner.placed[3][1] == pytest.approx(1001.0)
    assert time.slept == [pytest.approx(0.1)]  # 1.0 s window less the 0.9 s already elapsed


async def test_ten_positions_are_flattened_in_order_over_more_than_a_second() -> None:
    """Spec "Ten positions": a kill flattens ten positions against a cap of three per second."""
    keys = [
        position(key=type(OPTION).parse(f"NSE_FNO:{50000 + n}"), quantity=65) for n in range(10)
    ]
    adapter, inner, time = limiter(per_s=3, held=keys)
    exits = [order(SELL, 65, order_id=f"x{n}", key=keys[n].key) for n in range(10)]

    await asyncio.gather(*(adapter.place(o) for o in exits))

    assert [i for i, _ in inner.placed] == [f"x{n}" for n in range(10)]  # all ten, first come first
    sent_at = [t for _, t in inner.placed]
    assert sent_at[-1] - sent_at[0] > 1.0  # spread over more than a second
    assert (
        max(sum(1 for t in sent_at if s <= t < s + 1) for s in sent_at) <= 3
    )  # never above the cap
    assert time.slept  # it paced rather than sent a burst


async def test_a_cancel_has_exit_priority_and_waits_rather_than_drops() -> None:
    adapter, inner, time = limiter(per_s=3)
    for n in range(3):
        await adapter.cancel(order(BUY, 65, order_id=f"c{n}"))  # all three tokens, as cancels

    await adapter.cancel(order(BUY, 65, order_id="c3"))

    assert [i for i, _ in inner.cancelled] == ["c0", "c1", "c2", "c3"]
    assert time.slept == [pytest.approx(1.0)]


async def test_cancels_and_exits_share_one_first_come_first_served_queue() -> None:
    adapter, inner, _ = limiter(
        per_s=2, held=[position(quantity=65 * 3)]
    )  # reserved 1, but exits and cancels may use any token
    first = asyncio.create_task(adapter.place(exit_(0)))
    second = asyncio.create_task(adapter.cancel(order(BUY, 65, order_id="c1")))
    third = asyncio.create_task(adapter.place(exit_(2)))
    fourth = asyncio.create_task(adapter.cancel(order(BUY, 65, order_id="c3")))

    await asyncio.gather(first, second, third, fourth)

    assert inner.calls == ["x0", "c1", "x2", "c3"]  # the order they reached the broker


async def test_the_per_minute_cap_paces_exits_too() -> None:
    adapter, inner, time = limiter(per_s=5, per_min=3, held=[position(quantity=65 * 4)])
    for n in range(3):
        await adapter.place(exit_(n))
        time.now += 5.0

    await adapter.place(exit_(3))  # a fourth within the minute must wait for the first to age out

    assert inner.placed[3][1] - inner.placed[0][1] == pytest.approx(60.0)  # not the last + 60


# ---- reads, construction and the ceiling -----------------------------------------------------


async def test_reads_pass_straight_through_and_take_no_token() -> None:
    adapter, inner, time = limiter(per_s=2, held=[position(quantity=65 * 3)])
    await adapter.place(exit_(0))
    await adapter.place(exit_(1))  # every token of the second is now used

    books = [await adapter.order_book() for _ in range(3)]
    updates = [u async for u in adapter.updates()]

    assert all([b.broker_order_id for b in book] == ["b1"] for book in books) and updates == []
    assert time.slept == []  # a read that took a token would have had to wait for one
    assert len(inner.placed) == 2


async def test_an_entry_never_waits_behind_an_exit_that_is_waiting() -> None:
    """The exit holds the turn while it sleeps for a token; an entry must be refused at once, not
    queue behind it."""
    adapter, inner, time = limiter(per_s=2, held=[position(quantity=65 * 3)])
    await adapter.place(exit_(0))
    await adapter.place(exit_(1))
    time.gate = asyncio.Event()  # the next exit sleeps until the test lets it
    waiting = asyncio.create_task(adapter.place(exit_(2)))
    for _ in range(100):
        if time.slept:
            break
        await asyncio.sleep(0)  # let it reach its sleep
    assert time.slept

    with pytest.raises(BrokerRejected, match=RATE_LIMITED):
        await asyncio.wait_for(adapter.place(entry(0)), timeout=1)  # refused, promptly

    time.gate.set()
    await waiting
    assert [i for i, _ in inner.placed] == ["x0", "x1", "x2"]


async def test_a_timer_that_wakes_early_does_not_let_an_exit_through_early() -> None:
    """The wait is re-checked after the sleep: the cap holds even if the sleep returns early."""
    adapter, inner, time = limiter(per_s=2, held=[position(quantity=65 * 3)])
    await adapter.place(exit_(0))
    await adapter.place(exit_(1))
    time.early = True  # each sleep ends after half the time asked

    await adapter.place(exit_(2))

    assert inner.placed[2][1] >= 1000.0 + 1.0  # never earlier than a full second after the first
    assert len(time.slept) == 2  # it slept again for the rest instead of sending early


@pytest.mark.parametrize(
    ("per_s", "per_min", "share"),
    [
        (ORDERS_PER_S_CEILING + 1, 100, 0.4),  # above the per-second ceiling
        (10, 100, 0.4),  # SEBI's threshold itself
        (0, 100, 0.4),
        (1, 100, 0.4),  # one token cannot be both reserved for exits and left for entries
        (3, ORDERS_PER_MIN_CEILING + 1, 0.4),
        (3, 0, 0.4),
        (3, 1, 0.4),
        (3, 100, 0.0),
        (3, 100, 1.0),
    ],
)
def test_a_cap_above_its_ceiling_or_a_bad_share_is_refused(
    per_s: int, per_min: int, share: float
) -> None:
    time = FakeTime()

    with pytest.raises(ValueError):
        RateLimitedAdapter(
            FakeAdapter(time), per_s=per_s, per_min=per_min, exit_share=share,
            positions=lambda: {}, clock=time.clock, sleep=time.sleep,
        )  # fmt: skip


def test_the_caps_may_sit_exactly_at_the_ceilings_and_at_two() -> None:
    time = FakeTime()
    for per_s, per_min in ((ORDERS_PER_S_CEILING, ORDERS_PER_MIN_CEILING), (2, 2)):
        RateLimitedAdapter(
            FakeAdapter(time),
            per_s=per_s,
            per_min=per_min,
            exit_share=0.4,
            positions=lambda: {},
            clock=time.clock,
            sleep=time.sleep,
        )  # fmt: skip  (no error)


def test_the_limiter_offers_the_two_step_interface_the_order_service_uses() -> None:
    adapter, _, _ = limiter()

    assert isinstance(adapter, Admitting)


def test_the_ceilings_are_below_sebis_threshold_and_dhans_limit() -> None:
    assert ORDERS_PER_S_CEILING < 10  # SEBI: registration needed above 10 orders per second
    assert ORDERS_PER_MIN_CEILING <= 250  # Dhan's published per-minute limit


# ---- wrapped around the paper broker ---------------------------------------------------------


async def test_wrapping_the_paper_broker_leaves_orders_working() -> None:
    from datetime import UTC, datetime, timedelta

    from engine.broker.adapter import Filled
    from engine.broker.paper import PaperBroker
    from engine.market_data.bus import TickBus
    from engine.market_data.provider import Tick
    from tests.engine.risk_builders import INSTRUMENTS

    at = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    bus = TickBus()
    paper = PaperBroker(
        bus, INSTRUMENTS.get, slippage_ticks=1, max_price_age=timedelta(seconds=5), now=lambda: at
    )
    paper.on_tick(Tick(STOCK, D("100"), exchange_time=at, received_at=at))
    time = FakeTime()
    wrapped = RateLimitedAdapter(
        paper, per_s=3, per_min=100, exit_share=0.4, positions=lambda: {},
        clock=time.clock, sleep=time.sleep,
    )  # fmt: skip

    ack = await wrapped.place(order(BUY, 10, key=STOCK, order_id="p1"))
    stream = wrapped.updates()
    update = await asyncio.wait_for(stream.__anext__(), timeout=2)

    assert ack.broker_order_id and isinstance(update, Filled) and update.quantity == 10
    assert [b.engine_order_id for b in await wrapped.order_book()] == ["p1"]
