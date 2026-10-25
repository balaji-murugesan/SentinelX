"""Engine run loop: survives Redis being down, and stops cleanly on request (task 4.1).

Uses a stand-in Redis whose every call fails, so no server is needed: the point is that the
engine neither crashes nor stops beating when Redis is unavailable.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from contracts.settings import load_settings
from engine.config import EngineSettings
from engine.diagnostics import answer_pings
from engine.healthcheck import is_healthy
from engine.main import run


class DownRedis:
    """Every command raises, like a Redis server that is not there."""

    def __init__(self) -> None:
        self.calls = 0

    def _fail(self) -> Any:
        self.calls += 1
        raise RedisConnectionError("redis is down")

    async def xrevrange(self, *_a: Any, **_k: Any) -> Any:
        self._fail()

    async def xread(self, *_a: Any, **_k: Any) -> Any:
        self._fail()


def make_settings(monkeypatch: pytest.MonkeyPatch, heartbeat: Path) -> EngineSettings:
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@postgres:5432/x")
    monkeypatch.setenv("ENGINE_HEARTBEAT_PATH", str(heartbeat))
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    return load_settings(EngineSettings)


async def test_responder_retries_while_redis_is_down_and_stops_on_request() -> None:
    redis = DownRedis()
    stop = asyncio.Event()
    task = asyncio.create_task(answer_pings(redis, stop, retry_seconds=0.01))  # type: ignore[arg-type]
    await asyncio.sleep(0.15)
    assert redis.calls >= 3, "it should keep retrying"
    assert not task.done(), "it must not crash"
    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_engine_keeps_beating_with_redis_down_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "hb"
    settings = make_settings(monkeypatch, heartbeat)
    stop = asyncio.Event()

    engine = asyncio.create_task(run(settings, redis=DownRedis(), stop=stop))  # type: ignore[arg-type]
    await asyncio.sleep(0.2)
    assert is_healthy(heartbeat), "liveness must not depend on Redis"
    assert not engine.done()

    stop.set()  # what SIGTERM does
    await asyncio.wait_for(engine, timeout=2)  # returns normally: a clean shutdown
