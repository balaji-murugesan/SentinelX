"""Developer CLI: ``uv run --env-file .env python -m cli <group> <command>``.

``__main__.py`` is what ``python -m cli`` runs (like a console app's Program.Main).
Commands so far:

  dhan profile           Check today's Dhan token and Data API plan (read-only, one GET
                         /v2/profile). Run it each morning after pasting a fresh
                         DHAN_ACCESS_TOKEN into .env.
  dhan history KEY       One-off intraday-chart request (add-candles task 4.1): prints a
                         summary of Dhan's 1-minute history for KEY, never the token; with
                         --save PATH also writes a trimmed copy as a test fixture.
  instruments search T   Search the instrument master (today's cached file, else a download).
  feed watch KEY...      Print ticks and feed status for KEYs (e.g. IDX_I:13) until Ctrl+C,
                         from FEED_SOURCE (dhan or simulated).
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from contracts.market import Instrument, InstrumentKey
from contracts.settings import SettingsError, load_settings
from engine.broker.dhan.history import HistoryError, fetch_intraday, intraday_body
from engine.broker.dhan.instrument_cache import cache_path, load_instruments
from engine.broker.dhan.instruments_source import parse_instrument_file
from engine.broker.dhan.session import ProfileError, ProfileSummary, fetch_profile
from engine.config import DhanCredentials, EngineSettings
from engine.market_data.clock import IST, ist_date, utc_now
from engine.market_data.instruments import InstrumentMaster, Loader
from engine.market_data.provider import FeedState, FeedStatus, Tick
from engine.market_data.service import MarketDataService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cli", description="SentinelX developer CLI")
    groups = parser.add_subparsers(dest="group", required=True)
    dhan = groups.add_parser("dhan", help="Dhan account checks").add_subparsers(
        dest="command", required=True
    )
    dhan.add_parser(
        "profile",
        help="check token expiry and Data API plan (read-only GET /v2/profile)",
    )
    history = dhan.add_parser(
        "history", help="one-off 1-minute intraday history summary (POST /v2/charts/intraday)"
    )
    history.add_argument("key", type=InstrumentKey.parse, metavar="KEY")
    history.add_argument("--days", type=int, default=3, help="calendar days back (default 3)")
    history.add_argument("--save", type=Path, help="write a trimmed fixture to this path")
    instruments = groups.add_parser("instruments", help="instrument master").add_subparsers(
        dest="command", required=True
    )
    search = instruments.add_parser("search", help="search by symbol or name")
    search.add_argument("text")
    search.add_argument("--limit", type=int, default=20)
    feed = groups.add_parser("feed", help="market data feed").add_subparsers(
        dest="command", required=True
    )
    watch = feed.add_parser("watch", help="print ticks and feed status until Ctrl+C")
    # argparse calls InstrumentKey.parse on each value; a ValueError becomes a usage error.
    watch.add_argument("keys", nargs="+", type=InstrumentKey.parse, metavar="KEY")
    args = parser.parse_args(argv)

    try:
        if args.group == "dhan":
            credentials = load_settings(DhanCredentials)
            if args.command == "history":
                settings = load_settings(EngineSettings)
        else:
            settings = load_settings(EngineSettings)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        # asyncio.run is the sync -> async entry point (like `await` at the top of Main): it
        # creates the event loop, runs the coroutine to completion and closes the loop.
        if args.group == "dhan" and args.command == "history":
            return asyncio.run(dhan_history(settings, credentials, args.key, args.days, args.save))
        if args.group == "dhan":
            return asyncio.run(dhan_profile(credentials))
        if args.group == "instruments":
            return asyncio.run(instruments_search(settings, args.text, args.limit))
        return asyncio.run(feed_watch(settings, args.keys))
    except KeyboardInterrupt:  # Ctrl+C: asyncio.run cancels the tasks, then we exit quietly
        return 0


async def dhan_profile(
    credentials: DhanCredentials,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> int:
    """Print the safe profile summary. Returns the process exit code."""
    # `async with` closes the client afterwards (C# `await using`); a test may inject one.
    async with client or httpx.AsyncClient() as http:
        try:
            summary = await fetch_profile(
                http, credentials.dhan_api_base, credentials.dhan_access_token.get_secret_value()
            )
        except ProfileError as exc:
            print(f"Dhan profile check FAILED: {exc}", file=sys.stderr)
            return 1
    print(format_summary(summary, now or datetime.now(UTC)))
    return 0


def format_summary(summary: ProfileSummary, now: datetime) -> str:
    """Field names, token expiry and data-plan values only. Nothing personal is printed."""
    lines = [
        "Dhan profile (read-only GET /v2/profile)",
        f"  fields returned : {', '.join(summary.field_names)}",
        f"  token expiry    : {_expiry_text(summary, now)}",
        f"  data plan       : {summary.data_plan or '(not in response)'}",
        f"  data validity   : {summary.data_validity or '(not in response)'}",
    ]
    return "\n".join(lines)


def _expiry_text(summary: ProfileSummary, now: datetime) -> str:
    if summary.token_validity_raw is None:
        return "(tokenValidity not in response)"
    if summary.token_expiry_utc is None:
        return f"{summary.token_validity_raw!r} (format not recognised)"
    local = summary.token_expiry_utc.astimezone(IST)
    left = summary.token_expiry_utc - now
    if left.total_seconds() <= 0:
        remaining = "EXPIRED"
    else:
        hours, rest = divmod(int(left.total_seconds()), 3600)
        remaining = f"in {hours}h {rest // 60:02d}m"
    return f"{local:%Y-%m-%d %H:%M} IST ({remaining}); raw {summary.token_validity_raw!r}"


# ---- dhan history (add-candles task 4.1) ---------------------------------------------------

FIXTURE_ROWS = 20  # rows kept from each end of the response by --save
SERIES = ("open", "high", "low", "close", "volume", "timestamp")


async def dhan_history(
    settings: EngineSettings,
    credentials: DhanCredentials,
    key: InstrumentKey,
    days: int,
    save: Path | None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Fetch ``days`` of 1-minute history for ``key`` once and print what task 4.1 records."""
    now = utc_now()
    async with client or httpx.AsyncClient() as http:
        loaded = await cached_loader(settings, http)(ist_date(now))
        master = InstrumentMaster()
        if loaded is not None:
            master.replace(*loaded)
        instrument = master.get(key)
        if instrument is None:
            print(f"{key} is not in the instrument master.", file=sys.stderr)
            return 1
        body = intraday_body(key, instrument.instrument_type, now - timedelta(days=days), now)
        try:
            payload = await fetch_intraday(
                http,
                credentials.dhan_api_base,
                credentials.dhan_access_token.get_secret_value(),
                credentials.dhan_client_id.get_secret_value(),
                body,
            )
        except HistoryError as exc:
            print(f"Dhan history request FAILED: {exc}", file=sys.stderr)
            return 1
    print(f"request: {json.dumps(body)}")
    for line in summarize_history(payload, now):
        print(line)
    if save is not None:
        text = json.dumps(trim_history(payload), indent=1) + "\n"
        await asyncio.to_thread(_write_text, save, text)  # file I/O off the event loop
        print(f"saved trimmed fixture ({2 * FIXTURE_ROWS} rows max) to {save}")
    return 0


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def summarize_history(payload: dict[str, object], now: datetime) -> list[str]:
    """Field names, row count, timestamp conventions and the task 4.1 checks. No values that
    could identify the account (the response holds market data only)."""
    lines = [f"fields: {sorted(payload)}"]
    columns = {name: payload.get(name) for name in SERIES}
    if not all(isinstance(v, list) for v in columns.values()):
        return [*lines, "unexpected shape: not all of " + ", ".join(SERIES) + " are lists"]
    rows = list(zip(*(columns[name] for name in SERIES), strict=False))
    lines.append(f"rows: {len(rows)}")
    if not rows:
        return lines
    for label, row in (("first", rows[0]), ("last", rows[-1])):
        stamp = row[5]
        # The same number read two ways: a true (UTC) epoch shown in IST, or an "IST epoch"
        # whose UTC rendering is already the IST wall-clock time. One of them looks right.
        as_epoch = datetime.fromtimestamp(float(stamp), UTC)
        lines.append(
            f"{label} timestamp raw={stamp!r}: "
            f"if UTC epoch {as_epoch.astimezone(IST):%Y-%m-%d %H:%M:%S} IST, "
            f"if IST epoch {as_epoch:%Y-%m-%d %H:%M:%S} IST"
        )
    flat = sum(1 for r in rows if r[0] == r[1] == r[2] == r[3])
    lines.append(f"zero-volume rows: {sum(1 for r in rows if not r[4])}")
    lines.append(f"flat rows (open=high=low=close): {flat}")
    last = datetime.fromtimestamp(float(rows[-1][5]), UTC)
    lines.append(
        f"now: {now.astimezone(IST):%H:%M:%S} IST; last row is "
        f"{(now - last).total_seconds():.0f} s before now if UTC epoch "
        "(under 60 s means the forming minute is included)"
    )
    return lines


