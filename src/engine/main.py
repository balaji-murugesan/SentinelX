"""Engine entry point (``uv run --env-file .env python -m engine.main``).

Validates configuration, announces its mode and account, keeps a liveness heartbeat, answers
diagnostic pings, streams market data, and runs the order path: recovery, then the command
consumer, paper broker, order updates and the Redis snapshot, and the risk engine (limits, the
gate every order passes, the monitor, catastrophic stops, kill switch and square-off; see
``engine.risk.runtime``). Paper broker only (no ``DhanBroker`` yet).
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog
from opentelemetry import trace
from redis.asyncio import Redis

from contracts.commands import CommandResult, ResumeRecovery
from contracts.mode import TradingMode
from contracts.settings import SettingsError, load_settings
from engine.broker.paper import PaperBroker
from engine.config import EngineSettings
from engine.diagnostics import answer_pings
from engine.healthcheck import heartbeat_loop
from engine.market_data.service import MarketDataService
from engine.orders.commands import CommandConsumer
from engine.orders.recovery import recover_with_retry, resume_recovery
from engine.orders.service import OrderService
from engine.orders.snapshot import SnapshotWriter
from engine.persistence.postgres import PostgresRepository
from engine.persistence.postgres_risk import PostgresRiskRepository
from engine.persistence.repository import TradingRepository
from engine.positions.feed import PositionFeed
from engine.risk.ratelimit import RateLimitedAdapter
from engine.risk.repository import RiskRepository
from engine.risk.runtime import RiskRuntime
from observability.banner import log_halted_banner, log_startup_banner
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
    log_startup_banner("engine", settings.mode, settings.feed_source, settings.account_id)
    return settings


def _stop_on_signals(stop: asyncio.Event) -> None:
    """SIGTERM is what ``docker stop`` sends; set the stop event so everything winds down."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev machines: Ctrl+C still raises KeyboardInterrupt
            pass


async def _after(event: asyncio.Event, make: Callable[[], Awaitable[None]]) -> None:
    """Start the work once ``event`` is set (like awaiting a TaskCompletionSource in C#). It takes
    a factory, not a coroutine, so nothing is created if the engine stops before the event."""
    await event.wait()
    await make()


async def _until_stopped(stop: asyncio.Event, work: Awaitable[None]) -> None:
    """Run ``work`` (which has no stop event of its own) until it ends or ``stop`` is set, then
    cancel it. A failure in ``work`` propagates."""
    task = asyncio.ensure_future(work)
    stopper = asyncio.ensure_future(stop.wait())
    await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    else:
        task.result()  # re-raise its exception, if any


async def _keep_alive(
    name: str, stop: asyncio.Event, make: Callable[[], Awaitable[None]], *, restart_s: float = 5.0
) -> None:
    """Run a read-model task, restarting it if it crashes: its failure must never stop orders or
    the heartbeat (the Redis snapshot is a read model only; spec "State snapshot in Redis")."""
    log = structlog.get_logger("engine")
    while not stop.is_set():
        try:
            await make()
            return
        except Exception:
            log.exception("task crashed; restarting", task=name, restart_in=restart_s)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=restart_s)


