"""The risk monitor: the supervised loop that watches the account between orders (add-risk-engine
tasks 7.1 and 7.2; design §10; stale-data-guard and equity-floor specs).

It does three jobs and is the ``RiskState`` the gate reads:

  1. **Ticks** (``on_tick``): every tick updates the price tracker (``MtmTracker``), so the loss
     side always sees the latest price and the peak only ever sees confirmed, fresh ones. A garbage
     tick (NaN, infinity, zero) is logged as ``RISK_BAD_TICK`` and changes nothing; an outlier is
     logged and audited as ``RISK_OUTLIER_TICK``. This path is arithmetic only.
  2. **A timer** (``step``, about once a second): rolls the day over, computes ``MTM_day``,
     switches the day's expiry exposure on once an open position's contract expires today
     (add-risk-guards task 8.2: persisted, one-way, and from then on the floor uses the stricter
     expiry giveback), raises the peak and the floor (the ratchet), runs the continuous checks
     (floor breach, silent feed, blind positions) and acts on the most severe decision, then
     persists on a throttle. Before it acts on a KILL or a profit-target halt it writes the
     day's state (and goes on if that write fails: the stop is acted on either way).
  3. **A heartbeat**: ``step`` stamps the time it finished. The gate refuses entries if that stamp
     is older than three intervals, so a monitor that has died cannot leave the account unwatched;
     ``engine.main._keep_alive`` restarts it.

What a decision does here: a KILL refuses entries at once (a standing halt), writes the ratchet,
then calls the kill switch (``on_kill``, idempotent, on every pass while the breach holds). A
blind book (``RISK_MTM_BLIND``) raises a persisted day halt (entries are then refused for the day
by the gate) with no broker lock. A confirmed profit target (``RISK_TARGET_HALT``, add-risk-guards
task 9.2; the monitor counts the consecutive passes at or above the target) does the same at
``HaltLevel.TARGET_HALT``, after the KILL branch so a breach in the same pass wins. While the day is
locked, for any of these reasons, the monitor asks
for the flatten on **every** pass (``ensure_flat``, idempotent), so one that failed or was cut
short by a crash starts again. Any other halt is *standing*: the gate refuses entries for as long
as the condition holds, and it clears by itself on the first pass where the check stops firing.
Several checks can fire at once and tie on severity, so each action is taken whenever its own
check fires, not only when it happens to come first.

The monitor never mutates order state and never sends an order itself. It reads positions and
orders through the same ``TradingView`` as the gate, between awaits (atomic in asyncio).

Trading terms: peak - the day's highest MTM; reference - the previous close a carried position is
measured from (written by this monitor at 15:30 for tomorrow); flatten - exit every position.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

import structlog

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Position, ProductType
from contracts.snapshot import HaltLevel
from engine.market_data.bus import Item, Subscription
from engine.market_data.clock import IST, MARKET_OPEN, ist_date, market_close_utc, utc_now
from engine.market_data.provider import Tick
from engine.positions.book import PositionId
from engine.risk.audit import RiskAudit
from engine.risk.banlist import BanListService
from engine.risk.checks.common import money
from engine.risk.checks.flow import REJECT_BREAKER
from engine.risk.checks.stale import MTM_BLIND
from engine.risk.checks.target import TARGET_HALT, next_confirmations
from engine.risk.daybook import NOT_LOADED, DayBook
from engine.risk.decision import (
    ContinuousCheck,
    Decision,
    Quote,
    RiskContext,
    RiskTrigger,
    RuleResult,
    Severity,
    halt_new_entries,
    most_severe,
    no_spot,
    run_continuous,
)
from engine.risk.expiry import expires_today
from engine.risk.floor import next_floor
from engine.risk.gate import TradingView
from engine.risk.limits import STATE_UNAVAILABLE, RiskLimits
from engine.risk.limits_service import LimitsService, LimitsStatus
from engine.risk.mtm import MtmReading, MtmTracker, TickKind
from engine.risk.repository import RiskEvent, RiskRepository
from engine.risk.state import DayState

log = structlog.get_logger("engine.risk.monitor")

BAD_TICK: Final = "RISK_BAD_TICK"
OUTLIER_TICK: Final = "RISK_OUTLIER_TICK"
REFERENCE_MISSING: Final = "RISK_REFERENCE_MISSING"
MTM_UNKNOWN: Final = "RISK_MTM_UNKNOWN"
BEATS_TOLERATED: Final = 3  # the gate refuses entries after this many intervals without a beat
# Decisions below KILL whose start is still logged as critical (an alert for the owner), once per
# episode: the rejection breaker tripping (order-flow-guards "Broker rejections trip a breaker").
CRITICAL_ON_START: Final = frozenset({REJECT_BREAKER})

type KillHook = Callable[[Decision], Awaitable[None]]
type EnsureFlatHook = Callable[[str], Awaitable[None]]


def mark_key(pid: PositionId) -> str:
    """How a position is named in the day state's ``close_marks``: ``SEGMENT:ID|PRODUCT``."""
    _, key, product = pid
    return f"{key}|{product.value}"