def trim_history(payload: dict[str, object]) -> dict[str, object]:
    """The first and last FIXTURE_ROWS rows of each series, other fields dropped."""
    trimmed: dict[str, object] = {}
    for name in SERIES:
        values = payload.get(name)
        if isinstance(values, list):
            short = len(values) <= 2 * FIXTURE_ROWS
            trimmed[name] = values if short else values[:FIXTURE_ROWS] + values[-FIXTURE_ROWS:]
    return trimmed


# ---- instruments search / feed watch -------------------------------------------------------


def cached_loader(settings: EngineSettings, http: httpx.AsyncClient) -> Loader:
    """Today's cached instrument file if present, else download (the engine's fallback order).

    The engine downloads at every start; the CLI prefers today's cache so repeated commands
    don't re-download the ~40 MB file.
    """

    async def load(today: date) -> tuple[Iterable[Instrument], date] | None:
        path = cache_path(settings.instrument_cache_dir, today)
        if await asyncio.to_thread(path.is_file):
            result = await asyncio.to_thread(parse_instrument_file, path)
            return result.instruments, today
        loaded = await load_instruments(
            http, settings.dhan_instruments_url, settings.instrument_cache_dir, today
        )
        return None if loaded is None else (loaded.result.instruments, loaded.as_of)

    return load