async def run(
    settings: EngineSettings,
    *,
    redis: Redis | None = None,
    stop: asyncio.Event | None = None,
    market_data: MarketDataService | None = None,
    repository: TradingRepository | None = None,
    risk_repository: RiskRepository | None = None,
    snapshot_restart_s: float = 5.0,
) -> None:
    """Run the engine's background tasks until ``stop`` is set (or one of them fails).

    asyncio.TaskGroup is like Task.WhenAll with fail-fast: if any task raises, the others are
    cancelled and the error propagates, so the process exits non-zero and Docker restarts it.
    Market data contains its own failures (design §11), so it never triggers that.

    ``repository`` and ``risk_repository`` exist only so tests can inject in-memory fakes: in
    production both are Postgres, built from ``DATABASE_URL`` (design §13), each with its own
    connection pool so a risk write never queues behind an order write. If Postgres is down the
    engine stays up with new orders refused and keeps retrying recovery.
    """
    stop = stop or asyncio.Event()
    market_data = market_data or MarketDataService(settings)
    owned_redis = redis is None
    redis = redis or Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    postgres = (
        PostgresRepository(settings.database_url.get_secret_value()) if repository is None else None
    )
    repository = repository or postgres
    assert repository is not None
    risk_postgres = (
        PostgresRiskRepository(settings.database_url.get_secret_value())
        if risk_repository is None
        else None
    )
    risk_repository = risk_repository or risk_postgres
    assert risk_repository is not None
    account = settings.account_id
    paper = PaperBroker(
        market_data.bus,
        market_data.master.get,
        slippage_ticks=settings.paper_slippage_ticks,
        max_price_age=timedelta(seconds=settings.paper_price_max_age_s),
    )
    # Every broker call goes through the rate limiter (exits and cancels keep a reserved share).
    # ``service`` is built below: the lambda reads it only when an order is placed.
    broker = RateLimitedAdapter(
        paper,
        per_s=settings.order_rate_per_s,
        per_min=settings.order_rate_per_min,
        exit_share=settings.order_rate_exit_share,
        positions=lambda: service.positions,
        clock=time.monotonic,
    )
    # The risk engine is built first because the order service needs its gate; ``attach`` connects
    # the rest once the service exists (see ``engine.risk.runtime``).
    risk = RiskRuntime(
        settings=settings,
        repository=repository,
        risk_repository=risk_repository,
        instruments=market_data.master.get,
        subscribed=lambda: bool(market_data.subscriptions.keys),
    )
    service = OrderService(
        account_id=account,
        paper=settings.mode is TradingMode.PAPER,
        gate=risk.gate,
        adapter=broker,
        repository=repository,
        instruments=market_data.master,
        position_feed=PositionFeed(market_data.provider),
        tracer=trace.get_tracer("engine.orders"),
        ack_timeout_s=settings.order_ack_timeout_s,
    )
    risk.attach(service)
    assert risk.kill is not None and risk.sizing is not None
    risk_ticks = market_data.bus.subscribe("risk")

    async def resume(command: ResumeRecovery) -> CommandResult:
        return await resume_recovery(service, repository, broker, command, account_id=account)

    consumer = CommandConsumer(
        redis,
        service,
        account_id=account,
        resume=resume,
        set_limits=risk.limits.set_limits,
        size=risk.sizing.size,
        kill=risk.kill.handle,
    )
    snapshots = SnapshotWriter(
        redis,
        service,
        market_data.bus.subscribe("snapshot"),
        account_id=account,
        interval_s=settings.snapshot_interval_s,
        risk=risk.status,
    )
    recovery_started = asyncio.Event()

    async def recover() -> None:
        try:
            differences = await recover_with_retry(
                service,
                repository,
                broker,
                account_id=account,
                stop=stop,
                restore_broker=paper.restore,
                first_attempt=recovery_started,
            )
        finally:
            recovery_started.set()  # never leave the order tasks waiting if recovery fails
        if differences and not stop.is_set():
            log_halted_banner("engine", account, differences)

    _stop_on_signals(stop)
    log = structlog.get_logger("engine")
    log.info("engine running")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat_loop(settings.engine_heartbeat_path, stop))
            group.create_task(answer_pings(redis, stop))
            group.create_task(market_data.run(stop))
            group.create_task(recover())
            # Commands are read only after recovery's first attempt (design §9, §13).
            group.create_task(_after(recovery_started, lambda: consumer.run(stop)))
            group.create_task(
                _after(recovery_started, lambda: _until_stopped(stop, service.run_updates()))
            )
            group.create_task(service.run_pending_writes(stop))
            group.create_task(paper.run(stop))
            # The risk engine starts once recovery has had its first attempt (it needs the stored
            # book). Until it has, no monitor runs and the gate refuses every entry, never an exit.
            # It is supervised too: an unexpected error in it (a corrupt stored row, say) must not
            # take the engine down, which would stop orders and the heartbeat.
            group.create_task(
                _after(
                    recovery_started,
                    lambda: _keep_alive("risk start", stop, lambda: risk.start(stop)),
                )
            )
            for name, make in (
                ("risk monitor", lambda: risk.run_monitor(stop, risk_ticks)),
                ("catastrophic stops", lambda: risk.run_stops(stop)),
                ("risk audit", lambda: risk.audit.run(stop)),
                ("square-off", lambda: risk.run_square_off(stop)),
            ):
                group.create_task(_keep_alive(name, stop, make))
            group.create_task(
                _keep_alive(
                    "snapshot writer",
                    stop,
                    lambda: snapshots.run(stop),
                    restart_s=snapshot_restart_s,
                )
            )
    finally:
        if owned_redis:
            await redis.aclose()
        if postgres is not None:
            await postgres.close()
        if risk_postgres is not None:
            await risk_postgres.close()
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
