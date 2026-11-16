"""The day book: persisted counters, the loss streak and the cooldown (add-risk-engine task 6.4).

Scenarios: "Count after a crash" (a restart with the repository carrying state), "Third loss in a
row", "A win in between"; plus charges, scratch trades, partial exits, flips, carried positions, the
day rollover and the single serialised writer.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from contracts.orders import Fill, OrderSide, Position
from engine.persistence.repository import PersistenceError
from engine.positions.book import PositionId, apply, flat, position_id
from engine.risk.checks.counters import MAX_TRADES, MaxTrades
from engine.risk.daybook import NOT_LOADED, STALE_DATE, DayBook
from engine.risk.limits import RiskLimits
from engine.risk.state import DayState
from tests.engine.risk_builders import LIMITS, NOW, TODAY, context, fill, order, position
from tests.engine.risk_fakes import MemoryRiskRepository

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
COOLDOWN_S = 1800


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def limits(**changes: object) -> RiskLimits:
    return replace(LIMITS, loss_streak_limit=3, cooldown_seconds=COOLDOWN_S, **changes)  # type: ignore[arg-type]


def book(
    repo: MemoryRiskRepository | None = None,
    clock: Clock | None = None,
    active: RiskLimits | None = None,
) -> tuple[DayBook, MemoryRiskRepository, Clock]:
    repo = repo or MemoryRiskRepository()
    clock = clock or Clock()
    in_force = active or limits()
    return (
        DayBook(account_id="main", repository=repo, limits=lambda: in_force, now=clock),
        repo,
        clock,
    )


async def loaded(**kwargs: object) -> tuple[DayBook, MemoryRiskRepository, Clock]:
    made = book(**kwargs)  # type: ignore[arg-type]
    assert await made[0].load()
    return made


class Positions:
    """Feeds fills to a day book the way the order service does: apply, then call the listener."""

    def __init__(self, daybook: DayBook, start: list[Position] | None = None) -> None:
        self.daybook = daybook
        self.held: dict[PositionId, Position] = {position_id(p): p for p in start or []}

    async def play(self, f: Fill) -> Position:
        pid = position_id(f)
        current = self.held.get(pid) or flat(f.account_id, f.key, f.product, f.filled_at)
        updated, _ = apply(current, f)
        self.held[pid] = updated
        await self.daybook.on_fill(f, updated)
        return updated

    async def round_trip(self, entry: str, exit_: str, *, costs: str = "0", qty: int = 65) -> None:
        await self.play(
            fill(BUY, qty, entry, costs=costs, fill_id=f"in-{len(self.daybook.fills_today)}")
        )
        await self.play(
            fill(SELL, qty, exit_, costs=costs, fill_id=f"out-{len(self.daybook.fills_today)}")
        )


# ---- loading, availability and the rollover --------------------------------------------------


async def test_before_it_is_loaded_the_book_is_unavailable() -> None:
    daybook, *_ = book()

    assert daybook.unavailable(NOW) == NOT_LOADED


async def test_a_new_date_starts_fresh_and_a_stored_row_is_loaded() -> None:
    daybook, repo, _ = await loaded()
    assert daybook.day() == DayState("main", TODAY, NOW) and daybook.unavailable(NOW) is None

    stored = DayState("main", TODAY, NOW, entries_today=4, loss_streak=2)
    await repo.save_day(stored)
    again, *_ = book(repo)
    await again.load()

    assert again.day().entries_today == 4 and again.day().loss_streak == 2


async def test_a_load_that_fails_leaves_the_book_unavailable() -> None:
    class Down(MemoryRiskRepository):
        async def load_day(self, account_id: str, trade_date: object) -> None:  # type: ignore[override]
            raise PersistenceError("db down")

    daybook, *_ = book(Down())

    assert await daybook.load() is False and daybook.unavailable(NOW) == NOT_LOADED


async def test_next_day_the_counters_are_fresh() -> None:
    """Spec "Next day": the first evaluation on the next IST date has no row, so counters reset."""
    daybook, repo, clock = await loaded()
    for _ in range(3):
        assert await daybook.entry_allowed() is None
    clock.at = NOW + timedelta(days=1)

    assert daybook.unavailable(clock.at) == STALE_DATE  # until the rollover has run
    assert await daybook.roll_over_if_new_day() is True

    assert daybook.day().entries_today == 0 and daybook.day().trade_date == TODAY + timedelta(
        days=1
    )
    assert daybook.unavailable(clock.at) is None
    assert await daybook.roll_over_if_new_day() is False  # and a second call does nothing
    assert (await repo.load_day("main", TODAY)).entries_today == 3  # type: ignore[union-attr]


# ---- the entry counter -----------------------------------------------------------------------


async def test_each_allowed_entry_is_counted_and_persisted_before_it_returns() -> None:
    daybook, repo, _ = await loaded()

    for n in (1, 2, 3):
        assert await daybook.entry_allowed() is None
        assert (await repo.load_day("main", TODAY)).entries_today == n  # type: ignore[union-attr]

    assert daybook.day().entries_today == 3


async def test_count_after_a_crash_seven_entries_leave_three_of_ten() -> None:
    """Spec "Count after a crash": seven entries were allowed, the engine is killed and restarted
    (a new book on the repository that carries the state): three more entries are allowed."""
    active = limits(max_trades_per_day=10)
    first, repo, _ = await loaded(active=active)
    for _ in range(7):
        await first.entry_allowed()
    restarted, *_ = book(repo, active=active)  # first is gone: only the repository remains
    await restarted.load()

    allowed = 0
    while True:
        ctx = context(limits=active, day=restarted.day())
        if MaxTrades().pre_trade(ctx, order(BUY, 65)).code == MAX_TRADES:
            break
        assert await restarted.entry_allowed() is None
        allowed += 1

    assert allowed == 3 and restarted.day().entries_today == 10


async def test_an_entry_whose_count_cannot_be_saved_is_refused_and_not_counted() -> None:
    class SaveDown(MemoryRiskRepository):
        down = False

        async def save_day(self, state: DayState) -> DayState:
            if self.down:
                raise PersistenceError("db down")
            return await super().save_day(state)

    repo = SaveDown()
    daybook, *_ = await loaded(repo=repo)
    assert await daybook.entry_allowed() is None
    repo.down = True

    reason = await daybook.entry_allowed()

    assert reason is not None and "could not be saved" in reason
    assert daybook.day().entries_today == 1  # the refused entry is not counted
    repo.down = False
    assert await daybook.entry_allowed() is None and daybook.day().entries_today == 2


async def test_a_write_that_committed_and_then_failed_leaves_the_higher_count_safe() -> None:
    """A timeout after COMMIT: the store holds 1, the call reported a failure and memory took the
    count back. The next write cannot lower the stored count (GREATEST), and memory adopts it."""

    class CommitThenError(MemoryRiskRepository):
        fail_once = True

        async def save_day(self, state: DayState) -> DayState:
            stored = await super().save_day(state)
            if self.fail_once:
                self.fail_once = False
                raise PersistenceError("timed out after commit")
            return stored

    repo = CommitThenError()
    daybook, *_ = await loaded(repo=repo)

    assert await daybook.entry_allowed() is not None  # refused, memory back at 0, store holds 1
    assert await daybook.entry_allowed() is None  # next: memory 1, written over a stored 1

    assert (await repo.load_day("main", TODAY)).entries_today == 1  # type: ignore[union-attr]
    assert daybook.day().entries_today == 1


async def test_an_unloaded_book_refuses_the_count() -> None:
    daybook, *_ = book()

    assert await daybook.entry_allowed() == NOT_LOADED


# ---- the losing streak and the cooldown ------------------------------------------------------


async def test_third_loss_in_a_row_starts_the_cooldown() -> None:
    """Spec "Third loss in a row": a limit of 3 with a 30-minute cooldown, the third loss closes
    at 10:30, so entries are refused until 11:00."""
    daybook, repo, clock = await loaded()
    feed = Positions(daybook)

    await feed.round_trip("100", "99")
    await feed.round_trip("100", "99")
    assert daybook.day().cooldown_until is None
    await feed.round_trip("100", "99")

    assert daybook.day().loss_streak == 3
    assert daybook.day().cooldown_until == clock.at + timedelta(seconds=COOLDOWN_S)
    stored = await repo.load_day("main", TODAY)
    assert stored is not None and stored.cooldown_until == daybook.day().cooldown_until  # persisted


async def test_a_win_in_between_resets_the_streak() -> None:
    """Spec "A win in between": two losses, a win, then a loss: the streak is 1, no cooldown."""
    daybook, *_ = await loaded()
    feed = Positions(daybook)

    await feed.round_trip("100", "99")
    await feed.round_trip("100", "99")
    await feed.round_trip("100", "102")
    assert daybook.day().loss_streak == 0
    await feed.round_trip("100", "99")

    assert daybook.day().loss_streak == 1 and daybook.day().cooldown_until is None


async def test_a_trade_is_judged_net_of_charges() -> None:
    daybook, *_ = await loaded()
    feed = Positions(daybook)

    await feed.round_trip("100", "100.20", costs="10")  # +13.00 gross, 20 of charges: a loss
    assert daybook.day().loss_streak == 1
    await feed.round_trip("100", "101", costs="0")  # +65 gross, nothing paid: a win
    assert daybook.day().loss_streak == 0


async def test_a_scratch_trade_neither_extends_nor_resets_the_streak() -> None:
    daybook, *_ = await loaded()
    feed = Positions(daybook)

    await feed.round_trip("100", "99")  # a loss
    await feed.round_trip("100", "100")  # exactly zero net
    assert daybook.day().loss_streak == 1
    await feed.round_trip("100", "99")
    assert daybook.day().loss_streak == 2


async def test_a_trade_closed_in_parts_is_settled_once_when_flat() -> None:
    daybook, *_ = await loaded()
    feed = Positions(daybook)

    await feed.play(fill(BUY, 130, "100", fill_id="in"))
    await feed.play(fill(SELL, 65, "110", fill_id="out1"))  # +650 realised, still open
    assert daybook.day().loss_streak == 0
    await feed.play(fill(SELL, 65, "95", fill_id="out2"))  # -325: the trade nets +325: a win

    assert daybook.day().loss_streak == 0 and daybook.day().cooldown_until is None


async def test_a_flip_is_not_a_closed_trade() -> None:
    """The position never returns to flat, so nothing is settled (spec: "returned to flat")."""
    daybook, *_ = await loaded()
    feed = Positions(daybook)

    await feed.play(fill(BUY, 65, "100", fill_id="in"))
    await feed.play(fill(SELL, 130, "90", fill_id="flip"))  # closes the long at a loss, opens short

    assert daybook.day().loss_streak == 0


async def test_a_carried_position_closed_today_is_measured_from_its_entry_price() -> None:
    carried = position(quantity=65, average="100.00", realized_pnl=D("0"))
    daybook, *_ = await loaded()
    daybook.seed([carried], [])
    feed = Positions(daybook, [carried])

    await feed.play(fill(SELL, 65, "90", fill_id="out"))  # (90 - 100) x 65 = -650

    assert daybook.day().loss_streak == 1


async def test_each_further_loss_past_the_limit_extends_the_cooldown() -> None:
    daybook, _, clock = await loaded()
    feed = Positions(daybook)
    for _ in range(3):
        await feed.round_trip("100", "99")
    first_end = daybook.day().cooldown_until
    clock.at += timedelta(minutes=45)  # the first cooldown has passed

    await feed.round_trip("100", "99")  # the fourth loss in a row

    assert daybook.day().loss_streak == 4
    assert daybook.day().cooldown_until == clock.at + timedelta(seconds=COOLDOWN_S)
    assert first_end is not None and daybook.day().cooldown_until > first_end  # type: ignore[operator]


async def test_without_limits_a_loss_is_counted_but_no_cooldown_can_start() -> None:
    made = DayBook(
        account_id="main", repository=MemoryRiskRepository(), limits=lambda: None, now=Clock()
    )
    await made.load()
    feed = Positions(made)

    for _ in range(4):
        await feed.round_trip("100", "99")

    assert made.day().loss_streak == 4 and made.day().cooldown_until is None


async def test_todays_fills_are_kept_and_old_ones_are_not_seeded() -> None:
    daybook, *_ = await loaded()
    yesterday = fill(BUY, 65, "100", fill_id="old").model_copy(
        update={"filled_at": NOW - timedelta(days=1)}
    )
    today = fill(BUY, 65, "100", fill_id="new")

    daybook.seed([], [yesterday, today])
    feed = Positions(daybook)
    await feed.play(fill(BUY, 65, "100", fill_id="live"))

    assert [f.fill_id for f in daybook.fills_today] == ["new", "live"]


# ---- the single writer -----------------------------------------------------------------------


async def test_writes_never_overlap_and_the_later_one_carries_the_newer_state() -> None:
    class Tracking(MemoryRiskRepository):
        active = 0
        peak = 0
        writes: list[int] = []

        async def save_day(self, state: DayState) -> DayState:
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)  # a slow write: the other one would run in the gap
            self.writes.append(state.entries_today)
            stored = await super().save_day(state)
            self.active -= 1
            return stored

    repo = Tracking()
    repo.writes = []
    daybook, *_ = await loaded(repo=repo)
    feed = Positions(daybook)

    await asyncio.gather(
        daybook.entry_allowed(),
        daybook.entry_allowed(),
        feed.round_trip("100", "99"),  # the fill listener flushes when a cooldown starts
        daybook.flush(),
    )

    assert repo.peak == 1  # never two writes in flight
    assert repo.writes == sorted(repo.writes)  # each write carried at least what the last did
    assert (await repo.load_day("main", TODAY)).entries_today == 2  # type: ignore[union-attr]


async def test_flush_writes_only_when_something_changed_and_reports_failure() -> None:
    class Counting(MemoryRiskRepository):
        saves = 0
        down = False

        async def save_day(self, state: DayState) -> DayState:
            if self.down:
                raise PersistenceError("db down")
            self.saves += 1
            return await super().save_day(state)

    repo = Counting()
    daybook, *_ = await loaded(repo=repo)
    feed = Positions(daybook)

    assert await daybook.flush() is True and repo.saves == 0  # nothing changed
    await feed.round_trip("100", "102")  # a win: streak stays 0, but the book is not dirty either
    await feed.round_trip("100", "99")  # a loss: dirty, below the limit so not flushed inline
    assert daybook.dirty
    repo.down = True
    assert await daybook.flush() is False and daybook.dirty  # failed: still dirty, no exception
    repo.down = False
    assert await daybook.flush() is True and not daybook.dirty
    assert (await repo.load_day("main", TODAY)).loss_streak == 1  # type: ignore[union-attr]


async def test_the_book_adopts_a_higher_stored_count() -> None:
    """The stored row can be ahead (a write that committed, a restart): memory takes the larger."""
    daybook, repo, _ = await loaded()
    await repo.save_day(DayState("main", TODAY, NOW, entries_today=6))

    assert await daybook.entry_allowed() is None  # memory 1, stored 6: the merge keeps 6

    assert daybook.day().entries_today == 6


async def test_a_change_made_while_a_write_is_in_flight_keeps_the_book_dirty() -> None:
    """The write carried the state as it was when it started. A win that lands during it is not in
    that write, so the book must still report unwritten state."""

    class Slow(MemoryRiskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def save_day(self, state: DayState) -> DayState:
            self.started.set()
            await self.release.wait()
            return await super().save_day(state)

    repo = Slow()
    daybook, *_ = await loaded(repo=repo)
    feed = Positions(daybook)
    await feed.round_trip("100", "99")  # a loss: dirty
    flushing = asyncio.create_task(daybook.flush())
    await repo.started.wait()

    await feed.round_trip("100", "102")  # a win lands while the write is in flight
    repo.release.set()
    assert await flushing is True

    assert daybook.dirty  # the win (streak back to 0) is not in the database yet
    assert (await repo.load_day("main", TODAY)).loss_streak == 1  # type: ignore[union-attr]
    assert await daybook.flush() is True and not daybook.dirty
    assert (await repo.load_day("main", TODAY)).loss_streak == 0  # type: ignore[union-attr]


# ---- a restart in the middle of a trade (review round 1) -------------------------------------


async def restarted(
    first: DayBook, feed: Positions, repo: MemoryRiskRepository, clock: Clock
) -> tuple[DayBook, Positions]:
    """A new engine process: a new book on the same repository, seeded with the stored positions
    and the whole fill history, as ``engine.main`` will do after recovery."""
    held = list(feed.held.values())
    second, *_ = book(repo, clock)
    assert await second.load()
    second.seed(held, list(first.fills_today))
    return second, Positions(second, held)


async def test_a_restart_mid_trade_still_judges_the_whole_trade_net_of_its_entry_charges() -> None:
    """Bought with 10 of charges, restarted, sold for +13.00 gross with 10 more of charges: the
    trade lost 7.00. Judged on the exit alone it would be +3.00, count as a win and reset the
    streak, skipping the cooldown."""
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.play(fill(BUY, 65, "100", costs="10", fill_id="in"))

    second, feed2 = await restarted(first, feed, repo, clock)
    await feed2.play(fill(SELL, 65, "100.20", costs="10", fill_id="out"))

    assert second.day().loss_streak == 1


async def test_a_restart_mid_trade_does_not_turn_a_winning_trade_into_a_loss() -> None:
    """Half closed at +650 before the restart, the rest at -325 after it: the trade made 325."""
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.play(fill(BUY, 130, "100", fill_id="in"))
    await feed.play(fill(SELL, 65, "110", fill_id="part"))

    second, feed2 = await restarted(first, feed, repo, clock)
    await feed2.play(fill(SELL, 65, "95", fill_id="rest"))

    assert second.day().loss_streak == 0  # a win, not a false loss on the last leg alone


async def test_entry_charges_paid_on_an_earlier_day_count_towards_the_trade() -> None:
    """The trade is the position's whole round trip, whichever day each leg was on."""
    first, repo, clock = await loaded()
    yesterday = fill(BUY, 65, "100", costs="10", fill_id="in").model_copy(
        update={"filled_at": NOW - timedelta(days=1)}
    )
    held = position(quantity=65, average="100.00")
    second, *_ = book(repo, clock)
    await second.load()
    second.seed([held], [yesterday])
    feed = Positions(second, [held])

    await feed.play(fill(SELL, 65, "100.20", costs="10", fill_id="out"))

    assert second.day().loss_streak == 1  # 13.00 gross less 20.00 of charges
    assert [f.fill_id for f in second.fills_today] == ["out"]


