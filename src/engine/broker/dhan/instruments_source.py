"""Dhan's instrument file: parse rows into ``Instrument`` (download and cache come in task 2.3).

Source: ``api-scrip-master-detailed.csv`` (endpoints.INSTRUMENTS_URL), about 216k rows. Header as
observed on 2026-10-26 (the docs page lists only some of these):

  EXCH_ID, SEGMENT, SECURITY_ID, ISIN, INSTRUMENT, UNDERLYING_SECURITY_ID, UNDERLYING_SYMBOL,
  SYMBOL_NAME, DISPLAY_NAME, INSTRUMENT_TYPE, SERIES, LOT_SIZE, SM_EXPIRY_DATE, STRIKE_PRICE,
  OPTION_TYPE, TICK_SIZE, EXPIRY_FLAG, ... , SM_UPPER_LIMIT, SM_LOWER_LIMIT, SM_FREEZE_QTY

Codes and quirks observed in the real file:
  * EXCH_ID + SEGMENT: NSE/E equity, NSE/D F&O, NSE/I index (kept); C currency, M commodity,
    G and all BSE/MCX rows are dropped (PRD scope is NSE equity + NSE F&O).
  * TICK_SIZE is in PAISE for equity and F&O rows (5.0000 = Rs 0.05) but in RUPEES for index
    rows (0.0500). One index row has 0.0000; such rows are skipped.
  * LOT_SIZE arrives as "65.0".
  * Placeholders: expiry "0001-01-01" or empty, option type "XX" or empty, futures strike
    "-0.01000", freeze quantity 0 / -1 / empty. All become None ("not applicable/unknown").
  * SM_FREEZE_QTY follows NSE's meaning: an order with quantity >= this value is frozen.
    NIFTY options: lot 65, freeze 3511, so the largest single order is 3510 (54 lots).
    Owner-verified 2026-10-26 against NSE circular NSE/FAOP/76693 (effective 5 Oct 2026):
    NIFTY limit 3,510, BANKNIFTY 1,440 = file value - 1. NSE revises these often, so they are
    always read from the day's file, never hardcoded.

Pure and synchronous: the caller runs it on a worker thread (asyncio.to_thread).
"""

import csv
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from contracts.market import ExchangeSegment, Instrument, InstrumentKey

REQUIRED_COLUMNS = (
    "EXCH_ID",
    "SEGMENT",
    "SECURITY_ID",
    "INSTRUMENT",
    "UNDERLYING_SYMBOL",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "SM_EXPIRY_DATE",
    "STRIKE_PRICE",
    "OPTION_TYPE",
    "TICK_SIZE",
    "SM_FREEZE_QTY",
)

_KEPT_SEGMENTS = {
    ("NSE", "E"): ExchangeSegment.NSE_EQ,
    ("NSE", "D"): ExchangeSegment.NSE_FNO,
    ("NSE", "I"): ExchangeSegment.IDX_I,
}
_TICK_IN_PAISE = {ExchangeSegment.NSE_EQ, ExchangeSegment.NSE_FNO}
_PAISE_PER_RUPEE = Decimal(100)
_NO_EXPIRY = {"", "0001-01-01"}


class InstrumentFileError(Exception):
    """The file as a whole is unusable (missing columns, nothing to keep)."""


# @dataclass generates __init__/__eq__/__repr__ from the annotated fields, like a C# record.
# frozen=True makes instances immutable.
@dataclass(frozen=True)
class ParseResult:
    instruments: tuple[Instrument, ...]
    # Rows from kept segments that could not be used (zero tick, bad lot size, ...), by reason.
    skipped: Counter[str]
    # Rows from segments outside scope (BSE, MCX, currency, ...).
    ignored_rows: int


def parse_instrument_file(path: Path) -> ParseResult:
    # newline="" is what the csv module requires so quoted fields with line breaks survive.
    with path.open(newline="", encoding="utf-8") as handle:
        return parse_rows(csv.DictReader(handle))


def parse_rows(reader: "csv.DictReader[str]") -> ParseResult:
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or ())]
    if missing:
        raise InstrumentFileError(f"instrument file is missing columns: {', '.join(missing)}")
    instruments, skipped, ignored = _convert(reader)
    if not instruments:
        raise InstrumentFileError("instrument file has no NSE equity, F&O or index rows")
    return ParseResult(tuple(instruments), skipped, ignored)


def _convert(rows: Iterable[Mapping[str, str]]) -> tuple[list[Instrument], Counter[str], int]:
    instruments: list[Instrument] = []
    skipped: Counter[str] = Counter()
    ignored = 0
    for row in rows:
        segment = _KEPT_SEGMENTS.get((row["EXCH_ID"], row["SEGMENT"]))
        if segment is None:
            ignored += 1
            continue
        try:
            instruments.append(_to_instrument(row, segment))
        except _SkipRow as reason:
            skipped[str(reason)] += 1
        except ValueError:  # bad date text, or pydantic's ValidationError (a ValueError)
            skipped["invalid row"] += 1
    return instruments, skipped, ignored


class _SkipRow(Exception):
    pass


def _to_instrument(row: Mapping[str, str], segment: ExchangeSegment) -> Instrument:
    tick = _decimal(row["TICK_SIZE"])
    if tick is None or tick <= 0:
        raise _SkipRow("zero or invalid tick size")
    if segment in _TICK_IN_PAISE:
        tick = tick / _PAISE_PER_RUPEE

    lot = _decimal(row["LOT_SIZE"])
    if lot is None or lot <= 0 or lot != lot.to_integral_value():
        raise _SkipRow("invalid lot size")

    strike = _decimal(row["STRIKE_PRICE"])
    freeze = _decimal(row["SM_FREEZE_QTY"])
    option_type: Literal["CE", "PE"] | None = None
    if row["OPTION_TYPE"] == "CE":
        option_type = "CE"
    elif row["OPTION_TYPE"] == "PE":
        option_type = "PE"
    expiry = row["SM_EXPIRY_DATE"]

    return Instrument(
        key=InstrumentKey(segment=segment, security_id=row["SECURITY_ID"]),
        # Derivatives: SYMBOL_NAME is the contract symbol (NIFTY-Nov2026-24500-CE). Equity and
        # indices: UNDERLYING_SYMBOL is the exchange symbol (RELIANCE); SYMBOL_NAME is the
        # company name there.
        trading_symbol=row["SYMBOL_NAME"]
        if segment is ExchangeSegment.NSE_FNO
        else row["UNDERLYING_SYMBOL"],
        display_name=row["DISPLAY_NAME"],
        underlying_symbol=row["UNDERLYING_SYMBOL"],
        instrument_type=row["INSTRUMENT"],
        lot_size=int(lot),
        tick_size=_trim(tick),
        expiry=None if expiry in _NO_EXPIRY else date.fromisoformat(expiry),
        strike=_trim(strike) if strike is not None and strike > 0 else None,
        option_type=option_type,
        freeze_qty=int(freeze) if freeze is not None and freeze > 0 else None,
    )


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _trim(value: Decimal) -> Decimal:
    """Drop trailing zeros without changing the value: 0.0500 -> 0.05, 24500.00000 -> 24500.

    Decimal keeps the number of places it was written with, and prices rounded to the tick
    inherit the tick's places (22749.7500). normalize() alone would give 2.45E+4 for whole
    numbers, so those are quantized back to plain integers.
    """
    trimmed = value.normalize()
    return trimmed.quantize(Decimal(1)) if trimmed == trimmed.to_integral_value() else trimmed
