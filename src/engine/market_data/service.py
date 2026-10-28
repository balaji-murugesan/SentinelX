"""Market data inside the engine: source selection, startup order and containment (design §11).

One ``MarketDataService`` owns the tick bus, the instrument master, the subscription registry
and the provider (SimulatedFeed or DhanFeed, chosen by FEED_SOURCE). ``engine.main.run`` starts
``service.run(stop)`` as one task.

Startup order: session check (Dhan only) -> first instrument load -> feed. Each part runs under
``_supervise``, so a crash in market data is logged and contained; it never cancels the
heartbeat or the other engine tasks.

Candles (add-candles) run beside the feed as ``self.candles``. Nothing requests candles at
startup; components (S4 positions, the CLI) call ``candles.request``.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import structlog

from contracts.market import Instrument, InstrumentKey
from engine.broker.dhan.feed import DhanFeed
from engine.broker.dhan.history import DhanHistory
from engine.broker.dhan.instrument_cache import load_instruments
from engine.broker.dhan.session import check_session, warn_before_expiry
from engine.config import EngineSettings
from engine.market_data.bus import TickBus
from engine.market_data.candle_service import CandleService
from engine.market_data.candles import Candle
from engine.market_data.clock import utc_now
from engine.market_data.history_source import HistorySource, HistoryUnavailable
from engine.market_data.instruments import InstrumentMaster, Loader, refresh_loop
from engine.market_data.provider import (
    FeedState,
    FeedStatus,
    MarketDataProvider,
    Subscriptions,
    Tick,
)
from engine.market_data.simulated import SimulatedFeed, SimulatedHistory, WalkConfig

log = structlog.get_logger("engine.market_data")

PREFILL_OWNER = "prefill"  # owner name for FEED_INSTRUMENTS in the subscription registry
REPORT_INTERVAL_S = 60.0


class MarketDataService:
    def __init__(
        self,
        settings: EngineSettings,
        *,
        http: httpx.AsyncClient | None = None,
        load: Loader | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``http`` and ``load`` are injectable so tests never touch the network."""
        self._settings = settings
        self._http = http
        self._load = load
        self._now = now
        self.bus = TickBus()
        self.master = InstrumentMaster()
        self.subscriptions = Subscriptions(settings.feed_max_subscriptions)
        self.provider: MarketDataProvider = self._make_provider()
        self.history = _DeferredHistory()
        if settings.feed_source == "simulated":
            self.history.source = SimulatedHistory(
                self._walk(), tick_size=self._tick_size, now=self._now
            )
        self.candles = CandleService(
            self.provider,
            self.bus,
            self.history,
            intervals=settings.candle_interval_minutes,
            history_size=settings.candle_history,
            grace=timedelta(seconds=settings.candle_close_grace_s),
            now=self._now,
        )

    def _walk(self) -> WalkConfig:
        s = self._settings
        return WalkConfig(
            start_price=s.sim_start_price,
            step_ticks=s.sim_step_ticks,
            interval_s=s.sim_interval_s,
            default_tick=s.sim_tick_size,
            seed=s.sim_seed,
        )

    def _make_provider(self) -> MarketDataProvider:
        s = self._settings
        if s.feed_source == "simulated":
            return SimulatedFeed(
                self.bus,
                self.subscriptions,
                walk=self._walk(),
                tick_size=self._tick_size,
                now=self._now,
            )
        # EngineSettings' validator guarantees both are set when FEED_SOURCE=dhan.
        assert s.dhan_client_id is not None and s.dhan_access_token is not None
        return DhanFeed(
            self.bus,
            self.subscriptions,
            url=s.dhan_feed_url,
            client_id=s.dhan_client_id.get_secret_value(),
            token=s.dhan_access_token.get_secret_value(),
            tick_size=self._tick_size,
            reconnect_max_s=s.feed_reconnect_max_s,
            silence_s=s.feed_silence_s,
            now=self._now,
        )

    def _tick_size(self, key: InstrumentKey) -> Decimal | None:
        instrument = self.master.get(key)
        return instrument.tick_size if instrument else None

    def _instrument_type(self, key: InstrumentKey) -> str | None:
        instrument = self.master.get(key)
        return instrument.instrument_type if instrument else None

    async def run(self, stop: asyncio.Event) -> None:
        """Run every market-data part until ``stop`` is set. Never raises ``Exception``."""
        first_load = asyncio.Event()
        report = self.bus.subscribe("report")  # subscribe before any tick is published
        async with self._client() as http:
            load = self._load or self._downloader(http)
            s = self._settings
            if s.feed_source == "dhan":
                assert s.dhan_client_id is not None and s.dhan_access_token is not None
                self.history.source = DhanHistory(
                    http,
                    api_base=s.dhan_api_base,
                    token=s.dhan_access_token.get_secret_value(),
                    client_id=s.dhan_client_id.get_secret_value(),
                    instrument_type=self._instrument_type,
                    tick_size=self._tick_size,
                    rate_per_s=s.dhan_history_rate_per_s,
                    now=self._now,
                )

            async def load_and_signal(today: date) -> tuple[Iterable[Instrument], date] | None:
                try:
                    return await load(today)
                finally:
                    first_load.set()  # the feed starts after the first attempt, even a failed one

            # TaskGroup: like Task.WhenAll; every child is supervised, so none can fail the group.
            async with asyncio.TaskGroup() as group:
                group.create_task(
                    _supervise(
                        "instruments",
                        refresh_loop(
                            self.master,
                            load_and_signal,
                            self._settings.instrument_refresh_ist,
                            stop,
                            self._now,
                        ),
                    )
                )
                group.create_task(_supervise("report", self._report(report, stop)))
                group.create_task(_supervise("candles", self.candles.run(stop)))
                group.create_task(
                    _supervise("feed", self._run_feed(http, first_load, stop, group), self._down)
                )

    async def _run_feed(
        self,
        http: httpx.AsyncClient,
        first_load: asyncio.Event,
        stop: asyncio.Event,
        group: asyncio.TaskGroup,
    ) -> None:
        s = self._settings
        if s.feed_source == "dhan":
            assert s.dhan_access_token is not None
            check = await check_session(
                http, s.dhan_api_base, s.dhan_access_token.get_secret_value(), self._now()
            )
            if not check.ok:
                # Spec "Engine stays up": no feed, no retry; the rest of the engine runs on.
                self._down(check.reason)
                return
            if check.expires_at is not None:
                group.create_task(
                    _supervise(
                        "token-expiry",
                        warn_before_expiry(
                            check.expires_at,
                            timedelta(hours=s.dhan_token_warn_before_h),
                            timedelta(minutes=s.dhan_token_warn_interval_min),
                            stop,
                            self._now,
                        ),
                    )
                )
        # Stop may come before the first load is even attempted (refresh_loop then never runs
        # its body and never sets first_load), so wait for whichever happens first.
        await _first_set(first_load, stop)
        if stop.is_set():
            return
        if s.feed_instrument_keys:
            await self.provider.subscribe(s.feed_instrument_keys, PREFILL_OWNER)
        await self.provider.run(stop)

    async def _report(
        self, subscription: AsyncIterator[Tick | FeedStatus], stop: asyncio.Event
    ) -> None:
        """Log feed status changes, plus a tick count every REPORT_INTERVAL_S (not every tick)."""
        ticks, last_report = 0, time.monotonic()
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(anext(subscription), timeout=1.0)
            except TimeoutError:
                item = None
            if isinstance(item, FeedStatus):
                log.info("feed status", state=item.state.value, reason=item.reason)
            elif isinstance(item, Tick):
                ticks += 1
            if time.monotonic() - last_report >= REPORT_INTERVAL_S:
                log.info("market data", ticks=ticks, subscribed=len(self.subscriptions.keys))
                ticks, last_report = 0, time.monotonic()

    def _down(self, reason: str) -> None:
        self.bus.publish(FeedStatus(FeedState.DOWN, reason, self._now()))

    def _downloader(self, http: httpx.AsyncClient) -> Loader:
        s = self._settings

        async def download(today: date) -> tuple[Iterable[Instrument], date] | None:
            loaded = await load_instruments(
                http, s.dhan_instruments_url, s.instrument_cache_dir, today
            )
            return None if loaded is None else (loaded.result.instruments, loaded.as_of)

        return download

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """The injected client, or one owned (and closed) by this run.

        @asynccontextmanager turns this generator into something usable with ``async with``:
        code before ``yield`` is setup, code after it is cleanup (like a C# IAsyncDisposable).
        """
        if self._http is not None:
            yield self._http
            return
        async with httpx.AsyncClient() as client:
            yield client


async def _first_set(*events: asyncio.Event) -> None:
    """Return as soon as any of ``events`` is set (like Task.WhenAny over the events)."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


async def _supervise(
    name: str,
    work: Awaitable[None],
    on_failure: Callable[[str], None] | None = None,
) -> None:
    """Run one market-data part; contain its failure so the engine keeps running (design §11).

    ``on_failure`` (optional) is called with a short reason, e.g. to publish a DOWN status.
    """
    try:
        await work
    except Exception as exc:  # CancelledError (shutdown) is a BaseException and passes through
        # No automatic restart: the part stays down until the engine is restarted.
        log.exception("market data component failed", component=name)
        if on_failure is not None:
            # Exception type only: str(exc) could carry a URL or other sensitive text.
            on_failure(f"{name} failed: {type(exc).__name__}")


class _DeferredHistory:
    """The candle service's history source, filled in once its HTTP client exists (Dhan) or
    at construction (simulated). Before that, history is simply not ready yet."""

    def __init__(self) -> None:
        self.source: HistorySource | None = None

    async def minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        if self.source is None:
            raise HistoryUnavailable("history not ready: market data is not running")
        return await self.source.minutes(key, start, end)