async def test_a_trade_that_spans_midnight_is_not_forgotten_at_the_rollover() -> None:
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.play(fill(BUY, 65, "100", costs="10", fill_id="in"))

    clock.at = NOW + timedelta(days=1)
    assert await first.roll_over_if_new_day() is True  # the new day's state, same open trade
    await feed.play(fill(SELL, 65, "100.20", costs="10", fill_id="out"))

    assert first.day().loss_streak == 1


async def test_a_trade_that_closed_before_the_restart_is_not_settled_twice() -> None:
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.round_trip("100", "99")  # a loss, settled by the first process
    await first.flush()

    second, feed2 = await restarted(first, feed, repo, clock)

    assert second.day().loss_streak == 1  # the stored streak, not 2
    await feed2.round_trip("100", "99")
    assert second.day().loss_streak == 2


async def test_a_position_the_book_was_never_seeded_with_is_still_settled_when_it_closes() -> None:
    from structlog.testing import capture_logs

    daybook, *_ = await loaded()
    held = position(quantity=65, average="100.00")  # exists, but was not passed to seed()
    feed = Positions(daybook, [])  # the helper has no baseline either: it applies on top of it
    feed.held[position_id(held)] = held

    with capture_logs() as logs:
        await feed.play(fill(SELL, 65, "90", fill_id="out"))

    assert daybook.day().loss_streak == 1  # settled (a loss), not silently skipped
    assert any(entry["event"].startswith("fill for an open position") for entry in logs)


