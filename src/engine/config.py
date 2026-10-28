"""Engine settings: the shared mode/Redis/log settings plus the engine's database access."""

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.market import InstrumentKey
from contracts.mode import TradingMode
from contracts.settings import ModeSettings
from engine.broker.dhan import endpoints
from engine.healthcheck import DEFAULT_HEARTBEAT_PATH
from observability.masking import mask_account


class ClientId(SecretStr):
    """A Dhan client ID: shown as ``****1234`` in repr/str instead of fully hidden.

    Subclassing SecretStr keeps the value out of logs and tracebacks; overriding ``_display``
    (the hook SecretStr uses for repr/str) shows the last four characters, enough to tell
    accounts apart (CLAUDE.md: mask account IDs in logs).
    """

    def _display(self) -> str:
        return mask_account(self.get_secret_value())


class DhanCredentials(BaseSettings):
    """Just the Dhan credentials, for tools such as ``python -m cli dhan profile``."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    dhan_client_id: ClientId
    dhan_access_token: SecretStr
    dhan_api_base: str = endpoints.API_BASE

    @field_validator("dhan_client_id", "dhan_access_token")
    @classmethod
    def _not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value


class EngineSettings(ModeSettings):
    # Engine-role database URL (data read/write only, design decision 14). Required.
    # The DDL-capable migrate role's URL is never part of these settings.
    database_url: SecretStr

    # Where the event loop writes its liveness heartbeat (read by `python -m engine.healthcheck`,
    # which uses the same ENGINE_HEARTBEAT_PATH variable).
    engine_heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH

    # ---- market data (add-market-data) ----------------------------------------------------
    # Required, no default: a production paper run must never fall back to simulated prices.
    feed_source: Literal["dhan", "simulated"]
    # Prefill subscriptions: comma-separated SEGMENT:SECURITY_ID, e.g. "IDX_I:13,NSE_FNO:52175".
    feed_instruments: str = ""
    # Hard cap on streamed instruments (Dhan allows 5,000 per connection).
    feed_max_subscriptions: int = Field(default=200, ge=1, le=5000)
    feed_reconnect_max_s: float = Field(default=30, gt=0)
    # Reconnect if a connected feed is silent this long during market hours.
    feed_silence_s: float = Field(default=30, gt=0)

    # Needed only with FEED_SOURCE=dhan. The token is pasted daily by the owner (valid ~24 h).
    dhan_client_id: ClientId | None = None
    dhan_access_token: SecretStr | None = None
    dhan_token_warn_before_h: float = Field(default=3, gt=0)
    dhan_token_warn_interval_min: float = Field(default=30, gt=0)
    # Overridable only so tests can point at a local fake server.
    dhan_feed_url: str = endpoints.FEED_URL
    dhan_api_base: str = endpoints.API_BASE
    dhan_instruments_url: str = endpoints.INSTRUMENTS_URL

    instrument_cache_dir: Path = Path("/var/lib/sentinelx/instruments")
    # Daily instrument-master refresh time, IST (before the 09:15 open).
    instrument_refresh_ist: time = time(8, 45)

    # SimulatedFeed random walk: start price, max ticks moved per step, seconds between steps,
    # tick size for instruments not in the master, and an optional seed for repeatable runs.
    sim_start_price: Decimal = Field(default=Decimal("100"), gt=0)
    sim_step_ticks: int = Field(default=2, ge=1)
    sim_interval_s: float = Field(default=1.0, gt=0)
    sim_tick_size: Decimal = Field(default=Decimal("0.05"), gt=0)
    sim_seed: int | None = None

    # ---- candles (add-candles) -------------------------------------------------------------
    # Candle intervals in minutes, all anchored to the 09:15 IST open. Only intervals that
    # divide the 375-minute session exactly are allowed, so the day never ends with a short
    # candle: 1, 3, 5, 15.
    candle_intervals: str = "1,3,5,15"
    # Completed candles kept per instrument and interval; also how many warm-up loads aim for.
    candle_history: int = Field(default=200, ge=1, le=5000)
    # Seconds after an interval ends before its candle closes: ticks arrive up to ~1.1 s after
    # their exchange time (smoke test 2026-10-28), so 2 s lets the last trades land.
    candle_close_grace_s: float = Field(default=2, ge=0, le=30)
    # Dhan intraday-history request starts per second (kept under Dhan's data-API limit).
    dhan_history_rate_per_s: float = Field(default=4, gt=0, le=20)

    @field_validator("database_url")
    @classmethod
    def _database_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value

    @field_validator("feed_instruments")
    @classmethod
    def _instrument_keys(cls, value: str) -> str:
        for entry in value.split(","):
            if entry.strip():
                InstrumentKey.parse(entry.strip())  # raises ValueError naming the bad entry
        return value

    @field_validator("candle_intervals")
    @classmethod
    def _candle_intervals(cls, value: str) -> str:
        _parse_intervals(value)  # raises ValueError naming the bad value
        return value

    @model_validator(mode="after")
    def _feed_rules(self) -> "EngineSettings":
        if self.feed_source == "simulated" and self.mode is TradingMode.LIVE:
            raise ValueError(
                "FEED_SOURCE=simulated is not allowed in live mode: "
                "real orders must never be driven by simulated prices"
            )
        if self.feed_source == "dhan":
            for name, secret in (
                ("DHAN_CLIENT_ID", self.dhan_client_id),
                ("DHAN_ACCESS_TOKEN", self.dhan_access_token),
            ):
                if secret is None or not secret.get_secret_value().strip():
                    raise ValueError(f"{name} is required when FEED_SOURCE=dhan")
        if len(self.feed_instrument_keys) > self.feed_max_subscriptions:
            raise ValueError(
                f"FEED_INSTRUMENTS lists {len(self.feed_instrument_keys)} instruments, more than "
                f"FEED_MAX_SUBSCRIPTIONS={self.feed_max_subscriptions}"
            )
        return self

    @property
    def feed_instrument_keys(self) -> tuple[InstrumentKey, ...]:
        """Parsed prefill list, duplicates removed, order kept."""
        entries = (e.strip() for e in self.feed_instruments.split(","))
        # dict.fromkeys keeps first-seen order while dropping duplicates (an ordered set).
        return tuple(dict.fromkeys(InstrumentKey.parse(e) for e in entries if e))

    @property
    def candle_interval_minutes(self) -> tuple[int, ...]:
        """Parsed ``CANDLE_INTERVALS``, sorted ascending, duplicates removed."""
        return _parse_intervals(self.candle_intervals)


# Intervals up to 15 minutes that divide the 375-minute session (09:15-15:30 IST) exactly.
ALLOWED_CANDLE_INTERVALS = (1, 3, 5, 15)


def _parse_intervals(value: str) -> tuple[int, ...]:
    entries = [e.strip() for e in value.split(",") if e.strip()]
    if not entries:
        raise ValueError("must list at least one interval, e.g. 1,3,5,15")
    minutes: set[int] = set()
    for entry in entries:
        if not entry.isdigit() or int(entry) not in ALLOWED_CANDLE_INTERVALS:
            raise ValueError(
                f"{entry!r} is not an allowed interval; use minutes from "
                f"{', '.join(map(str, ALLOWED_CANDLE_INTERVALS))}"
            )
        minutes.add(int(entry))
    return tuple(sorted(minutes))
