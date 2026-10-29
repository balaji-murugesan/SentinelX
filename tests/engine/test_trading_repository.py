"""TradingRepository contract suite (add-order-position-core tasks 2.1, 2.2).

Every test runs against the in-memory fake and, when TEST_DATABASE_URL points at a real
Postgres (CI provides one), against PostgresRepository on a freshly migrated schema. Locally
without a database the Postgres half is skipped; in CI a missing URL fails loudly.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from contracts.commands import CommandResult
from contracts.market import InstrumentKey
from contracts.orders import (
    CostBreakdown,
    Fill,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProductType,
)
from engine.persistence.migrate import alembic_config
from engine.persistence.postgres import PostgresRepository
from engine.persistence.repository import PersistenceError, TradingRepository
from tests.engine.trading_fakes import MemoryRepository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IN_CI = os.environ.get("CI") == "true"
TABLES = ("order_events", "fills", "positions", "command_results", "orders")

NOW = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")


def migrate(url: str, target: str) -> None:
    """Alembic is synchronous and runs its own event loop, so tests call it in a thread."""
    if target == "head":
        command.upgrade(alembic_config(url), "head")
    else:
        command.downgrade(alembic_config(url), target)


async def prepare_database(url: str) -> None:
    assert url.rsplit("/", 1)[-1].endswith("_test"), "TEST_DATABASE_URL must name a *_test db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        # infra/postgres/init.sql creates this schema in the stack; a bare test database
        # (CI service container) needs it before Alembic can store its version there.
        await conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS migrations"))
    await engine.dispose()
    await asyncio.to_thread(migrate, url, "head")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE {', '.join(TABLES)}"))
    await engine.dispose()


@pytest.fixture(params=["memory", "postgres"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[TradingRepository]:
    if request.param == "memory":
        yield MemoryRepository()
        return
    if not TEST_DATABASE_URL:
        if IN_CI:
            pytest.fail("TEST_DATABASE_URL must be set in CI (a Postgres service is provided)")
        pytest.skip("TEST_DATABASE_URL not set: no real Postgres to test")
    await prepare_database(TEST_DATABASE_URL)
    postgres = PostgresRepository(TEST_DATABASE_URL)
    yield postgres
    await postgres.close()


def order(order_id: str = "o1", **overrides: object) -> Order:
    values: dict[str, object] = {
        "order_id": order_id,
        "account_id": "main",
        "key": OPTION,
        "side": OrderSide.BUY,
        "quantity": 65,
        "product": ProductType.INTRADAY,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("190.05"),
        "idempotency_key": f"k-{order_id}",
        "source": "cli",
        "paper": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Order(**{**values, **overrides})  # type: ignore[arg-type]


def result(key: str, accepted: bool = True, order_id: str | None = "o1") -> CommandResult:
    return CommandResult(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", accepted=accepted,
        order_id=order_id if accepted else None, reason=None if accepted else "no", decided_at=NOW,
    )  # fmt: skip


def fill(fill_id: str, order_id: str = "o1", price: str = "190.05", at: int = 1) -> Fill:
    return Fill(
        fill_id=fill_id, order_id=order_id, account_id="main", key=OPTION,
        product=ProductType.INTRADAY, side=OrderSide.BUY, quantity=65, price=Decimal(price),
        filled_at=NOW + timedelta(seconds=at),
        costs=CostBreakdown(brokerage=Decimal("20"), exchange=Decimal("4.32"), gst=Decimal("4.38")),
        paper=True,
    )  # fmt: skip


def position(quantity: int, average: str) -> Position:
    return Position(
        account_id="main", key=OPTION, product=ProductType.INTRADAY, quantity=quantity,
        average_price=Decimal(average), realized_pnl=Decimal("0"), costs=Decimal("28.70"),
        updated_at=NOW,
    )  # fmt: skip


async def test_create_order_stores_order_and_result_together(repo: TradingRepository) -> None:
    await repo.create_order(order(), result("k-o1"))
    assert await repo.find_result("main", "k-o1") == result("k-o1")
    state = await repo.load("main")
    assert state.open_orders == [order()]


async def test_duplicate_idempotency_key_is_refused(repo: TradingRepository) -> None:
    await repo.create_order(order(), result("k-o1"))
    with pytest.raises(PersistenceError):
        await repo.create_order(order("o2"), result("k-o1", order_id="o2"))
    assert [o.order_id for o in (await repo.load("main")).open_orders] == ["o1"]  # all or nothing


async def test_rejection_result_without_order(repo: TradingRepository) -> None:
    rejected = result("k-x", accepted=False)
    await repo.save_result(rejected)
    assert await repo.find_result("main", "k-x") == rejected
    assert await repo.find_result("other", "k-x") is None


async def test_fill_updates_order_and_position_and_final_orders_leave_open_set(
    repo: TradingRepository,
) -> None:
    await repo.create_order(order(), result("k-o1"))
    opened = order(state=OrderState.OPEN, broker_order_id="paper-1")
    await repo.update_order(opened)
    filled = order(
        state=OrderState.FILLED, broker_order_id="paper-1", filled_quantity=65,
        average_fill_price=Decimal("190.05"),
    )  # fmt: skip
    await repo.record_fill(filled, fill("f1"), position(65, "190.05"))
    state = await repo.load("main")
    assert state.open_orders == []
    assert state.fills == [fill("f1")]
    assert state.positions == [position(65, "190.05")]


async def test_exact_decimals_survive(repo: TradingRepository) -> None:
    await repo.create_order(order(), result("k-o1"))
    average = Decimal(195 * 100) / Decimal(195) + Decimal(1) / Decimal(3)  # 100.333...
    await repo.record_fill(order(state=OrderState.FILLED), fill("f1"), position(195, str(average)))
    [stored] = (await repo.load("main")).positions
    assert stored.average_price == average  # unconstrained NUMERIC: not rounded


async def test_position_upserts_and_fills_keep_order(repo: TradingRepository) -> None:
    await repo.create_order(order(), result("k-o1"))
    await repo.create_order(order("o2"), result("k-o2", order_id="o2"))
    await repo.record_fill(order(state=OrderState.FILLED), fill("f1", at=1), position(65, "190"))
    await repo.record_fill(
        order("o2", state=OrderState.FILLED), fill("f2", "o2", "200", at=2), position(130, "195")
    )
    state = await repo.load("main")
    assert [f.fill_id for f in state.fills] == ["f1", "f2"]
    assert state.positions == [position(130, "195")]


async def test_update_of_unknown_order_fails(repo: TradingRepository) -> None:
    with pytest.raises(PersistenceError):
        await repo.update_order(order("missing"))


async def test_other_accounts_are_not_loaded(repo: TradingRepository) -> None:
    await repo.create_order(
        order(account_id="other"), result("k-o1").model_copy(update={"account_id": "other"})
    )
    assert (await repo.load("main")).open_orders == []


# ---- migration (task 2.1) -----------------------------------------------------------------


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
async def test_migration_downgrades_and_reapplies() -> None:
    assert TEST_DATABASE_URL
    await prepare_database(TEST_DATABASE_URL)
    await asyncio.to_thread(migrate, TEST_DATABASE_URL, "0001")
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    assert not set(TABLES) & set(names)
    await engine.dispose()
    await asyncio.to_thread(migrate, TEST_DATABASE_URL, "head")
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    assert set(TABLES) <= set(names)
    await engine.dispose()


async def test_unreachable_database_raises_persistence_error() -> None:
    repo = PostgresRepository("postgresql+asyncpg://u:p@127.0.0.1:1/x_test", timeout_s=2)
    with pytest.raises(PersistenceError) as info:
        await repo.find_result("main", "k")
    assert "u:p" not in str(info.value)
    await repo.close()
