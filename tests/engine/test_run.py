"""Engine run loop: survives Redis being down, and stops cleanly on request (task 4.1).

Uses a stand-in Redis whose every call fails, so no server is needed: the point is that the
engine neither crashes nor stops beating when Redis is unavailable.
"""

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from contracts.settings import load_settings
from engine.config import EngineSettings
from engine.diagnostics import answer_pings
from engine.healthcheck import is_healthy
from engine.main import run
from engine.market_data.service import MarketDataService
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.trading_fakes import MemoryRepository


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

    async def xgroup_create(self, *_a: Any, **_k: Any) -> Any:
        self._fail()

    async def set(self, *_a: Any, **_k: Any) -> Any:
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

    async def no_instruments(_today: date) -> None:
        return None  # no instrument download in this test

    market_data = MarketDataService(settings, load=no_instruments)
    engine = asyncio.create_task(
        run(
            settings,
            redis=DownRedis(),  # type: ignore[arg-type]
            stop=stop,
            market_data=market_data,
            repository=MemoryRepository(),
            risk_repository=MemoryRiskRepository(),
        )
    )
    await asyncio.sleep(0.2)
    assert is_healthy(heartbeat), "liveness must not depend on Redis"
    assert not engine.done()

    stop.set()  # what SIGTERM does
    await asyncio.wait_for(engine, timeout=2)  # returns normally: a clean shutdown


async def _no_instruments(_today: date) -> None:
    return None  # no instrument download in these tests


@pytest.mark.parametrize("task", ["run_monitor", "run_stops", "run_square_off", "start"])
async def test_a_crashing_risk_task_is_supervised_and_does_not_take_the_engine_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task: str
) -> None:
    """Each risk task (and the start-up that loads the limits and the day) is restarted by
    ``_keep_alive``; a bug in one must not stop orders or the heartbeat (without the supervisor the
    crash would end the whole task group and the engine with it)."""
    from engine.risk.runtime import RiskRuntime

    runs: list[int] = []

    async def crashes(self: RiskRuntime, *args: Any) -> None:
        runs.append(1)
        raise RuntimeError("risk bug")

    monkeypatch.setattr(RiskRuntime, task, crashes)
    heartbeat = tmp_path / "hb"
    settings = make_settings(monkeypatch, heartbeat)
    stop = asyncio.Event()
    engine = asyncio.create_task(
        run(
            settings,
            redis=DownRedis(),  # type: ignore[arg-type]
            stop=stop,
            market_data=MarketDataService(settings, load=_no_instruments),
            repository=MemoryRepository(),
            risk_repository=MemoryRiskRepository(),
        )
    )
    await asyncio.sleep(0.3)

    assert runs == [1]  # it ran and crashed
    assert not engine.done() and is_healthy(heartbeat)  # and the engine carried on

    stop.set()
    await asyncio.wait_for(engine, timeout=2)


async def test_the_engine_hands_the_risk_engine_to_every_part_that_needs_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run`` builds its parts itself, so what each is built with is recorded from outside: the
    order service gets the real gate, the rate limiter reads the service's own position book (it
    tells an exit from an entry by it), the command consumer gets the three risk commands and the
    snapshot writer the risk block."""
    import engine.main as main_module
    from engine.risk.gate import RiskEngine

    seen: dict[str, Any] = {}

    class SpyAdapter(main_module.RateLimitedAdapter):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["positions"] = kwargs["positions"]
            super().__init__(*args, **kwargs)

    class SpyService(main_module.OrderService):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["gate"] = kwargs["gate"]
            seen["service"] = self
            super().__init__(*args, **kwargs)

    class SpyConsumer(main_module.CommandConsumer):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["consumer"] = kwargs
            super().__init__(*args, **kwargs)

    class SpySnapshots(main_module.SnapshotWriter):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["snapshots"] = kwargs
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_module, "RateLimitedAdapter", SpyAdapter)
    monkeypatch.setattr(main_module, "OrderService", SpyService)
    monkeypatch.setattr(main_module, "CommandConsumer", SpyConsumer)
    monkeypatch.setattr(main_module, "SnapshotWriter", SpySnapshots)
    heartbeat = tmp_path / "hb"
    settings = make_settings(monkeypatch, heartbeat)
    stop = asyncio.Event()
    engine = asyncio.create_task(
        run(
            settings,
            redis=DownRedis(),  # type: ignore[arg-type]
            stop=stop,
            market_data=MarketDataService(settings, load=_no_instruments),
            repository=MemoryRepository(),
            risk_repository=MemoryRiskRepository(),
        )
    )
    await asyncio.sleep(0.2)

    assert isinstance(seen["gate"], RiskEngine)
    assert seen["positions"]() is seen["service"].positions
    assert all(seen["consumer"][hook] is not None for hook in ("set_limits", "size", "kill"))
    assert seen["snapshots"]["risk"] is not None

    stop.set()
    await asyncio.wait_for(engine, timeout=2)


def test_engine_refuses_to_start_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trading state needs Postgres: no DATABASE_URL, no engine (design §13)."""
    from engine.main import start

    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as stopped:
        start()

    assert "DATABASE_URL" in str(stopped.value)
