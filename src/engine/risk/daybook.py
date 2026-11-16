"""The day's risk counters, kept in memory and persisted through one writer (add-risk-engine task
6.4; risk-rules spec "Maximum trades per day" and "Consecutive-loss cooldown"; design §6).

``DayBook`` owns the account's ``DayState`` for the current IST trading day: entries allowed today,
the losing streak, the cooldown end, and (from the monitor, task 7.1) the peak, the floor and the
halt level. The in-memory copy is authoritative while the engine runs and is written to Postgres
through ``RiskRepository.save_day``, which merges with ``GREATEST`` so a late or repeated write can
never lower a count, a floor or a halt.

One writer: every write goes through ``_save`` under a single ``asyncio.Lock`` and writes the
state as it is *at that moment*. The fill listener and the monitor's timer both mark the book
dirty and may both ask for a flush; because ``loss_streak`` and ``updated_at`` are plain sets (a
win legitimately lowers the streak), two unserialised writes could land out of order and move them
backwards. Serialised, the later write always carries the newer state.

Trading terms:
  * entry         - an order the gate allowed that opens or grows a position. It counts toward
                    the day's maximum as soon as it is allowed, whether or not the broker takes it.
  * closed trade  - a position that returned to flat. It is a *loss* if its P&L net of charges is
                    below zero, a *win* if above, and neither if exactly zero (a loss streak then
                    neither grows nor resets).
  * loss streak   - consecutive losing closed trades; only a win resets it.
  * cooldown      - once the streak reaches the limit, no entries until ``cooldown_seconds`` later.

The round trip is measured from the position's cumulative ``realized_pnl`` before and after each
fill, less each fill's charges, summed from when the position left flat: the whole trade, entry
charges and earlier days included. After a restart ``seed`` rebuilds those running totals by
replaying the whole fill history (see ``seed``).
"""

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog

from contracts.orders import Fill, OrderSide, Position
from contracts.snapshot import HaltLevel
from engine.market_data.clock import ist_date, utc_now
from engine.persistence.repository import PersistenceError
from engine.positions.book import PositionId, apply, flat, position_id
from engine.risk.limits import RiskLimits
from engine.risk.repository import RiskRepository
from engine.risk.state import DayState, merge_day_state

log = structlog.get_logger("engine.risk.daybook")

NOT_LOADED: Final = "today's risk state has not been loaded"
STALE_DATE: Final = "the risk state is for an earlier trading day"


