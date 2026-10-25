"""Market-data types shared across processes: exchange segment, instrument key, instrument.

These travel in later order commands (S2) and dashboard snapshots (S5), so they live in
``contracts``. Ticks themselves never leave the engine and are defined in engine.market_data.

Trading terms used below:
  * security ID  - Dhan's numeric identifier for one tradable contract (for NSE it is the
                   exchange token).
  * lot size     - F&O contracts trade in whole lots; quantity must be a multiple of this.
  * tick size    - the smallest allowed price step, e.g. 0.05 rupees for NSE options.
  * freeze qty   - the exchange's maximum quantity per single order for an underlying; larger
                   orders must be split ("sliced").
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field


class ExchangeSegment(StrEnum):
    """Dhan exchange segments (DhanHQ v2 annexure). The value is the name Dhan uses in JSON."""

    IDX_I = "IDX_I"
    NSE_EQ = "NSE_EQ"
    NSE_FNO = "NSE_FNO"
    NSE_CURRENCY = "NSE_CURRENCY"
    BSE_EQ = "BSE_EQ"
    MCX_COMM = "MCX_COMM"
    BSE_CURRENCY = "BSE_CURRENCY"
    BSE_FNO = "BSE_FNO"

    @property
    def feed_code(self) -> int:
        """Numeric code Dhan uses for this segment in binary feed packet headers."""
        return _FEED_CODES[self]

    @classmethod
    def from_feed_code(cls, code: int) -> "ExchangeSegment":
        """Raises KeyError for a code not in the annexure."""
        return _SEGMENTS_BY_CODE[code]


_FEED_CODES: dict[ExchangeSegment, int] = {
    ExchangeSegment.IDX_I: 0,
    ExchangeSegment.NSE_EQ: 1,
    ExchangeSegment.NSE_FNO: 2,
    ExchangeSegment.NSE_CURRENCY: 3,
    ExchangeSegment.BSE_EQ: 4,
    ExchangeSegment.MCX_COMM: 5,
    ExchangeSegment.BSE_CURRENCY: 7,
    ExchangeSegment.BSE_FNO: 8,
}
_SEGMENTS_BY_CODE = {code: segment for segment, code in _FEED_CODES.items()}


class InstrumentKey(BaseModel):
    """Broker-neutral instrument identity (D-023): segment + security ID.

    Written as ``SEGMENT:ID`` in configuration and logs, e.g. ``NSE_FNO:52175``. Frozen pydantic
    models are hashable, so keys can be dict keys and set members (like a C# record struct).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    segment: ExchangeSegment
    security_id: str = Field(pattern=r"^[0-9]+$")

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse ``SEGMENT:ID``. Raises ValueError naming the bad entry."""
        segment, sep, security_id = text.partition(":")
        if not sep or segment not in ExchangeSegment.__members__ or not security_id.isdigit():
            raise ValueError(
                f"{text!r} is not SEGMENT:SECURITY_ID (segment one of "
                f"{', '.join(ExchangeSegment.__members__)}; numeric security ID)"
            )
        return cls(segment=ExchangeSegment(segment), security_id=security_id)

    def __str__(self) -> str:
        return f"{self.segment.value}:{self.security_id}"


class Instrument(BaseModel):
    """One tradable contract from the instrument master."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    key: InstrumentKey
    trading_symbol: str
    display_name: str
    # For derivatives, the symbol of what the contract is on (e.g. NIFTY); for equity and
    # indices, the instrument's own symbol.
    underlying_symbol: str
    # Dhan's instrument enum: INDEX, EQUITY, FUTIDX, OPTIDX, FUTSTK, OPTSTK, ...
    instrument_type: str
    lot_size: int = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: Literal["CE", "PE"] | None = None
    # None means "unknown" (not configured), never zero or unlimited.
    freeze_qty: int | None = Field(default=None, gt=0)
