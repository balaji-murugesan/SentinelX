"""Engine liveness: a heartbeat file the event loop refreshes, and the container health check.

The engine opens no network port (see design decision 6). Instead its event loop rewrites a
small file every few seconds. If the loop stalls or dies the file goes stale, and Docker's
health check (``python -m engine.healthcheck``) reports the container unhealthy. That proves
the loop is alive, not that the broker is reachable; richer health arrives with later stages.
"""

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

DEFAULT_HEARTBEAT_PATH = Path("/tmp/sentinelx-engine.heartbeat")  # noqa: S108 (container-local)
HEARTBEAT_INTERVAL_SECONDS = 5.0
MAX_HEARTBEAT_AGE_SECONDS = 15.0  # three missed beats


def write_heartbeat(path: Path, *, now: float | None = None) -> None:
    """Record "alive at <unix time>". Written to a temp file then renamed, so a reader can
    never see a half-written file."""
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(str(time.time() if now is None else now))
    os.replace(temp, path)


def is_healthy(
    path: Path, *, max_age_seconds: float = MAX_HEARTBEAT_AGE_SECONDS, now: float | None = None
) -> bool:
    """True if the heartbeat file exists, parses, and is recent."""
    try:
        beat = float(path.read_text())
    except (OSError, ValueError):
        return False
    age = (time.time() if now is None else now) - beat
    return 0 <= age <= max_age_seconds


async def heartbeat_loop(
    path: Path, stop: asyncio.Event, *, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
) -> None:
    """Refresh the heartbeat until ``stop`` is set. Runs on the engine's own event loop, so a
    blocked loop means no beats, which is exactly what the health check should detect."""
    while not stop.is_set():
        write_heartbeat(path)
        # Sleep for the interval, but wake at once if stop is set. `with` is a context manager
        # (like C# `using`); contextlib.suppress(X) is `try { ... } catch (X) { }` in one line.
        # `await` yields to the event loop while waiting, like C# await on a Task.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


def main() -> None:
    """Container health check: exit 0 if healthy, 1 if not."""
    path = Path(os.environ.get("ENGINE_HEARTBEAT_PATH", str(DEFAULT_HEARTBEAT_PATH)))
    sys.exit(0 if is_healthy(path) else 1)


if __name__ == "__main__":
    main()
