"""Instrument master lookups, freshness and refresh loop (task 2.5).

Spec instruments: "Instrument lookups", "Freshness reporting", "Loading does not stall the
engine", and the "Daily refresh" / "Corrupt download" behaviour of the refresh loop.
"""

import asyncio
import time as systime
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from contracts.market import Instrument, InstrumentKey
from engine.broker.dhan.instruments_source import parse_instrument_file
from engine.market_data.instruments import InstrumentMaster, refresh_loop

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "instruments_detailed.csv"
INSTRUMENTS = parse_instrument_file(FIXTURE).instruments
AS_OF = date(2026, 10, 26)


@pytest.fixture
def master() -> InstrumentMaster:
    m = InstrumentMaster()
    m.replace(INSTRUMENTS, AS_OF)
    return m


# ---- lookups ----------------------------------------------------------------------


def test_get_by_key(master: InstrumentMaster) -> None:
    found = master.get(InstrumentKey.parse("IDX_I:13"))
    assert found is not None
    assert found.display_name == "Nifty 50"


def test_unknown_key_is_none(master: InstrumentMaster) -> None:
    assert master.get(InstrumentKey.parse("NSE_FNO:999999")) is None


def test_contract_lookup_option(master: InstrumentMaster) -> None:
    put = master.find_contract("nifty", date(2026, 11, 9), Decimal("24500"), "PE")
    assert put is not None
    assert str(put.key) == "NSE_FNO:40004"


def test_contract_lookup_future(master: InstrumentMaster) -> None:
    future = master.find_contract("NIFTY", date(2026, 10, 27))
    assert future is not None
    assert future.instrument_type == "FUTIDX"


def test_contract_lookup_miss(master: InstrumentMaster) -> None:
    assert master.find_contract("NIFTY", date(2026, 11, 9), Decimal("24550"), "CE") is None


def test_search_is_case_insensitive_and_limited(master: InstrumentMaster) -> None:
    results = master.search("nifty", limit=3)
    assert len(results) == 3
    for item in results:
        assert "NIFTY" in item.trading_symbol.upper() or "NIFTY" in item.display_name.upper()


def test_search_finds_all_matches_within_limit(master: InstrumentMaster) -> None:
    keys = {str(i.key) for i in master.search("reliance", limit=20)}
    assert keys == {"NSE_EQ:2885", "NSE_FNO:48987", "NSE_FNO:106474"}


def test_search_ranking(master: InstrumentMaster) -> None:
    """Exact symbol, then prefix matches (future before options, nearest expiry, CE before PE),
    then substring matches (the Nifty Bank index before BANKNIFTY options)."""
    keys = [str(i.key) for i in master.search("nifty", limit=6)]
    assert keys == [
        "IDX_I:13",  # NIFTY: exact
        "NSE_FNO:48704",  # NIFTY future, Oct expiry
        "NSE_FNO:40003",  # NIFTY 24500 CE, Nov expiry
        "NSE_FNO:40004",  # NIFTY 24500 PE
        "IDX_I:25",  # BANKNIFTY index: substring only
        "NSE_FNO:35000",  # BANKNIFTY option
    ]


# ---- the spot instrument of an underlying (add-risk-guards task 7.1) ----------------------


@pytest.mark.parametrize(("underlying", "spot"), [("NIFTY", "IDX_I:13"), ("BANKNIFTY", "IDX_I:25")])
def test_each_index_options_underlying_maps_to_exactly_one_index_row(
    master: InstrumentMaster, underlying: str, spot: str
) -> None:
    """Against the recorded Dhan master: every option on the underlying names it with the same
    text as exactly one index row (BANKNIFTY, not the display name "Nifty Bank"), so no alias
    table is needed."""
    options = [i for i in INSTRUMENTS if i.option_type is not None]
    on_it = [o for o in options if o.underlying_symbol == underlying]
    rows = [
        i
        for i in INSTRUMENTS
        if i.key.segment.value == "IDX_I" and i.underlying_symbol == underlying
    ]

    assert on_it  # the fixture has options on it
    assert len(rows) == 1
    found = master.spot_of(underlying)
    assert found is not None and str(found.key) == spot
    assert all(master.spot_of(o.underlying_symbol) == found for o in on_it)


# Rows copied unchanged from Dhan's api-scrip-master-detailed.csv downloaded on 2026-11-21. They are
# kept apart from the main fixture so its search-ranking and row-count tests do not move. In that
# full download exactly one IDX_I row carries the underlying symbol FINNIFTY (security 27); the
# other financial-services indices have their own symbols, and one of them (469) is included here
# as a decoy, so the lookup must match the symbol exactly, not by a name that merely looks alike.
FINNIFTY_FIXTURE = FIXTURE.with_name("instruments_finnifty.csv")


def test_finnifty_options_map_to_the_finnifty_index_row() -> None:
    rows = parse_instrument_file(FINNIFTY_FIXTURE).instruments
    m = InstrumentMaster()
    m.replace([*INSTRUMENTS, *rows], AS_OF)
    option = m.get(InstrumentKey.parse("NSE_FNO:57153"))  # FINNIFTY 26550 CE
    assert option is not None and option.underlying_symbol == "FINNIFTY"

    spot = m.spot_of(option.underlying_symbol)

    assert spot is not None and str(spot.key) == "IDX_I:27"
    assert m.spot_of("FINNIFTY") == spot


