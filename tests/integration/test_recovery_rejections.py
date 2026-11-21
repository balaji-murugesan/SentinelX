"""A rejection missed while the engine was down, applied by restart recovery on a REAL Postgres
(handle-late-broker-rejections task 2.3; trading-state "A rejection missed while the engine was
down is applied at recovery").

An ``Open`` order is stored, the engine "restarts" (a new ``OrderService`` on the same database),
and the scripted broker book reports the order ``Rejected`` with nothing filled. After recovery
the stored order and its audit trail must read ``Rejected``, and nothing is halted.

Needs TEST_DATABASE_URL (a ``*_test`` database). Skipped loudly without it locally, never in CI.
"""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from contracts.orders import OrderState
from engine.broker.adapter import BrokerOrder
from engine.orders.recovery import recover
from engine.persistence import tables as t
from engine.persistence.postgres import PostgresRepository
from tests.engine.test_order_service import build, command
from tests.engine.test_recovery import BookAdapter
from tests.engine.test_trading_repository import prepare_database

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IN_CI = os.environ.get("CI") == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL and not IN_CI, reason="TEST_DATABASE_URL not set: no real Postgres"
    ),
]


async def test_recovery_stores_a_rejection_the_broker_made_while_the_engine_was_down() -> None:
    assert TEST_DATABASE_URL, "TEST_DATABASE_URL must be set (CI provides a Postgres service)"
    await prepare_database(TEST_DATABASE_URL)
    repo = PostgresRepository(TEST_DATABASE_URL)
    reader = create_async_engine(TEST_DATABASE_URL)
    try:
        # First life: one resting limit order, stored Open. ``build`` is typed for the in-memory
        # fake, but any ``TradingRepository`` works (Python checks the shape, not the class).
        first, _, _ = build(repository=repo)  # type: ignore[arg-type]
        order_id = (await first.place(command("k1", limit="100"))).order_id or ""
        assert [o.state for o in (await repo.load("main")).open_orders] == [OrderState.OPEN]

        # Second life: the broker rejected the order while the engine was down.
        adapter = BookAdapter()
        adapter.entries = [BrokerOrder("b-1", order_id, OrderState.REJECTED, 0, None)]
        restarted, _, _ = build(adapter=adapter, repository=repo)  # type: ignore[arg-type]

        differences = await recover(restarted, repo, adapter, account_id="main")

        assert differences == [] and restarted.reconciled
        # Read the rows back on a separate connection: what Postgres holds, not what the
        # repository remembers. ``async with`` is ``await using`` in C#.
        async with reader.connect() as conn:
            order_row = (
                await conn.execute(
                    sa.select(t.orders.c.state, t.orders.c.reason).where(
                        t.orders.c.order_id == order_id
                    )
                )
            ).one()
            events = (
                await conn.execute(
                    sa.select(t.order_events.c.state, t.order_events.c.reason)
                    .where(t.order_events.c.order_id == order_id)
                    .order_by(t.order_events.c.event_id)
                )
            ).all()
        assert tuple(order_row) == ("Rejected", "broker reports Rejected")
        assert [tuple(e) for e in events] == [
            ("Requested", None), ("Submitted", None), ("Open", None),
            ("Rejected", "broker reports Rejected"),
        ]  # fmt: skip
        reloaded = await repo.load("main")
        assert reloaded.open_orders == [] and reloaded.halt is None
    finally:
        await reader.dispose()
        await repo.close()
