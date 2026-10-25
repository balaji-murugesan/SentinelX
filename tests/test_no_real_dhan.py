"""Proves the autouse guard in conftest.py stops tests reaching Dhan (task 1.5)."""

import socket

import httpx
import pytest

from tests.conftest import RealBrokerCallBlocked, _is_dhan


@pytest.mark.parametrize("host", ["api.dhan.co", "DHAN.CO", "api-feed.dhan.co.", "auth.dhan.co"])
def test_dns_lookup_blocked(host: str) -> None:
    with pytest.raises(RealBrokerCallBlocked):
        socket.getaddrinfo(host, 443)


def test_direct_connection_blocked() -> None:
    with pytest.raises(RealBrokerCallBlocked):
        socket.create_connection(("api.dhan.co", 443), timeout=1)


def test_http_client_blocked() -> None:
    with pytest.raises(RealBrokerCallBlocked), httpx.Client() as client:
        client.get("https://api.dhan.co/v2/orders")


async def test_async_http_client_blocked() -> None:
    with pytest.raises(RealBrokerCallBlocked):
        async with httpx.AsyncClient() as client:
            await client.get("https://api.dhan.co/v2/orders")


def test_other_hosts_unaffected() -> None:
    assert socket.getaddrinfo("localhost", 80)


@pytest.mark.parametrize("host", ["notdhan.co", "dhan.com", "dhan.co.evil.example", "example.com"])
def test_lookalike_hosts_not_matched(host: str) -> None:
    assert not _is_dhan(host)
