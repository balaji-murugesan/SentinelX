"""BrokerAdapter, PaperBroker and trading costs (tasks 4.1-4.4).

Scenarios from spec paper-broker: "Paper market order", "Buy with one tick of slippage",
"Stale price", "Resting buy limit", "Marketable limit", "Restart with a resting limit"
(broker level), "Option sell costs", "Costs reduce P&L"; spec orders: "Cancel a resting limit
order" (broker level).
"""

import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from contracts.market import Instrument, InstrumentKey
from contracts.orders import CostBreakdown, Order, OrderSide, OrderState, OrderType, ProductType
from engine.broker.adapter import (
    BrokerAck,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerRejected,
    BrokerUpdate,
    Cancelled,
    Filled,
)
from engine.broker.costs import CostCategory, category, fill_costs
from engine.broker.paper import PaperBroker, market_fill_price
from engine.market_data.bus import TickBus
from engine.market_data.provider import Tick

NOW = datetime(2026, 10, 29, 4, 30, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")
TICK = Decimal("0.05")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def instrument(key: InstrumentKey) -> Instrument | None:
    if key != OPTION:
        return None
    return Instrument(
        key=OPTION, trading_symbol="NIFTY-Nov2026-22600-CE", display_name="NIFTY 22600 CE",
        underlying_symbol="NIFTY", instrument_type="OPTIDX", lot_size=65, tick_size=TICK,
        freeze_qty=1800,
    )  # fmt: skip


def order(
    side: OrderSide = OrderSide.BUY,
    limit: str | None = None,
    order_id: str = "o1",
    **changes: object,
) -> Order:
    values: dict[str, object] = {
        "order_id": order_id, "account_id": "main", "key": OPTION, "side": side,
        "quantity": 65, "product": ProductType.INTRADAY,
        "order_type": OrderType.LIMIT if limit else OrderType.MARKET,
        "limit_price": Decimal(limit) if limit else None, "idempotency_key": f"k-{order_id}",
        "source": "cli", "paper": True, "state": OrderState.SUBMITTED,
        "created_at": NOW, "updated_at": NOW,
    }  # fmt: skip
    return Order(**{**values, **changes})  # type: ignore[arg-type]


def make(clock: Clock, slippage: int = 1) -> PaperBroker:
    return PaperBroker(
        TickBus(),
        instrument,
        slippage_ticks=slippage,
        max_price_age=timedelta(seconds=5),
        now=clock,
    )


def price(broker: PaperBroker, clock: Clock, value: str, age_s: float = 0) -> None:
    received = clock.now - timedelta(seconds=age_s)
    broker.on_tick(Tick(OPTION, Decimal(value), exchange_time=received, received_at=received))


def pending(broker: PaperBroker) -> list[BrokerUpdate]:
    queue = broker._updates
    return [queue.get_nowait() for _ in range(queue.qsize())]


# ---- 4.1 adapter --------------------------------------------------------------------------


class FakeBroker:
    """A minimal second implementation: mypy checks it against the Protocol too."""

    async def place(self, order: Order) -> BrokerAck:
        return BrokerAck("fake-1")

    async def cancel(self, order: Order) -> None:
        return None

    async def order_book(self) -> list[BrokerOrder]:
        return []

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        if False:  # pragma: no cover - makes this an async generator that yields nothing
            yield Cancelled("x", "x", NOW)


def test_implementations_satisfy_the_protocol() -> None:
    adapters: list[BrokerAdapter] = [make(Clock()), FakeBroker()]  # mypy checks the shapes
    assert len(adapters) == 2


# ---- 4.2 market orders --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "last", "ticks", "expected"),
    [
        (OrderSide.BUY, "193.85", 1, "193.90"),
        (OrderSide.SELL, "193.85", 1, "193.80"),
        (OrderSide.BUY, "193.85", 0, "193.85"),
        (OrderSide.BUY, "193.85", 3, "194.00"),
        (OrderSide.SELL, "0.05", 1, "0.05"),  # never below one tick
        (OrderSide.SELL, "0.10", 5, "0.05"),
    ],
)
def test_market_fill_price(side: OrderSide, last: str, ticks: int, expected: str) -> None:
    assert market_fill_price(Decimal(last), TICK, ticks, side) == Decimal(expected)


async def test_paper_market_order_with_one_tick_of_slippage() -> None:
    clock = Clock()
    broker = make(clock)
    price(broker, clock, "193.85")
    ack = await broker.place(order())
    [fill] = pending(broker)
    assert isinstance(fill, Filled) and fill.broker_order_id == ack.broker_order_id
    assert fill.quantity == 65 and fill.price == Decimal("193.90")
    assert fill.costs.total > 0
    [entry] = await broker.order_book()
    assert entry.state is OrderState.FILLED and entry.engine_order_id == "o1"


@pytest.mark.parametrize("age_s", [30, None])
async def test_stale_or_missing_price(age_s: float | None) -> None:
    clock = Clock()
    broker = make(clock)
    if age_s is not None:
        price(broker, clock, "193.85", age_s=age_s)
    with pytest.raises(BrokerRejected) as info:
        await broker.place(order())
    assert "stale" in str(info.value) and pending(broker) == []


async def test_paper_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PaperBroker made a network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    clock = Clock()
    broker = make(clock)
    price(broker, clock, "193.85")
    await broker.place(order())
    await broker.place(order(OrderSide.SELL, limit="200.00", order_id="o2"))
    price(broker, clock, "200.05")
    assert len(pending(broker)) == 2


# ---- 4.3 limit orders ---------------------------------------------------------------------