def parse_mark_key(account_id: str, text: str) -> PositionId:
    key, _, product = text.partition("|")
    return account_id, InstrumentKey.parse(key), ProductType(product)


def previous_weekday(day: date) -> date:
    """The weekday before ``day``: where a carried position's previous close was written. There is
    no holiday calendar yet (``engine.market_data.clock``), so only weekends are skipped."""
    before = day - timedelta(days=1)
    while before.weekday() >= 5:  # Saturday, Sunday
        before -= timedelta(days=1)
    return before


class RiskMonitor:
    def __init__(
        self,
        *,
        account_id: str,
        limits: LimitsService,
        daybook: DayBook,
        repository: RiskRepository,
        audit: RiskAudit,
        instruments: Callable[[InstrumentKey], Instrument | None],
        checks: Sequence[ContinuousCheck],
        subscribed: Callable[[], bool],
        interval_s: float,
        persist_interval_s: float,
        on_kill: KillHook | None = None,
        ensure_flat: EnsureFlatHook | None = None,
        lock_pending: Callable[[], bool] = lambda: False,
        unprotected: Callable[[], frozenset[PositionId]] = frozenset,
        rejections: Callable[[datetime], tuple[datetime, ...]] = lambda now: (),
        ban_lists: BanListService | None = None,
        spot_key: Callable[[str], InstrumentKey | None] = no_spot,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``on_kill`` is called with the KILL decision on every pass while a breach holds, so it
        must be idempotent and return promptly. ``ensure_flat`` is called on every pass while the
        day is locked and asks for the book to be flattened; it is idempotent too (it starts a
        flatten only if none is running), which is how a flatten cut short by a crash resumes.
        ``lock_pending`` says a kill was asked for while today's state could not be read and is
        still waiting to be recorded on the day: the monitor then asks ``ensure_flat`` too, even
        though the loaded day is not locked yet, which is how that kill reaches the day.
        ``unprotected`` returns the open positions that have no catastrophic stop. ``rejections``
        returns the times of the broker rejections within 300 s of the time it is given
        (``RejectionWindow.recent``); the default, none, suits a monitor with no order service.
        ``ban_lists`` holds the F&O ban lists: the monitor retries its read on every pass while it
        is unread, and serves today's list to the gate (``ban_list``); without one no list is ever
        loaded, so F&O entries are refused. ``spot_key`` maps an underlying symbol to its spot
        instrument, for the instrument policy's strike distance (default: none known)."""
        self._account_id = account_id
        self._limits = limits
        self._daybook = daybook
        self._repo = repository
        self._audit = audit
        self._instruments = instruments
        self._checks = tuple(checks)
        self._subscribed = subscribed
        self._interval = timedelta(seconds=interval_s)
        self._interval_s = interval_s
        self._persist_every = timedelta(seconds=persist_interval_s)
        self._on_kill = on_kill
        self._ensure_flat = ensure_flat
        self._lock_pending = lock_pending
        self._unprotected = unprotected
        self._rejections = rejections
        self._ban_lists = ban_lists
        self._spot_key = spot_key
        self._now = now
        self._trading: TradingView | None = None
        self._tracker: MtmTracker | None = None
        self._applied: RiskLimits | None = None  # the limits the tracker was last configured with
        self._last_limits: RiskLimits | None = None  # the last limits that were in force
        self._reading: MtmReading | None = None
        # Consecutive passes with MTM_day at or above the profit target (add-risk-guards task 9.1).
        # Memory only: a restart starts it at zero, and the target halt is in the day row anyway.
        self._target_confirmations = 0
        self._standing: Decision | None = None
        self._outliers: list[Tick] = []  # outlier ticks seen since the last pass, to be audited
        self._beat: datetime | None = None
        self._started_at = now()
        self._last_tick_at: datetime | None = None
        self._watch_since: dict[InstrumentKey, datetime] = {}
        self._references: dict[PositionId, Decimal] = {}
        self._references_for: date | None = None
        self._noticed: set[str] = set()  # notices already audited today (reference missing)
        self._active_codes: set[str] = set()  # the codes the checks are raising right now
        self._last_persist = now()
        self._marks_written_for: date | None = None

    def bind(self, trading: TradingView) -> None:
        self._trading = trading

    # ---- the RiskState the gate reads ------------------------------------------------------

    def unavailable(self, now: datetime) -> str | None:
        """Why entries must be refused as ``RISK_STATE_UNAVAILABLE``: the monitor is not connected
        to trading state, has not run, has stopped beating, or the day's counters cannot be
        trusted."""
        if self._trading is None:
            return "the risk monitor is not connected to trading state"
        if self._beat is None:
            return "the risk monitor has not run yet"
        if now - self._beat > BEATS_TOLERATED * self._interval:
            return f"the risk monitor has not run for {(now - self._beat).total_seconds():.0f}s"
        return self._daybook.unavailable(now)

    def day(self) -> DayState:
        return self._daybook.day()

    def mtm_day(self) -> Decimal | None:
        return None if self._reading is None else self._reading.mtm_day

    def quote(self, key: InstrumentKey) -> Quote | None:
        tracker = self._tracker
        price = None if tracker is None else tracker.latest_price(key)
        age = None if tracker is None else tracker.age(key, self._now())
        return None if price is None or age is None else Quote(price, age)

    def entry_halt(self) -> Decision | None:
        return self._standing

    def recent_entries(self, now: datetime) -> tuple[datetime, ...]:
        return self._daybook.recent_entries(now)

    def broker_rejections(self, now: datetime) -> tuple[datetime, ...]:
        return self._rejections(now)

    def ban_list(self, now: datetime) -> frozenset[str] | None:
        return None if self._ban_lists is None else self._ban_lists.for_now(now)

    async def entry_allowed(self, lots: int, expiring: bool) -> str | None:
        return await self._daybook.entry_allowed(lots, expiring)

    # ---- ticks -----------------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        """Feed one tick to the tracker. Cheap: arithmetic and a log line, no I/O."""
        tracker = self._tracker
        if tracker is None:
            return  # no limits yet, so no thresholds to judge a price by
        kind = tracker.on_tick(tick)
        if kind is TickKind.REJECTED:
            log.warning("bad tick ignored", code=BAD_TICK, key=str(tick.key))
            return  # garbage is not a tick: it must not make a silent feed look alive
        self._last_tick_at = max(self._last_tick_at or tick.received_at, tick.received_at)
        if kind is TickKind.OUTLIER:
            log.warning("outlier tick awaiting confirmation", code=OUTLIER_TICK, key=str(tick.key))
            self._outliers.append(tick)

    # ---- the timer -------------------------------------------------------------------------

    async def step(self) -> None:
        """One pass of the monitor. Never raises: a failure is logged and the pass is abandoned.
        The heartbeat is stamped regardless, so one bad pass does not look like a dead monitor;
        it shows instead as a standing halt of new entries (``RISK_STATE_UNAVAILABLE``)."""
        try:
            await self._pass()
        except Exception:
            log.error("risk monitor pass failed", exc_info=True)
            self._target_confirmations = 0  # a failed pass did not see MTM_day at the target
            self._standing = halt_new_entries(
                STATE_UNAVAILABLE, "the risk monitor failed while watching the account"
            )
        self._beat = self._now()

    async def _pass(self) -> None:
        now = self._now()
        if self._limits.status is LimitsStatus.NOT_LOADED:
            await self._limits.load()
        if not await self._ensure_day(now):
            self._reading, self._standing = None, None  # entries are refused for that reason
            self._target_confirmations = 0  # MTM_day is unknown on this pass
            return
        trading = self._trading
        # If the limits are lost mid-run (a failed save that could not be read back), keep
        # watching with the last ones in force: the floor must still be able to kill. Entries are
        # refused meanwhile by the gate, for the limits' own reason.
        limits = self._limits.current or self._last_limits
        if limits is None or trading is None:
            self._reading, self._standing = None, None  # nothing to watch against yet
            self._target_confirmations = 0
            return
        self._last_limits = limits
        references_ok = await self._ensure_references(now)
        tracker = self._sync_tracker(limits)
        # With the previous close unavailable, the day's MTM is unknown for this pass (which
        # refuses entries): measuring from average prices would raise the peak and floor, and
        # they never come back down.
        self._reading = self._evaluate(tracker, now) if references_ok else None
        # Counted before the checks run, from this pass's MTM_day: a dip, an unknown MTM_day or no
        # target set puts it back to zero (profit-target "A dip resets the count").
        self._target_confirmations = next_confirmations(
            self._target_confirmations, self.mtm_day(), limits.daily_profit_target
        )
        exposure_switched_on = self._note_expiry_exposure(now)  # before the ratchet uses it
        self._ratchet(tracker, limits)
        results = self._judge(limits, trading, now)
        await self._act(results)
        await self._keep_flattening()
        await self._record(results, now)  # after acting: a slow audit must not delay a flatten
        if self._ban_lists is not None and not self._ban_lists.read:
            # Retried after acting: a hung database must never delay a kill or a flatten (exits
            # come first). Until the lists are read, F&O entries are refused.
            await self._ban_lists.load()
        # The expiry-exposure flag is written as soon as it switches on (after acting, so it never
        # delays a kill), not at the next throttled write: a restart in between must not forget it.
        await self._persist_if_due(now, at_once=exposure_switched_on)
        await self._write_close_marks_if_due(now)

    # ---- the day ---------------------------------------------------------------------------

    async def _ensure_day(self, now: datetime) -> bool:
        """The day's counters are loaded and for today. True when they are."""
        if self._daybook.unavailable(now) == NOT_LOADED:
            if not await self._daybook.load():
                return False
        elif await self._daybook.roll_over_if_new_day():
            self._new_day()
        return self._daybook.unavailable(now) is None

    def _new_day(self) -> None:
        if self._tracker is not None:
            self._tracker.new_day()
        self._target_confirmations = 0  # a new day's MTM_day starts again from zero
        self._noticed.clear()  # (a standing halt needs no clearing: every pass recomputes it)
        self._references_for = None
        self._watch_since.clear()

    async def _ensure_references(self, now: datetime) -> bool:
        """Load the previous close of each carried position: the marks written at the close of the
        previous weekday. True once they are known (loaded or genuinely absent), False if the
        read failed (retried on the next pass)."""
        today = ist_date(now)
        if self._references_for == today:
            return True
        try:
            row = await self._repo.load_day(self._account_id, previous_weekday(today))
        except Exception:
            log.error("previous close not loaded", exc_info=True)
            return False
        references: dict[PositionId, Decimal] = {}
        for text, price in (row.close_marks if row is not None and row.close_marks else {}).items():
            try:
                value = Decimal(price)
                if not value.is_finite() or value <= 0:
                    raise ValueError("not a price")
                references[parse_mark_key(self._account_id, text)] = value
            except (ValueError, InvalidOperation, TypeError):
                # One bad entry must not stop the pass: that position falls back to its average
                # price and is recorded as RISK_REFERENCE_MISSING.
                log.error("a stored close mark is unusable and was skipped", mark=text)
        self._references, self._references_for = references, today
        return True

    def _sync_tracker(self, limits: RiskLimits) -> MtmTracker:
        """Build the tracker the first time limits exist, and apply tightened thresholds later."""
        stale_after = timedelta(seconds=limits.stale_s)
        tracker = self._tracker
        if tracker is None:
            tracker = self._tracker = MtmTracker(
                outlier_pct=limits.outlier_pct, stale_after=stale_after
            )
            for pid, held in self._positions().items():
                if held.quantity != 0:
                    baseline = self._references.get(pid) or held.average_price
                    tracker.seed_baseline(pid[1], baseline)
        elif limits is not self._applied:
            tracker.reconfigure(outlier_pct=limits.outlier_pct, stale_after=stale_after)
        self._applied = limits
        return tracker

    def _positions(self) -> dict[PositionId, Position]:
        return {} if self._trading is None else dict(self._trading.positions)

    def _evaluate(self, tracker: MtmTracker, now: datetime) -> MtmReading | None:
        try:
            return tracker.evaluate(
                self._positions().values(),
                self._daybook.fills_today,
                self._references,
                self._instruments,
                now,
            )
        except Exception:
            # Two positions with one id, a fill that belongs to no position, or anything else
            # unexpected: the state is inconsistent and is never turned into a number. MTM is
            # unknown, which refuses entries (and the checks below still run).
            log.error("day P&L cannot be computed", code=MTM_UNKNOWN, exc_info=True)
            return None

    def _note_expiry_exposure(self, now: datetime) -> bool:
        """Switch the day's ``expiry_exposure`` on when an open position's contract expires today
        by its master expiry date. One-way within the day: a later pass with no such position,
        the expiring position closed for instance, leaves it on (expiry-day-profile "Expiring
        position closed"). True if this pass switched it on.

        A position with no master record does not switch it on (expiry-day-profile "A contract
        expires today only by its master expiry date"). The lookup returns None until the master
        has loaded, which happens concurrently at start-up (or never, if that load fails): a
        restart with an open position must not lock the stricter giveback in for a whole
        non-expiry day, and a floor KILL from it would lock the day."""
        if self._daybook.day().expiry_exposure:
            return False

        def expiring(pid: PositionId, held: Position) -> bool:
            instrument = self._instruments(pid[1])
            return held.quantity != 0 and instrument is not None and expires_today(instrument, now)

        found = any(expiring(pid, held) for pid, held in self._positions().items())
        return found and self._daybook.note_expiry_exposure()

    def _ratchet(self, tracker: MtmTracker, limits: RiskLimits) -> None:
        """Raise the day's peak and floor in the day book (they only ever rise). That is also how a
        restart resumes with ``max(stored, recomputed)``: the book holds the stored values and
        ``ratchet`` keeps the larger of them and what has just been computed.

        The floor uses the expiry giveback once the day has had expiring exposure (the persisted
        flag, so a restart keeps it). Switching that term on can only raise the floor: the expiry
        giveback is never wider than the base one, and the ratchet never lowers the floor."""
        day = self._daybook.day()
        floor = day.floor
        try:
            floor = next_floor(
                day.floor, limits.floor_params(expiry=day.expiry_exposure), tracker.peak
            )
        except (ValueError, TypeError, ArithmeticError):
            log.error("floor cannot be computed", exc_info=True)  # the check halts entries
        self._daybook.ratchet(tracker.peak, floor)

    # ---- the checks ------------------------------------------------------------------------

    def _judge(self, limits: RiskLimits, trading: TradingView, now: datetime) -> list[RuleResult]:
        """Run the continuous checks on a snapshot. No awaits, so what is judged is what is acted
        on."""
        tracker = self._tracker
        assert tracker is not None
        session_open = datetime.combine(ist_date(now), MARKET_OPEN, tzinfo=IST)
        unpriced: dict[InstrumentKey, timedelta] = {}
        for pid, held in trading.positions.items():
            if held.quantity == 0:
                continue
            key = pid[1]
            if tracker.latest_price(key) is None:
                # Waiting for its first tick since the engine began watching it. (MtmBlind caps
                # this at the time since today's open, so a wait that began last evening does not
                # count.)
                unpriced[key] = now - self._watch_since.setdefault(key, now)
            else:
                self._watch_since.pop(key, None)
        # Silence is measured from the last real tick, but never from before today's open: the
        # last tick of yesterday says nothing about whether the feed is down now.
        anchor = max(self._last_tick_at or self._started_at, session_open)
        ctx = RiskContext(
            account_id=self._account_id,
            now=now,
            limits=limits,
            day=self._daybook.day(),
            mtm_day=self.mtm_day(),
            positions=trading.positions,
            orders=trading.orders,
            instruments=self._instruments,
            quote=self.quote,
            feed_silent_for=max(timedelta(0), now - anchor),
            feed_subscribed=self._subscribed(),
            unpriced_for=unpriced,
            unprotected=self._unprotected(),
            recent_entries=self._daybook.recent_entries(now),
            broker_rejections=self._rejections(now),
            target_confirmations=self._target_confirmations,  # the monitor's only: see task 9.1
            ban_list=self.ban_list(now),
            spot_key=self._spot_key,
        )
        return run_continuous(self._checks, ctx, RiskTrigger.TIMER)

    async def _act(self, results: list[RuleResult]) -> None:
        """Act on what the checks said. Several can fire at once and tie on severity (a silent feed
        raises both ``RISK_FEED_DOWN`` and, later, ``RISK_MTM_BLIND``), so blindness is acted on
        whenever it fires, not only when it happens to be the first of the most severe."""
        winner = most_severe(results)
        if winner is not None and winner.effective >= Severity.KILL:
            # Refuse entries until the kill has locked the day: no other rule refuses them if the
            # hook is slow or missing.
            self._standing = winner.decision
            try:
                await self._daybook.flush()  # the ratchet is written before acting
            except Exception:  # a failure, of any kind, is not a reason to wait: act anyway
                log.error("the day state could not be written before the kill", exc_info=True)
            await self._kill(winner.decision)
            return
        # After the KILL branch, so a breach in the same pass wins (profit-target "A breach and the
        # target together"). Only an enforced target acts: in shadow mode it is only logged.
        if any(r.enforced and r.decision.code == TARGET_HALT for r in results):
            target = next(r.decision for r in results if r.decision.code == TARGET_HALT)
            await self._lock_target_day(target)
        if any(r.enforced and r.decision.code == MTM_BLIND for r in results):
            blind = next(r.decision for r in results if r.decision.code == MTM_BLIND)
            await self._lock_blind_day(blind)
        # The two day locks are not standing halts: the gate refuses entries for them through the
        # day's halt level (RISK_HALTED_DAY), which does not clear when the condition does.
        others = most_severe(r for r in results if r.decision.code not in (MTM_BLIND, TARGET_HALT))
        self._standing = None if others is None else others.decision

    async def _kill(self, decision: Decision) -> None:
        if self._on_kill is None:
            log.critical("kill requested but no kill switch is wired", code=decision.code)
            return
        try:
            await self._on_kill(decision)
        except Exception:
            log.critical("the kill switch failed: retrying next pass", exc_info=True)

    async def _lock_blind_day(self, decision: Decision) -> None:
        """Positions cannot be valued: lock entries for the day (persisted first)."""
        if self._daybook.day().halt_level >= HaltLevel.MTM_BLIND:
            return  # already done today
        reason = decision.reason() or MTM_BLIND
        log.critical("book is blind: locking the day and flattening", code=MTM_BLIND, reason=reason)
        self._daybook.raise_halt(HaltLevel.MTM_BLIND, reason)
        await self._daybook.flush()

    async def _lock_target_day(self, decision: Decision) -> None:
        """The profit target is reached: lock entries for the rest of the day at ``TARGET_HALT``,
        persisted before anything is acted on. The flatten is then asked for by
        ``_keep_flattening`` (as for a blind book or a kill), which also cancels pending entries.

        An automatic stop with no intraday resume (CLAUDE.md safety rule 16): the level lives in
        the day's row, which only rises (``raise_halt``) and is replaced by the next IST date's
        row, so neither a restart nor the recovery resume command (it clears only a
        reconciliation halt) can clear it. ``TARGET_HALT`` (20) sits between ``MTM_BLIND`` and
        ``KILL``, so a later kill still escalates the day to ``KILL``. The broker's own lock
        after an automatic stop (D-030) is an optional broker capability that no adapter offers
        yet (stage 7 adds it); like the floor's kill, this path calls none."""
        if self._daybook.day().halt_level >= HaltLevel.TARGET_HALT:
            return  # already done today (or a kill locked it higher)
        reason = decision.reason() or TARGET_HALT
        log.critical("profit target reached: locking the day and flattening", reason=reason)
        self._daybook.raise_halt(HaltLevel.TARGET_HALT, reason)
        try:
            await self._daybook.flush()  # written before acting; a failed write is retried later
        except Exception:  # a failure, of any kind, is not a reason to leave the book open
            log.error("the day state could not be written before the target halt", exc_info=True)

    async def _keep_flattening(self) -> None:
        """While the day is locked (a blind book, the profit target, or a kill), keep asking for
        the flatten. It is asked on every pass, not once, so a flatten that failed or was cut
        short by a crash is started again."""
        day = self._daybook.day()
        pending = self._lock_pending()
        if (day.halt_level < HaltLevel.MTM_BLIND and not pending) or self._ensure_flat is None:
            return
        try:
            await self._ensure_flat(day.halt_reason or "day locked")
        except Exception:
            log.critical("could not start the flatten: retrying next pass", exc_info=True)

    # ---- audit -----------------------------------------------------------------------------

    async def _record(self, results: list[RuleResult], now: datetime) -> None:
        """Audit and log what the pass noticed: outlier ticks, carried positions measured without a
        previous close (once each per day), and a decision when it *starts* (not on every pass: a
        silent feed would otherwise write a row a second)."""
        while self._outliers:
            tick = self._outliers.pop(0)
            await self._notice(
                OUTLIER_TICK, now, {"instrument": str(tick.key), "price": money(tick.ltp)}
            )
        if self._reading is not None:
            for pid in self._reading.reference_missing:
                if (text := mark_key(pid)) not in self._noticed:
                    self._noticed.add(text)
                    log.warning("no previous close: using the average price", position=text)
                    await self._notice(REFERENCE_MISSING, now, {"position": text})
        await self._audit_changes(results, now)

    async def _notice(self, code: str, now: datetime, inputs: Mapping[str, str]) -> None:
        await self._audit.record(
            RiskEvent(
                at=now,
                account_id=self._account_id,
                rule="stale_data",
                phase="continuous",
                decision="ALLOW",
                enforced=True,
                latency_us=0,
                reason_code=code,
                inputs=dict(inputs),
            )  # fmt: skip
        )

    async def _audit_changes(self, results: list[RuleResult], now: datetime) -> None:
        """Tracked by code, not by rule name: two checks share the rule name ``stale_data``, and
        one's silence must not look like the other's change.

        A KILL starts with a critical log line, and so does an enforced code in
        ``CRITICAL_ON_START`` (the rejection breaker): once when it trips, not on every pass while
        it holds, and again only after it has cleared and trips anew. A shadow rule's trip is
        logged as a warning: it is being tried out and has stopped nothing."""
        firing = [
            (r, r.decision.code)
            for r in results
            if r.decision.severity > Severity.ALLOW and r.decision.code is not None
        ]
        for result, code in firing:
            if code in self._active_codes:
                continue
            critical = result.decision.severity >= Severity.KILL or (
                result.enforced and code in CRITICAL_ON_START
            )
            level = log.critical if critical else log.warning
            level(
                "risk decision started",
                code=code,
                rule=result.rule,
                severity=result.decision.severity.name,
                enforced=result.enforced,
            )
            await self._audit.record(
                RiskEvent(
                    at=now,
                    account_id=self._account_id,
                    rule=result.rule,
                    phase=result.phase.value,
                    decision=result.decision.severity.name,
                    enforced=result.enforced,
                    latency_us=result.latency_us,
                    reason_code=code,
                    inputs=dict(result.decision.inputs),
                )  # fmt: skip
            )
        self._active_codes = {code for _, code in firing}

    # ---- persistence and the close ---------------------------------------------------------

    async def _persist_if_due(self, now: datetime, *, at_once: bool = False) -> None:
        """Write the day state on the throttle, or now when ``at_once``. (The ``*`` makes
        ``at_once`` keyword-only, like a C# named argument that must be spelled out.)"""
        if at_once or now - self._last_persist >= self._persist_every:
            self._last_persist = now
            await self._daybook.flush()

    async def _write_close_marks_if_due(self, now: datetime) -> None:
        """At the 15:30 close, record each open position's last confirmed price: tomorrow's
        reference. The accepted price, not the latest, so a spike on the last tick cannot become
        tomorrow's reference and open the day with a phantom loss."""
        today = ist_date(now)
        if self._marks_written_for == today or now < market_close_utc(today):
            return
        tracker = self._tracker
        if tracker is None:
            return
        marks: dict[str, str] = {}
        for pid, held in self._positions().items():
            price = tracker.accepted_price(pid[1]) or tracker.latest_price(pid[1])
            if held.quantity != 0 and price is not None:
                marks[mark_key(pid)] = money(price)
        self._daybook.set_close_marks(marks)
        if await self._daybook.flush():
            self._marks_written_for = today

    # ---- the supervised loop ---------------------------------------------------------------

    async def run(self, stop: asyncio.Event, ticks: Subscription[Item]) -> None:
        """Consume ticks and run ``step`` every interval until ``stop``. A crash propagates to
        the supervisor (``_keep_alive``), which restarts it; the beat then goes stale and the gate
        refuses entries until the new run's first pass."""
        deadline = time.monotonic()
        while not stop.is_set():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = await asyncio.wait_for(
                    ticks.__anext__(), timeout=min(remaining, self._interval_s)
                )
                self._take(item)
                while (more := ticks.get_nowait()) is not None:
                    self._take(more)
            except TimeoutError:
                pass
            if time.monotonic() >= deadline:
                await self.step()
                deadline = time.monotonic() + self._interval_s

    def _take(self, item: Item) -> None:
        if isinstance(item, Tick):
            self.on_tick(item)
