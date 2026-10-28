"""Shared pytest fixtures.

An autouse fixture in conftest.py applies to every test without being requested
(think of a global [SetUp] in NUnit/xUnit's fixture base class).
"""

import socket
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from observability.tracing import shutdown_tracing

BLOCKED_SUFFIX = "dhan.co"


class RealBrokerCallBlocked(RuntimeError):
    """Raised when a test tries to reach Dhan. Deliberately not an OSError so HTTP clients
    cannot swallow or retry it as an ordinary connection failure."""


def _is_dhan(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    if not isinstance(host, str):
        return False
    host = host.lower().rstrip(".")
    return host == BLOCKED_SUFFIX or host.endswith("." + BLOCKED_SUFFIX)


def _blocked(host: object) -> RealBrokerCallBlocked:
    return RealBrokerCallBlocked(
        f"Test tried to contact {host!r}. Tests must never call the real Dhan API "
        "(CLAUDE.md safety rule 11): use PaperBroker, SimulatedFeed or recorded fixtures."
    )


@pytest.fixture(autouse=True)
def block_real_dhan(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any DNS lookup or connection aimed at *.dhan.co, whatever client library is used."""
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_dhan(host):
            raise _blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_dhan(address[0]):
            raise _blocked(address[0])
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    """configure_logging()/configure_tracing() change global state; undo it after each test."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    shutdown_tracing()


CONFIG_VARS = (
    "TRADING_MODE",
    "LIVE_CONFIRM",
    "REDIS_URL",
    "DATABASE_URL",
    "LOG_LEVEL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "FEED_SOURCE",
    "FEED_INSTRUMENTS",
    "FEED_MAX_SUBSCRIPTIONS",
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "CANDLE_INTERVALS",
    "CANDLE_HISTORY",
    "CANDLE_CLOSE_GRACE_S",
    "DHAN_HISTORY_RATE_PER_S",
    "ACCOUNT_ID",
    "PAPER_SLIPPAGE_TICKS",
    "PAPER_PRICE_MAX_AGE_S",
    "ORDER_ACK_TIMEOUT_S",
    "SNAPSHOT_INTERVAL_S",
)


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with none of the config variables set, so a developer's shell
    (or a stray TRADING_MODE=live) can never leak into a test."""
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
