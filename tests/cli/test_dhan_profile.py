"""``python -m cli dhan profile``: read-only, prints only safe fields (task 3.1).

The fake profile response deliberately includes personal values; the tests prove none of them,
nor the token or full client ID, ever reach the output.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cli.__main__ import dhan_profile, main
from engine.config import DhanCredentials

TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJkaGFuQ2xpZW50SWQiOiIxMTAwMjIzMzQ0In0.c2lnbmF0dXJl"
CLIENT_ID = "1100223344"
PERSONAL = ("Balaji Example", "owner@example.com", "9876543210", "ABCDE1234F")
PROFILE = {
    "dhanClientId": CLIENT_ID,
    "tokenValidity": "27/10/2026 08:15",
    "activeSegment": "Equity, Derivative",
    "ddpi": "Active",
    "mtf": "Active",
    "dataPlan": "Active",
    "dataValidity": "2026-11-05 09:37:52.0",
    "name": PERSONAL[0],
    "email": PERSONAL[1],
    "mobile": PERSONAL[2],
    "pan": PERSONAL[3],
}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "profile.json"
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)  # 23:30 IST, token expires 08:15 IST


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> DhanCredentials:
    monkeypatch.setenv("DHAN_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("DHAN_API_BASE", "https://dhan.example.test/v2")
    return DhanCredentials()  # type: ignore[call-arg]


def fake(respond: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def assert_nothing_sensitive(text: str) -> None:
    assert TOKEN not in text
    assert CLIENT_ID not in text
    for value in PERSONAL:
        assert value not in text


async def test_prints_only_safe_fields(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    code = await dhan_profile(credentials, fake(httpx.Response(200, json=PROFILE), seen), NOW)
    out, err = capsys.readouterr()
    assert code == 0
    assert_nothing_sensitive(out + err)
    assert "tokenValidity" in out  # field names are listed
    assert "name" in out
    assert "2026-10-27 08:15 IST (in 8h 45m)" in out
    assert "data plan       : Active" in out
    assert "2026-11-05 09:37:52.0" in out


async def test_is_a_single_read_only_get(credentials: DhanCredentials) -> None:
    seen: list[httpx.Request] = []
    await dhan_profile(credentials, fake(httpx.Response(200, json=PROFILE), seen), NOW)
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v2/profile")]
    assert seen[0].headers["access-token"] == TOKEN
    assert TOKEN not in str(seen[0].url)


async def test_expired_token_shown(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    later = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)  # 08:30 IST
    await dhan_profile(credentials, fake(httpx.Response(200, json=PROFILE), []), later)
    assert "EXPIRED" in capsys.readouterr().out


async def test_unknown_validity_format_reported(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {**PROFILE, "tokenValidity": "tomorrow-ish"}
    await dhan_profile(credentials, fake(httpx.Response(200, json=body), []), NOW)
    assert "format not recognised" in capsys.readouterr().out


async def test_real_response_shape(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shape and formats of a real /v2/profile response (values replaced with fake ones)."""
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    await dhan_profile(credentials, fake(httpx.Response(200, json=body), []), NOW)
    out = capsys.readouterr().out
    assert (
        "fields returned : activeSegment, dataPlan, dataValidity, ddpi, dhanClientId, mtf, "
        "tokenValidity"
    ) in out
    assert "2026-10-27 09:10 IST (in 9h 40m)" in out
    assert "1000000001" not in out


async def test_rejected_token_never_echoes_body(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {"errorType": "Invalid_Authentication", "errorCode": "DH-901", "errorMessage": TOKEN}
    code = await dhan_profile(credentials, fake(httpx.Response(401, json=body), []), NOW)
    out, err = capsys.readouterr()
    assert code == 1
    assert "HTTP 401, Dhan error code DH-901" in err
    assert_nothing_sensitive(out + err)


async def test_unreachable(
    credentials: DhanCredentials, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await dhan_profile(credentials, client, NOW) == 1
    out, err = capsys.readouterr()
    assert "unreachable (ConnectError)" in err
    assert_nothing_sensitive(out + err)


def test_missing_token_exits_without_calling_dhan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DHAN_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    assert main(["dhan", "profile"]) == 1
    err = capsys.readouterr().err
    assert "DHAN_ACCESS_TOKEN" in err
    assert CLIENT_ID not in err
