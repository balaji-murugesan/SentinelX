"""Instrument download, cache and fallback (task 2.3, spec instruments "Cache and fallback").

httpx.MockTransport answers requests in-process (no sockets), like a fake HttpMessageHandler
in .NET, so nothing reaches Dhan.
"""

from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from engine.broker.dhan.instrument_cache import KEEP_CACHE_FILES, load_instruments

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dhan" / "instruments_detailed.csv"
URL = "https://example.test/instruments.csv"
TODAY = date(2026, 10, 26)


def client_returning(respond: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=FIXTURE.read_bytes())


def server_error(_: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route", request=request)


def corrupt(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"NOT,A,REAL,HEADER\n1,2,3,4\n")


def names(directory: Path) -> list[str]:
    """Sync helper: async test bodies must not call blocking Path methods directly (ASYNC240)."""
    return sorted(p.name for p in directory.iterdir())


def seed_cache(cache: Path, day: date, content: bytes | None = None) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"instruments-{day.isoformat()}.csv"
    path.write_bytes(FIXTURE.read_bytes() if content is None else content)
    return path


async def test_download_succeeds_and_is_cached(tmp_path: Path) -> None:
    async with client_returning(ok) as client:
        loaded = await load_instruments(client, URL, tmp_path, TODAY)
    assert loaded is not None
    assert (loaded.source, loaded.as_of) == ("download", TODAY)
    assert len(loaded.result.instruments) == 11
    assert names(tmp_path) == ["instruments-2026-10-26.csv"]


@pytest.mark.parametrize("respond", [server_error, unreachable])
async def test_download_fails_cache_exists(
    tmp_path: Path, respond: Callable[[httpx.Request], httpx.Response]
) -> None:
    seed_cache(tmp_path, date(2026, 10, 20))
    seed_cache(tmp_path, date(2026, 10, 23))
    async with client_returning(respond) as client:
        loaded = await load_instruments(client, URL, tmp_path, TODAY)
    assert loaded is not None
    assert (loaded.source, loaded.as_of) == ("cache", date(2026, 10, 23))


async def test_download_fails_no_cache(tmp_path: Path) -> None:
    async with client_returning(unreachable) as client:
        assert await load_instruments(client, URL, tmp_path / "missing", TODAY) is None


async def test_corrupt_download_keeps_previous_cache(tmp_path: Path) -> None:
    good = seed_cache(tmp_path, date(2026, 10, 23))
    async with client_returning(corrupt) as client:
        loaded = await load_instruments(client, URL, tmp_path, TODAY)
    assert loaded is not None
    assert loaded.as_of == date(2026, 10, 23)
    assert names(tmp_path) == [good.name]  # no .part, no bad file


async def test_unusable_cache_falls_back_to_older(tmp_path: Path) -> None:
    seed_cache(tmp_path, date(2026, 10, 22))
    seed_cache(tmp_path, date(2026, 10, 23), content=b"garbage\n")
    async with client_returning(server_error) as client:
        loaded = await load_instruments(client, URL, tmp_path, TODAY)
    assert loaded is not None
    assert loaded.as_of == date(2026, 10, 22)


async def test_old_cache_files_pruned(tmp_path: Path) -> None:
    for day in (19, 20, 21, 22, 23):
        seed_cache(tmp_path, date(2026, 10, day))
    async with client_returning(ok) as client:
        await load_instruments(client, URL, tmp_path, TODAY)
    cached = names(tmp_path)
    assert len(cached) == KEEP_CACHE_FILES
    assert cached[-1] == "instruments-2026-10-26.csv"
