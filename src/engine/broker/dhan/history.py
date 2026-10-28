"""Dhan intraday chart history (add-candles design §5, §10).

``POST /v2/charts/intraday`` (DhanHQ v2 docs) returns 1-minute OHLCV bars for one instrument
between two IST date-times, as parallel arrays: ``open``, ``high``, ``low``, ``close``,
``volume`` and ``timestamp``. One request may span up to 90 days.

Findings from the owner's recorded responses (task 4.1, 2026-10-28 10:54 IST, IDX_I:13 and
NSE_FNO:53720 over 3 days; fixtures tests/fixtures/dhan/history_*.json):

- ``timestamp`` is a true UTC epoch (seconds, sent as a float): read as UTC the first row is
  09:15:00 IST. This differs from the live feed's LTT, which is an IST epoch.
- The still-forming minute is included (last row 10:54:00 at 10:54:02), so rows at or after
  the last completed minute boundary are discarded.
- No zero-volume rows. Index rows do carry a volume (millions), unlike live index ticks, so it
  is set to 0 here to keep history and live index candles identical (spec "Index candle").
- The option returned 870 rows to the index's 850 (= 375 + 375 + 100 session minutes): about
  10 rows a day outside 09:15-15:30 (likely the post-close session). Rows outside the session
  are dropped.
- Prices are JSON floats; they become Decimal like the live decoder's (2 dp, then tick size).
- Rate limit: DhanHQ v2 docs list the data APIs at 5 requests/s (not measured);
  DHAN_HISTORY_RATE_PER_S defaults to 4 to stay under it.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import structlog

from contracts.market import ExchangeSegment, InstrumentKey
from engine.broker.dhan.packets import price_from_float, round_to_tick
from engine.market_data.candles import Candle
from engine.market_data.clock import IST, interval_start, last_completed_minute, utc_now
from engine.market_data.history_source import HistoryUnavailable

log = structlog.get_logger("engine.broker.dhan.history")

INTRADAY_PATH = "/charts/intraday"
TIMEOUT = httpx.Timeout(15.0)
# Dhan error codes for an invalid token (DH-901), no access to the API or data plan (DH-902)
# and account problems (DH-903). DH-904 is rate limiting: retryable, so not listed here.
AUTH_ERROR_CODES = frozenset({"DH-901", "DH-902", "DH-903"})


class HistoryError(Exception):
    """A history request failed. ``auth`` is True when Dhan rejected the token or data plan,
    which no retry can fix until restart (spec "Data plan inactive")."""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def intraday_body(
    key: InstrumentKey, instrument_type: str, start: datetime, end: datetime
) -> dict[str, object]:
    """Request body for 1-minute bars in [start, end); Dhan expects IST wall-clock times."""
    return {
        "securityId": key.security_id,
        "exchangeSegment": key.segment.value,
        "instrument": instrument_type,
        "interval": "1",
        "oi": False,
        "fromDate": f"{start.astimezone(IST):%Y-%m-%d %H:%M:%S}",
        "toDate": f"{end.astimezone(IST):%Y-%m-%d %H:%M:%S}",
    }


async def fetch_intraday(
    client: httpx.AsyncClient,
    api_base: str,
    token: str,
    client_id: str,
    body: Mapping[str, object],
) -> dict[str, object]:
    """POST the request and return the decoded JSON object. Raises HistoryError.

    Errors carry the HTTP status and Dhan's error code only, never the body (CLAUDE.md
    rule 10).
    """
    try:
        response = await client.post(
            f"{api_base.rstrip('/')}{INTRADAY_PATH}",
            headers={
                "access-token": token,
                "client-id": client_id,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=dict(body),
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise HistoryError(f"Dhan unreachable ({type(exc).__name__})") from None
    if response.status_code != 200:
        code = _error_code(response)
        auth = response.status_code in (401, 403) or code in AUTH_ERROR_CODES
        detail = f"HTTP {response.status_code}" + (f", Dhan error code {code}" if code else "")
        raise HistoryError(f"Dhan rejected the history request: {detail}", auth=auth)
    try:
        payload = response.json()
    except ValueError:
        raise HistoryError("Dhan returned a non-JSON history response") from None
    if not isinstance(payload, dict):
        raise HistoryError("Dhan returned an unexpected history response shape")
    return payload


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("errorCode") if isinstance(body, dict) else None
    return None if code is None else str(code)


SERIES = ("open", "high", "low", "close", "volume", "timestamp")


def parse_intraday(
    payload: Mapping[str, object],
    key: InstrumentKey,
    tick: Decimal | None,
    before: datetime,
) -> list[Candle]:
    """Dhan's parallel arrays -> completed in-session 1-minute candles starting before
    ``before`` (the last completed boundary), oldest first. Raises HistoryUnavailable on an
    unexpected shape."""
    columns = [payload.get(name) for name in SERIES]
    if not all(isinstance(c, list) for c in columns):
        raise HistoryUnavailable("Dhan history response is missing a price series")
    lists: list[Sequence[object]] = [c for c in columns if isinstance(c, list)]
    if len({len(c) for c in lists}) > 1:
        raise HistoryUnavailable("Dhan history series have different lengths")
    index = key.segment is ExchangeSegment.IDX_I
    candles = []
    for o, h, low, c, v, stamp in zip(*lists, strict=True):
        start = history_time_to_utc(_number(stamp))
        if start >= before or interval_start(start, 1) != start:
            continue  # forming minute, or outside 09:15-15:30
        candles.append(
            Candle(
                key,
                1,
                start,
                open=_price(o, tick),
                high=_price(h, tick),
                low=_price(low, tick),
                close=_price(c, tick),
                volume=0 if index else int(_number(v)),
            )
        )
    return candles


def history_time_to_utc(epoch: float) -> datetime:
    """Intraday-chart timestamp -> aware UTC datetime. A true UTC epoch (task 4.1), unlike
    the live feed's LTT; kept in one function like ``ltt_to_utc``."""
    return datetime.fromtimestamp(epoch, UTC)


