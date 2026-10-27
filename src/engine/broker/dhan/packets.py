"""Decode Dhan live-feed binary packets (DhanHQ v2 "Live Market Feed" + annexure).

Every packet starts with an 8-byte little-endian header:

    byte 1     response code (uint8)        which packet type follows
    bytes 2-3  message length (int16)
    byte 4     exchange segment code (uint8) ExchangeSegment.feed_code, e.g. 2 = NSE_FNO
    bytes 5-8  security ID (int32)

``struct`` format strings describe the byte layout: ``<`` = little-endian, ``B`` uint8,
``h`` int16, ``i`` int32, ``f`` float32 (like BinaryReader.ReadSingle in C#).

Trading fields:
    LTP  last traded price          LTQ  last traded quantity (size of that trade)
    LTT  last trade time            ATP  average traded price for the day
    OI   open interest: number of open F&O contracts (not used in Stage 1)

Pure functions, no I/O: everything here is unit-tested with packets built by struct.pack.
"""

import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from contracts.market import ExchangeSegment, InstrumentKey

HEADER = struct.Struct("<BhBi")  # code, length, segment, security id: 8 bytes

# Response codes (annexure "Feed Response Code").
INDEX, TICKER, QUOTE, OI, PREV_CLOSE, MARKET_STATUS, FULL, DISCONNECT = 1, 2, 4, 5, 6, 7, 8, 50

_TICKER = struct.Struct("<fi")  # LTP, LTT
_QUOTE = struct.Struct("<fhifiiiffff")  # LTP, LTQ, LTT, ATP, volume, sell qty, buy qty, O, C, H, L
_FULL = struct.Struct("<fhifiiiiiiffff")  # as quote plus OI, high OI, low OI (before O/C/H/L)
_DISCONNECT = struct.Struct("<h")

# Total packet sizes (header + body). FULL adds 100 bytes of 5-level market depth (unused).
SIZES = {
    INDEX: HEADER.size + _TICKER.size,
    TICKER: HEADER.size + _TICKER.size,
    QUOTE: HEADER.size + _QUOTE.size,
    OI: HEADER.size + 4,
    PREV_CLOSE: HEADER.size + 8,
    MARKET_STATUS: HEADER.size,
    FULL: HEADER.size + _FULL.size + 100,
    DISCONNECT: HEADER.size + _DISCONNECT.size,
}

# ASSUMPTION, verified in task 7.2 (owner smoke test): Dhan's LTT is "IST epoch", i.e. the IST
# wall-clock time counted as if it were UTC, as the DhanHQ SDK's display code implies. Converting
# therefore subtracts 5h30m. If the smoke test shows exchange_time 5h30m away from the receive
# time, flip this one constant.
LTT_IS_IST_EPOCH = True
_IST_OFFSET = timedelta(hours=5, minutes=30)
# Guard for that assumption: if the constant is wrong, every exchange time is off by exactly
# the IST offset, so a live tick's lag (receive time - exchange time) sits near +5h30m or
# -5h30m. DhanFeed checks live ticks during market hours with lag_suggests_wrong_ltt_zone.
LTT_ZONE_TOLERANCE = timedelta(minutes=10)


def lag_suggests_wrong_ltt_zone(lag: timedelta) -> bool:
    """True if ``lag`` is within LTT_ZONE_TOLERANCE of +-5h30m (see LTT_IS_IST_EPOCH)."""
    return abs(abs(lag) - _IST_OFFSET) <= LTT_ZONE_TOLERANCE


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    key: InstrumentKey
    ltp: Decimal
    exchange_time: datetime  # aware UTC
    ltq: int | None = None
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class Disconnect:
    """Dhan is closing the feed; ``code`` is a Data API error code such as 807 (token expired)."""

    code: int


@dataclass(frozen=True, slots=True)
class Skipped:
    """A packet we understood but do not use (OI, previous close, market status)."""

    code: int


@dataclass(frozen=True, slots=True)
class Undecodable:
    """Unknown code or truncated data. The rest of the message is dropped."""

    code: int | None
    reason: str


Packet = TradeUpdate | Disconnect | Skipped | Undecodable


def decode_message(data: bytes) -> list[Packet]:
    """Decode one WebSocket binary message (normally one packet; several are handled too)."""
    packets: list[Packet] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            packets.append(Undecodable(None, "truncated header"))
            break
        code, _length, segment_code, security_id = HEADER.unpack_from(data, offset)
        size = SIZES.get(code)
        if size is None:
            packets.append(Undecodable(code, "unknown response code"))
            break
        if len(data) - offset < size:
            packets.append(Undecodable(code, "truncated packet"))
            break
        packets.append(_decode(code, segment_code, security_id, data, offset + HEADER.size))
        offset += size
    return packets


def _decode(code: int, segment_code: int, security_id: int, data: bytes, body: int) -> Packet:
    if code == DISCONNECT:
        return Disconnect(_DISCONNECT.unpack_from(data, body)[0])
    if code not in (INDEX, TICKER, QUOTE, FULL):
        return Skipped(code)
    try:
        segment = ExchangeSegment.from_feed_code(segment_code)
    except KeyError:
        return Undecodable(code, f"unknown segment code {segment_code}")
    key = InstrumentKey(segment=segment, security_id=str(security_id))
    if code in (INDEX, TICKER):
        ltp, ltt = _TICKER.unpack_from(data, body)
        return TradeUpdate(key, price_from_float(ltp), ltt_to_utc(ltt))
    layout = _QUOTE if code == QUOTE else _FULL
    ltp, ltq, ltt, _atp, volume, *_ = layout.unpack_from(data, body)
    return TradeUpdate(key, price_from_float(ltp), ltt_to_utc(ltt), ltq=ltq, volume=volume)


def price_from_float(value: float) -> Decimal:
    """float32 rupees -> Decimal with 2 decimal places.

    Dhan sends float32 (24 bits of precision): 24500.05 arrives as 24500.05078125. Formatting to
    2 places recovers the exchange price. Above about Rs 1,31,072 neighbouring float32 values are
    ~0.0156 apart, so 2 dp can be 0.01 off; the feed then rounds to the instrument's tick size
    (round_to_tick), which is exact for the 0.05 grid.
    """
    return Decimal(f"{value:.2f}")


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Nearest multiple of the instrument's tick size (half rounds up)."""
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def ltt_to_utc(epoch: int) -> datetime:
    """Dhan's last-trade-time epoch -> aware UTC datetime (see LTT_IS_IST_EPOCH)."""
    moment = datetime.fromtimestamp(epoch, UTC)
    return moment - _IST_OFFSET if LTT_IS_IST_EPOCH else moment
