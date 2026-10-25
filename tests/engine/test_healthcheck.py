"""Heartbeat file and container health check (task 4.1)."""

import asyncio
from pathlib import Path

import pytest

from engine.healthcheck import (
    MAX_HEARTBEAT_AGE_SECONDS,
    heartbeat_loop,
    is_healthy,
    main,
    write_heartbeat,
)


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    write_heartbeat(path, now=1000.0)
    assert is_healthy(path, now=1001.0)


def test_stale_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    write_heartbeat(path, now=1000.0)
    assert is_healthy(path, now=1000.0 + MAX_HEARTBEAT_AGE_SECONDS)  # boundary: still fine
    assert not is_healthy(path, now=1000.0 + MAX_HEARTBEAT_AGE_SECONDS + 0.1)


def test_missing_file_is_unhealthy(tmp_path: Path) -> None:
    assert not is_healthy(tmp_path / "does-not-exist")


@pytest.mark.parametrize("content", ["", "not a number", "\x00\x01"])
def test_garbage_file_is_unhealthy(tmp_path: Path, content: str) -> None:
    path = tmp_path / "hb"
    path.write_text(content)
    assert not is_healthy(path)


def test_heartbeat_from_the_future_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    write_heartbeat(path, now=5000.0)
    assert not is_healthy(path, now=1000.0)  # clock went backwards: do not trust it


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    write_heartbeat(tmp_path / "hb")
    assert [p.name for p in tmp_path.iterdir()] == ["hb"]


async def test_heartbeat_loop_beats_until_stopped(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    stop = asyncio.Event()
    task = asyncio.create_task(heartbeat_loop(path, stop, interval_seconds=0.05))
    await asyncio.sleep(0.02)
    assert is_healthy(path)
    first = path.read_text()
    await asyncio.sleep(0.12)
    assert path.read_text() != first  # it keeps refreshing
    stop.set()
    await asyncio.wait_for(task, timeout=1)


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hb"
    monkeypatch.setenv("ENGINE_HEARTBEAT_PATH", str(path))
    with pytest.raises(SystemExit) as unhealthy:
        main()
    assert unhealthy.value.code == 1

    write_heartbeat(path)
    with pytest.raises(SystemExit) as healthy:
        main()
    assert healthy.value.code == 0
