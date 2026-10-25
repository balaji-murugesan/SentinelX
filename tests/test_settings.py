"""Settings loading for the engine and api: types, required/invalid values, secret masking,
and the paper/live gate as seen through real environment variables (tasks 2.2 and 2.3)."""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from api.config import ApiSettings
from contracts.mode import TradingMode
from contracts.settings import ModeSettings, SettingsError, load_settings
from engine.config import EngineSettings

REDIS_PASSWORD = "r3dis-pw-SECRET"
DB_PASSWORD = "db-pw-SECRET"
REDIS_URL = f"redis://:{REDIS_PASSWORD}@redis:6379/0"
DATABASE_URL = f"postgresql+asyncpg://sentinelx_engine:{DB_PASSWORD}@postgres:5432/sentinelx"
SECRETS = (REDIS_PASSWORD, DB_PASSWORD)

BOTH = pytest.mark.parametrize("settings_cls", [EngineSettings, ApiSettings])


def set_valid_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "REDIS_URL": REDIS_URL,
        "DATABASE_URL": DATABASE_URL,
        "FEED_SOURCE": "simulated",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def error_text(settings_cls: type[ModeSettings]) -> str:
    with pytest.raises(SettingsError) as info:
        load_settings(settings_cls)
    return str(info.value)


# ---- valid configuration ----------------------------------------------------------


@BOTH
def test_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch)
    settings = load_settings(settings_cls)
    assert settings.mode is TradingMode.PAPER
    assert settings.log_level == "INFO"
    assert settings.redis_url.get_secret_value() == REDIS_URL


def test_engine_has_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)
    assert load_settings(EngineSettings).database_url.get_secret_value() == DATABASE_URL


# ---- required and invalid values --------------------------------------------------


@BOTH
def test_missing_redis_url(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.delenv("REDIS_URL")
    assert "REDIS_URL" in error_text(settings_cls)


def test_engine_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    assert "DATABASE_URL" in error_text(EngineSettings)


@BOTH
def test_invalid_log_level(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch, LOG_LEVEL="LOUD")
    assert "LOG_LEVEL" in error_text(settings_cls)


@BOTH
def test_invalid_redis_scheme(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch, REDIS_URL=f"http://user:{REDIS_PASSWORD}@redis")
    text = error_text(settings_cls)
    assert "REDIS_URL" in text
    assert REDIS_PASSWORD not in text


def test_invalid_database_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch, DATABASE_URL=f"mysql://u:{DB_PASSWORD}@db/x")
    text = error_text(EngineSettings)
    assert "DATABASE_URL" in text
    assert DB_PASSWORD not in text


# ---- secrets are never printed ----------------------------------------------------


@BOTH
def test_settings_repr_masks_secrets(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch)
    settings = load_settings(settings_cls)
    for rendered in (repr(settings), str(settings)):
        assert not any(secret in rendered for secret in SECRETS)
        assert "**********" in rendered


@BOTH
@pytest.mark.parametrize(
    "bad", [{"TRADING_MODE": "Live"}, {"LIVE_CONFIRM": "true"}, {"LOG_LEVEL": "LOUD"}]
)
def test_validation_errors_never_echo_secrets(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings], bad: dict[str, str]
) -> None:
    set_valid_env(monkeypatch, **bad)
    text = error_text(settings_cls)
    assert not any(secret in text for secret in SECRETS)


# ---- the paper/live gate through the environment ----------------------------------


@BOTH
@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_trading_mode_is_paper(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings], value: str | None
) -> None:
    set_valid_env(monkeypatch)
    if value is not None:
        monkeypatch.setenv("TRADING_MODE", value)
    assert load_settings(settings_cls).mode is TradingMode.PAPER


@BOTH
def test_live_with_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    # The engine refuses the simulated feed in live mode, so use the Dhan feed here.
    set_valid_env(
        monkeypatch,
        TRADING_MODE="live",
        LIVE_CONFIRM="yes",
        FEED_SOURCE="dhan",
        DHAN_CLIENT_ID="1100223344",
        DHAN_ACCESS_TOKEN="test-access-token",
    )
    assert load_settings(settings_cls).mode is TradingMode.LIVE


@BOTH
@pytest.mark.parametrize("confirm", [None, ""])
def test_live_without_confirmation_refuses(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings], confirm: str | None
) -> None:
    set_valid_env(monkeypatch, TRADING_MODE="live")
    if confirm is not None:
        monkeypatch.setenv("LIVE_CONFIRM", confirm)
    assert "LIVE_CONFIRM=yes" in error_text(settings_cls)


@BOTH
@pytest.mark.parametrize("mode", ["Live", "LIVE", " live", "prod", "1"])
def test_bad_trading_mode_refuses_instead_of_paper(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings], mode: str
) -> None:
    set_valid_env(monkeypatch, TRADING_MODE=mode, LIVE_CONFIRM="yes")
    assert "TRADING_MODE" in error_text(settings_cls)


@BOTH
@pytest.mark.parametrize("mode", [None, "paper", "live"])
def test_bad_live_confirm_refuses_in_every_mode(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings], mode: str | None
) -> None:
    set_valid_env(monkeypatch, LIVE_CONFIRM="true")
    if mode is not None:
        monkeypatch.setenv("TRADING_MODE", mode)
    assert "LIVE_CONFIRM" in error_text(settings_cls)


@BOTH
def test_live_confirm_alone_stays_paper(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    set_valid_env(monkeypatch, LIVE_CONFIRM="yes")
    assert load_settings(settings_cls).mode is TradingMode.PAPER


# ---- no ambient files, api has no database ----------------------------------------


@BOTH
def test_dotenv_file_in_cwd_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls: type[ModeSettings]
) -> None:
    """Settings read the real environment only, so tests never touch a developer's .env."""
    (tmp_path / ".env").write_text("TRADING_MODE=live\nLIVE_CONFIRM=yes\n")
    monkeypatch.chdir(tmp_path)
    set_valid_env(monkeypatch)
    assert load_settings(settings_cls).mode is TradingMode.PAPER


def test_api_settings_have_no_database_fields() -> None:
    """D-013: the api talks only to Redis, so it must hold no database URL or credentials."""
    forbidden = ("database", "db_", "postgres", "sql", "dsn")
    names = [name.lower() for name in ApiSettings.model_fields]
    assert not [n for n in names if any(word in n for word in forbidden)], names


def test_api_ignores_a_database_url_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)  # DATABASE_URL is set for the engine but must not reach the api
    assert not hasattr(load_settings(ApiSettings), "database_url")


# ---- .env.example (task 2.3) ------------------------------------------------------


@BOTH
def test_env_example_resolves_to_paper(
    monkeypatch: pytest.MonkeyPatch, settings_cls: type[ModeSettings]
) -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    for name, value in dotenv_values(example).items():
        monkeypatch.setenv(name, value or "")
    assert load_settings(settings_cls).mode is TradingMode.PAPER
