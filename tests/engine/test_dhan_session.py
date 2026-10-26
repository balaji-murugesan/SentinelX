"""Engine startup session check and token-expiry warnings (tasks 3.2, 3.3).

Spec broker-session: "Startup session check", "Failed session check keeps engine running"
(no retry, unreachable), "Credentials never exposed", "Token expiry warnings", "Engine never
mints tokens". structlog.testing.capture_logs collects log records as dicts (like an in-memory
ILogger sink in .NET tests).
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from engine.broker.dhan import session
from engine.broker.dhan.session import SessionStatus, check_session, warn_before_expiry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "profile.json"
PROFILE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJkaGFuQ2xpZW50SWQiOiIxMDAwMDAwMDAxIn0.c2lnbmF0dXJl"
CLIENT_ID = PROFILE["dhanClientId"]
API = "https://dhan.example.test/v2"
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)  # 23:30 IST; token valid until 09:10 IST


def client(response: httpx.Response | Exception, calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run_check(response: httpx.Response | Exception) -> tuple[Any, list[httpx.Request], str]:
    calls: list[httpx.Request] = []
    with capture_logs() as logs:
        async with client(response, calls) as http:
            result = await check_session(http, API, TOKEN, NOW)
    return result, calls, json.dumps(logs, default=str)


# ---- startup check ----------------------------------------------------------------


async def test_valid_token_and_plan() -> None:
    result, calls, logs = await run_check(httpx.Response(200, json=PROFILE))
    assert result.ok
    assert result.expires_at == datetime(2026, 10, 27, 3, 40, tzinfo=UTC)  # 09:10 IST
    assert "dhan session ok" in logs
    assert "2026-10-27T09:10:00+05:30" in logs  # expiry logged in IST
    assert [(r.method, r.url.path) for r in calls] == [("GET", "/v2/profile")]


async def test_rejected_token() -> None:
    body = {"errorType": "Invalid_Authentication", "errorCode": "DH-901", "errorMessage": TOKEN}
    result, calls, logs = await run_check(httpx.Response(401, json=body))
    assert result.status is SessionStatus.TOKEN_REJECTED
    assert "new DHAN_ACCESS_TOKEN" in result.reason
    assert "market data unavailable" in logs
    assert len(calls) == 1  # never retried
    assert TOKEN not in logs + result.reason
    assert CLIENT_ID not in logs


async def test_expired_by_validity() -> None:
    body = {**PROFILE, "tokenValidity": "26/10/2026 09:10"}
    result, _, _ = await run_check(httpx.Response(200, json=body))
    assert result.status is SessionStatus.TOKEN_EXPIRED


async def test_plan_inactive() -> None:
    body = {**PROFILE, "dataPlan": "Inactive"}
    result, _, logs = await run_check(httpx.Response(200, json=body))
    assert result.status is SessionStatus.PLAN_INACTIVE
    assert "Data API plan is not active" in logs


async def test_unreachable() -> None:
    result, calls, logs = await run_check(httpx.ConnectTimeout("slow"))
    assert result.status is SessionStatus.UNREACHABLE
    assert len(calls) == 1
    assert "market data unavailable" in logs


async def test_unknown_expiry_still_valid_with_warning() -> None:
    body = {**PROFILE, "tokenValidity": "later"}
    result, _, logs = await run_check(httpx.Response(200, json=body))
    assert result.ok
    assert result.expires_at is None
    assert "expiry unknown" in logs


# ---- expiry warnings --------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.waits: list[timedelta] = []

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace the real wait with one that just advances the fake clock."""
    fake = Clock(datetime(2026, 10, 27, 1, 30, tzinfo=UTC))  # 07:00 IST

    async def advance(stop: asyncio.Event, pause: timedelta) -> bool:
        fake.waits.append(pause)
        fake.now += pause
        return stop.is_set()

    monkeypatch.setattr(session, "_stopped_within", advance)
    return fake


async def run_warnings(clock: Clock, expires_at: datetime) -> list[dict[str, Any]]:
    with capture_logs() as logs:
        await warn_before_expiry(
            expires_at, timedelta(hours=3), timedelta(minutes=30), asyncio.Event(), clock
        )
    return logs


async def test_expires_before_close_warned_at_start(clock: Clock) -> None:
    logs = await run_warnings(clock, datetime(2026, 10, 27, 3, 40, tzinfo=UTC))  # 09:10 IST
    assert "expires before today's market close" in logs[0]["event"]


async def test_no_close_warning_when_token_outlives_session(clock: Clock) -> None:
    logs = await run_warnings(clock, datetime(2026, 10, 28, 1, 0, tzinfo=UTC))  # tomorrow 06:30
    assert not any("market close" in r["event"] for r in logs)


async def test_warnings_inside_window_at_most_once_per_interval(clock: Clock) -> None:
    expires = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    logs = await run_warnings(clock, expires)
    expiring = [r for r in logs if r["event"] == "dhan token expiring"]
    # 3 h window, one warning per 30 min: 6 warnings, then the expiry error.
    assert len(expiring) == 6
    assert expiring[0]["minutes_left"] == 180
    assert logs[-1]["event"] == "dhan access token has expired; market data will stop"
    assert clock.waits[0] == expires - datetime(2026, 10, 27, 1, 30, tzinfo=UTC) - timedelta(
        hours=3
    )  # first sleep lasts until the warning window opens
    assert all(w <= timedelta(minutes=30) for w in clock.waits[1:])


async def test_stop_ends_loop() -> None:
    stop = asyncio.Event()
    stop.set()
    expires = datetime.now(UTC) + timedelta(days=1)
    await asyncio.wait_for(
        warn_before_expiry(expires, timedelta(hours=3), timedelta(minutes=30), stop), timeout=1
    )


def test_engine_never_mints_tokens() -> None:
    """The session module knows only the read-only profile endpoint."""
    source = Path(session.__file__).read_text(encoding="utf-8")
    for forbidden in ("RenewToken", "generateAccessToken", "consumeApp-consent", ".post("):
        assert forbidden not in source
