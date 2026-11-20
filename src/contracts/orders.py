"""Order, fill and position values shared across processes (add-order-position-core).

Trading terms used below:
  * product type - how the exchange and broker treat the position: INTRADAY is squared off the
                   same day (and may be short); CNC is equity delivery (held overnight, sold
                   only from holdings); MARGIN carries F&O positions overnight.
  * market order - fills at the best available price now; limit order - fills only at the
                   limit price or better.
  * stop-market  - a resting order that does nothing until the price trades at or through its
                   trigger price, then becomes a market order. It is how a stop-loss rests at
                   the exchange: a sell stop below the price protects a long position.
  * initial stop - the price at which the trader accepts being wrong on an entry. It sizes the
                   trade (risk = distance to the stop x quantity) and is NOT a resting order;
                   the engine watches it (the exchange-side backstop is the catastrophic stop).
  * fill         - one execution of (part of) an order at one price.
  * MTM          - mark-to-market: unrealized P&L of an open position at the latest price.

pydantic models validate on construction (like a C# record with guard clauses in its
constructor); ``model_config = ConfigDict(frozen=True)`` makes them immutable.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from contracts.market import InstrumentKey


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(StrEnum):
    INTRADAY = "INTRADAY"
    CNC = "CNC"
    MARGIN = "MARGIN"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class OrderState(StrEnum):
    """The order lifecycle of PRD §5."""

    REQUESTED = "Requested"
    SUBMITTED = "Submitted"
    OPEN = "Open"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"
    UNKNOWN = "Unknown"

    @property
    def is_final(self) -> bool:
        return self in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED)


def check_limit_price(
    order_type: OrderType, limit_price: Decimal | None, trigger_price: Decimal | None = None
) -> None:
    """Which prices each order type carries. LIMIT needs a limit price; MARKET and STOP_MARKET
    must not have one. STOP_MARKET needs a trigger price; the other types must not have one."""
    if order_type is OrderType.LIMIT and limit_price is None:
        raise ValueError("limit_price is required for a LIMIT order")
    if order_type is not OrderType.LIMIT and limit_price is not None:
        raise ValueError(f"limit_price must not be set for a {order_type.value} order")
    if order_type is OrderType.STOP_MARKET and trigger_price is None:
        raise ValueError("trigger_price is required for a STOP_MARKET order")
    if order_type is not OrderType.STOP_MARKET and trigger_price is not None:
        raise ValueError(f"trigger_price must not be set for a {order_type.value} order")


class Order(BaseModel):
    """One order as the engine tracks it. Quantities are in units (shares or contracts x lot)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    order_id: str
    account_id: str
    key: InstrumentKey
    side: OrderSide
    quantity: int = Field(gt=0)
    product: ProductType
    order_type: OrderType
    limit_price: Decimal | None = None
    # New in add-risk-engine, optional with a default, so rows and payloads from before it still
    # parse (schema_version stays 1). The trigger belongs to STOP_MARKET orders only; the
    # initial stop belongs to entries (the risk rules require it, the contract does not).
    # ``Field(default=None, gt=0)`` is a declarative guard, like a C# ``[Range]`` attribute: the
    # value is checked when the model is built, and None (absent) skips the check.
    trigger_price: Decimal | None = Field(default=None, gt=0)
    initial_stop: Decimal | None = Field(default=None, gt=0)
    idempotency_key: str
    source: str
    state: OrderState = OrderState.REQUESTED
    filled_quantity: int = Field(default=0, ge=0)
    average_fill_price: Decimal | None = None
    broker_order_id: str | None = None
    paper: bool
    reason: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    # @model_validator(mode="after") runs once all fields are set, for cross-field rules.
    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> Self:
        check_limit_price(self.order_type, self.limit_price, self.trigger_price)
        return self


class CostBreakdown(BaseModel):
    """Trading costs of one fill in rupees, each rounded to the paisa."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brokerage: Decimal = Decimal("0")
    stt: Decimal = Decimal("0")  # securities transaction tax
    exchange: Decimal = Decimal("0")  # exchange transaction charges
    sebi: Decimal = Decimal("0")  # SEBI turnover fee
    stamp_duty: Decimal = Decimal("0")  # charged on buys only
    gst: Decimal = Decimal("0")  # 18% of brokerage + exchange + SEBI

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp_duty + self.gst


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    fill_id: str
    order_id: str
    account_id: str
    key: InstrumentKey
    product: ProductType
    side: OrderSide
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    filled_at: AwareDatetime
    costs: CostBreakdown
    paper: bool


class Position(BaseModel):
    """Net position per account, instrument and product. ``quantity`` is signed: positive long,
    negative short, zero flat (``average_price`` is then 0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str
    key: InstrumentKey
    product: ProductType
    quantity: int = 0
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")  # gross, before costs
    costs: Decimal = Decimal("0")
    updated_at: datetime
