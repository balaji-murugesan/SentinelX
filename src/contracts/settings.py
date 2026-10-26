"""Settings shared by every service: mode gate, Redis URL, log level.

pydantic-settings is like ASP.NET Core's IOptions<T> bound to environment variables: you
declare a typed class and it fills the fields from the environment and validates them.
Each service subclasses ModeSettings and adds its own fields.
"""

from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.mode import TradingMode, resolve_trading_mode


class SettingsError(Exception):
    """Configuration is invalid. The message lists variable names and reasons only."""


class ModeSettings(BaseSettings):
    """Fields every service needs. Field ``trading_mode`` is read from env var TRADING_MODE, etc."""

    # hide_input_in_errors: a validation error never echoes the values that were supplied.
    # No env_file: values come from the real environment only (Compose injects them; locally
    # use `uv run --env-file .env ...`).
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    # Raw strings on purpose: the gate must see "Live" or "true" exactly as typed.
    trading_mode: str = ""
    live_confirm: str = ""

    # Required (no default): a missing REDIS_URL stops the service. SecretStr masks the value
    # in repr/logs because Redis URLs can contain a password.
    redis_url: SecretStr
    # Literal[...] restricts a str to these exact values (like a string-backed enum); anything
    # else is a validation error at startup.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # OpenTelemetry collector (OTLP/gRPC), e.g. http://otel-collector:4317. Empty = no export.
    # The name is the standard OpenTelemetry variable.
    otel_exporter_otlp_endpoint: str = ""

    # Decorators (@name above a def) wrap or register the function, a little like C# attributes
    # that also change behaviour. @field_validator registers this as the check for one field;
    # pydantic requires it to be a @classmethod (a static-like method that receives the class).
    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _otel_scheme(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("must be empty or start with http:// or https://")
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("redis://", "rediss://")):
            raise ValueError("must start with redis:// or rediss://")
        return value

    # mode="after": runs once every field has been parsed, so it can look at two fields together.
    @model_validator(mode="after")
    def _enforce_mode_gate(self) -> "ModeSettings":
        # Raises ModeConfigError (a ValueError) -> pydantic reports it as a validation error.
        resolve_trading_mode(self.trading_mode, self.live_confirm)
        return self

    @property  # a read-only computed property, like C# `public TradingMode Mode => ...;`
    def mode(self) -> TradingMode:
        """Effective trading mode. Already validated, so this cannot raise."""
        return resolve_trading_mode(self.trading_mode, self.live_confirm)


def load_settings[S: BaseSettings](settings_cls: type[S]) -> S:
    """Build settings from the environment, or raise SettingsError listing what is wrong.

    ``[S: BaseSettings]`` is a generic method with a constraint, like C#
    ``T Load<T>() where T : BaseSettings``. Services pass a ModeSettings subclass; small
    tools (the dev CLI) may pass a narrower settings class.

    Entry points catch SettingsError and exit non-zero, so a bad configuration stops the
    process before it does anything.
    """
    try:
        # Required fields are filled from the environment (BaseSettings' constructor takes no
        # required arguments, so mypy accepts this call for any settings class).
        return settings_cls()
    except ValidationError as exc:
        lines = []
        for err in exc.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in err["loc"]).upper()
            reason = err["msg"].removeprefix("Value error, ")
            lines.append(f"{name}: {reason}" if name else reason)
        raise SettingsError("Invalid configuration:\n  " + "\n  ".join(lines)) from None
