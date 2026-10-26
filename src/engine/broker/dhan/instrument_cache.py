"""Download Dhan's instrument file into a dated disk cache, falling back to the cache on failure.

Order of preference (spec instruments "Cache and fallback"):
  1. download today's file, check it parses, then keep it as ``instruments-YYYY-MM-DD.csv``;
  2. otherwise the newest cached file that still parses (logged as a warning);
  3. otherwise nothing: the caller reports the master unavailable and the engine keeps running.

A download is written to a ``.part`` file and only renamed into place after it parses, so a
truncated or corrupt download never replaces a good cache file. The file is a public static
file (no token, not a rate-limited API); one download per day plus one per engine start.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import BinaryIO, Literal

import httpx
import structlog

from engine.broker.dhan.instruments_source import (
    InstrumentFileError,
    ParseResult,
    parse_instrument_file,
)

KEEP_CACHE_FILES = 3
_CACHE_NAME = re.compile(r"^instruments-(\d{4}-\d{2}-\d{2})\.csv$")
_TIMEOUT = httpx.Timeout(30.0, read=60.0)

log = structlog.get_logger("engine.instruments")


@dataclass(frozen=True)
class LoadedInstruments:
    result: ParseResult
    # IST trading date the data belongs to (download date, or the cache file's date).
    as_of: date
    source: Literal["download", "cache"]


async def load_instruments(
    client: httpx.AsyncClient, url: str, cache_dir: Path, today: date
) -> LoadedInstruments | None:
    """Return today's instruments, the newest usable cache, or None if neither is available."""
    try:
        result = await download_to_cache(client, url, cache_dir, today)
        return LoadedInstruments(result, today, "download")
    except (httpx.HTTPError, InstrumentFileError, OSError) as exc:
        log.warning("instrument download failed", error=type(exc).__name__, detail=str(exc))

    for cache_date, path in await asyncio.to_thread(_cached_files, cache_dir):
        try:
            result = await asyncio.to_thread(parse_instrument_file, path)
        except (InstrumentFileError, OSError, UnicodeDecodeError) as exc:
            log.error("cached instrument file unusable", file=path.name, error=type(exc).__name__)
            continue
        log.warning("using cached instrument file", cache_date=cache_date.isoformat())
        return LoadedInstruments(result, cache_date, "cache")

    log.error("no instrument data available: download failed and no usable cache")
    return None


async def download_to_cache(
    client: httpx.AsyncClient, url: str, cache_dir: Path, today: date
) -> ParseResult:
    """Download, verify by parsing, then atomically move into the cache. Raises on failure."""
    # Every filesystem call is blocking I/O, so each one runs on a worker thread
    # (asyncio.to_thread, like Task.Run) and the event loop keeps serving ticks meanwhile.
    final = cache_dir / f"instruments-{today.isoformat()}.csv"
    part = final.with_name(final.name + ".part")
    handle = await asyncio.to_thread(_open_part, part)
    try:
        # `async with` is the async form of C#'s `await using`: the response is closed on exit.
        async with client.stream("GET", url, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                await asyncio.to_thread(handle.write, chunk)
        await asyncio.to_thread(handle.close)
        result = await asyncio.to_thread(parse_instrument_file, part)
        await asyncio.to_thread(_commit, part, final)
    finally:
        await asyncio.to_thread(_discard, handle, part)
    log.info(
        "instrument file downloaded",
        instruments=len(result.instruments),
        skipped=sum(result.skipped.values()),
        ignored=result.ignored_rows,
    )
    return result


def _open_part(part: Path) -> BinaryIO:
    part.parent.mkdir(parents=True, exist_ok=True)
    return part.open("wb")


def _commit(part: Path, final: Path) -> None:
    part.replace(final)  # atomic rename on the same filesystem
    _prune(final.parent)


def _discard(handle: BinaryIO, part: Path) -> None:
    """Close and delete the partial file if it is still there (failed or interrupted download)."""
    handle.close()
    part.unlink(missing_ok=True)


def _cached_files(cache_dir: Path) -> list[tuple[date, Path]]:
    """Cache files, newest first."""
    if not cache_dir.is_dir():
        return []
    found = []
    for path in cache_dir.iterdir():
        match = _CACHE_NAME.match(path.name)
        if match:
            found.append((date.fromisoformat(match.group(1)), path))
    return sorted(found, reverse=True)


def _prune(cache_dir: Path) -> None:
    for _, path in _cached_files(cache_dir)[KEEP_CACHE_FILES:]:
        path.unlink(missing_ok=True)
