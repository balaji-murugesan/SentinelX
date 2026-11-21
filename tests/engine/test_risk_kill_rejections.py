"""Exits the broker rejects after acknowledging them (handle-late-broker-rejections task 4.1;
kill-switch spec "A rejected exit is placed again after the chase interval").

The ``World`` harness of ``test_risk_kill`` (a real ``OrderService`` on a fake clock, a broker
double that acknowledges every order, and a fake ``sleep`` that advances the clock by 0.5 s and
runs the broker script). Each exit here goes the normal way: placed, acknowledged with a broker
order id, ``Open``. The broker script below then rejects it on the update stream
(``service.on_update(Rejected(...))``) a set time after it was placed, as Dhan's RMS can after an
ack. The flattener is not changed by this change (design §5): these tests record that a rejected
exit is placed again once the chase interval since it was placed has passed, never sooner, for the
position's current quantity at the current price, and that rejections do not use up
``max_chase_steps``.

Trading terms: RMS - the broker's risk management system, which can reject an order it has
already acknowledged (for example for margin); chase interval - how long an exit may rest before
the flattener re-prices it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from contracts.commands import SetRiskLimits
from contracts.orders import OrderSide, OrderState, OrderType
from engine.broker.adapter import Rejected
from tests.engine.risk_builders import NOW, OPTION
from tests.engine.test_risk_kill import World, until
from tests.engine.test_risk_limits import BASE

D = Decimal
AT_1000 = datetime(2026, 11, 16, 4, 30, tzinfo=UTC)  # 10:00:00 IST, inside market hours
CHASE_S = 5  # the spec's chase interval
STEPS = 3  # the spec's maximum number of chase steps
LIMITS = {**BASE, "chase_interval_s": CHASE_S, "max_chase_steps": STEPS}


async def rejection_world() -> World:
    """A world at 10:00:00 IST with the spec's chase interval and step cap. Fills are off, so an
    exit rests (``Open``) until the script rejects it or the test turns fills on."""
    w = World(AT_1000)
    await w.limits.set_limits(
        SetRiskLimits(
            command_id="c", idempotency_key="k", account_id="main", source="t", sent_at=NOW,
            limits=LIMITS,
        )
    )  # fmt: skip
    assert await w.daybook.load()
    w.fills_on = False
    return w


def reject_after(w: World, seconds: float, times: int) -> list[tuple[str, datetime]]:
    """Make the broker reject each of the flattener's acknowledged exits on its update stream
    ``seconds`` after it was placed, for the first ``times`` exits. Returns the rejections made,
    as (engine order id, when)."""
    made: list[tuple[str, datetime]] = []
    inner: Callable[[], Awaitable[None]] = w.script

    async def script() -> None:
        for order in list(w.service.orders.values()):
            if (
                len(made) < times
                and order.source == "risk-kill"
                and order.state is OrderState.OPEN
                and order.broker_order_id  # acknowledged the normal way
                and (w.clock.at - order.created_at).total_seconds() >= seconds
            ):
                made.append((order.order_id, w.clock.at))
                await w.service.on_update(
                    Rejected(order.broker_order_id, "RMS: rejected after ack", w.clock.at)
                )
        await inner()

    w.script = script  # type: ignore[method-assign]
    return made


async def test_an_exit_rejected_after_the_acknowledgement_is_placed_again_after_the_interval() -> (
    None
):
    """Spec "Exit rejected after the acknowledgement": chase interval 5 s; the exit placed at
    10:00:00 is acknowledged, then rejected at 10:00:01. No exit goes out before 10:00:05; at the
    first look from 10:00:05 a new exit for 65 is placed at the current price. Once it fills the
    flatten finishes."""
    w = await rejection_world()
    w.hold(OPTION, 65)
    rejections = reject_after(w, 1.0, times=1)
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    first = w.orders()[0]
    assert first.created_at == AT_1000 and first.broker_order_id  # placed and acknowledged
    w.prices[OPTION] = D("91")  # the market moves before the retry: it is priced from here

    await until(lambda: len(w.orders()) == 2)

    assert rejections == [(first.order_id, AT_1000 + timedelta(seconds=1))]
    assert w.service.orders[first.order_id].state is OrderState.REJECTED
    second = w.orders()[1]
    assert second.created_at == AT_1000 + timedelta(seconds=CHASE_S)  # not one look sooner
    assert second.side is OrderSide.SELL and second.quantity == 65
    assert second.order_type is OrderType.LIMIT  # never a market order
    assert second.limit_price == D("89.15")  # 91 less 2 % is 89.18, rounded down to the tick
    assert len(w.broker.placed) == 2
    w.fills_on = True  # the replacement fills
    await asyncio.wait_for(task, timeout=5)

    assert w.flat() and w.service.orders[second.order_id].state is OrderState.FILLED
    assert len(w.orders()) == 2


async def test_the_exit_placed_after_a_rejection_is_for_the_positions_current_quantity() -> None:
    """While the rejected exit waits out the chase interval an entry fills (its cancel came too
    late) and the position grows to 130: the new exit is for 130, not the rejected order's 65."""
    w = await rejection_world()
    w.hold(OPTION, 65)
    reject_after(w, 1.0, times=1)
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    await until(lambda: w.orders()[0].state is OrderState.REJECTED)
    w.hold(OPTION, 130)  # the late entry fill, as the book now shows it

    await until(lambda: len(w.orders()) == 2)

    assert w.orders()[1].quantity == 130
    w.fills_on = True
    await asyncio.wait_for(task, timeout=5)
    assert w.flat()


