"""Api app basics that need no Redis (task 4.1)."""

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from api.app import create_app
from api.config import ApiSettings
from contracts.settings import load_settings


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    # Never connects: the client is created lazily, and /healthz does not use it.
    client = Redis.from_url("redis://redis:6379/0")
    return create_app(load_settings(ApiSettings), redis_client=client)


async def test_healthz(app: FastAPI) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_does_not_need_redis(app: FastAPI) -> None:
    """Redis is unreachable here (no such host), yet liveness still answers."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).status_code == 200


def test_app_exposes_no_order_endpoints(app: FastAPI) -> None:
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert {"/healthz", "/diagnostics/ping"} <= paths
    assert not any("order" in path for path in paths)
