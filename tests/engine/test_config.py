"""Market-data settings: feed source, Dhan credentials, prefill list and cap (task 1.3).

Scenarios from specs broker-session ("Dhan credentials from environment", "Credentials never
exposed") and market-feed ("Feed source selection", "Simulated feed refused in live mode",
"Malformed entry", "Prefill over the cap").
"""

import pytest

from contracts.market import InstrumentKey
from contracts.settings import SettingsError, load_settings
from engine.config import EngineSettings

TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJzZWNyZXQiOiJ0b2tlbiJ9.c2lnbmF0dXJl"
CLIENT_ID = "1100223344"


@pytest.fixture(autouse=True)
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@postgres:5432/x")


def dhan_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {"FEED_SOURCE": "dhan", "DHAN_CLIENT_ID": CLIENT_ID, "DHAN_ACCESS_TOKEN": TOKEN}
    for name, value in {**values, **overrides}.items():
        monkeypatch.setenv(name, value)


def error_text() -> str:
    with pytest.raises(SettingsError) as info:
        load_settings(EngineSettings)
    return str(info.value)


# ---- feed source ------------------------------------------------------------------


@pytest.mark.parametrize("source", ["dhan", "simulated"])
def test_feed_source_accepted(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    dhan_env(monkeypatch, FEED_SOURCE=source)
    assert load_settings(EngineSettings).feed_source == source


@pytest.mark.parametrize("value", [None, "", "Dhan", "sim", "SIMULATED", " dhan"])
def test_missing_or_unknown_feed_source(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    dhan_env(monkeypatch)
    if value is None:
        monkeypatch.delenv("FEED_SOURCE")
    else:
        monkeypatch.setenv("FEED_SOURCE", value)
    assert "FEED_SOURCE" in error_text()


def test_simulated_refused_in_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_CONFIRM", "yes")
    assert "simulated is not allowed in live mode" in error_text()


def test_dhan_allowed_in_live(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch, TRADING_MODE="live", LIVE_CONFIRM="yes")
    assert load_settings(EngineSettings).feed_source == "dhan"


# ---- credentials ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_dhan_needs_credentials(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    dhan_env(monkeypatch)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    assert name in error_text()


def test_simulated_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    settings = load_settings(EngineSettings)
    assert settings.dhan_access_token is None


def test_settings_repr_masks_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch)
    settings = load_settings(EngineSettings)
    text = repr(settings) + str(settings)
    assert TOKEN not in text
    assert CLIENT_ID not in text
    assert "****3344" in text
    assert settings.dhan_client_id is not None
    assert settings.dhan_client_id.get_secret_value() == CLIENT_ID


def test_error_never_echoes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch, FEED_SOURCE="bogus")
    text = error_text()
    assert TOKEN not in text
    assert CLIENT_ID not in text


# ---- prefill list -----------------------------------------------------------------


def test_prefill_parsed_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch, FEED_INSTRUMENTS=" IDX_I:13, NSE_FNO:52175,,IDX_I:13 ")
    keys = load_settings(EngineSettings).feed_instrument_keys
    assert keys == (InstrumentKey.parse("IDX_I:13"), InstrumentKey.parse("NSE_FNO:52175"))


def test_prefill_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch)
    assert load_settings(EngineSettings).feed_instrument_keys == ()


@pytest.mark.parametrize("bad", ["NIFTY", "NSE_FNO:abc", "nse_fno:1", "XYZ:13"])
def test_malformed_prefill_entry(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    dhan_env(monkeypatch, FEED_INSTRUMENTS=f"IDX_I:13,{bad}")
    text = error_text()
    assert "FEED_INSTRUMENTS" in text
    assert repr(bad) in text


def test_prefill_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(
        monkeypatch, FEED_INSTRUMENTS="IDX_I:13,IDX_I:25,NSE_EQ:1333", FEED_MAX_SUBSCRIPTIONS="2"
    )
    text = error_text()
    assert "FEED_INSTRUMENTS" in text
    assert "FEED_MAX_SUBSCRIPTIONS" in text


@pytest.mark.parametrize("cap", ["0", "5001"])
def test_cap_bounds(monkeypatch: pytest.MonkeyPatch, cap: str) -> None:
    dhan_env(monkeypatch, FEED_MAX_SUBSCRIPTIONS=cap)
    assert "FEED_MAX_SUBSCRIPTIONS" in error_text()


# ---- candles (add-candles task 1.1; spec candles "Configured intervals") -----------------


def test_default_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch)
    settings = load_settings(EngineSettings)
    assert settings.candle_interval_minutes == (1, 3, 5, 15)
    assert settings.candle_history == 200
    assert settings.candle_close_grace_s == 2
    assert settings.dhan_history_rate_per_s == 4


def test_intervals_sorted_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    dhan_env(monkeypatch, CANDLE_INTERVALS="15, 5,5,1")
    assert load_settings(EngineSettings).candle_interval_minutes == (1, 5, 15)


@pytest.mark.parametrize("bad", ["7", "60", "0", "-5", "1.5", "five", "", " , "])
def test_interval_that_does_not_divide_the_session(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    dhan_env(monkeypatch, CANDLE_INTERVALS=f"1,{bad}" if bad.strip(" ,") else bad)
    text = error_text()
    assert "CANDLE_INTERVALS" in text
    if bad.strip(" ,"):
        assert repr(bad) in text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CANDLE_HISTORY", "0"),
        ("CANDLE_HISTORY", "5001"),
        ("CANDLE_CLOSE_GRACE_S", "-1"),
        ("CANDLE_CLOSE_GRACE_S", "31"),
        ("DHAN_HISTORY_RATE_PER_S", "0"),
        ("DHAN_HISTORY_RATE_PER_S", "21"),
    ],
)
def test_candle_setting_bounds(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    dhan_env(monkeypatch, **{name: value})
    assert name in error_text()
