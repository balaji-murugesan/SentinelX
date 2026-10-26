"""Developer CLI: ``uv run --env-file .env python -m cli <group> <command>``.

``__main__.py`` is what ``python -m cli`` runs (like a console app's Program.Main).
Commands so far:

  dhan profile   Check today's Dhan token and Data API plan (read-only, one GET /v2/profile).
                 Run it each morning after pasting a fresh DHAN_ACCESS_TOKEN into .env.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from contracts.settings import SettingsError, load_settings
from engine.broker.dhan.session import ProfileError, ProfileSummary, fetch_profile
from engine.config import DhanCredentials
from engine.market_data.clock import IST


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
    parser.parse_args(argv)  # only `dhan profile` exists so far

    try:
        credentials = load_settings(DhanCredentials)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    # asyncio.run is the sync -> async entry point (like `await` at the top of Main).
    return asyncio.run(dhan_profile(credentials))


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


if __name__ == "__main__":
    sys.exit(main())
