"""1-minute candle builder and aggregation (add-candles tasks 2.1, 2.2).

Scenarios from spec candles: "OHLC from ticks", "Volume from cumulative day volume", "Index
candle", "Tick after publication", "Illiquid option", "Derived 15-minute candle".
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from contracts.market import InstrumentKey
from engine.market_data.candles import Candle, MinuteBuilder, aggregate
from engine.market_data.clock import IST, interval_start
from engine.market_data.provider import Tick

OPTION = InstrumentKey.parse("NSE_FNO:53720")
NIFTY = InstrumentKey.parse("IDX_I:13")
GRACE = timedelta(seconds=2)
OPEN = datetime(2026, 10, 28, 9, 15, tzinfo=IST).astimezone(UTC)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 28, hour, minute, second, tzinfo=IST).astimezone(UTC)


def tick(
    when: datetime, price: str, volume: int | None = None, key: InstrumentKey = OPTION
) -> Tick:
    return Tick(key, Decimal(price), exchange_time=when, received_at=when, volume=volume)


def builder(*keys: InstrumentKey, since: datetime = OPEN) -> MinuteBuilder:
    b = MinuteBuilder()
    for key in keys or (OPTION,):
        b.watch(key, since)
    return b


def feed(b: MinuteBuilder, ticks: Sequence[Tick]) -> list[Candle]:
    closed: list[Candle] = []
    for t in ticks:
        closed += b.add(t)
    return closed


# ---- 2.1 builder ------------------------------------------------------------------------


def test_ohlc_from_ticks() -> None:
    b = builder()
    feed(b, [tick(at(10, 5, s), p) for s, p in [(1, "100.00"), (9, "101.50"), (30, "99.80")]])
    feed(b, [tick(at(10, 5, 50), "100.40")])
    [candle] = b.close_due(at(10, 6) + GRACE, GRACE)
    assert (candle.open, candle.high, candle.low, candle.close) == tuple(
        map(Decimal, ["100.00", "101.50", "99.80", "100.40"])
    )
    assert candle.start == at(10, 5) and candle.minutes == 1


def test_volume_from_cumulative_day_volume() -> None:
    b = builder()
    feed(b, [tick(at(10, 4, 58), "100", volume=1_000)])  # baseline, in the previous minute
    feed(b, [tick(at(10, 5, 10), "100", volume=1_300), tick(at(10, 5, 50), "100", volume=1_650)])
    candles = b.close_due(at(10, 6) + GRACE, GRACE)
    assert candles[-1].volume == 650
    assert not candles[-1].partial


def test_first_tick_has_no_volume_baseline() -> None:
    b = builder()
    feed(b, [tick(at(10, 5, 1), "100", volume=5_000), tick(at(10, 5, 2), "100", volume=5_065)])
    [candle] = b.close_due(at(10, 6) + GRACE, GRACE)
    assert candle.volume == 65
    assert candle.partial


def test_volume_reset_marks_partial() -> None:
    b = builder()
    feed(b, [tick(at(10, 4, 59), "100", volume=5_000)])
    feed(b, [tick(at(10, 5, 1), "100", volume=10), tick(at(10, 5, 2), "100", volume=40)])
    candle = b.close_due(at(10, 6) + GRACE, GRACE)[-1]
    assert candle.volume == 30 and candle.partial


def test_index_candle_has_zero_volume() -> None:
    b = builder(NIFTY)
    feed(b, [tick(at(10, 5, s), "22600", key=NIFTY) for s in (1, 2, 3)])
    [candle] = b.close_due(at(10, 6) + GRACE, GRACE)
    assert candle.volume == 0 and not candle.partial


def test_close_waits_for_grace() -> None:
    b = builder()
    feed(b, [tick(at(10, 5, 40), "100")])
    assert b.close_due(at(10, 6, 1), GRACE) == []
    assert len(b.close_due(at(10, 6, 2), GRACE)) == 1


def test_later_tick_closes_early() -> None:
    b = builder()
    feed(b, [tick(at(10, 5, 40), "100")])
    [closed] = feed(b, [tick(at(10, 6, 0), "101")])
    assert closed.start == at(10, 5) and closed.close == Decimal("100")


def test_tick_after_publication_is_ignored_and_counted() -> None:
    b = builder()
    feed(b, [tick(at(10, 5, 40), "100")])
    [published] = b.close_due(at(10, 6) + GRACE, GRACE)
    assert feed(b, [tick(at(10, 5, 59), "120")]) == []
    assert b.late == 1
    assert b.close_due(at(10, 7) + GRACE, GRACE) == []  # nothing re-opened
    assert published.high == Decimal("100")


def test_illiquid_option_has_no_candle_for_empty_minutes() -> None:
    b = builder()
    closed = feed(b, [tick(at(11, 1, 5), "50"), tick(at(11, 4, 5), "51")])
    closed += b.close_due(at(11, 5) + GRACE, GRACE)
    assert [c.start for c in closed] == [at(11, 1), at(11, 4)]


def test_started_mid_minute_is_partial() -> None:
    b = builder(since=at(10, 5, 20))
    feed(b, [tick(at(10, 5, 30), "100")])
    [candle] = b.close_due(at(10, 6) + GRACE, GRACE)
    assert candle.partial


def test_outside_session_and_unwatched_ignored() -> None:
    b = builder()
    assert feed(b, [tick(at(9, 8), "100"), tick(at(15, 40), "100")]) == []
    assert feed(b, [tick(at(10, 0), "100", key=NIFTY)]) == []
    assert b.close_due(at(16, 0), GRACE) == []


def test_outage_marks_forming_and_reconnect_minute_partial() -> None:
    b = builder()
    feed(b, [tick(at(12, 0, 10), "100", volume=10)])
    b.mark_partial()  # feed went down at 12:00:30
    b.resync(at(12, 2, 40))  # reconnected
    candles = feed(b, [tick(at(12, 2, 45), "100", volume=900)])
    candles += feed(
        b, [tick(at(12, 3, 5), "100", volume=950), tick(at(12, 3, 6), "100", volume=990)]
    )
    candles += b.close_due(at(12, 4) + GRACE, GRACE)
    by_start = {c.start: c for c in candles}
    assert by_start[at(12, 2)].partial and by_start[at(12, 2)].volume == 0
    assert not by_start[at(12, 3)].partial and by_start[at(12, 3)].volume == 90


# ---- 2.2 aggregation --------------------------------------------------------------------


def minute(
    start: datetime, o: str, h: str, low: str, c: str, v: int, partial: bool = False
) -> Candle:
    return Candle(OPTION, 1, start, *map(Decimal, (o, h, low, c)), volume=v, partial=partial)  # type: ignore[arg-type]


def test_derived_fifteen_minute_candle() -> None:
    minutes = [
        minute(at(9, 15 + i), str(100 + i), str(105 + i), str(95 + i), str(101 + i), 10 + i)
        for i in range(15)
    ]
    [candle] = aggregate(minutes, 15)
    assert candle.start == at(9, 15) and candle.minutes == 15
    assert candle.open == Decimal("100")
    assert candle.high == Decimal("119")
    assert candle.low == Decimal("95")
    assert candle.close == Decimal("115")
    assert candle.volume == sum(10 + i for i in range(15))
    assert not candle.partial


def test_aggregate_is_partial_if_any_minute_is() -> None:
    minutes = [minute(at(9, 30 + i), "1", "1", "1", "1", 1, partial=(i == 2)) for i in range(5)]
    [candle] = aggregate(minutes, 5)
    assert candle.partial


def test_aggregate_splits_on_session_anchor() -> None:
    minutes = [minute(at(9, 15 + i), "1", "1", "1", "1", 1) for i in range(7)]
    assert [c.start for c in aggregate(minutes, 3)] == [at(9, 15), at(9, 18), at(9, 21)]


# Property (design §12): building 1-minute candles and aggregating them to N minutes gives the
# same candles as building N-minute candles straight from the ticks.

prices = st.integers(min_value=1, max_value=100_000).map(lambda n: Decimal(n) / 20)
tick_lists = st.lists(
    st.tuples(st.integers(min_value=0, max_value=60 * 60 - 1), prices, st.integers(0, 500)),
    min_size=1,
    max_size=80,
)


def reference(ticks: Sequence[Tick], minutes: int) -> list[Candle]:
    """Straightforward N-minute candles from ticks, the way the spec defines them."""
    out: dict[datetime, list[Tick]] = {}
    for t in ticks:
        start = interval_start(t.exchange_time, minutes)
        assert start is not None
        out.setdefault(start, []).append(t)
    candles = []
    previous_volume: int | None = None
    for start, group in out.items():
        volume = 0
        partial = False
        for t in group:
            assert t.volume is not None
            if previous_volume is None:
                partial = True
            else:
                volume += t.volume - previous_volume
            previous_volume = t.volume
        lows = [t.ltp for t in group]
        candles.append(
            Candle(
                OPTION,
                minutes,
                start,
                group[0].ltp,
                max(lows),
                min(lows),
                group[-1].ltp,
                volume,
                partial,
            )  # fmt: skip
        )
    return candles


@settings(max_examples=200, deadline=None)
@given(raw=tick_lists, minutes=st.sampled_from([1, 3, 5, 15]))
def test_aggregation_matches_direct_build(
    raw: list[tuple[int, Decimal, int]], minutes: int
) -> None:
    raw.sort(key=lambda r: r[0])
    volume = 0
    ticks = []
    for offset, price, traded in raw:
        volume += traded
        ticks.append(tick(OPEN + timedelta(seconds=offset), str(price), volume=volume))
    b = builder()
    one_minute = feed(b, ticks) + b.close_due(OPEN + timedelta(hours=2), GRACE)
    aggregated = aggregate(one_minute, minutes)
    assert aggregated == reference(ticks, minutes)
    for c in aggregated:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
