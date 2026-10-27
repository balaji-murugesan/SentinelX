"""``cli instruments search`` and ``cli feed watch`` (task 6.2). No network: MockTransport only."""

import asyncio
import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest

from cli.__main__ import feed_watch, instruments_search, main
from contracts.market import Instrument, InstrumentKey
from contracts.settings import load_settings
from engine.broker.dhan.instrument_cache import cache_path
from engine.config import EngineSettings
from engine.market_data.clock import ist_date, utc_now

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "instruments_detailed.csv"
NIFTY = InstrumentKey.parse("IDX_I:13")


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> EngineSettings:
    values = {
        "REDIS_URL": "redis://redis:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/x",
        "INSTRUMENT_CACHE_DIR": str(tmp_path / "instruments"),
        "DHAN_INSTRUMENTS_URL": "https://files.test/instruments.csv",
        "FEED_SOURCE": "simulated",
        "SIM_INTERVAL_S": "0.01",
        **env,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_settings(EngineSettings)


def test_help_lists_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "instruments" in out
    assert "feed" in out


def test_watch_rejects_bad_key(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["feed", "watch", "NIFTY"])
    assert info.value.code == 2  # argparse usage error


async def test_search_uses_todays_cache_without_downloading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    cached = cache_path(settings.instrument_cache_dir, ist_date(utc_now()))
    cached.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, cached)

    def no_download(request: httpx.Request) -> httpx.Response:
        raise AssertionError("today's cache exists; nothing should be downloaded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_download))
    assert await instruments_search(settings, "NIFTY", 5, client) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith(str(NIFTY))  # the index ranks first for an exact symbol match


async def test_search_downloads_when_no_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    requests: list[str] = []

    def files(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=FIXTURE.read_bytes())

    client = httpx.AsyncClient(transport=httpx.MockTransport(files))
    assert await instruments_search(settings, "NIFTY", 5, client) == 0
    assert requests == ["https://files.test/instruments.csv"]
    assert "IDX_I:13" in capsys.readouterr().out


async def no_instruments(_today: date) -> tuple[list[Instrument], date] | None:
    return None


async def test_watch_prints_status_then_ticks_for_the_given_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path, FEED_INSTRUMENTS="NSE_EQ:2885")
    stop = asyncio.Event()
    lines: list[str] = []

    def out(line: str) -> None:
        lines.append(line)
        if sum("ltp=" in seen for seen in lines) >= 3:
            stop.set()

    code = await asyncio.wait_for(
        feed_watch(settings, [NIFTY], stop, out=out, load=no_instruments), timeout=5
    )
    assert code == 0
    assert lines[0] == "-- feed CONNECTED: simulated feed"
    ticks = [line for line in lines if "ltp=" in line]
    assert ticks and all("IDX_I:13" in line for line in ticks)  # watched key only, not prefill


async def test_watch_exits_1_when_feed_goes_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(
        monkeypatch,
        tmp_path,
        FEED_SOURCE="dhan",
        DHAN_CLIENT_ID="1100223344",
        DHAN_ACCESS_TOKEN="expired-token",
        DHAN_API_BASE="https://dhan.test/v2",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={"errorCode": "DH-901"}))
    )
    lines: list[str] = []
    code = await asyncio.wait_for(
        feed_watch(settings, [NIFTY], out=lines.append, client=client, load=no_instruments),
        timeout=5,
    )
    assert code == 1
    assert lines[-1].startswith("-- feed DOWN:")
    assert "expired-token" not in "\n".join(lines)
