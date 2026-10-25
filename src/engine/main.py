"""Engine entry point (``uv run --env-file .env python -m engine.main``).

Stage 0 engine: validates configuration, announces its mode, keeps a liveness heartbeat and
answers diagnostic pings. No trading logic yet.
"""

import asyncio
import signal

import structlog
from redis.asyncio import Redis

from contracts.settings import SettingsError, load_settings
from engine.config import EngineSettings
from engine.diagnostics import answer_pings
from engine.healthcheck import heartbeat_loop
from observability.banner import log_startup_banner
from observability.logging_setup import configure_logging
from observability.tracing import configure_tracing, shutdown_tracing


def start() -> EngineSettings:
    """Load and validate configuration, set up logging and tracing, announce the mode.

    Refuses to start (exits with the error message) on invalid configuration.
    """
    try:
        settings = load_settings(EngineSettings)
    except SettingsError as exc:
        # SystemExit with a string prints it to stderr and exits with status 1: the process
        # refuses to start rather than running with a bad configuration.
        raise SystemExit(str(exc)) from None
    configure_logging("engine", settings.log_level)
    configure_tracing("engine", settings.otel_exporter_otlp_endpoint)
    log_startup_banner("engine", settings.mode)
    return settings


def _stop_on_signals(stop: asyncio.Event) -> None:
    """SIGTERM is what ``docker stop`` sends; set the stop event so everything winds down."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev machines: Ctrl+C still raises KeyboardInterrupt
            pass


async def run(
    settings: EngineSettings, *, redis: Redis | None = None, stop: asyncio.Event | None = None
) -> None:
    """Run the engine's background tasks until ``stop`` is set (or one of them fails).

    asyncio.TaskGroup is like Task.WhenAll with fail-fast: if any task raises, the others are
    cancelled and the error propagates, so the process exits non-zero and Docker restarts it.
    """
    stop = stop or asyncio.Event()
    owned_redis = redis is None
    redis = redis or Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    _stop_on_signals(stop)
    log = structlog.get_logger("engine")
    log.info("engine running")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat_loop(settings.engine_heartbeat_path, stop))
            group.create_task(answer_pings(redis, stop))
    finally:
        if owned_redis:
            await redis.aclose()
        log.info("engine stopped")


def main() -> None:
    settings = start()
    try:
        # asyncio.run creates the event loop, runs the coroutine to completion, and closes the
        # loop: the one place sync code enters async code (like `await Main()` in C#).
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_tracing()  # flush any pending spans before exiting


if __name__ == "__main__":
    main()
