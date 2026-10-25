"""Startup banner and refusing to start on bad config (task 2.4)."""

import json
from collections.abc import Callable

import pytest
from structlog.testing import capture_logs

import api.main
import engine.main
from contracts.mode import TradingMode, startup_banner
from observability.banner import log_startup_banner

REDIS_PASSWORD = "r3dis-pw-SECRET"
DB_PASSWORD = "db-pw-SECRET"

ENTRY_POINTS = pytest.mark.parametrize("entry", [engine.main.start, api.main.start])


def set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "REDIS_URL": f"redis://:{REDIS_PASSWORD}@redis:6379/0",
        "DATABASE_URL": f"postgresql+asyncpg://u:{DB_PASSWORD}@postgres:5432/x",
        # Dhan feed (not simulated) so the live-mode tests are a valid engine configuration.
        "FEED_SOURCE": "dhan",
        "DHAN_CLIENT_ID": "1100223344",
        "DHAN_ACCESS_TOKEN": "test-access-token",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def no_telemetry_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """capture_logs() swaps structlog's processors; the real configure_logging() inside main()
    would replace them and hide the records. The real pipeline is tested separately below."""
    for module in (engine.main, api.main):
        monkeypatch.setattr(module, "configure_logging", lambda *_a, **_k: None)
        monkeypatch.setattr(module, "configure_tracing", lambda *_a, **_k: None)


def test_paper_banner_text() -> None:
    banner = startup_banner("engine", TradingMode.PAPER)
    assert "ENGINE" in banner
    assert "PAPER" in banner
    assert "LIVE" not in banner


def test_live_banner_text() -> None:
    banner = startup_banner("api", TradingMode.LIVE)
    assert "API" in banner
    assert "LIVE" in banner
    assert "REAL ORDERS" in banner


def test_paper_banner_logged_at_info() -> None:
    with capture_logs() as logs:
        log_startup_banner("engine", TradingMode.PAPER)
    assert len(logs) == 1
    assert logs[0]["log_level"] == "info"
    assert logs[0]["mode"] == "paper"
    assert logs[0]["service"] == "engine"


def test_live_banner_logged_at_warning() -> None:
    with capture_logs() as logs:
        log_startup_banner("engine", TradingMode.LIVE)
    assert len(logs) == 1
    assert logs[0]["log_level"] == "warning"
    assert logs[0]["mode"] == "live"
    assert "REAL ORDERS" in logs[0]["event"]


@ENTRY_POINTS
def test_service_start_logs_one_banner_and_no_secrets(
    no_telemetry_setup: None, monkeypatch: pytest.MonkeyPatch, entry: Callable[[], None]
) -> None:
    set_env(monkeypatch)
    with capture_logs() as logs:
        entry()
    assert len(logs) == 1
    assert logs[0]["mode"] == "paper"
    rendered = str(logs)
    assert REDIS_PASSWORD not in rendered
    assert DB_PASSWORD not in rendered


@ENTRY_POINTS
def test_live_start_is_prominent(
    no_telemetry_setup: None, monkeypatch: pytest.MonkeyPatch, entry: Callable[[], None]
) -> None:
    set_env(monkeypatch, TRADING_MODE="live", LIVE_CONFIRM="yes")
    with capture_logs() as logs:
        entry()
    assert [(r["log_level"], r["mode"]) for r in logs] == [("warning", "live")]


@ENTRY_POINTS
@pytest.mark.parametrize(
    "bad", [{"TRADING_MODE": "Live"}, {"TRADING_MODE": "live"}, {"LIVE_CONFIRM": "true"}]
)
def test_bad_config_exits_without_banner_or_secrets(
    no_telemetry_setup: None,
    monkeypatch: pytest.MonkeyPatch,
    entry: Callable[[], None],
    bad: dict[str, str],
) -> None:
    set_env(monkeypatch, **bad)
    with capture_logs() as logs, pytest.raises(SystemExit) as info:
        entry()
    assert logs == []  # nothing announced: the service never started
    message = str(info.value)
    assert message.startswith("Invalid configuration")
    assert REDIS_PASSWORD not in message
    assert DB_PASSWORD not in message


@ENTRY_POINTS
def test_real_logging_pipeline_prints_json_banner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: Callable[[], None],
) -> None:
    """End to end: main() configures logging, and the banner comes out as masked JSON."""
    set_env(monkeypatch, TRADING_MODE="live", LIVE_CONFIRM="yes")
    entry()
    out = capsys.readouterr().out
    (line,) = out.splitlines()
    record = json.loads(line)
    assert record["level"] == "warning"
    assert record["mode"] == "live"
    assert record["service"] in {"engine", "api"}
    assert "REAL ORDERS" in record["message"]
    assert REDIS_PASSWORD not in out
    assert DB_PASSWORD not in out
