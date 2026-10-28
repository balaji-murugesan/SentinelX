"""History sources (add-candles tasks 4.2-4.4).

DhanHistory runs against the owner's recorded intraday-chart responses (task 4.1) through
``httpx.MockTransport``; no test calls Dhan. Scenarios from spec candles: "Twenty instruments at
once", "Data plan inactive", "Repeatable simulated warm-up".
"""

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from contracts.market import InstrumentKey
from engine.broker.dhan.history import DhanHistory, RequestSpacer, parse_intraday
from engine.market_data.clock import IST
from engine.market_data.history_source import HistoryUnavailable, lookback_start
from engine.market_data.simulated import SimulatedHistory, WalkConfig

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
NIFTY = InstrumentKey.parse("IDX_I:13")
OPTION = InstrumentKey.parse("NSE_FNO:53720")
TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJzZWNyZXQiOiJ0b2tlbiJ9.c2lnbmF0dXJl"
CLIENT_ID = "1100223344"
TICK = Decimal("0.05")
# When the owner recorded the fixtures: the 10:54 row is the forming minute.
RECORDED = datetime(2026, 10, 28, 10, 54, 2, tzinfo=IST).astimezone(UTC)


def fixture(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def ist(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(UTC)


def make_history(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    now: datetime = RECORDED,
    rate: float = 1000,
    types: dict[InstrumentKey, str] | None = None,
) -> DhanHistory:
    known = types if types is not None else {NIFTY: "INDEX", OPTION: "OPTIDX"}
    return DhanHistory(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base="https://dhan.test/v2",
        token=TOKEN,
        client_id=CLIENT_ID,
        instrument_type=known.get,
        tick_size=lambda _key: TICK,
        rate_per_s=rate,
        now=lambda: now,
    )


def serve(payload: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=payload)


# ---- fixture findings (task 4.1) and parsing (4.2) ------------------------------------------


def test_fixtures_hold_no_credentials() -> None:
    for name in ("history_index.json", "history_option.json"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert set(json.loads(text)) == {"open", "high", "low", "close", "volume", "timestamp"}
        for secret in ("token", "client", "dhanClientId", CLIENT_ID):
            assert secret not in text


def test_timestamps_are_utc_epoch_and_forming_minute_dropped() -> None:
    candles = parse_intraday(fixture("history_option.json"), OPTION, TICK, before=ist(28, 10, 54))
    assert candles[0].start == ist(26, 9, 15)
    assert candles[-1].start == ist(28, 10, 53)  # 10:54 was still forming
    assert all(c.minutes == 1 and not c.partial for c in candles)
    assert all(c.volume > 0 for c in candles)
    for c in candles:
        for price in (c.open, c.high, c.low, c.close):
            assert price % TICK == 0
        assert c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close)


def test_index_history_volume_is_zero() -> None:
    candles = parse_intraday(fixture("history_index.json"), NIFTY, TICK, before=RECORDED)
    assert candles and all(c.volume == 0 for c in candles)


def test_rows_outside_the_session_dropped() -> None:
    stamps = [ist(27, 15, 29), ist(27, 15, 30), ist(27, 15, 39)]
    payload = {
        "open": [1.0] * 3,
        "high": [1.0] * 3,
        "low": [1.0] * 3,
        "close": [1.0] * 3,
        "volume": [10.0] * 3,
        "timestamp": [s.timestamp() for s in stamps],
    }
    candles = parse_intraday(payload, OPTION, TICK, before=RECORDED)
    assert [c.start for c in candles] == [ist(27, 15, 29)]


@pytest.mark.parametrize(
    "payload",
    [
        {"open": [1.0]},
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [], "timestamp": []},
        {k: ["x"] for k in ("open", "high", "low", "close", "volume", "timestamp")},
    ],
)
def test_malformed_response_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(HistoryUnavailable):
        parse_intraday(payload, OPTION, TICK, before=RECORDED)


def test_lookback_window_defaults() -> None:
    end = ist(28, 10, 54)
    assert end - lookback_start(end, 200, 15) == timedelta(days=16)  # 8 sessions + margin
    assert end - lookback_start(end, 200, 1) == timedelta(days=6)


async def test_request_shape_and_window() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture("history_option.json"))

    history = make_history(handler)
    candles = await history.minutes(OPTION, ist(28, 9, 15), RECORDED + timedelta(hours=1))
    [request] = seen
    assert request.url.path == "/v2/charts/intraday"
    assert request.headers["access-token"] == TOKEN
    body = json.loads(request.content)
    assert body == {
        "securityId": "53720",
        "exchangeSegment": "NSE_FNO",
        "instrument": "OPTIDX",
        "interval": "1",
        "oi": False,
        "fromDate": "2026-10-28 09:15:00",
        "toDate": "2026-10-28 10:54:00",  # capped at the last completed minute
    }
    assert candles[0].start >= ist(28, 9, 15)  # older fixture rows filtered by start
    assert candles[-1].start == ist(28, 10, 53)