async def test_a_fresh_position_needs_no_seeding_and_raises_no_warning() -> None:
    from structlog.testing import capture_logs

    daybook, *_ = await loaded()
    feed = Positions(daybook)

    with capture_logs() as logs:
        await feed.round_trip("100", "99")

    assert daybook.day().loss_streak == 1
    assert not any("not seeded" in entry["event"] for entry in logs)


async def test_a_restart_rebuilds_the_charges_of_every_leg_not_just_the_last() -> None:
    """Bought 130 with 700 of charges, 65 sold at +65 before the restart, the rest sold at +65
    after it: gross +130, charges 700, a loss. Only the last leg's +65 (or the two exits' +130)
    would look like a win."""
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.play(fill(BUY, 130, "100", costs="700", fill_id="in"))
    await feed.play(fill(SELL, 65, "101", fill_id="part"))

    second, feed2 = await restarted(first, feed, repo, clock)
    await feed2.play(fill(SELL, 65, "101", fill_id="rest"))

    assert second.day().loss_streak == 1


async def test_a_new_trade_after_the_restart_starts_from_nothing() -> None:
    """A trade closed before the restart (with 10 of entry charges), then a small win after it: the
    win must not be netted against anything left over from the old trade, or it would count as a
    loss and extend the streak."""
    first, repo, clock = await loaded()
    feed = Positions(first)
    await feed.round_trip("100", "99", costs="10")  # a loss, streak 1
    await first.flush()

    second, feed2 = await restarted(first, feed, repo, clock)
    await feed2.round_trip("100", "100.1")  # +6.50 gross, no charges: a win

    assert second.day().loss_streak == 0


