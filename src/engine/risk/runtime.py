"""Assembles the risk engine for ``engine.main`` (add-risk-engine task 11.1).

Every piece is built and tested on its own; this module only connects them, so the connections
are in one place and a test can build the same assembly as the engine does.

Build order (the one circular knot): ``OrderService`` needs a gate when it is built, and the gate
needs the service's orders and positions. So ``RiskRuntime(...)`` builds everything that does not
need the service (limits, day book, audit, monitor, gate), the caller builds the service with
``runtime.gate``, and ``attach(service)`` then builds what needs the service (stops, flattener,
kill switch, square-off, sizing), binds the gate and monitor to it and registers the two fill
listeners. Think of it as constructor injection where one dependency is supplied afterwards.

What runs, and what happens if it crashes (``engine.main`` supervises each with ``_keep_alive``):
  * ``start()``           once, after recovery: loads the limits and the day, seeds the day book
                          from the stored positions and the whole fill history. It retries until
                          the database answers; until then no monitor runs and the gate refuses
                          every entry (exits are never refused).
  * ``run_monitor``       the monitor loop; its heartbeat is what the gate reads.
  * ``stops.run``         the catastrophic stops (placement, resize, the morning sweep).
  * ``audit.run``         replays audit rows the database refused.
  * ``run_square_off``    the engine's own exit of intraday positions at the configured time.

What each hook does here:
  * a floor breach (monitor)            -> ``KillSwitch.on_kill``
  * a locked day (monitor, every pass)  -> ``KillSwitch.ensure_flattening``
  * a KILL from a pre-trade rule (gate) -> ``KillSwitch.request`` (scheduled: the gate runs inside
                                           the order service's lock, which the kill's cancels need)

Trading terms: day book - the account's counters for today (entries, losing streak, peak, floor,
halt level); square-off - exiting intraday positions before the broker's own cut-off.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import structlog

from contracts.market import Instrument, InstrumentKey
from contracts.snapshot import HaltLevel, PositionRef, RiskStatus
from engine.config import EngineSettings
from engine.market_data.bus import Item, Subscription
from engine.market_data.clock import utc_now
from engine.orders.service import OrderService
from engine.persistence.repository import PersistenceError, TradingRepository
from engine.positions.book import PositionId
from engine.risk.audit import RiskAudit
from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.daybook import DayBook
from engine.risk.decision import Decision
from engine.risk.gate import RiskEngine
from engine.risk.kill import ChaseFlattener, KillSwitch, SquareOff
from engine.risk.limits_service import LimitsService
from engine.risk.monitor import RiskMonitor
from engine.risk.repository import RiskRepository
from engine.risk.sizing import SizingService
from engine.risk.stops import CatastrophicStops
from observability.banner import log_day_halt_banner

log = structlog.get_logger("engine.risk.runtime")

RETRY_S = 2.0  # how long ``start`` waits between attempts while the database does not answer
SQUARE_OFF_EVERY_S = 1.0


class RiskRuntime:
    def __init__(
        self,
        *,
        settings: EngineSettings,
        repository: TradingRepository,
        risk_repository: RiskRepository,
        instruments: Callable[[InstrumentKey], Instrument | None],
        subscribed: Callable[[], bool],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        account = settings.account_id
        self._account_id = account
        self._repository = repository
        self._instruments = instruments
        self._now = now
        self.stops: CatastrophicStops | None = None
        self.kill: KillSwitch | None = None
        self.square_off: SquareOff | None = None
        self.sizing: SizingService | None = None

        self.audit = RiskAudit(risk_repository)
        self.limits = LimitsService(
            account_id=account, repository=risk_repository, results=repository,
            figures=self._figures, now=now,
        )  # fmt: skip
        self.daybook = DayBook(
            account_id=account, repository=risk_repository, limits=lambda: self.limits.current,
            now=now,
        )  # fmt: skip
        self.monitor = RiskMonitor(
            account_id=account, limits=self.limits, daybook=self.daybook,
            repository=risk_repository, audit=self.audit, instruments=instruments,
            checks=continuous_checks(), subscribed=subscribed,
            interval_s=settings.risk_monitor_interval_s,
            persist_interval_s=settings.risk_persist_interval_s,
            on_kill=self._on_kill, ensure_flat=self._ensure_flat, lock_pending=self._lock_pending,
            unprotected=self._unprotected,
            now=now,
        )  # fmt: skip
        self.gate = RiskEngine(
            account_id=account, mode=settings.mode, limits=self.limits, state=self.monitor,
            audit=self.audit, instruments=instruments, checks=default_checks(),
            on_kill=self._request_kill, now=now,
        )  # fmt: skip
        self._started = asyncio.Event()

    # ---- the pieces that need the order service ----------------------------------------------

    def attach(self, service: OrderService) -> None:
        """Build what needs ``service``, bind the gate and monitor to it, and register the fill
        listeners. Called once, right after the service is built."""
        account = self._account_id
        flattener = ChaseFlattener(
            account_id=account, service=service, limits=self.limits, quote=self.monitor.quote,
            instruments=self._instruments, now=self._now,
        )  # fmt: skip
        self.kill = KillSwitch(
            account_id=account, daybook=self.daybook, service=service, flattener=flattener,
            results=self._repository, now=self._now,
        )  # fmt: skip
        self.square_off = SquareOff(
            limits=self.limits, service=service, flattener=flattener, now=self._now
        )
        self.stops = CatastrophicStops(
            account_id=account, service=service, limits=self.limits,
            instruments=self._instruments, now=self._now,
        )  # fmt: skip
        self.sizing = SizingService(
            account_id=account, limits=self.limits, instruments=self._instruments, now=self._now
        )
        self.gate.bind(service)
        self.monitor.bind(service)
        # The day book counts entries and trades; the stop manager only queues its work from a
        # fill (the listener runs inside the service's lock). Both run for every fill.
        service.add_fill_listener(self.daybook.on_fill)
        service.add_fill_listener(self.stops.on_fill)

    # ---- start-up, after recovery ------------------------------------------------------------

    async def start(self, stop: asyncio.Event) -> None:
        """Load the limits and today's state and seed the day book from the stored book. Retries
        until the database answers (or ``stop``): until then the monitor does not run, so the gate
        refuses every entry and still accepts exits."""
        while not stop.is_set():
            # Each attempt races ``stop``: a database that does not answer takes its timeout to
            # fail, and the engine must not wait that long to shut down.
            attempt = asyncio.ensure_future(self._try_start())
            stopping = asyncio.ensure_future(stop.wait())
            await asyncio.wait({attempt, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if not attempt.done():
                attempt.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await attempt
                return
            if attempt.result():
                self._started.set()
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RETRY_S)

    async def _try_start(self) -> bool:
        if not await self.limits.load() or not await self.daybook.load():
            return False
        # The stored book is read last, right before it is seeded, with no await in between: a fill
        # that lands while the awaits above run is already in the day book (``attach`` registered
        # its listener) and would be wiped by the seed if the book had been read before them.
        try:
            state = await self._repository.load(self._account_id)
        except PersistenceError as error:
            log.error("risk_start_waiting_for_database", error=str(error))
            return False
        # The whole fill history, not only today's: a trade opened before a restart is judged as a
        # whole (see ``DayBook.seed``).
        self.daybook.seed(state.positions, state.fills)
        day = self.daybook.day()
        if day.halt_level > HaltLevel.NONE:
            log_day_halt_banner("engine", self._account_id, day.halt_level.name, day.halt_reason)
        return True

    async def _until_started(self, stop: asyncio.Event) -> bool:
        """Wait for ``start`` to succeed. False if ``stop`` came first (so shutdown never hangs
        on a start that is still waiting for the database)."""
        started = asyncio.ensure_future(self._started.wait())
        stopping = asyncio.ensure_future(stop.wait())
        await asyncio.wait({started, stopping}, return_when=asyncio.FIRST_COMPLETED)
        started.cancel()
        stopping.cancel()
        return self._started.is_set()

    # ---- the supervised tasks ----------------------------------------------------------------

    async def run_monitor(self, stop: asyncio.Event, ticks: Subscription[Item]) -> None:
        if await self._until_started(stop):
            await self.monitor.run(stop, ticks)

    async def run_stops(self, stop: asyncio.Event) -> None:
        assert self.stops is not None
        if await self._until_started(stop):
            await self.stops.run(stop)

    async def run_square_off(self, stop: asyncio.Event) -> None:
        assert self.square_off is not None
        if not await self._until_started(stop):
            return
        while not stop.is_set():
            await self.square_off.maybe_run(self._now())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=SQUARE_OFF_EVERY_S)

    # ---- hooks -------------------------------------------------------------------------------

    async def _on_kill(self, decision: Decision) -> None:
        assert self.kill is not None
        await self.kill.on_kill(decision.reason() or decision.code or "risk")

    async def _ensure_flat(self, reason: str) -> None:
        assert self.kill is not None
        await self.kill.ensure()  # the flatten, and any resting entry still to be cancelled

    def _request_kill(self, decision: Decision) -> None:
        assert self.kill is not None
        self.kill.request(decision.reason() or decision.code or "pre-trade KILL", "risk")

    def _lock_pending(self) -> bool:
        return self.kill is not None and self.kill.pending

    def _unprotected(self) -> frozenset[PositionId]:
        return frozenset() if self.stops is None else self.stops.unprotected()

    def _figures(self) -> tuple[Decimal, Decimal | None] | None:
        """Today's peak and floor, for the floor re-simulation of a limits edit; None while the day
        is not available (not loaded yet, or for another date), so that an edit is refused then
        rather than judged against a made-up peak of zero."""
        if self.daybook.unavailable(self._now()) is not None:
            return None
        day = self.daybook.day()
        return day.peak, day.floor

    # ---- the snapshot's risk block -----------------------------------------------------------

    def status(self) -> RiskStatus | None:
        """The engine's risk figures for the snapshot, or None until the day is loaded."""
        if self.daybook.unavailable(self._now()) is not None:
            return None
        day = self.daybook.day()
        unprotected = sorted(
            (p for p in self._unprotected()), key=lambda p: (str(p[1]), p[2].value)
        )
        return RiskStatus(
            mtm_day=self.monitor.mtm_day(), floor=day.floor, peak=day.peak,
            halt_level=day.halt_level, halt_reason=day.halt_reason,
            entries_today=day.entries_today, entry_lots_today=day.entry_lots_today,
            loss_streak=day.loss_streak,
            cooldown_until=day.cooldown_until,
            unprotected=[PositionRef(key=k, product=prod) for _, k, prod in unprotected],
        )  # fmt: skip
