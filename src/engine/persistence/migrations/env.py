"""Alembic environment: how migrations connect to the database.

Alembic runs this file for every command. It connects with the MIGRATE role (the only role
with permission to change the schema) and keeps its bookkeeping table in a separate
``migrations`` schema that the engine role cannot touch.
"""

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

# Stage 2 will point this at the SQLAlchemy models' MetaData for autogenerate.
target_metadata = None

# Created by infra/postgres/init.sql and owned by the migrate role. Keeping alembic_version
# here means the engine role has no access to it.
VERSION_TABLE_SCHEMA = "migrations"


def database_url() -> str:
    url = config.attributes.get("url") or os.environ.get("MIGRATE_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATE_DATABASE_URL is not set")
    return str(url)


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=VERSION_TABLE_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations (SQL scripts) are not supported")
asyncio.run(run_online())
