"""``RiskRepository`` on Postgres (add-risk-engine design §6, §9, §15).

Same pattern as ``PostgresRepository``: SQLAlchemy 2.0 Core over asyncpg, every call in one
transaction bounded by a timeout, every failure raised as ``PersistenceError``.

The part worth reading is ``save_day``: it is ONE ``INSERT ... ON CONFLICT DO UPDATE`` (an
"upsert": insert the row, or update it if the key already exists, like SQL ``MERGE``) whose
``SET`` clause is ``merge_day_state`` written in SQL. ``GREATEST(a, b)`` is the larger of two
values and ignores NULL (like ``Math.Max`` that treats null as "no value"), so a delayed write
carrying a lower floor leaves the stored floor unchanged. Because it is a single statement,
Postgres applies it atomically: under its default isolation a second concurrent write waits for
the row lock and then merges into the newer row, so two writes cannot undo each other.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from contracts.commands import LimitValue
from contracts.snapshot import HaltLevel
from engine.persistence import tables as t
from engine.persistence.postgres import DEFAULT_TIMEOUT_S
from engine.persistence.repository import PersistenceError
from engine.risk.repository import RiskEvent, StoredLimits
from engine.risk.state import DayState


class PostgresRiskRepository:
    def __init__(self, url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        # pool_pre_ping: test each pooled connection before use, so a Postgres restart costs one
        # failed call at most instead of a stream of dead-connection errors. This repository has
        # its own connection pool, so a risk write never queues behind an order write.
        self._engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._timeout_s = timeout_s

    async def close(self) -> None:
        await self._engine.dispose()

    # ``@asynccontextmanager`` turns a generator into something usable with ``async with``: the
    # code before ``yield`` is the setup, the code after it the cleanup (like C# ``await using``).
    # ``async with a, b`` enters both: the timeout covers opening the connection and the work.
    # Keep this in step with ``PostgresRepository._transaction``.
    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        """Commit on success, roll back on error, give up after the timeout."""
        try:
            async with asyncio.timeout(self._timeout_s), self._engine.begin() as conn:
                yield conn
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            # Type only: driver messages can echo connection details.
            raise PersistenceError(f"database write failed ({type(exc).__name__})") from None

    # ---- limits ---------------------------------------------------------------------------

    async def load_limits(self, account_id: str) -> StoredLimits | None:
        async with self._transaction() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(t.risk_limits).where(t.risk_limits.c.account_id == account_id)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _decoded(_stored_limits, row)

    async def save_limits(
        self, account_id: str, limits: dict[str, LimitValue], at: datetime
    ) -> int:
        insert = pg_insert(t.risk_limits).values(
            account_id=account_id, limits=limits, version=1, updated_at=at
        )
        statement = insert.on_conflict_do_update(
            index_elements=["account_id"],
            set_={
                "limits": insert.excluded.limits,
                "version": t.risk_limits.c.version + 1,
                "updated_at": insert.excluded.updated_at,
            },
        ).returning(t.risk_limits.c.version)
        async with self._transaction() as conn:
            version = (await conn.execute(statement)).scalar_one()
        return int(version)

    # ---- day state ------------------------------------------------------------------------

    async def load_day(self, account_id: str, trade_date: date) -> DayState | None:
        async with self._transaction() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(t.risk_day_state).where(
                            t.risk_day_state.c.account_id == account_id,
                            t.risk_day_state.c.trade_date == trade_date,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _decoded(_day_state, row)

    async def save_day(self, state: DayState) -> DayState:
        day = t.risk_day_state.c
        insert = pg_insert(t.risk_day_state).values(_day_row(state))
        # ``excluded`` is Postgres's name for the row this INSERT tried to add: ``new`` below is
        # the incoming write and ``day.<column>`` is the row already stored.
        new = insert.excluded
        statement = insert.on_conflict_do_update(
            index_elements=["account_id", "trade_date"],
            # This SET clause is engine.risk.state.merge_day_state in SQL. Every expression reads
            # the row as it was before this statement, so the order of the lines does not matter.
            set_={
                "peak": sa.func.greatest(day.peak, new.peak),
                "floor": sa.func.greatest(day.floor, new.floor),
                "halt_level": sa.func.greatest(day.halt_level, new.halt_level),
                # The reason belongs to the level that set it: a higher level brings its own
                # (even none), the same level may add or replace but never wipe, a lower level
                # changes nothing.
                "halt_reason": sa.case(
                    (new.halt_level > day.halt_level, new.halt_reason),
                    (
                        new.halt_level == day.halt_level,
                        sa.func.coalesce(new.halt_reason, day.halt_reason),
                    ),
                    else_=day.halt_reason,
                ),
                "entries_today": sa.func.greatest(day.entries_today, new.entries_today),
                "loss_streak": new.loss_streak,
                "cooldown_until": sa.func.greatest(day.cooldown_until, new.cooldown_until),
                "close_marks": sa.func.coalesce(day.close_marks, new.close_marks),
                "updated_at": new.updated_at,
            },
        ).returning(*t.risk_day_state.c)
        async with self._transaction() as conn:
            row = (await conn.execute(statement)).mappings().one()
        return _decoded(_day_state, row)

    # ---- audit ----------------------------------------------------------------------------

    async def append_event(self, event: RiskEvent) -> None:
        async with self._transaction() as conn:
            await conn.execute(sa.insert(t.risk_event).values(_event_row(event)))


# ---- row <-> model conversions ------------------------------------------------------------


# ``[T]`` is a generic type parameter: like C# ``T Decoded<T>(Func<RowMapping, T> decode,
# RowMapping row)``, T is whatever the decoder returns.
def _decoded[T](decode: Callable[[RowMapping], T], row: RowMapping) -> T:
    """Turn a stored row into a model, or fail closed.

    A row that does not decode (a halt level this version does not know, such as the reserved 20
    after a rollback, or a damaged document) must never be read as "no halt": it raises the same
    ``PersistenceError`` as any other database failure, so callers have one thing to catch.
    """
    try:
        return decode(row)
    except (ValueError, TypeError, KeyError) as exc:
        raise PersistenceError(f"stored risk data is invalid ({type(exc).__name__})") from None


def _stored_limits(r: RowMapping) -> StoredLimits:
    return StoredLimits(dict(r["limits"]), r["version"], r["updated_at"])


def _day_row(s: DayState) -> dict[str, Any]:
    return {
        "account_id": s.account_id,
        "trade_date": s.trade_date,
        "peak": s.peak,
        "floor": s.floor,
        "halt_level": int(s.halt_level),
        "halt_reason": s.halt_reason,
        "entries_today": s.entries_today,
        "loss_streak": s.loss_streak,
        "cooldown_until": s.cooldown_until,
        "close_marks": s.close_marks,
        "updated_at": s.updated_at,
    }


def _day_state(r: RowMapping) -> DayState:
    return DayState(
        account_id=r["account_id"],
        trade_date=r["trade_date"],
        updated_at=r["updated_at"],
        peak=r["peak"],
        floor=r["floor"],
        halt_level=HaltLevel(r["halt_level"]),
        halt_reason=r["halt_reason"],
        entries_today=r["entries_today"],
        loss_streak=r["loss_streak"],
        cooldown_until=r["cooldown_until"],
        close_marks=None if r["close_marks"] is None else dict(r["close_marks"]),
    )


def _event_row(e: RiskEvent) -> dict[str, Any]:
    return {
        "at": e.at,
        "account_id": e.account_id,
        "rule": e.rule,
        "phase": e.phase,
        "decision": e.decision,
        "reason_code": e.reason_code,
        "inputs": e.inputs,
        "enforced": e.enforced,
        "latency_us": e.latency_us,
    }