async def instruments_search(
    settings: EngineSettings, text: str, limit: int, client: httpx.AsyncClient | None = None
) -> int:
    async with client or httpx.AsyncClient() as http:
        loaded = await cached_loader(settings, http)(ist_date(utc_now()))
    if loaded is None:
        print("No instrument data: download failed and no cached file.", file=sys.stderr)
        return 1
    master = InstrumentMaster()
    master.replace(*loaded)
    matches = master.search(text, limit)
    for instrument in matches:
        print(format_instrument(instrument))
    if not matches:
        print(f"No instruments match {text!r}.", file=sys.stderr)
    return 0


def format_instrument(i: Instrument) -> str:
    """One line per instrument: key first, so it can be pasted into FEED_INSTRUMENTS."""
    parts = [f"{i.key!s:<16}", f"{i.trading_symbol:<28}", f"{i.instrument_type:<7}"]
    parts.append(f"lot={i.lot_size} tick={i.tick_size}")
    if i.expiry:
        parts.append(f"expiry={i.expiry:%Y-%m-%d}")
    if i.strike is not None:
        parts.append(f"strike={i.strike} {i.option_type or ''}".rstrip())
    parts.append(f"freeze={i.freeze_qty or 'unknown'}")
    return "  ".join(parts)


def _print_now(line: str) -> None:
    """Flush each line: piped stdout is block-buffered and a live watch would look stalled."""
    print(line, flush=True)


async def feed_watch(
    settings: EngineSettings,
    keys: Sequence[InstrumentKey],
    stop: asyncio.Event | None = None,
    *,
    out: Callable[[str], None] = _print_now,
    client: httpx.AsyncClient | None = None,
    load: Loader | None = None,
) -> int:
    """Run the engine's market-data service for ``keys`` only and print what arrives.

    Returns 1 if the feed goes DOWN (bad token, inactive plan), else 0 when stopped.
    """
    stop = stop or asyncio.Event()
    # model_copy(update=...) returns a modified copy (like a C# record `with` expression):
    # the watched keys replace FEED_INSTRUMENTS as the service's prefill.
    watch_settings = settings.model_copy(update={"feed_instruments": ",".join(map(str, keys))})
    async with client or httpx.AsyncClient() as http:
        service = MarketDataService(
            watch_settings, http=http, load=load or cached_loader(settings, http)
        )
        subscription = service.bus.subscribe("cli")
        runner = asyncio.create_task(service.run(stop))
        try:
            while not stop.is_set():
                try:
                    item = await asyncio.wait_for(anext(subscription), timeout=1.0)
                except TimeoutError:
                    continue
                out(format_item(item, service.master))
                if isinstance(item, FeedStatus) and item.state is FeedState.DOWN:
                    return 1
        finally:
            stop.set()
            await runner
    return 0


def format_item(item: Tick | FeedStatus, master: InstrumentMaster) -> str:
    if isinstance(item, FeedStatus):
        return f"-- feed {item.state.value.upper()}: {item.reason}"
    instrument = master.get(item.key)
    name = instrument.trading_symbol if instrument else ""
    received = item.received_at.astimezone(IST)
    exchange = item.exchange_time.astimezone(IST)
    # Lag = receive time - exchange trade time. About 5h30m would mean the LTT time-zone
    # assumption (packets.LTT_IS_IST_EPOCH) is wrong: the check for smoke test 7.2.
    lag_ms = int((item.received_at - item.exchange_time).total_seconds() * 1000)
    return (
        f"{received:%H:%M:%S.%f}"[:-3]
        + f"  {item.key!s:<16} {name:<24} ltp={item.ltp}"
        + f"  ltq={item.ltq if item.ltq is not None else '-'}"
        + f"  vol={item.volume if item.volume is not None else '-'}"
        + f"  exch={exchange:%H:%M:%S}  lag={lag_ms}ms"
    )


if __name__ == "__main__":
    sys.exit(main())