def _price(value: object, tick: Decimal | None) -> Decimal:
    price = price_from_float(_number(value))
    return round_to_tick(price, tick) if tick else price


def _number(value: object) -> float:
    """A JSON number from the response; anything else is a malformed response."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HistoryUnavailable("Dhan history response holds a non-numeric value")
    return float(value)


class RequestSpacer:
    """Spaces request starts at least ``1 / rate`` seconds apart (design §10). Callers queue
    on the lock instead of failing; the S3 central rate limiter replaces this later."""

    # perf_counter, not monotonic: on Windows monotonic only advances every ~15.6 ms.
    def __init__(self, rate_per_s: float, clock: Callable[[], float] = time.perf_counter) -> None:
        self._gap = 1.0 / rate_per_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last = float("-inf")

    async def wait(self) -> None:
        async with self._lock:  # `async with` on a lock: acquire/release, like SemaphoreSlim
            delay = self._last + self._gap - self._clock()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = self._clock()


class DhanHistory:
    """``HistorySource`` backed by Dhan's intraday chart API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_base: str,
        token: str,
        client_id: str,
        instrument_type: Callable[[InstrumentKey], str | None],
        tick_size: Callable[[InstrumentKey], Decimal | None],
        rate_per_s: float,
        now: Callable[[], datetime] = utc_now,
        spacer: RequestSpacer | None = None,
    ) -> None:
        self._client = client
        self._api_base = api_base
        self._token = token
        self._client_id = client_id
        self._instrument_type = instrument_type
        self._tick_size = tick_size
        self._now = now
        self._spacer = spacer or RequestSpacer(rate_per_s)
        self.disabled_reason: str | None = None

    async def minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        if self.disabled_reason is not None:
            raise HistoryUnavailable(self.disabled_reason, permanent=True)
        instrument_type = self._instrument_type(key)
        if instrument_type is None:
            raise HistoryUnavailable(f"{key} is not in the instrument master")
        before = min(end, last_completed_minute(self._now()))
        await self._spacer.wait()
        try:
            payload = await fetch_intraday(
                self._client,
                self._api_base,
                self._token,
                self._client_id,
                intraday_body(key, instrument_type, start, before),
            )
        except HistoryError as exc:
            if exc.auth:
                # Sticky until restart: a rejected token or plan cannot heal by retrying.
                self.disabled_reason = str(exc)
                log.error("dhan history disabled until restart", reason=str(exc))
            raise HistoryUnavailable(str(exc), permanent=exc.auth) from None
        return [
            c
            for c in parse_intraday(payload, key, self._tick_size(key), before)
            if c.start >= start
        ]
