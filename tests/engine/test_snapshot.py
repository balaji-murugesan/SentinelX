"""Redis snapshot writer (task 7.2; trading-state "State snapshot in Redis" / "Redis down")."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from contracts.orders import OrderState, Position, ProductType
from contracts.snapshot import Snapshot, snapshot_key
from engine.broker.adapter import Rejected
from engine.market_data.bus import TickBus
from engine.market_data.provider import Tick
from engine.orders.snapshot import SnapshotWriter
from engine.positions.book import position_id
from tests.engine.test_order_service import NOW, OPTION, STOCK, build, command


class FakeRedis:
    """Just ``set``: fails like an unreachable server for the first ``failures`` calls."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RedisConnectionError("down")
        self.values[key] = value


def tick(key: Any, price: str) -> Tick:
    return Tick(key=key, ltp=Decimal(price), exchange_time=NOW, received_at=NOW)


def position(key: Any, quantity: int, average: str, realized: str = "0") -> Position:
    return Position(
        account_id="main", key=key, product=ProductType.INTRADAY, quantity=quantity,
        average_price=Decimal(average), realized_pnl=Decimal(realized), costs=Decimal("10"),
        updated_at=NOW,
    )  # fmt: skip


def writer(redis: FakeRedis, bus: TickBus) -> tuple[SnapshotWriter, Any]:
    service, _, _ = build()
    snapshot = SnapshotWriter(
        redis,  # type: ignore[arg-type]  # FakeRedis implements the one method used
        service, bus.subscribe("snapshot"), account_id="main", interval_s=0.01, now=lambda: NOW,
    )  # fmt: skip
    return snapshot, service


async def test_snapshot_carries_mtm_realized_and_net_pnl_and_round_trips() -> None:
    bus, redis = TickBus(), FakeRedis()
    snapshot, service = writer(redis, bus)
    long_ = position(OPTION, 65, "100", realized="5")
    service.positions[position_id(long_)] = long_
    pending = await service.place(command("k1", limit="50"))  # rests as an open order
    assert pending.accepted
    bus.publish(tick(OPTION, "102"))

    await snapshot.write_once()

    loaded = Snapshot.model_validate_json(redis.values[snapshot_key("main")])
    assert loaded.schema_version == 1 and loaded.taken_at == NOW
    (view,) = loaded.positions
    assert view.last_price == Decimal("102")
    assert view.mtm == Decimal("130")  # (102 - 100) x 65
    assert view.net_pnl == Decimal("125")  # realized 5 + mtm 130 - costs 10
    assert [o.order_id for o in loaded.open_orders] == [pending.order_id]
    assert all(not o.state.is_final for o in loaded.open_orders)
    assert loaded.open_orders[0].state is OrderState.OPEN


async def test_an_order_the_broker_rejected_after_its_ack_is_no_longer_an_open_order() -> None:
    """handle-late-broker-rejections task 1.3: the order is acknowledged (``Open``), then the
    broker rejects it on the update stream; the snapshot's open orders drop it, the other stays."""
    bus, redis = TickBus(), FakeRedis()
    snapshot, service = writer(redis, bus)
    late = await service.place(command("k1", limit="50"))
    resting = await service.place(command("k2", limit="50"))
    acked = service.orders[late.order_id or ""]
    assert acked.state is OrderState.OPEN and acked.broker_order_id
    assert {o.order_id for o in snapshot.build().open_orders} == {late.order_id, resting.order_id}

    await service.on_update(Rejected(acked.broker_order_id, "RMS: insufficient margin", NOW))

    assert [o.order_id for o in snapshot.build().open_orders] == [resting.order_id]


async def test_open_position_without_a_price_shows_none_not_zero() -> None:
    bus, redis = TickBus(), FakeRedis()
    snapshot, service = writer(redis, bus)
    held = position(STOCK, 10, "50")
    service.positions[position_id(held)] = held

    (view,) = snapshot.build().positions

    assert view.last_price is None and view.mtm is None and view.net_pnl is None


async def test_flat_position_shows_realized_less_costs() -> None:
    bus, redis = TickBus(), FakeRedis()
    snapshot, service = writer(redis, bus)
    flat_ = position(OPTION, 0, "0", realized="40")
    service.positions[position_id(flat_)] = flat_

    (view,) = snapshot.build().positions

    assert view.mtm == Decimal(0) and view.net_pnl == Decimal("30")


async def test_redis_down_keeps_trading_and_retries_the_write() -> None:
    bus, redis = TickBus(), FakeRedis(failures=2)
    snapshot, service = writer(redis, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(snapshot.run(stop))
    try:
        # Orders keep working while Redis is unreachable...
        result = await service.place(command("k2"))
        assert result.accepted
        # ...and the snapshot appears once Redis answers again.
        for _ in range(100):
            if snapshot_key("main") in redis.values:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert redis.failures == 0
    assert (
        datetime.fromisoformat(
            Snapshot.model_validate_json(redis.values[snapshot_key("main")]).taken_at.isoformat()
        ).tzinfo
        == UTC
    )


async def test_snapshot_reports_a_halt_and_its_differences() -> None:
    bus, redis = TickBus(), FakeRedis()
    snapshot, service = writer(redis, bus)
    assert not snapshot.build().halted and snapshot.build().halt_differences == []

    service.halt_differences = ["order o1 is Open here but not at the broker"]
    built = snapshot.build()

    assert built.halted and built.halt_differences == service.halt_differences
    assert Snapshot.model_validate_json(built.model_dump_json()) == built
