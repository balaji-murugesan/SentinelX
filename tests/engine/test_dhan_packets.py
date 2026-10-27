"""Dhan binary packet decoder (task 5.1). Packets are built with struct.pack per the docs."""

import struct
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from contracts.market import InstrumentKey
from engine.broker.dhan.packets import (
    DISCONNECT,
    FULL,
    HEADER,
    INDEX,
    OI,
    PREV_CLOSE,
    QUOTE,
    SIZES,
    TICKER,
    Disconnect,
    Skipped,
    TradeUpdate,
    Undecodable,
    decode_message,
    ltt_to_utc,
    price_from_float,
    round_to_tick,
)

# 2026-10-27 10:00:00 IST written as an "IST epoch" (IST wall clock counted as UTC).
LTT_IST_EPOCH = int(datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp())
LTT_UTC = datetime(2026, 10, 27, 4, 30, tzinfo=UTC)


def header(code: int, segment: int, security_id: int) -> bytes:
    return HEADER.pack(code, SIZES[code], segment, security_id)


def ticker(ltp: float, code: int = TICKER, segment: int = 0, security_id: int = 13) -> bytes:
    return header(code, segment, security_id) + struct.pack("<fi", ltp, LTT_IST_EPOCH)


def quote(ltp: float, ltq: int = 75, volume: int = 123456, security_id: int = 52175) -> bytes:
    body = struct.pack(
        "<fhifiiiffff", ltp, ltq, LTT_IST_EPOCH, 101.5, volume, 10, 20, 100.0, 0.0, 110.0, 95.0
    )
    return header(QUOTE, 2, security_id) + body


def full(ltp: float) -> bytes:
    body = struct.pack(
        "<fhifiiiiiiffff", ltp, 50, LTT_IST_EPOCH, 1.0, 999, 1, 2, 3, 4, 5, 1.0, 0.0, 2.0, 0.5
    )
    return header(FULL, 2, 52175) + body + bytes(100)


def test_quote_packet() -> None:
    [packet] = decode_message(quote(24500.05))
    assert packet == TradeUpdate(
        InstrumentKey.parse("NSE_FNO:52175"), Decimal("24500.05"), LTT_UTC, ltq=75, volume=123456
    )


@pytest.mark.parametrize("code", [TICKER, INDEX])
def test_ticker_and_index_packets(code: int) -> None:
    [packet] = decode_message(ticker(24512.35, code=code))
    assert isinstance(packet, TradeUpdate)
    assert packet.key == InstrumentKey.parse("IDX_I:13")
    assert packet.ltp == Decimal("24512.35")
    assert packet.ltq is None and packet.volume is None


def test_full_packet_ignores_depth() -> None:
    [packet] = decode_message(full(310.4))
    assert isinstance(packet, TradeUpdate)
    assert (packet.ltp, packet.ltq, packet.volume) == (Decimal("310.40"), 50, 999)


@pytest.mark.parametrize(("code", "body"), [(OI, b"\x00" * 4), (PREV_CLOSE, b"\x00" * 8)])
def test_unused_packets_skipped(code: int, body: bytes) -> None:
    assert decode_message(header(code, 2, 1) + body) == [Skipped(code)]


def test_disconnect_packet() -> None:
    message = header(DISCONNECT, 0, 0) + struct.pack("<h", 807)
    assert decode_message(message) == [Disconnect(807)]


def test_several_packets_in_one_message() -> None:
    packets = decode_message(ticker(100.0) + quote(200.5))
    assert [p.ltp for p in packets if isinstance(p, TradeUpdate)] == [
        Decimal("100.00"),
        Decimal("200.50"),
    ]


def test_unknown_code() -> None:
    message = HEADER.pack(99, 8, 2, 1)
    assert decode_message(message) == [Undecodable(99, "unknown response code")]


@pytest.mark.parametrize("cut", [3, 12])
def test_truncated(cut: int) -> None:
    [packet] = decode_message(quote(1.0)[:cut])
    assert isinstance(packet, Undecodable)
    assert "truncated" in packet.reason


def test_unknown_segment_code() -> None:
    message = HEADER.pack(TICKER, SIZES[TICKER], 6, 1) + struct.pack("<fi", 1.0, 0)
    [packet] = decode_message(message)
    assert isinstance(packet, Undecodable)
    assert "segment" in packet.reason


# ---- prices and times -------------------------------------------------------------


def as_float32(value: str) -> float:
    """What Dhan actually sends: the nearest float32."""
    return float(struct.unpack("<f", struct.pack("<f", float(value)))[0])


def test_price_not_representable_in_binary() -> None:
    assert as_float32("24500.05") != 24500.05  # proves the wire value is inexact
    assert price_from_float(as_float32("24500.05")) == Decimal("24500.05")


def test_high_price_needs_tick_rounding() -> None:
    """Above ~Rs 1,31,072 float32 steps are ~0.0156, so 2 dp alone can be off by 0.01."""
    exact = Decimal("140000.05")
    two_dp = price_from_float(as_float32(str(exact)))
    assert abs(two_dp - exact) <= Decimal("0.01")
    assert round_to_tick(two_dp, Decimal("0.05")) == exact


def test_ltt_converted_to_utc() -> None:
    assert ltt_to_utc(LTT_IST_EPOCH) == LTT_UTC
    assert ltt_to_utc(LTT_IST_EPOCH).tzinfo is UTC