async def test_unknown_instrument() -> None:
    history = make_history(serve({}), types={})
    with pytest.raises(HistoryUnavailable) as info:
        await history.minutes(OPTION, ist(28, 9, 15), RECORDED)
    assert not info.value.permanent


async def test_errors_never_log_token_or_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errorCode": "DH-905", "errorMessage": f"x {TOKEN}"})

    history = make_history(handler)
    with capture_logs() as logs, pytest.raises(HistoryUnavailable) as info:
        await history.minutes(OPTION, ist(28, 9, 15), RECORDED)
    text = str(info.value) + json.dumps(logs, default=str)
    assert "DH-905" in str(info.value)
    assert TOKEN not in text and CLIENT_ID not in text and "errorMessage" not in text
    assert not info.value.permanent


# ---- rate and sticky disable (4.3) ----------------------------------------------------------


async def test_twenty_instruments_at_once() -> None:
    starts: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        starts.append(time.perf_counter())
        return httpx.Response(200, json=fixture("history_option.json"))

    rate = 50.0  # same spacing logic as the 4/s default, compressed to keep the test fast
    history = make_history(handler, rate=rate)
    results = await asyncio.gather(
        *(history.minutes(OPTION, ist(28, 9, 15), RECORDED) for _ in range(20))
    )
    assert len(results) == 20 and all(results)
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert min(gaps) >= 1 / rate - 0.002  # never more than `rate` starts in any second


async def test_spacer_waits_by_the_clock() -> None:
    now = [100.0]
    spacer = RequestSpacer(4, clock=lambda: now[0])
    await spacer.wait()
    started = time.monotonic()
    now[0] = 100.2  # 0.05 s short of the 0.25 s gap
    await spacer.wait()
    assert time.monotonic() - started >= 0.04


@pytest.mark.parametrize(
    ("status", "code"), [(401, None), (403, None), (400, "DH-901"), (400, "DH-902")]
)
async def test_data_plan_inactive_disables_until_restart(status: int, code: str | None) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"errorCode": code} if code else {})

    history = make_history(handler)
    with capture_logs() as logs, pytest.raises(HistoryUnavailable) as first:
        await history.minutes(OPTION, ist(28, 9, 15), RECORDED)
    assert first.value.permanent
    with pytest.raises(HistoryUnavailable) as second:
        await history.minutes(NIFTY, ist(28, 9, 15), RECORDED)
    assert second.value.permanent
    assert calls == 1  # no further requests sent
    assert any(r["event"] == "dhan history disabled until restart" for r in logs)


async def test_rate_limit_error_is_not_permanent() -> None:
    history = make_history(lambda _r: httpx.Response(429, json={"errorCode": "DH-904"}))
    with pytest.raises(HistoryUnavailable) as info:
        await history.minutes(OPTION, ist(28, 9, 15), RECORDED)
    assert not info.value.permanent
    assert history.disabled_reason is None


# ---- simulated history (4.4) -------------------------------------------------------------


def walk(seed: int | None = 7) -> WalkConfig:
    return WalkConfig(Decimal("100"), 2, 1.0, TICK, seed)


async def test_repeatable_simulated_warm_up() -> None:
    def make() -> SimulatedHistory:
        return SimulatedHistory(walk(), now=lambda: RECORDED)

    start = lookback_start(RECORDED, 200, 15)
    first = await make().minutes(OPTION, start, RECORDED)
    second = await make().minutes(OPTION, start, RECORDED)
    assert first == second
    assert first[-1].start == ist(28, 10, 53)
    assert {c.start.astimezone(IST).weekday() for c in first} <= {0, 1, 2, 3, 4}
    days = {c.start.astimezone(IST).date() for c in first}
    # The window starts and ends at 10:54, so its first and last days add up to one session.
    assert len(first) == 375 * (len(days) - 1)


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(0, 2**32), start_ticks=st.integers(1, 5000))
async def test_simulated_prices_on_the_tick_grid(seed: int, start_ticks: int) -> None:
    config = WalkConfig(start_ticks * TICK, 3, 1.0, TICK, seed)
    candles = await SimulatedHistory(config, now=lambda: RECORDED).minutes(
        OPTION, ist(27, 9, 15), RECORDED
    )
    for c in candles:
        for price in (c.open, c.high, c.low, c.close):
            assert price > 0 and price % TICK == 0