class DayBook:
    def __init__(
        self,
        *,
        account_id: str,
        repository: RiskRepository,
        limits: Callable[[], RiskLimits | None],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``limits`` returns the limits in force (None if none): the streak limit and the cooldown
        length are read from it when a trade closes."""
        self._account_id = account_id
        self._repo = repository
        self._limits = limits
        self._now = now
        self._day: DayState | None = None
        self.fills_today: list[Fill] = []  # the day's fills, for MTM (task 7.1)
        self._previous: dict[PositionId, Position] = {}  # each position as of its last fill
        self._trade_net: dict[PositionId, Decimal] = {}  # P&L net of charges since it left flat
        self._write_lock = asyncio.Lock()  # the one writer (see the module docstring)
        self._version = 0  # bumped on every change; a flush marks clean only if it is unchanged
        self._saved_version = 0

    # ---- reading ---------------------------------------------------------------------------

    def day(self) -> DayState:
        """Today's state. Raises if it is not loaded: callers check ``unavailable`` first."""
        if self._day is None:
            raise RuntimeError(NOT_LOADED)
        return self._day

    def unavailable(self, now: datetime) -> str | None:
        """Why the counters cannot be trusted: not loaded, or for another day. None if fine."""
        if self._day is None:
            return NOT_LOADED
        if self._day.trade_date != ist_date(now):
            return STALE_DATE
        return None

    @property
    def dirty(self) -> bool:
        """Is there state that has not reached the database?"""
        return self._version != self._saved_version

    # ---- loading and the day rollover ------------------------------------------------------

    async def load(self) -> bool:
        """Load (or start) today's state: the stored row for today's IST date, else a fresh one
        (a new date has no row: that is the post-close reset). True once the database answered;
        False if it did not (the counters stay unavailable and entries are refused)."""
        today = ist_date(self._now())
        try:
            stored = await self._repo.load_day(self._account_id, today)
        except PersistenceError as error:
            log.error("risk_day_not_loaded", error=str(error))
            return False
        self._day = stored or DayState(self._account_id, today, self._now())
        self.fills_today = [f for f in self.fills_today if ist_date(f.filled_at) == today]
        # ``_trade_net`` is deliberately kept: a position carried over midnight is still the same
        # trade, so a new trading day must not forget what it has made or paid so far.
        self._version = self._saved_version = 0
        return True

    def seed(self, positions: Iterable[Position], fills: Iterable[Fill]) -> None:
        """Take over the book as it stands after a restart.

        ``positions`` are the stored positions (each fill's baseline) and ``fills`` is **every
        fill in the order it was applied**, not only today's. Replaying them from flat with the
        same ``apply`` that built the positions rebuilds, for each position that is open now, the
        net P&L of its current trade: everything made and paid since it last left flat, entry
        charges and earlier days included. Without that, a trade opened before the restart would
        be judged on the part after it, and a small win after charges could count as a win when
        the whole trade lost, which would skip the cooldown. A trade that closed before the
        restart is not settled again: its effect on the streak was stored with the day's state.
        """
        today = ist_date(self._now())
        self._previous = {position_id(p): p for p in positions}
        self._trade_net = {}
        self.fills_today = []
        replay: dict[PositionId, Position] = {}
        for f in fills:
            pid = position_id(f)
            current = replay.get(pid) or flat(f.account_id, f.key, f.product, f.filled_at)
            updated, realized = apply(current, f)
            replay[pid] = updated
            if updated.quantity == 0:
                self._trade_net.pop(pid, None)  # a closed trade: settled before the restart
            else:
                self._trade_net[pid] = (
                    self._trade_net.get(pid, Decimal(0)) + realized - f.costs.total
                )
            if ist_date(f.filled_at) == today:
                self.fills_today.append(f)
        self._reconcile_replay(replay)

    def _reconcile_replay(self, replay: dict[PositionId, Position]) -> None:
        """Compare the replayed positions with the stored ones. Where they disagree on the quantity
        (pruned fills, a position imported without its fills) the replay cannot be trusted for that
        position, so its running total is dropped: it is then treated like an unseeded position,
        measured from zero, rather than from an invented history. A stored position the history
        never mentions is kept as the baseline for its next fill."""
        for pid in replay.keys() | self._previous.keys():
            replayed = replay.get(pid)
            stored = self._previous.get(pid)
            replayed_quantity = 0 if replayed is None else replayed.quantity
            stored_quantity = 0 if stored is None else stored.quantity
            if replayed_quantity != stored_quantity:
                self._trade_net.pop(pid, None)
                log.warning(
                    "fill history does not match the stored position: its trade starts from zero",
                    account_id=pid[0],
                    key=str(pid[1]),
                    product=pid[2].value,
                    replayed=replayed_quantity,
                    stored=stored_quantity,
                )

    async def roll_over_if_new_day(self) -> bool:
        """Start the next trading day's state when the IST date has changed. True if it did."""
        if self._day is None or self._day.trade_date == ist_date(self._now()):
            return False
        return await self.load()

    # ---- what the monitor raises (task 7.1) ------------------------------------------------

    def ratchet(self, peak: Decimal, floor: Decimal | None) -> None:
        """Raise the day's peak and floor. They only ever rise: a lower or non-finite value is
        ignored, so the monitor may pass what it has just computed. The change is written by the
        next ``flush`` (the monitor flushes before it acts on a kill)."""
        state = self.day()
        new_peak = peak if peak.is_finite() and peak > state.peak else state.peak
        new_floor = state.floor
        if floor is not None and floor.is_finite() and (new_floor is None or floor > new_floor):
            new_floor = floor
        if new_peak != state.peak or new_floor != state.floor:
            self._day = replace(state, peak=new_peak, floor=new_floor)
            self._version += 1

    def raise_halt(self, level: HaltLevel, reason: str) -> None:
        """Lock the day at ``level`` (it only ever rises, and clears with the next trading day)."""
        state = self.day()
        if level > state.halt_level:
            self._day = replace(state, halt_level=level, halt_reason=reason)
            self._version += 1

    def set_close_marks(self, marks: dict[str, str]) -> None:
        """Record each open position's last price at the close: tomorrow's reference. The first
        marks written for a day stand (a restart after the close must not replace them)."""
        state = self.day()
        if state.close_marks is None:
            self._day = replace(state, close_marks=dict(marks))
            self._version += 1

    # ---- the entry counter -----------------------------------------------------------------

    async def entry_allowed(self) -> str | None:
        """Count one allowed entry, and persist the count **before** the entry is sent (spec
        "Maximum trades per day"). None on success; otherwise the reason it could not be
        persisted, and the entry must be refused (fail closed). The count is taken back when the
        write fails, so a refused entry is not counted: the stored value can only be higher (a
        write that committed and then reported an error), which is the safe side, and ``GREATEST``
        keeps it.

        This increments without checking the maximum: the cap is atomic only because the gate
        runs inside ``OrderService``'s lock, so the ``MaxTrades`` check and this count see one
        snapshot. A caller of the gate outside that lock could let two entries pass on the same
        count."""
        if (why := self.unavailable(self._now())) is not None:
            return why
        async with self._write_lock:
            state = self.day()
            self._day = replace(state, entries_today=state.entries_today + 1)
            self._version += 1
            try:
                await self._save_locked()
            except PersistenceError as error:
                self._day = replace(self.day(), entries_today=self.day().entries_today - 1)
                return f"the entry count could not be saved: {error}"
        return None

    # ---- the fill listener -----------------------------------------------------------------

    async def on_fill(self, fill: Fill, position: Position) -> None:
        """Register as ``OrderService`` fill listener. Keeps today's fills, and when a position
        returns to flat settles the closed trade: a loss extends the streak (and may start a
        cooldown), a win resets it. It runs inside the order service's lock, so it only updates
        memory and, when a cooldown starts, writes once: it never places or cancels an order."""
        pid = position_id(position)
        before = self._previous.get(pid)
        self._previous[pid] = position
        self.fills_today.append(fill)
        # What the position held before this fill follows from the fill itself, so a position the
        # book was never seeded with is still settled when it closes.
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        quantity_before = position.quantity - signed
        if before is None and quantity_before != 0:
            log.warning(
                "fill for an open position the day book was not seeded with",
                account_id=position.account_id,
                key=str(position.key),
                product=position.product.value,
            )
        # A fill that opens from flat realises nothing, whatever the position's cumulative
        # ``realized_pnl`` already holds from earlier trades (a baseline that was not seeded
        # must not hand an old profit to the new trade).
        realized = (
            Decimal(0)
            if quantity_before == 0
            else position.realized_pnl - (before.realized_pnl if before else Decimal(0))
        )
        accumulated = self._trade_net.get(pid, Decimal(0)) + realized - fill.costs.total
        went_flat = position.quantity == 0 and quantity_before != 0
        if not went_flat:
            self._trade_net[pid] = accumulated
            return
        self._trade_net.pop(pid, None)
        await self._settle_trade(accumulated)

    async def _settle_trade(self, net: Decimal) -> None:
        if self._day is None or net == 0:
            return  # no state to update, or a scratch trade: neither a loss nor a win
        state = self._day
        if net > 0:
            self._day = replace(state, loss_streak=0)
            self._version += 1
            return
        streak = state.loss_streak + 1
        cooldown_until = state.cooldown_until
        limits = self._limits()
        started = limits is not None and streak >= limits.loss_streak_limit
        if limits is not None and started:
            ends = self._now() + timedelta(seconds=limits.cooldown_seconds)
            cooldown_until = ends if cooldown_until is None else max(cooldown_until, ends)
        self._day = replace(state, loss_streak=streak, cooldown_until=cooldown_until)
        self._version += 1
        if started:
            await self.flush()  # a cooldown is rare and matters: do not wait for the timer

    # ---- persistence -----------------------------------------------------------------------

    async def flush(self) -> bool:
        """Write the state if it changed (called by the monitor's timer). True when the database
        holds everything; False (and still dirty) if the write failed. Never raises."""
        if self._day is None or not self.dirty:
            return True
        async with self._write_lock:
            try:
                await self._save_locked()
            except PersistenceError as error:
                log.error("risk_day_not_saved", error=str(error))
                return False
        return True

    async def _save_locked(self) -> None:
        """Write the current state. The caller holds ``_write_lock``."""
        state = replace(self.day(), updated_at=self._now())
        version = self._version
        started = time.perf_counter()
        stored = await self._repo.save_day(state)
        log.debug("risk_day_saved", ms=int((time.perf_counter() - started) * 1000))
        # Believe the larger of what is stored and what memory holds now (the stored row may be
        # ahead after a restart or a late write), and keep memory's own plain-set fields.
        self._day = merge_day_state(stored, self.day())
        if self._version == version:
            self._saved_version = version
