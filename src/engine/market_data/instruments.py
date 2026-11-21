"""The instrument master: lookups over the day's instruments, plus the daily refresh loop.

Broker-neutral (D-023): it holds ``contracts.market.Instrument`` values and receives them from
a loader function, so it never imports engine.broker. The Dhan loader is wired in by the
market-data service.

Thread/async safety: each load builds a complete, immutable ``_Snapshot`` and swaps it in with
one reference assignment. Readers always see either the old or the new master, never a
half-built one (like replacing an ImmutableDictionary reference in C#).
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

import structlog

from contracts.market import ExchangeSegment, Instrument, InstrumentKey
from engine.market_data.clock import ist_date, next_ist_time, utc_now

log = structlog.get_logger("engine.instruments")

# (underlying, expiry, strike, option type). Futures have strike and option type None.
ContractKey = tuple[str, date | None, Decimal | None, Literal["CE", "PE"] | None]


# The segments a spot instrument is looked for in, in order of preference (``spot_of``): an index
# row first (NIFTY, BANKNIFTY), else a cash-equity row (a stock option's underlying share).
_SPOT_SEGMENTS: tuple[ExchangeSegment, ...] = (ExchangeSegment.IDX_I, ExchangeSegment.NSE_EQ)


@dataclass(frozen=True)
class _Snapshot:
    as_of: date
    by_key: dict[InstrumentKey, Instrument]
    by_contract: dict[ContractKey, Instrument]
    # For each spot segment, the rows by underlying symbol. A symbol with more than one row in a
    # segment is ambiguous and maps to None (``spot_of`` then refuses to guess).
    spots: dict[ExchangeSegment, dict[str, Instrument | None]]
    all: tuple[Instrument, ...] = field(repr=False)


class InstrumentMaster:
    def __init__(self) -> None:
        self._snapshot: _Snapshot | None = None

    def replace(self, instruments: Iterable[Instrument], as_of: date) -> None:
        items = tuple(instruments)
        by_contract: dict[ContractKey, Instrument] = {}
        spots: dict[ExchangeSegment, dict[str, Instrument | None]] = {s: {} for s in _SPOT_SEGMENTS}
        for item in items:
            if item.expiry is not None:  # only derivatives are looked up by contract
                contract = (item.underlying_symbol, item.expiry, item.strike, item.option_type)
                by_contract.setdefault(contract, item)
            if (rows := spots.get(item.key.segment)) is not None:
                symbol = item.underlying_symbol.upper()
                rows[symbol] = None if symbol in rows else item  # a second row: ambiguous
        self._snapshot = _Snapshot(
            as_of=as_of, by_key={i.key: i for i in items}, by_contract=by_contract, spots=spots,
            all=items,
        )  # fmt: skip

    @property
    def available(self) -> bool:
        return self._snapshot is not None

    @property
    def as_of(self) -> date | None:
        """IST trading date of the data held, or None when unavailable."""
        return self._snapshot.as_of if self._snapshot else None

    def is_fresh(self, now: datetime) -> bool:
        """True only if the data is for today's IST date. Later stages refuse entries if not."""
        return self._snapshot is not None and self._snapshot.as_of == ist_date(now)

    def get(self, key: InstrumentKey) -> Instrument | None:
        return self._snapshot.by_key.get(key) if self._snapshot else None

    def find_contract(
        self,
        underlying: str,
        expiry: date,
        strike: Decimal | None = None,
        option_type: Literal["CE", "PE"] | None = None,
    ) -> Instrument | None:
        """One option (give strike + CE/PE) or future (omit both) by its contract terms."""
        if self._snapshot is None:
            return None
        return self._snapshot.by_contract.get((underlying.upper(), expiry, strike, option_type))

    def spot_of(self, symbol: str) -> Instrument | None:
        """The *spot* instrument of an underlying: the price an option's strike is measured
        against (add-risk-guards task 7.1, design §7). The ``IDX_I`` index row whose underlying
        symbol is ``symbol`` (NIFTY, BANKNIFTY), else the ``NSE_EQ`` equity row (RELIANCE for a
        RELIANCE stock option). None when there is no such row, or more than one in the segment
        that would answer: the caller then fails closed (no fresh spot price, no strike-distance
        judgement), which is safer than picking one.

        Matched on the master's own ``underlying_symbol``, never on a guessed name. In the recorded
        Dhan master an index row's underlying symbol is the same text its options carry (NIFTY,
        BANKNIFTY, not the display name "Nifty Bank"), so no alias table is needed;
        ``tests/engine/test_instrument_master.py`` pins that against the fixture."""
        if self._snapshot is None:
            return None
        wanted = symbol.upper()
        for segment in _SPOT_SEGMENTS:
            rows = self._snapshot.spots[segment]
            if wanted in rows:
                return rows[wanted]  # None when ambiguous: never fall through to another segment
        return None

    def search(self, text: str, limit: int = 20) -> list[Instrument]:
        """Case-insensitive substring search over trading symbol and display name.

        A linear scan over ~100k instruments (tens of milliseconds): fine for the CLI and the
        future trade panel, never used on the tick path.
        """
        if self._snapshot is None or limit <= 0 or not text.strip():
            return []
        needle = text.strip().upper()
        matches = [
            i
            for i in self._snapshot.all
            if needle in i.trading_symbol.upper() or needle in i.display_name.upper()
        ]
        return _rank(matches, needle)[:limit]


def _rank(matches: list[Instrument], needle: str) -> list[Instrument]:
    """Order search results so the most useful ones survive the limit.

    ``matches`` all contain ``needle`` (upper case) in their trading symbol or display name.
    """
    # Sort key is a tuple compared left to right (like LINQ OrderBy().ThenBy()); False sorts
    # before True, so "is NOT an exact match" puts exact matches first.
    return sorted(
        matches,
        key=lambda i: (
            i.trading_symbol.upper() != needle,  # 1. exact symbol: NIFTY, RELIANCE
            not i.trading_symbol.upper().startswith(needle),  # 2. prefix beats substring
            _TYPE_ORDER.get(i.instrument_type, len(_TYPE_ORDER)),  # 3. underlying first
            i.expiry or date.max,  # 4. nearest expiry first
            i.strike or Decimal(0),  # 5. then by strike
            i.trading_symbol,  # 6. stable tie-break (CE before PE)
        ),
    )


# Index / equity first, then futures, then options: the underlying itself is usually what a
# search is after, and there are thousands of option contracts per underlying.
_TYPE_ORDER = {"INDEX": 0, "EQUITY": 1, "FUTIDX": 2, "FUTSTK": 2, "OPTIDX": 3, "OPTSTK": 3}


# A loader returns (instruments, as_of IST date), or None if nothing could be loaded.
Loader = Callable[[date], Awaitable[tuple[Iterable[Instrument], date] | None]]


async def refresh_loop(
    master: InstrumentMaster,
    load: Loader,
    refresh_at: time,
    stop: asyncio.Event,
    now: Callable[[], datetime] = utc_now,
) -> None:
    """Load once now, then again every day at ``refresh_at`` IST until ``stop`` is set.

    A failed refresh keeps the previous master in use (spec "Corrupt download").
    """
    while not stop.is_set():
        loaded = await load(ist_date(now()))
        if loaded is not None:
            instruments, as_of = loaded
            master.replace(instruments, as_of)
            log.info("instrument master loaded", as_of=as_of.isoformat())
        elif not master.available:
            log.error("instrument master unavailable")
        delay = (next_ist_time(refresh_at, now()) - now()).total_seconds()
        try:
            # Sleep until the next refresh, but wake immediately if stop is set.
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