async def test_resting_buy_limit_fills_at_the_limit() -> None:
    clock = Clock()
    broker = make(clock)
    price(broker, clock, "193.85")
    await broker.place(order(limit="190.00"))
    assert pending(broker) == []
    price(broker, clock, "191.00")
    assert pending(broker) == []
    price(broker, clock, "189.95")  # trades through the limit
    [fill] = pending(broker)
    assert isinstance(fill, Filled) and fill.price == Decimal("190.00")
    price(broker, clock, "185.00")
    assert pending(broker) == []  # filled once only


async def test_resting_sell_limit() -> None:
    clock = Clock()
    broker = make(clock)
    await broker.place(order(OrderSide.SELL, limit="200.00"))  # no price yet: rests
    price(broker, clock, "199.95")
    assert pending(broker) == []
    price(broker, clock, "200.00")
    [fill] = pending(broker)
    assert isinstance(fill, Filled) and fill.price == Decimal("200.00")


@pytest.mark.parametrize(
    ("side", "limit", "expected"),
    [(OrderSide.BUY, "195.00", "193.85"), (OrderSide.SELL, "190.00", "193.85")],
)
async def test_marketable_limit_fills_at_once_at_the_better_price(
    side: OrderSide, limit: str, expected: str
) -> None:
    clock = Clock()
    broker = make(clock)
    price(broker, clock, "193.85")
    await broker.place(order(side, limit=limit))
    [fill] = pending(broker)
    assert isinstance(fill, Filled) and fill.price == Decimal(expected)


async def test_cancel_a_resting_limit_order() -> None:
    clock = Clock()
    broker = make(clock)
    ack = await broker.place(order(limit="190.00"))
    await broker.cancel(order(limit="190.00", broker_order_id=ack.broker_order_id))
    [update] = pending(broker)
    assert isinstance(update, Cancelled)
    price(broker, clock, "180.00")
    assert pending(broker) == []  # no fill afterwards
    with pytest.raises(BrokerError):
        await broker.cancel(order(limit="190.00", broker_order_id=ack.broker_order_id))


async def test_restart_with_a_resting_limit() -> None:
    clock = Clock()
    resting = order(limit="190.00", state=OrderState.OPEN, broker_order_id="paper-abc")
    broker = make(clock)  # a fresh broker after restart
    broker.restore([resting, order(order_id="o2", state=OrderState.FILLED, broker_order_id="x")])
    assert [o.broker_order_id for o in await broker.order_book()] == ["paper-abc"]
    price(broker, clock, "189.00")
    [fill] = pending(broker)
    assert isinstance(fill, Filled) and fill.broker_order_id == "paper-abc"


# ---- 4.4 costs ----------------------------------------------------------------------------


def test_option_sell_costs_worked_example() -> None:
    # 65 x 200.00 = 13,000 premium turnover.
    costs = fill_costs(CostCategory.OPTION, OrderSide.SELL, 65, Decimal("200.00"))
    assert costs == CostBreakdown(
        brokerage=Decimal("20.00"),  # flat per order
        stt=Decimal("13.00"),  # 0.1% of 13,000, sell side only
        exchange=Decimal("4.55"),  # 0.03503% of 13,000 = 4.5539
        sebi=Decimal("0.01"),  # 0.0001% of 13,000 = 0.013
        stamp_duty=Decimal("0.00"),  # buys only
        gst=Decimal("4.42"),  # 18% of (20 + 4.55 + 0.01) = 4.4208
    )
    assert costs.total == Decimal("41.98")


def test_option_buy_has_stamp_duty_and_no_stt() -> None:
    costs = fill_costs(CostCategory.OPTION, OrderSide.BUY, 65, Decimal("200.00"))
    assert costs.stt == Decimal("0.00") and costs.stamp_duty == Decimal("0.39")  # 0.003%


def test_intraday_brokerage_capped_by_percentage() -> None:
    small = fill_costs(CostCategory.EQUITY_INTRADAY, OrderSide.BUY, 10, Decimal("100.00"))
    assert small.brokerage == Decimal("0.30")  # 0.03% of 1,000 < Rs 20
    large = fill_costs(CostCategory.EQUITY_INTRADAY, OrderSide.BUY, 100, Decimal("1500.00"))
    assert large.brokerage == Decimal("20.00")


def test_delivery_has_no_brokerage_and_stt_both_sides() -> None:
    buy = fill_costs(CostCategory.EQUITY_DELIVERY, OrderSide.BUY, 10, Decimal("1500.00"))
    sell = fill_costs(CostCategory.EQUITY_DELIVERY, OrderSide.SELL, 10, Decimal("1500.00"))
    assert buy.brokerage == sell.brokerage == Decimal("0.00")
    assert buy.stt == sell.stt == Decimal("15.00")  # 0.1% of 15,000


def test_costs_reduce_pnl_on_a_flat_round_trip() -> None:
    buy = fill_costs(CostCategory.OPTION, OrderSide.BUY, 65, Decimal("200.00"))
    sell = fill_costs(CostCategory.OPTION, OrderSide.SELL, 65, Decimal("200.00"))
    gross = (Decimal("200.00") - Decimal("200.00")) * 65
    assert gross - buy.total - sell.total == -(Decimal("29.37") + Decimal("41.98"))


def test_cost_category() -> None:
    from contracts.market import ExchangeSegment as Seg

    assert category(Seg.NSE_FNO, "OPTIDX", ProductType.MARGIN) is CostCategory.OPTION
    assert category(Seg.NSE_FNO, "FUTSTK", ProductType.INTRADAY) is CostCategory.FUTURE
    assert category(Seg.NSE_EQ, "EQUITY", ProductType.CNC) is CostCategory.EQUITY_DELIVERY
    assert category(Seg.NSE_EQ, "EQUITY", ProductType.INTRADAY) is CostCategory.EQUITY_INTRADAY