# ---- hardening of the replay (review round 2) -------------------------------------------------


async def test_seeding_twice_replaces_the_running_totals_instead_of_adding_to_them() -> None:
    first, repo, clock = await loaded()
    paid_10 = fill(BUY, 65, "100", costs="10", fill_id="in")
    feed = Positions(first)
    await feed.play(paid_10)
    held = list(feed.held.values())

    second, *_ = book(repo, clock)
    await second.load()
    second.seed(held, [paid_10])  # the open trade has paid 10 so far
    free = fill(BUY, 65, "100", costs="0", fill_id="in")  # the same trade, history now says 0
    second.seed(held, [free])  # a second seed: it must start over, not add to the first
    feed2 = Positions(second, held)
    await feed2.play(fill(SELL, 65, "100.20", costs="10", fill_id="out"))

    assert second.day().loss_streak == 0  # +13.00 gross less the exit's 10 only: a win


async def test_a_history_that_disagrees_with_the_stored_position_is_not_trusted() -> None:
    """The replay says 65 are open, the stored position is flat (pruned fills, say). The invented
    running total is dropped with a warning, so the next trade starts from zero instead of
    inheriting 10 of phantom charges."""
    from structlog.testing import capture_logs

    daybook, *_ = await loaded()
    stored_flat = position(quantity=0, average="0")
    phantom = fill(BUY, 65, "100", costs="10", fill_id="phantom")

    with capture_logs() as logs:
        daybook.seed([stored_flat], [phantom])
    feed = Positions(daybook, [stored_flat])
    await feed.round_trip("100", "100.1")  # +6.50 gross, no charges: a win

    assert daybook.day().loss_streak == 0
    assert any("does not match the stored position" in entry["event"] for entry in logs)


async def test_a_fill_that_opens_from_flat_realises_nothing_whatever_the_cumulative_pnl() -> None:
    """A flat position with +500 of realised P&L from earlier trades that the book was not seeded
    with: the first fill of a new, losing trade must not absorb that +500 and look like a win."""
    daybook, *_ = await loaded()
    history = position(quantity=0, average="0", realized_pnl=D("500"))
    feed = Positions(daybook, [history])

    await feed.round_trip("100", "99")  # -65 gross

    assert daybook.day().loss_streak == 1
