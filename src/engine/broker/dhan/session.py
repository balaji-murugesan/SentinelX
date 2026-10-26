"""Dhan session: read the profile endpoint to learn token expiry and Data API plan status.

Read-only: one ``GET /v2/profile`` (DhanHQ v2 docs), authenticated with the ``access-token``
header. The response also carries personal/account data (client ID and more); only the values
listed in SAFE_VALUE_FIELDS are ever kept. Everything else is reduced to its field name, so it
can never reach logs or the terminal (CLAUDE.md safety rule 10).

Used by ``python -m cli dhan profile`` now and by the engine's startup check (task 3.2).
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
import structlog

from engine.market_data.clock import IST, ist_date, market_close_utc, utc_now

log = structlog.get_logger("engine.dhan")

# The only response values we keep. Field names confirmed against a real response on
# 2026-10-27: activeSegment, dataPlan, dataValidity, ddpi, dhanClientId, mtf, tokenValidity.
TOKEN_VALIDITY = "tokenValidity"
DATA_PLAN = "dataPlan"
DATA_VALIDITY = "dataValidity"
SAFE_VALUE_FIELDS = (TOKEN_VALIDITY, DATA_PLAN, DATA_VALIDITY)

# tokenValidity as Dhan sends it (real response, 2026-10-27): "27/10/2026 09:10", i.e.
# day/month/year hour:minute in IST wall-clock time. Anything else is reported as unknown.
_VALIDITY_FORMAT = "%d/%m/%Y %H:%M"
_TIMEOUT = httpx.Timeout(10.0)


class ProfileError(Exception):
    """The profile call failed. The message holds status/code only, never a response body."""

    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        # True: no answer from Dhan (network/timeout). False: Dhan answered with an error.
        self.unreachable = unreachable


@dataclass(frozen=True)
class ProfileSummary:
    field_names: tuple[str, ...]
    token_validity_raw: str | None
    # None if the raw text is missing or in an unrecognised format.
    token_expiry_utc: datetime | None
    data_plan: str | None
    data_validity: str | None


async def fetch_profile(client: httpx.AsyncClient, api_base: str, token: str) -> ProfileSummary:
    """GET /profile and reduce the response to a ProfileSummary. Raises ProfileError."""
    try:
        response = await client.get(
            f"{api_base.rstrip('/')}/profile",
            headers={"access-token": token, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        # Exception text from httpx names the URL (no token in it: the token is a header).
        raise ProfileError(f"Dhan unreachable ({type(exc).__name__})", unreachable=True) from None
    if response.status_code != 200:
        raise ProfileError(f"Dhan rejected the profile request: {_describe_error(response)}")
    try:
        payload = response.json()
    except ValueError:
        raise ProfileError("Dhan returned a non-JSON profile response") from None
    if not isinstance(payload, dict):
        raise ProfileError("Dhan returned an unexpected profile response shape")
    return summarize_profile(payload)


def summarize_profile(payload: Mapping[str, object]) -> ProfileSummary:
    raw = _text(payload.get(TOKEN_VALIDITY))
    return ProfileSummary(
        field_names=tuple(sorted(payload)),
        token_validity_raw=raw,
        token_expiry_utc=parse_token_validity(raw) if raw else None,
        data_plan=_text(payload.get(DATA_PLAN)),
        data_validity=_text(payload.get(DATA_VALIDITY)),
    )


def parse_token_validity(text: str) -> datetime | None:
    """Parse Dhan's tokenValidity (IST wall-clock) into a UTC instant, or None if unknown."""
    try:
        local = datetime.strptime(text.strip(), _VALIDITY_FORMAT)
    except ValueError:
        return None
    return local.replace(tzinfo=IST).astimezone(UTC)


# ---- engine startup check (spec broker-session "Startup session check") ------------------


class SessionStatus(StrEnum):
    VALID = "valid"
    TOKEN_REJECTED = "token_rejected"  # Dhan refused the token (expired, invalid, wrong ID)
    TOKEN_EXPIRED = "token_expired"  # Dhan answered, but tokenValidity is already past
    PLAN_INACTIVE = "plan_inactive"  # Data API plan not active: no live feed possible
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class SessionCheck:
    status: SessionStatus
    reason: str
    expires_at: datetime | None = None  # UTC; None if unknown or the check failed

    @property
    def ok(self) -> bool:
        return self.status is SessionStatus.VALID