async def test_rejections_do_not_use_up_the_chase_steps() -> None:
    """Spec "Rejections do not use up the chase steps": with ``max_chase_steps`` 3 the broker
    rejects four exits in a row (each acknowledged first); a fifth is still placed, one chase
    interval after the fourth. Left resting, the fifth is still re-priced after the chase interval
    (had the rejections used up the steps, it would rest at a stale price for ever), and the
    flatten finishes when the re-priced exit fills."""
    w = await rejection_world()
    w.hold(OPTION, 65)
    rejections = reject_after(w, 1.0, times=4)
    task = asyncio.create_task(w.flattener.flatten("test"))

    await until(lambda: len(w.orders()) >= 5 or w.slept >= 60)

    assert len(rejections) == 4
    exits = w.orders()
    assert len(exits) == 5
    assert all(o.state is OrderState.REJECTED for o in exits[:4])
    assert [o.created_at for o in exits] == [
        AT_1000 + timedelta(seconds=CHASE_S * n) for n in range(5)
    ]  # each one chase interval after the one before
    fifth = exits[4]

    # The fifth rests unfilled: one chase interval later it is cancelled to be re-priced.
    await until(lambda: fifth.order_id in w.broker.cancel_asked or w.slept >= 60)
    assert w.broker.cancel_asked == [fifth.order_id]
    assert w.clock.at >= fifth.created_at + timedelta(seconds=CHASE_S)
    w.fills_on = True  # the cancel is confirmed and the re-priced exit fills
    await asyncio.wait_for(task, timeout=5)

    assert w.flat() and len(w.orders()) == 6
    assert w.service.orders[fifth.order_id].state is OrderState.CANCELLED
    assert w.orders()[5].state is OrderState.FILLED


async def test_a_flatten_whose_exit_was_rejected_finishes_when_the_position_closes_elsewhere() -> (
    None
):
    """The exit is rejected after its ack and the owner then closes the position in the broker's
    app: the book is flat and the rejected exit is final, so the flatten returns and places
    nothing more."""
    w = await rejection_world()
    w.hold(OPTION, 65)
    reject_after(w, 1.0, times=1)
    task = asyncio.create_task(w.flattener.flatten("test"))
    await until(lambda: len(w.orders()) == 1)
    await until(lambda: w.orders()[0].state is OrderState.REJECTED)

    w.hold(OPTION, 0, average="0")  # closed elsewhere and reconciled into the book
    await asyncio.wait_for(task, timeout=5)

    assert len(w.orders()) == 1 and w.broker.cancel_asked == []
