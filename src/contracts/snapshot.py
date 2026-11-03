"""The engine's state snapshot in Redis: what clients (the CLI now, the dashboard later) read.

Clients never compute P&L or risk (CLAUDE.md): every figure here is calculated by the engine
and only displayed elsewhere. The snapshot is one JSON value overwritten each interval (like a
single row that is repeatedly UPDATEd), not a history.
"""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from contracts.orders import Order, Position


def snapshot_key(account_id: str) -> str:
    """The Redis string key holding the newest snapshot for one account."""
    return f"sentinelx:snapshot:{account_id}"


class PositionView(BaseModel):
    """A position plus the engine's live figures for it. ``last_price``, ``mtm`` and
    ``net_pnl`` are None while an open position has no price yet (never a guessed zero)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: Position
    last_price: Decimal | None = None
    mtm: Decimal | None = None  # unrealized P&L at last_price
    net_pnl: Decimal | None = None  # realized + mtm - costs


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str
    taken_at: AwareDatetime
    open_orders: list[Order]
    positions: list[PositionView]