def test_a_stock_options_spot_is_its_equity_row(master: InstrumentMaster) -> None:
    option = master.get(InstrumentKey.parse("NSE_FNO:106474"))  # RELIANCE 720 CE
    assert option is not None

    spot = master.spot_of(option.underlying_symbol)

    assert spot is not None and str(spot.key) == "NSE_EQ:2885"


def test_spot_lookup_is_case_insensitive_and_misses_are_none(master: InstrumentMaster) -> None:
    found = master.spot_of("nifty")
    assert found is not None and str(found.key) == "IDX_I:13"
    assert master.spot_of("NOSUCH") is None
    assert InstrumentMaster().spot_of("NIFTY") is None  # nothing loaded


def test_an_index_row_wins_over_an_equity_row_of_the_same_name() -> None:
    index = Instrument(
        key=InstrumentKey.parse("IDX_I:99"), trading_symbol="ABC", display_name="ABC Index",
        underlying_symbol="ABC", instrument_type="INDEX", lot_size=1, tick_size=Decimal("0.05"),
    )  # fmt: skip
    stock = index.model_copy(update={"key": InstrumentKey.parse("NSE_EQ:98")})
    m = InstrumentMaster()
    m.replace([stock, index], AS_OF)

    assert m.spot_of("ABC") == index


def test_two_index_rows_with_one_name_are_ambiguous_and_never_guessed() -> None:
    """Fail closed: with two candidates the lookup answers None, and it does not fall back to an
    equity row of that name either."""
    first = Instrument(
        key=InstrumentKey.parse("IDX_I:91"), trading_symbol="ABC", display_name="ABC 1",
        underlying_symbol="ABC", instrument_type="INDEX", lot_size=1, tick_size=Decimal("0.05"),
    )  # fmt: skip
    second = first.model_copy(update={"key": InstrumentKey.parse("IDX_I:92")})
    stock = first.model_copy(update={"key": InstrumentKey.parse("NSE_EQ:93")})
    m = InstrumentMaster()
    m.replace([first, second, stock], AS_OF)

    assert m.spot_of("ABC") is None


@pytest.mark.parametrize(("text", "limit"), [("", 5), ("   ", 5), ("nifty", 0)])
def test_search_empty(master: InstrumentMaster, text: str, limit: int) -> None:
    assert master.search(text, limit) == []


# ---- availability and freshness ---------------------------------------------------


def test_empty_master_is_unavailable() -> None:
    empty = InstrumentMaster()
    assert not empty.available
    assert empty.as_of is None
    assert empty.get(InstrumentKey.parse("IDX_I:13")) is None
    assert empty.search("nifty") == []
    assert not empty.is_fresh(datetime(2026, 10, 26, 4, 0, tzinfo=UTC))


def test_fresh_today(master: InstrumentMaster) -> None:
    assert master.is_fresh(datetime(2026, 10, 26, 4, 0, tzinfo=UTC))  # 09:30 IST same day


def test_stale_from_older_cache() -> None:
    m = InstrumentMaster()
    m.replace(INSTRUMENTS, date(2026, 10, 23))
    assert m.as_of == date(2026, 10, 23)
    assert not m.is_fresh(datetime(2026, 10, 26, 4, 0, tzinfo=UTC))


# ---- refresh loop -----------------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


async def test_refresh_loads_then_keeps_previous_on_failure() -> None:
    master = InstrumentMaster()
    stop = asyncio.Event()
    calls: list[date] = []
    results: list[tuple[Iterable[Instrument], date] | None] = [(INSTRUMENTS, AS_OF), None]

    async def load(today: date) -> tuple[Iterable[Instrument], date] | None:
        calls.append(today)
        result = results[len(calls) - 1] if len(calls) <= len(results) else None
        if len(calls) == 2:
            stop.set()
        return result

    # 08:44:59.9 IST, so the next daily refresh is 0.1 s away.
    clock = FakeClock(datetime(2026, 10, 26, 3, 14, 59, 900000, tzinfo=UTC))
    await asyncio.wait_for(refresh_loop(master, load, time(8, 45), stop, clock), timeout=2)
    assert calls == [AS_OF, AS_OF]
    assert master.as_of == AS_OF  # second (failed) load kept the first master


async def test_slow_load_does_not_stall_event_loop() -> None:
    """Parsing runs on a worker thread, so other tasks (heartbeat, ticks) keep running."""
    master = InstrumentMaster()
    stop = asyncio.Event()
    beats = 0

    def slow_parse() -> tuple[Iterable[Instrument], date]:
        systime.sleep(0.3)  # simulates a multi-second parse of the real file
        return INSTRUMENTS, AS_OF

    async def load(_: date) -> tuple[Iterable[Instrument], date]:
        stop.set()
        return await asyncio.to_thread(slow_parse)

    async def heartbeat() -> None:
        nonlocal beats
        while not master.available:
            beats += 1
            await asyncio.sleep(0.02)

    await asyncio.gather(refresh_loop(master, load, time(8, 45), stop), heartbeat())
    assert beats >= 5
    assert master.available
