"""The engine's state snapshot in Redis: what clients (the CLI now, the dashboard later) read.

Clients never compute P&L or risk (CLAUDE.md): every figure here is calculated by the engine
and only displayed elsewhere. The snapshot is one JSON value overwritten each interval (like a
single row that is repeatedly UPDATEd), not a history.
"""

from decimal import Decimal
from enum import IntEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from contracts.market import InstrumentKey
from contracts.orders import Order, Position, ProductType


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


class HaltLevel(IntEnum):
    """How firmly the risk engine has stopped the account for the day. An IntEnum is an enum
    whose members are also integers (a C# ``enum`` with explicit values): it is stored and sent
    as a number, and ``max()`` picks the more severe. A halt only ever goes up within a day.

    The numbers are a stored contract: the risk engine persists them with ``GREATEST`` (add-risk-
    engine task 2.3), so they must never be renumbered. They are spaced out on purpose: 20 is
    reserved for the daily-profit-target halt that ``add-risk-guards`` adds (PRD §7.2 ranking
    HALT_NEW_ENTRIES < TARGET_HALT < KILL)."""

    NONE = 0
    MTM_BLIND = 10  # prices were unknown too long: flattened, no new entries today
    KILL = 30  # kill switch or equity-floor breach: flattened, no new entries today


class PositionRef(BaseModel):
    """Names one position: the same instrument can be held under two products."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: InstrumentKey
    product: ProductType


class RiskStatus(BaseModel):
    """The risk engine's figures for the account today, all computed by the engine (the UI
    never computes P&L or risk). ``mtm_day`` and ``floor`` are None while unknown (MTM has no
    price yet, or no limits are set): never a guessed zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mtm_day: Decimal | None = None  # the day's P&L net of charges (PRD §7)
    floor: Decimal | None = None  # the equity floor: only ever rises (PRD §7.1)
    peak: Decimal = Decimal("0")  # the day's highest mtm_day, from fresh prices only
    halt_level: HaltLevel = HaltLevel.NONE
    halt_reason: str | None = None
    entries_today: int = 0
    loss_streak: int = 0
    cooldown_until: AwareDatetime | None = None
    # Open positions with no catastrophic stop resting. The ``[]`` default is safe: pydantic
    # copies a mutable default for each instance, like a C# field initialiser ``= new()``.
    unprotected: list[PositionRef] = []


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str
    taken_at: AwareDatetime
    open_orders: list[Order]
    positions: list[PositionView]
    # HALTED (design §9): new entries are refused until `recovery resume`. The differences are
    # the reasons, as logged. Defaults keep older snapshots readable (schema_version stays 1).
    halted: bool = False
    halt_differences: list[str] = []
    # Added by add-risk-engine. None until the risk engine has produced its first figures.
    risk: RiskStatus | None = None
