"""Instrument key parsing and instrument model types (task 1.2)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.market import ExchangeSegment, Instrument, InstrumentKey


@pytest.mark.parametrize("text", ["NSE_FNO:52175", "IDX_I:13", "NSE_EQ:1333"])
def test_key_round_trip(text: str) -> None:
    assert str(InstrumentKey.parse(text)) == text


@pytest.mark.parametrize(
    "text", ["", "NSE_FNO", "NSE_FNO:", ":13", "nse_fno:13", "NSE_FNO:12a", "XYZ:13", "NSE_FNO 13"]
)
def test_bad_key_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="SEGMENT:SECURITY_ID"):
        InstrumentKey.parse(text)


def test_key_is_hashable_value() -> None:
    a = InstrumentKey.parse("NSE_FNO:52175")
    b = InstrumentKey(segment=ExchangeSegment.NSE_FNO, security_id="52175")
    assert a == b
    assert {a: 1}[b] == 1


@pytest.mark.parametrize("segment", list(ExchangeSegment))
def test_feed_code_round_trip(segment: ExchangeSegment) -> None:
    assert ExchangeSegment.from_feed_code(segment.feed_code) is segment


def _option(**overrides: object) -> Instrument:
    fields: dict[str, object] = {
        "key": InstrumentKey.parse("NSE_FNO:52175"),
        "trading_symbol": "NIFTY-Oct2026-24500-CE",
        "display_name": "NIFTY 27 OCT 24500 CALL",
        "underlying_symbol": "NIFTY",
        "instrument_type": "OPTIDX",
        "lot_size": 75,
        "tick_size": "0.05",
        "expiry": "2026-10-27",
        "strike": "24500.0",
        "option_type": "CE",
    }
    fields.update(overrides)
    return Instrument.model_validate(fields)


def test_decimal_fields() -> None:
    option = _option()
    assert option.tick_size == Decimal("0.05")
    assert isinstance(option.tick_size, Decimal)
    assert option.strike == Decimal("24500")
    assert option.freeze_qty is None


@pytest.mark.parametrize("field", ["lot_size", "tick_size", "freeze_qty"])
def test_non_positive_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _option(**{field: 0})
