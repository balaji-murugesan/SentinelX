"""``TradingRepository`` on Postgres: SQLAlchemy 2.0 Core over asyncpg (design §3).

``async with engine.begin() as conn:`` opens a transaction that commits when the block ends
and rolls back if it raises (like a C# ``TransactionScope`` with ``Complete()`` on success).
Each call is bounded by a timeout: a hung database must fail the call, not stall the order
lock (design, Risks).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from contracts.commands import CommandResult
from contracts.market import ExchangeSegment, InstrumentKey
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
from engine.persistence import tables as t
from engine.persistence.repository import LoadedState, PersistenceError

DEFAULT_TIMEOUT_S = 3.0
_OPEN_STATES = [s.value for s in OrderState if not s.is_final]


class PostgresRepository:
    def __init__(self, url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        # pool_pre_ping: test each pooled connection before use, so a Postgres restart costs
        # one failed call at most instead of a stream of dead-connection errors.
        self._engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._timeout_s = timeout_s

    async def close(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        try:
            async with asyncio.timeout(self._timeout_s), self._engine.begin() as conn:
                yield conn
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            # Type only: driver messages can echo connection details (masked in logs anyway).
            raise PersistenceError(f"database write failed ({type(exc).__name__})") from None

    async def find_result(self, account_id: str, idempotency_key: str) -> CommandResult | None:
        async with self._transaction() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(t.command_results).where(
                            t.command_results.c.account_id == account_id,
                            t.command_results.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _result(row)

    async def save_result(self, result: CommandResult) -> None:
        async with self._transaction() as conn:
            await conn.execute(sa.insert(t.command_results).values(_result_row(result)))

    async def create_order(self, order: Order, result: CommandResult) -> None:
        async with self._transaction() as conn:
            await conn.execute(sa.insert(t.orders).values(_order_row(order)))
            await conn.execute(sa.insert(t.order_events).values(_event_row(order)))
            await conn.execute(sa.insert(t.command_results).values(_result_row(result)))

    async def update_order(self, order: Order) -> None:
        async with self._transaction() as conn:
            await _update_order(conn, order)

    async def record_fill(self, order: Order, fill: Fill, position: Position) -> None:
        async with self._transaction() as conn:
            await _update_order(conn, order)
            await conn.execute(sa.insert(t.fills).values(_fill_row(fill)))
            await _upsert_position(conn, position)

    async def load(self, account_id: str) -> LoadedState:
        async with self._transaction() as conn:
            orders = (
                await conn.execute(
                    sa.select(t.orders)
                    .where(t.orders.c.account_id == account_id, t.orders.c.state.in_(_OPEN_STATES))
                    .order_by(t.orders.c.created_at)
                )
            ).mappings()
            open_orders = [_order(r) for r in orders]
            fill_rows = (
                await conn.execute(
                    sa.select(t.fills)
                    .where(t.fills.c.account_id == account_id)
                    .order_by(t.fills.c.seq)
                )
            ).mappings()
            fills = [_fill(r) for r in fill_rows]
            position_rows = (
                await conn.execute(
                    sa.select(t.positions).where(t.positions.c.account_id == account_id)
                )
            ).mappings()
            positions = [_position(r) for r in position_rows]
        return LoadedState(open_orders, fills, positions)


async def _update_order(conn: AsyncConnection, order: Order) -> None:
    row = _order_row(order)
    updated = await conn.execute(
        sa.update(t.orders).where(t.orders.c.order_id == order.order_id).values(row)
    )
    if updated.rowcount != 1:
        raise PersistenceError(f"order {order.order_id} is not stored")
    await conn.execute(sa.insert(t.order_events).values(_event_row(order)))


async def _upsert_position(conn: AsyncConnection, position: Position) -> None:
    # INSERT ... ON CONFLICT DO UPDATE: one statement whether the position exists or not.
    row = _position_row(position)
    statement = pg_insert(t.positions).values(row)
    keys = ("account_id", "segment", "security_id", "product")
    statement = statement.on_conflict_do_update(
        index_elements=list(keys), set_={k: v for k, v in row.items() if k not in keys}
    )
    await conn.execute(statement)


# ---- row <-> model conversions ------------------------------------------------------------


def _key(row: RowMapping) -> InstrumentKey:
    return InstrumentKey(segment=ExchangeSegment(row["segment"]), security_id=row["security_id"])


def _order_row(o: Order) -> dict[str, Any]:
    return {
        "order_id": o.order_id,
        "account_id": o.account_id,
        "segment": o.key.segment.value,
        "security_id": o.key.security_id,
        "side": o.side.value,
        "quantity": o.quantity,
        "product": o.product.value,
        "order_type": o.order_type.value,
        "limit_price": o.limit_price,
        "idempotency_key": o.idempotency_key,
        "source": o.source,
        "state": o.state.value,
        "filled_quantity": o.filled_quantity,
        "average_fill_price": o.average_fill_price,
        "broker_order_id": o.broker_order_id,
        "paper": o.paper,
        "reason": o.reason,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
    }


def _order(r: RowMapping) -> Order:
    return Order(
        order_id=r["order_id"],
        account_id=r["account_id"],
        key=_key(r),
        side=OrderSide(r["side"]),
        quantity=r["quantity"],
        product=ProductType(r["product"]),
        order_type=OrderType(r["order_type"]),
        limit_price=r["limit_price"],
        idempotency_key=r["idempotency_key"],
        source=r["source"],
        state=OrderState(r["state"]),
        filled_quantity=r["filled_quantity"],
        average_fill_price=r["average_fill_price"],
        broker_order_id=r["broker_order_id"],
        paper=r["paper"],
        reason=r["reason"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _event_row(o: Order) -> dict[str, Any]:
    return {
        "order_id": o.order_id,
        "account_id": o.account_id,
        "state": o.state.value,
        "reason": o.reason,
        "at": o.updated_at,
    }


_COSTS = ("brokerage", "stt", "exchange", "sebi", "stamp_duty", "gst")


def _fill_row(f: Fill) -> dict[str, Any]:
    return {
        "fill_id": f.fill_id,
        "order_id": f.order_id,
        "account_id": f.account_id,
        "segment": f.key.segment.value,
        "security_id": f.key.security_id,
        "product": f.product.value,
        "side": f.side.value,
        "quantity": f.quantity,
        "price": f.price,
        "filled_at": f.filled_at,
        "paper": f.paper,
        **{name: getattr(f.costs, name) for name in _COSTS},
    }


def _fill(r: RowMapping) -> Fill:
    return Fill(
        fill_id=r["fill_id"],
        order_id=r["order_id"],
        account_id=r["account_id"],
        key=_key(r),
        product=ProductType(r["product"]),
        side=OrderSide(r["side"]),
        quantity=r["quantity"],
        price=r["price"],
        filled_at=r["filled_at"],
        costs=CostBreakdown(**{name: Decimal(r[name]) for name in _COSTS}),
        paper=r["paper"],
    )


def _position_row(p: Position) -> dict[str, Any]:
    return {
        "account_id": p.account_id,
        "segment": p.key.segment.value,
        "security_id": p.key.security_id,
        "product": p.product.value,
        "quantity": p.quantity,
        "average_price": p.average_price,
        "realized_pnl": p.realized_pnl,
        "costs": p.costs,
        "updated_at": p.updated_at,
    }


def _position(r: RowMapping) -> Position:
    return Position(
        account_id=r["account_id"],
        key=_key(r),
        product=ProductType(r["product"]),
        quantity=r["quantity"],
        average_price=r["average_price"],
        realized_pnl=r["realized_pnl"],
        costs=r["costs"],
        updated_at=r["updated_at"],
    )


def _result_row(r: CommandResult) -> dict[str, Any]:
    return {
        "account_id": r.account_id,
        "idempotency_key": r.idempotency_key,
        "command_id": r.command_id,
        "accepted": r.accepted,
        "order_id": r.order_id,
        "reason": r.reason,
        "decided_at": r.decided_at,
    }


def _result(r: RowMapping) -> CommandResult:
    return CommandResult(
        command_id=r["command_id"],
        idempotency_key=r["idempotency_key"],
        account_id=r["account_id"],
        accepted=r["accepted"],
        order_id=r["order_id"],
        reason=r["reason"],
        decided_at=r["decided_at"],
    )
