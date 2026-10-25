"""Shared pytest fixtures.

An autouse fixture in conftest.py applies to every test without being requested
(think of a global [SetUp] in NUnit/xUnit's fixture base class).
"""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

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