async def check_session(
    client: httpx.AsyncClient, api_base: str, token: str, now: datetime
) -> SessionCheck:
    """One profile call at engine startup; never retried (spec "No retry loop").

    The caller connects the feed only if the result is ``ok``. A failed check leaves market
    data unavailable until the engine is restarted with a new token: retrying a dead token
    would only add load on Dhan and noise in the logs.
    """
    try:
        summary = await fetch_profile(client, api_base, token)
    except ProfileError as exc:
        status = SessionStatus.UNREACHABLE if exc.unreachable else SessionStatus.TOKEN_REJECTED
        hint = "" if exc.unreachable else "; supply a new DHAN_ACCESS_TOKEN and restart"
        return _logged(SessionCheck(status, f"{exc}{hint}"))

    expires = summary.token_expiry_utc
    if expires is not None and expires <= now:
        return _logged(
            SessionCheck(
                SessionStatus.TOKEN_EXPIRED,
                "access token has expired; supply a new DHAN_ACCESS_TOKEN and restart",
                expires,
            )
        )
    if summary.data_plan != "Active":
        return _logged(
            SessionCheck(
                SessionStatus.PLAN_INACTIVE,
                f"Data API plan is not active (dataPlan={summary.data_plan!r}); "
                "renew it on web.dhan.co",
                expires,
            )
        )
    if expires is None:
        log.warning(
            "dhan token expiry unknown; no expiry warnings will be given",
            raw=summary.token_validity_raw,
        )
    return _logged(SessionCheck(SessionStatus.VALID, "token valid, Data API plan active", expires))


def _logged(check: SessionCheck) -> SessionCheck:
    fields = {"status": check.status.value}
    if check.expires_at is not None:
        fields["token_expires_at"] = check.expires_at.astimezone(IST).isoformat()
    if check.ok:
        log.info("dhan session ok", **fields)
    else:
        log.error(
            "dhan session check failed; market data unavailable", reason=check.reason, **fields
        )
    return check


# ---- expiry warnings (spec broker-session "Token expiry warnings") ------------------------


async def warn_before_expiry(
    expires_at: datetime,
    warn_before: timedelta,
    interval: timedelta,
    stop: asyncio.Event,
    now: Callable[[], datetime] = utc_now,
) -> None:
    """Log warnings as the token's expiry approaches. Computed locally: no Dhan calls.

    At start: warns if the token expires before today's 15:30 IST close. Then, once inside the
    ``warn_before`` window, warns at most once per ``interval``; logs an error at expiry and
    returns. Ends early when ``stop`` is set.
    """
    start = now()
    close = market_close_utc(ist_date(start))
    if start < close and expires_at < close:
        log.warning(
            "dhan token expires before today's market close (15:30 IST); generate a new one",
            token_expires_at=expires_at.astimezone(IST).isoformat(),
        )
    while True:
        remaining = expires_at - now()
        if remaining <= timedelta(0):
            log.error("dhan access token has expired; market data will stop")
            return
        if remaining <= warn_before:
            log.warning(
                "dhan token expiring",
                token_expires_at=expires_at.astimezone(IST).isoformat(),
                minutes_left=int(remaining.total_seconds() // 60),
            )
            pause = min(interval, remaining)
        else:
            pause = remaining - warn_before  # sleep until the warning window opens
        if await _stopped_within(stop, pause):
            return


async def _stopped_within(stop: asyncio.Event, pause: timedelta) -> bool:
    """Wait up to ``pause``; True if ``stop`` was set meanwhile."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=pause.total_seconds())
    except TimeoutError:
        return False
    return True


def _describe_error(response: httpx.Response) -> str:
    """HTTP status plus Dhan's error code if present; never the message body itself."""
    code = None
    try:
        body = response.json()
        if isinstance(body, dict):
            code = _text(body.get("errorCode"))
    except ValueError:
        pass
    return f"HTTP {response.status_code}" + (f", Dhan error code {code}" if code else "")


def _text(value: object) -> str | None:
    return None if value is None else str(value)
