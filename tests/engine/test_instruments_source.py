"""Dhan instrument-file parsing (tasks 2.1, 2.2, 2.4).

The fixture holds real rows from Dhan's detailed instrument file (2026-10-26), trimmed:
NIFTY/BANKNIFTY index, NIFTY options and future, RELIANCE stock F&O, three NSE equities, the
zero-tick "Gift Nifty" index row, and BSE / MCX / currency rows that must be dropped.
"""

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from contracts.market import ExchangeSegment, Instrument, InstrumentKey
from engine.broker.dhan.instruments_source import (
    REQUIRED_COLUMNS,
    InstrumentFileError,
    parse_instrument_file,
    parse_rows,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "instruments_detailed.csv"


@pytest.fixture(scope="module")
def by_key() -> dict[str, Instrument]:
    return {str(i.key): i for i in parse_instrument_file(FIXTURE).instruments}


def test_fixture_has_the_mapped_columns() -> None:
    with FIXTURE.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert set(REQUIRED_COLUMNS) <= set(header)


# ---- segment filter ---------------------------------------------------------------


def test_only_nse_equity_fno_and_index_kept() -> None:
    result = parse_instrument_file(FIXTURE)
    segments = {i.key.segment for i in result.instruments}
    assert segments == {ExchangeSegment.NSE_EQ, ExchangeSegment.NSE_FNO, ExchangeSegment.IDX_I}
    assert result.ignored_rows == 3  # BSE equity, MCX future, NSE currency option


def test_zero_tick_row_skipped_and_counted(by_key: dict[str, Instrument]) -> None:
    assert "IDX_I:5024" not in by_key  # Gift Nifty, tick 0.0000
    assert parse_instrument_file(FIXTURE).skipped == {"zero or invalid tick size": 1}


# ---- field mapping ----------------------------------------------------------------


def test_index_option(by_key: dict[str, Instrument]) -> None:
    call = by_key["NSE_FNO:40003"]
    assert call.underlying_symbol == "NIFTY"
    assert call.instrument_type == "OPTIDX"
    assert call.expiry == date(2026, 11, 9)
    assert call.strike == Decimal("24500")
    assert call.option_type == "CE"
    assert call.lot_size == 65
    assert call.trading_symbol.startswith("NIFTY-")
    assert by_key["NSE_FNO:40004"].option_type == "PE"


def test_future_has_no_strike_or_option_type(by_key: dict[str, Instrument]) -> None:
    future = by_key["NSE_FNO:48704"]  # strike "-0.01000", option type "XX" in the file
    assert future.instrument_type == "FUTIDX"
    assert future.strike is None
    assert future.option_type is None
    assert future.expiry == date(2026, 10, 27)


def test_equity_uses_exchange_symbol(by_key: dict[str, Instrument]) -> None:
    reliance = by_key["NSE_EQ:2885"]
    assert reliance.trading_symbol == "RELIANCE"
    assert reliance.display_name == "Reliance Industries"
    assert reliance.expiry is None
    assert reliance.lot_size == 1


def test_index_placeholders_become_none(by_key: dict[str, Instrument]) -> None:
    nifty = by_key["IDX_I:13"]
    assert nifty.display_name == "Nifty 50"
    assert nifty.expiry is None
    assert nifty.option_type is None
    assert nifty.strike is None


# ---- tick size in rupees (spec "Tick size in rupees") ------------------------------


@pytest.mark.parametrize(
    ("key", "rupees"),
    [
        ("NSE_FNO:40003", "0.05"),  # 5.0000 paise
        ("NSE_FNO:48704", "0.10"),  # NIFTY future, 10.0000 paise
        ("NSE_EQ:14366", "0.01"),  # IDEA, 1.0000 paise
        ("NSE_EQ:2885", "0.10"),  # RELIANCE, 10.0000 paise
        ("IDX_I:13", "0.05"),  # index rows are already rupees
    ],
)
def test_tick_size_in_rupees(by_key: dict[str, Instrument], key: str, rupees: str) -> None:
    assert by_key[key].tick_size == Decimal(rupees)
    assert isinstance(by_key[key].tick_size, Decimal)


# ---- freeze quantity (spec "Freeze quantity source") ------------------------------


@pytest.mark.parametrize(
    ("key", "freeze"),
    [("NSE_FNO:40003", 3511), ("NSE_FNO:35000", 1441), ("NSE_FNO:106474", 20001)],
)
def test_freeze_qty_from_file(by_key: dict[str, Instrument], key: str, freeze: int) -> None:
    assert by_key[key].freeze_qty == freeze


def test_zero_freeze_is_unknown(by_key: dict[str, Instrument]) -> None:
    assert by_key["IDX_I:13"].freeze_qty is None  # file says 0


# ---- row and file errors ----------------------------------------------------------


def _csv(*rows: dict[str, str], columns: tuple[str, ...] = REQUIRED_COLUMNS) -> io.StringIO:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return buffer


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "SECURITY_ID": "1",
        "INSTRUMENT": "OPTIDX",
        "UNDERLYING_SYMBOL": "NIFTY",
        "SYMBOL_NAME": "NIFTY-X",
        "DISPLAY_NAME": "NIFTY X",
        "LOT_SIZE": "65.0",
        "SM_EXPIRY_DATE": "2026-11-09",
        "STRIKE_PRICE": "24500.00000",
        "OPTION_TYPE": "CE",
        "TICK_SIZE": "5.0000",
        "SM_FREEZE_QTY": "3511",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"LOT_SIZE": "65.5"}, "invalid lot size"),
        ({"LOT_SIZE": "abc"}, "invalid lot size"),
        ({"TICK_SIZE": "NaN"}, "zero or invalid tick size"),
        ({"SM_EXPIRY_DATE": "09-11-2026"}, "invalid row"),
        ({"SECURITY_ID": "12a"}, "invalid row"),
    ],
)
def test_bad_rows_are_skipped_not_fatal(overrides: dict[str, str], reason: str) -> None:
    result = parse_rows(csv.DictReader(_csv(_row(SECURITY_ID="9"), _row(**overrides))))
    assert [str(i.key) for i in result.instruments] == ["NSE_FNO:9"]
    assert result.skipped == {reason: 1}


@pytest.mark.parametrize("freeze", ["", "-1", "abc"])
def test_unusable_freeze_is_unknown(freeze: str) -> None:
    result = parse_rows(csv.DictReader(_csv(_row(SM_FREEZE_QTY=freeze))))
    assert result.instruments[0].freeze_qty is None


def test_missing_columns_rejected() -> None:
    columns = tuple(c for c in REQUIRED_COLUMNS if c != "TICK_SIZE")
    row = {k: v for k, v in _row().items() if k != "TICK_SIZE"}
    with pytest.raises(InstrumentFileError, match="TICK_SIZE"):
        parse_rows(csv.DictReader(_csv(row, columns=columns)))


def test_file_without_kept_rows_rejected() -> None:
    with pytest.raises(InstrumentFileError, match="no NSE"):
        parse_rows(csv.DictReader(_csv(_row(EXCH_ID="BSE"))))


def test_key_type() -> None:
    result = parse_rows(csv.DictReader(_csv(_row(SECURITY_ID="40003"))))
    assert result.instruments[0].key == InstrumentKey.parse("NSE_FNO:40003")
