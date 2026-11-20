"""OrderService.place (task 6.1).

Scenarios: orders "Place command answered", "Same key twice" (service level), "One trace per
order", "Live mode before the risk engine" and "Gate rejection is final" (no broker call),
plus the construction rule "OrderService cannot be built without a gate".
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from contracts.commands import PlaceOrder
from contracts.market import Instrument, InstrumentKey
from contracts.mode import TradingMode
from contracts.orders import Order, OrderSide, OrderState, OrderType, Position, ProductType
from engine.broker.adapter import BrokerAck, BrokerOrder, BrokerRejected, BrokerUpdate
from engine.broker.paper import PaperBroker
from engine.market_data.bus import TickBus
from engine.market_data.instruments import InstrumentMaster
from engine.market_data.provider import Subscriptions, Tick
from engine.market_data.simulated import SimulatedFeed
from engine.orders.gate import RiskGate
from engine.orders.service import OrderService
from engine.persistence.repository import PersistenceError
from engine.positions.book import position_id
from engine.positions.feed import PositionFeed
from tests.engine.trading_fakes import MemoryRepository, PermissiveGate

NOW = datetime(2026, 10, 29, 4, 30, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")
STOCK = InstrumentKey.parse("NSE_EQ:1333")
TICK = Decimal("0.05")


def master() -> InstrumentMaster:
    items = [
        Instrument(
            key=OPTION, trading_symbol="NIFTY-Nov2026-22600-CE", display_name="NIFTY 22600 CE",
            underlying_symbol="NIFTY", instrument_type="OPTIDX", lot_size=65, tick_size=TICK,
            freeze_qty=1800,
        ),
        Instrument(
            key=STOCK, trading_symbol="HDFCBANK", display_name="HDFC Bank",
            underlying_symbol="HDFCBANK", instrument_type="EQUITY", lot_size=1, tick_size=TICK,
            freeze_qty=None,
        ),
    ]  # fmt: skip
    instruments = InstrumentMaster()
    instruments.replace(items, date(2026, 10, 29))
    return instruments


def command(
    key: str = "k1",
    *,
    instrument: InstrumentKey = OPTION,
    side: OrderSide = OrderSide.BUY,
    quantity: int = 65,
    product: ProductType = ProductType.INTRADAY,
    limit: str | None = None,
) -> PlaceOrder:
    return PlaceOrder(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", source="cli", sent_at=NOW,
        key=instrument, side=side, quantity=quantity, product=product,
        order_type=OrderType.LIMIT if limit else OrderType.MARKET,
        limit_price=Decimal(limit) if limit else None,
    )  # fmt: skip


class FakeAdapter:
    """Acks every order, counts calls, can be told to refuse or to never answer."""

    def __init__(self, *, refuse: str | None = None, hang: bool = False) -> None:
        self.placed: list[Order] = []
        self._refuse = refuse
        self._hang = hang

    async def place(self, order: Order) -> BrokerAck:
        self.placed.append(order)
        if self._hang:
            await asyncio.Event().wait()  # never returns; the service's timeout cancels it
        if self._refuse:
            raise BrokerRejected(self._refuse)
        return BrokerAck(f"b-{len(self.placed)}")

    async def cancel(self, order: Order) -> None: ...

    async def order_book(self) -> list[BrokerOrder]:
        return []

    async def updates(self) -> AsyncIterator[BrokerUpdate]:
        return
        yield  # makes this an async generator


class Unwritable(MemoryRepository):
    """create_order fails like a database that is down."""

    async def create_order(self, order: Order, result: Any) -> None:
        raise PersistenceError("database down")


def position_feed() -> PositionFeed:
    return PositionFeed(SimulatedFeed(TickBus(), Subscriptions(10), script=[]))


def build(
    adapter: Any = None,
    repository: MemoryRepository | None = None,
    mode: TradingMode = TradingMode.PAPER,
    timeout_s: float = 5,
    exporter: InMemorySpanExporter | None = None,
    now: Callable[[], datetime] = lambda: NOW,
    retry_min_s: float = 0.5,
    gate: RiskGate | None = None,
) -> tuple[OrderService, FakeAdapter, MemoryRepository]:
    adapter = adapter or FakeAdapter()
    repository = repository or MemoryRepository()
    provider = TracerProvider()
    if exporter:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    service = OrderService(
        account_id="main", paper=mode is TradingMode.PAPER, gate=gate or PermissiveGate(),
        adapter=adapter, repository=repository, instruments=master(), position_feed=position_feed(),
        tracer=provider.get_tracer("test"), ack_timeout_s=timeout_s, now=now,
        retry_min_s=retry_min_s,
    )  # fmt: skip
    return service, adapter, repository


async def test_place_command_answered() -> None:
    service, adapter, repo = build()

    result = await service.place(command())

    assert result.accepted and result.order_id and result.reason is None
    stored = repo.orders[result.order_id]
    assert stored.state is OrderState.OPEN and stored.broker_order_id == "b-1"
    assert [e[1] for e in repo.events] == ["Requested", "Submitted", "Open"]
    assert repo.results[("main", "k1")] == result and len(adapter.placed) == 1


async def test_paper_broker_acknowledges_a_market_order() -> None:
    bus = TickBus()
    broker = PaperBroker(
        bus, master().get, slippage_ticks=1, max_price_age=timedelta(seconds=5),
        now=lambda: NOW,
    )  # fmt: skip
    broker.on_tick(Tick(OPTION, Decimal("100"), exchange_time=NOW, received_at=NOW))
    service, _, repo = build(adapter=broker)

    result = await service.place(command())

    assert result.accepted and repo.orders[result.order_id or ""].state is OrderState.OPEN


async def test_same_key_twice_returns_the_first_answer_and_places_once() -> None:
    service, adapter, repo = build()

    first = await service.place(command())
    second = await service.place(command(quantity=130))  # same key, different body

    assert second == first
    assert len(adapter.placed) == 1 and len(repo.orders) == 1


async def test_validation_failure_creates_no_order_and_is_remembered() -> None:
    service, adapter, repo = build()

    result = await service.place(command(quantity=70))  # not a whole number of 65-unit lots

    assert not result.accepted and result.order_id is None and "lot" in (result.reason or "")
    assert adapter.placed == [] and repo.orders == {}
    assert repo.results[("main", "k1")] == result  # a repeat gets the same answer


async def test_live_mode_gate_rejection_is_final_and_never_reaches_the_broker() -> None:
    """The REAL gate in live mode (add-risk-engine task 11.1): live orders are refused until a
    live broker exists, the refusal is a final Rejected order, and nothing reaches the broker."""
    from engine.risk.audit import RiskAudit
    from engine.risk.checks.registry import default_checks
    from engine.risk.gate import RiskEngine
    from engine.risk.limits_service import LimitsService
    from tests.engine.risk_builders import day_state, instrument_of
    from tests.engine.risk_fakes import MemoryRiskRepository, StubState

    risk_repo = MemoryRiskRepository()
    gate = RiskEngine(
        account_id="main", mode=TradingMode.LIVE, state=StubState(day_state()),
        limits=LimitsService(
            account_id="main", repository=risk_repo, results=MemoryRepository(),
            figures=lambda: (Decimal(0), None),
        ),
        audit=RiskAudit(risk_repo), instruments=instrument_of, checks=default_checks(),
    )  # fmt: skip
    service, adapter, repo = build(mode=TradingMode.LIVE, gate=gate)

    result = await service.place(command())

    assert not result.accepted and (result.reason or "").startswith("RISK_LIVE_NOT_ENABLED")
    assert adapter.placed == []  # zero broker calls
    stored = repo.orders[result.order_id or ""]
    assert stored.state is OrderState.REJECTED
    assert (stored.reason or "").startswith("RISK_LIVE_NOT_ENABLED")


def test_service_cannot_be_built_without_a_gate() -> None:
    with pytest.raises(TypeError, match="gate"):
        OrderService(  # type: ignore[call-arg]
            account_id="main", paper=True, adapter=FakeAdapter(), repository=MemoryRepository(),
            instruments=master(), position_feed=position_feed(), ack_timeout_s=5,
            tracer=TracerProvider().get_tracer("t"),
        )  # fmt: skip


async def test_broker_refusal_ends_rejected_with_its_reason() -> None:
    service, _, repo = build(adapter=FakeAdapter(refuse="no fresh price"))

    result = await service.place(command())

    stored = repo.orders[result.order_id or ""]
    assert stored.state is OrderState.REJECTED and stored.reason == "no fresh price"


async def test_ack_timeout_leaves_the_order_unknown_after_one_place_call() -> None:
    service, adapter, repo = build(adapter=FakeAdapter(hang=True), timeout_s=0.05)

    result = await service.place(command())

    assert result.accepted
    assert repo.orders[result.order_id or ""].state is OrderState.UNKNOWN
    assert len(adapter.placed) == 1  # never resent (safety rule 4)


async def test_unwritable_database_refuses_the_order_and_makes_no_broker_call() -> None:
    service, adapter, _ = build(repository=Unwritable())

    result = await service.place(command())

    assert not result.accepted and "cannot be saved" in (result.reason or "")
    assert adapter.placed == [] and not service.persistence_ok
    after = await service.place(command("k2"))  # fail closed until persistence recovers
    assert not after.accepted


async def test_one_trace_per_order_with_broker_latency() -> None:
    exporter = InMemorySpanExporter()
    service, _, _ = build(exporter=exporter)

    await service.place(command())

    spans = exporter.get_finished_spans()
    assert len({s.context.trace_id for s in spans if s.context}) == 1
    names = {s.name for s in spans}
    assert {"order.place", "order.validate", "order.gate", "order.persist", "broker.place"} <= names
    root = next(s for s in spans if s.name == "order.place")
    assert root.attributes and root.attributes["order_id"]
    assert root.attributes["broker.latency_ms"] >= 0  # type: ignore[operator]


async def test_cnc_sell_is_limited_to_held_minus_open_sells() -> None:
    service, adapter, _ = build()
    held = Position(
        account_id="main", key=STOCK, product=ProductType.CNC, quantity=10,
        average_price=Decimal("100"), updated_at=NOW,
    )  # fmt: skip
    service.positions[position_id(held)] = held

    too_many = await service.place(
        command("k1", instrument=STOCK, side=OrderSide.SELL, quantity=15, product=ProductType.CNC)
    )
    assert not too_many.accepted and "10 held" in (too_many.reason or "")

    sell_six = await service.place(
        command("k2", instrument=STOCK, side=OrderSide.SELL, quantity=6, product=ProductType.CNC)
    )
    assert sell_six.accepted  # 6 of 10 is fine

    sell_five = await service.place(
        command("k3", instrument=STOCK, side=OrderSide.SELL, quantity=5, product=ProductType.CNC)
    )
    assert not sell_five.accepted  # only 4 left once the open 6-unit sell is counted
    assert len(adapter.placed) == 1


# ---- add-risk-engine task 1.1: the new order fields reach the order --------------------------


async def test_the_initial_stop_is_carried_onto_the_order() -> None:
    service, _, repo = build()
    entry = command("k-stop", limit="200.00").model_copy(update={"initial_stop": Decimal("190.00")})

    result = await service.place(entry)

    assert result.accepted
    assert repo.orders[result.order_id or ""].initial_stop == Decimal("190.00")


async def test_a_stop_market_command_is_accepted_and_the_order_carries_its_trigger() -> None:
    service, adapter, repo = build()
    stop = PlaceOrder(
        command_id="c-stop", idempotency_key="k-stop", account_id="main", source="cli",
        sent_at=NOW, key=OPTION, side=OrderSide.SELL, quantity=65, product=ProductType.INTRADAY,
        order_type=OrderType.STOP_MARKET, trigger_price=Decimal("160.00"),
    )  # fmt: skip

    result = await service.place(stop)

    assert result.accepted
    stored = repo.orders[result.order_id or ""]
    assert stored.order_type is OrderType.STOP_MARKET and stored.trigger_price == Decimal("160.00")
    assert [o.trigger_price for o in adapter.placed] == [Decimal("160.00")]  # the broker got it


async def test_a_stop_market_with_a_trigger_off_the_tick_grid_never_reaches_the_broker() -> None:
    service, adapter, repo = build()
    stop = PlaceOrder(
        command_id="c-stop", idempotency_key="k-stop", account_id="main", source="cli",
        sent_at=NOW, key=OPTION, side=OrderSide.SELL, quantity=65, product=ProductType.INTRADAY,
        order_type=OrderType.STOP_MARKET, trigger_price=Decimal("160.03"),
    )  # fmt: skip

    result = await service.place(stop)

    assert not result.accepted and "tick grid" in (result.reason or "")
    assert adapter.placed == [] and repo.orders == {}
