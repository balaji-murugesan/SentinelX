"""The risk monitor (add-risk-engine tasks 7.1 and 7.2; stale-data-guard and equity-floor specs).

Driven with a fake clock, ``on_tick`` and ``step`` (no real tasks, no real time): scenarios "Feed
goes quiet", "Feed returns", "After the close", "Blind too long", "Next day", "Breach" and
"Restart after a high peak"; the heartbeat the gate reads; the ratchet and its persistence; the
previous close; and the fail-closed paths. add-risk-guards task 8.2: the expiry giveback
("Entering an expiring contract", "Expiring position closed", "Restart keeps it").

The kill and flatten hooks read the stored day state *at the moment they are called*, which is how
these tests prove that the ratchet and the day lock are persisted before the engine acts.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from contracts.commands import SetRiskLimits
from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderState
from contracts.snapshot import HaltLevel
from engine.market_data.bus import TickBus
from engine.market_data.provider import Tick
from engine.persistence.repository import PersistenceError
from engine.positions.book import position_id
from engine.risk.audit import RiskAudit
from engine.risk.checks.flow import REJECT_BREAKER, RejectBreaker
from engine.risk.checks.stale import (
    FEED_DOWN,
    FLOOR_BREACH,
    MTM_BLIND,
    FeedDown,
    FloorBreach,
    MtmBlind,
)
from engine.risk.daybook import DayBook
from engine.risk.decision import ALLOW, ContinuousCheck, Decision, RiskContext, RiskTrigger
from engine.risk.floor import floor_of
from engine.risk.limits import MODE_PREFIX
from engine.risk.limits_service import LimitsService, LimitsStatus
from engine.risk.monitor import REFERENCE_MISSING, RiskMonitor, mark_key, previous_weekday
from engine.risk.pnl import estimate_exit_cost
from engine.risk.rejections import RejectionWindow
from engine.risk.state import DayState
from tests.engine.risk_builders import (
    INSTRUMENTS,
    NOW,
    OPTION,
    STOCK,
    TODAY,
    fill,
    instrument_of,
    order,
    position,
)
from tests.engine.risk_fakes import MemoryRiskRepository, StubTrading
from tests.engine.test_risk_limits import BASE
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
AFTER_CLOSE = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)  # 16:00 IST, Monday
BEFORE_OPEN = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 08:30 IST
AT_OPEN = datetime(2026, 11, 16, 3, 45, tzinfo=UTC)  # 09:15 IST
SATURDAY = datetime(2026, 11, 14, 5, 30, tzinfo=UTC)  # 11:00 IST
FRIDAY = date(2026, 11, 13)
CHECKS: tuple[ContinuousCheck, ...] = (FeedDown(), MtmBlind(), FloorBreach())


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Env:
    """A monitor with real limits service, day book and audit on an in-memory repository."""

    def __init__(
        self,
        *,
        positions: list | None = None,  # type: ignore[type-arg]
        limits: dict | None = None,  # type: ignore[type-arg]
        repo: MemoryRiskRepository | None = None,
        at: datetime = NOW,
        checks: tuple[ContinuousCheck, ...] = CHECKS,
        on_kill: bool = True,
        rejections: RejectionWindow | None = None,
        instruments: Callable[[InstrumentKey], Instrument | None] = instrument_of,
    ) -> None:
        self.clock = Clock(at)
        self.repo = repo or MemoryRiskRepository()
        self.limit_values = limits or BASE
        self.trading = StubTrading({position_id(p): p for p in positions or []})
        self.subscribed = True
        self.unprotected: frozenset = frozenset()  # type: ignore[type-arg]
        self.kills: list[Decision] = []
        self.kill_saw: list[DayState | None] = []  # the stored day at the moment of each kill
        self.flat_calls: list[str] = []
        self.flat_saw: list[DayState | None] = []  # the stored day at the moment of each call
        self.daybook = DayBook(
            account_id="main", repository=self.repo, limits=lambda: self.limits.current,
            now=self.clock,
        )  # fmt: skip
        self.limits = LimitsService(
            account_id="main", repository=self.repo, results=MemoryRepository(),
            figures=self._figures, now=self.clock,
        )  # fmt: skip
        self.audit = RiskAudit(self.repo)
        self.rejections = rejections or RejectionWindow()
        self.monitor = RiskMonitor(
            account_id="main", limits=self.limits, daybook=self.daybook, repository=self.repo,
            audit=self.audit, instruments=instruments, checks=checks,
            subscribed=lambda: self.subscribed, interval_s=1.0, persist_interval_s=10.0,
            on_kill=self._kill if on_kill else None, ensure_flat=self._ensure_flat,
            unprotected=lambda: self.unprotected, rejections=self.rejections.recent,
            now=self.clock,
        )  # fmt: skip
        self.monitor.bind(self.trading)

    def _figures(self) -> tuple[Decimal, Decimal | None]:
        if self.daybook.unavailable(self.clock.at) is not None:
            return D(0), None
        return self.daybook.day().peak, self.daybook.day().floor

    async def _kill(self, decision: Decision) -> None:
        self.kills.append(decision)
        self.kill_saw.append(await self.repo.load_day("main", TODAY))

    async def _ensure_flat(self, reason: str) -> None:
        self.flat_calls.append(reason)
        self.flat_saw.append(await self.repo.load_day("main", TODAY))

    async def ready(self, *, set_limits: bool = True) -> "Env":
        assert await self.daybook.load()
        self.daybook.seed(self.trading.positions.values(), [])
        if set_limits:
            await self.limits.set_limits(
                SetRiskLimits(
                    command_id="c", idempotency_key="k", account_id="main", source="t",
                    sent_at=NOW, limits=self.limit_values,
                )
            )  # fmt: skip
        return self

    def tick(self, price: str, key=OPTION) -> None:  # type: ignore[no-untyped-def]
        self.monitor.on_tick(
            Tick(key, D(price), exchange_time=self.clock.at, received_at=self.clock.at)
        )

    def rows(self, code: str) -> list:  # type: ignore[type-arg]
        return [e for e in self.repo.events if e.reason_code == code]


def carried(quantity: int = 65, average: str = "100.00"):  # type: ignore[no-untyped-def]
    return position(OPTION, quantity, average)


async def stepped(env: Env, seconds: float = 0) -> None:
    env.clock.advance(seconds)
    await env.monitor.step()
    await env.audit.drain()


def mtm_of(price: str, quantity: int = 65, average: str = "100.00") -> Decimal:
    """The day's MTM of a carried position measured from its average (no reference), net of the
    estimated exit charge."""
    held, instrument = carried(quantity, average), instrument_of(OPTION)
    assert instrument is not None
    return quantity * (D(price) - D(average)) - estimate_exit_cost(held, D(price), instrument)


# ---- the heartbeat the gate reads ------------------------------------------------------------


async def test_before_the_first_pass_the_monitor_has_not_run() -> None:
    env = await Env().ready()

    assert "has not run yet" in (env.monitor.unavailable(env.clock.at) or "")


async def test_a_monitor_that_was_never_bound_says_so_instead_of_looking_healthy() -> None:
    env = await Env().ready()
    unbound = RiskMonitor(
        account_id="main", limits=env.limits, daybook=env.daybook, repository=env.repo,
        audit=env.audit, instruments=instrument_of, checks=CHECKS, subscribed=lambda: True,
        interval_s=1.0, persist_interval_s=10.0, now=env.clock,
    )  # fmt: skip

    await unbound.step()  # a pass that does nothing, but stamps the beat

    assert "not connected to trading state" in (unbound.unavailable(env.clock.at) or "")


async def test_a_pass_stamps_the_heartbeat_and_a_stalled_monitor_goes_stale() -> None:
    """A stalled monitor makes the gate reject entries: more than three intervals without a beat."""
    env = await Env().ready()

    await stepped(env)
    assert env.monitor.unavailable(env.clock.at) is None
    env.clock.advance(3)  # three intervals: still tolerated
    assert env.monitor.unavailable(env.clock.at) is None
    env.clock.advance(0.5)
    assert "has not run for" in (env.monitor.unavailable(env.clock.at) or "")
    await stepped(env)
    assert env.monitor.unavailable(env.clock.at) is None  # a fresh pass clears it


async def test_the_state_is_unavailable_until_the_day_is_loaded() -> None:
    env = Env()
    await env.limits.load()

    await stepped(env)  # the pass loads the day itself

    assert env.monitor.unavailable(env.clock.at) is None
    assert env.daybook.day().trade_date == TODAY


class LimitsDown(MemoryRiskRepository):
    down = False

    async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
        if self.down:
            raise PersistenceError("db down")
        return await super().load_limits(account_id)


async def test_a_pass_retries_limits_that_could_not_be_loaded() -> None:
    repo = LimitsDown()
    repo.down = True
    await MemoryRiskRepository.save_limits(repo, "main", dict(BASE), NOW)
    env = await Env(repo=repo).ready(set_limits=False)
    await env.limits.load()
    assert env.limits.current is None  # not loaded

    await stepped(env)
    assert env.limits.current is None
    repo.down = False
    await stepped(env)

    assert env.limits.current is not None  # the pass reloaded them


async def test_without_limits_a_pass_watches_nothing_and_halts_nothing() -> None:
    env = await Env().ready(set_limits=False)
    await env.limits.load()

    await stepped(env)

    assert env.monitor.entry_halt() is None and env.monitor.mtm_day() is None
    assert env.kills == [] and env.flat_calls == []


async def test_limits_lost_mid_run_do_not_stop_the_floor_from_killing() -> None:
    """A failed save whose store cannot be read back leaves the limits NOT_LOADED. The monitor keeps
    watching with the last ones in force, so a breach still kills (entries are refused meanwhile by
    the gate, for the limits' own reason)."""
    repo = LimitsDown()
    env = await Env(positions=[carried()], repo=repo).ready()
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)
    repo.down = True
    env.limits._current, env.limits._status = None, LimitsStatus.NOT_LOADED  # type: ignore[attr-defined]

    env.tick("53")  # -3,055 against a floor of -3,000
    await stepped(env, 1)

    assert env.limits.current is None  # still lost
    assert [k.code for k in env.kills] == [FLOOR_BREACH]


# ---- ticks -----------------------------------------------------------------------------------


async def test_a_tick_gives_a_price_and_an_age() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)  # builds the tracker

    env.tick("101")
    env.clock.advance(2)
    quote = env.monitor.quote(OPTION)

    assert quote is not None and quote.price == D("101") and quote.age == timedelta(seconds=2)
    assert env.monitor.quote(STOCK) is None  # an instrument with no tick has no quote


async def test_a_garbage_tick_changes_nothing_and_is_not_a_tick() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")
    env.clock.advance(20)

    with capture_logs() as logs:
        env.tick("NaN")  # a damaged packet
    await stepped(env)

    quote = env.monitor.quote(OPTION)
    assert quote is not None and quote.price == D("101") and quote.age == timedelta(seconds=20)
    halt = env.monitor.entry_halt()
    assert (
        halt is not None and halt.code == FEED_DOWN
    )  # the garbage did not make a silent feed live
    assert any(e["event"] == "bad tick ignored" and e["code"] == "RISK_BAD_TICK" for e in logs)


async def test_an_outlier_tick_is_logged_and_audited() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")

    with capture_logs() as logs:
        env.tick("140")  # beyond the 20% band
    await stepped(env, 1)

    assert any(e["code"] == "RISK_OUTLIER_TICK" for e in logs)
    [row] = env.rows("RISK_OUTLIER_TICK")
    assert row.inputs["instrument"] == str(OPTION) and row.inputs["price"] == "140"


async def test_ticks_before_limits_exist_are_ignored() -> None:
    env = await Env(positions=[carried()]).ready(set_limits=False)
    await env.limits.load()
    await stepped(env)

    env.tick("101")

    assert env.monitor.quote(OPTION) is None  # no thresholds yet, so no tracker


async def test_a_tightened_outlier_limit_applies_to_the_next_tick() -> None:
    """Spec "Tightening mid-day": the tracker is reconfigured, not rebuilt, so prices survive."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("100")
    env.tick("110")  # 10% is inside the 20% band: accepted
    assert env.rows("RISK_OUTLIER_TICK") == []

    edit = await env.limits.set_limits(
        SetRiskLimits(
            command_id="t", idempotency_key="t", account_id="main", source="t", sent_at=NOW,
            limits={**BASE, "outlier_pct": "5"},
        )
    )  # fmt: skip
    assert edit.accepted, edit.reason
    await stepped(env, 1)  # the pass applies the new threshold
    env.tick("121")  # 10% from 110: now beyond the 5% band
    await stepped(env, 1)

    assert len(env.rows("RISK_OUTLIER_TICK")) == 1
    quote = env.monitor.quote(OPTION)
    assert quote is not None and quote.price == D("121")  # and the earlier prices were kept


# ---- MTM, the peak and the floor -------------------------------------------------------------


async def test_the_days_mtm_follows_the_latest_price() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)

    env.tick("104")
    await stepped(env, 1)

    assert env.monitor.mtm_day() == mtm_of("104")


async def test_the_peak_and_floor_only_rise_and_are_written_by_the_flush() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)

    for price in ("110", "125", "140"):  # each within the band of the last accepted price
        env.tick(price)
        await stepped(env, 1)
    peak = env.daybook.day().peak
    assert peak == mtm_of("140") and peak > D("1000")
    assert env.limits.current is not None
    floor = floor_of(env.limits.current.floor_params(), peak)  # the lock and giveback terms
    assert floor > D("200") and env.daybook.day().floor == floor

    env.tick("130")  # falls back, but not to the floor
    await stepped(env, 1)

    assert env.daybook.day().peak == peak  # the peak does not fall
    assert env.daybook.day().floor == floor  # nor does the floor
    assert env.kills == []
    await env.daybook.flush()
    stored = await env.repo.load_day("main", TODAY)
    assert stored is not None and stored.peak == peak and stored.floor == floor


async def test_restart_after_a_high_peak_resumes_with_the_stored_peak() -> None:
    """Spec "Restart after a high peak": the stored peak is 3,000, the restart recomputes less."""
    repo = MemoryRiskRepository()
    await repo.save_day(DayState("main", TODAY, NOW, peak=D(3000), floor=D(-1500)))
    plain = {  # only the starting loss: the formula says -3,000, lower than the stored floor
        **BASE, "lock_mode": "OFF", "lock_trigger": None, "lock_at": None, "trail_loss_x": None,
        "trail_loss_y": None, "trail_step": None, "trail_by": None, "giveback_arm": None,
        "giveback_abs": None, "giveback_pct": None,
    }  # fmt: skip
    env = await Env(positions=[carried()], repo=repo, limits=plain).ready()
    await stepped(env)

    env.tick("101")
    await stepped(env, 1)

    # max(stored, recomputed): the peak and the floor both resume at the stored, higher values
    assert env.daybook.day().peak == D(3000) and env.daybook.day().floor == D(-1500)


async def test_a_corrupt_first_tick_for_a_held_position_cannot_raise_the_peak() -> None:
    """The tracker is seeded with the position's average price, so a first tick ten times the real
    price is an outlier that needs a confirming tick, not an instant peak."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)

    env.tick("1000")
    await stepped(env, 1)

    assert env.daybook.day().peak == D(0)


# ---- the expiry giveback (add-risk-guards task 8.2) --------------------------------------------
#
# Limits: BASE's giveback (armed at 2,000, the larger of 1,000 and 30% of the peak) with an expiry
# giveback of the larger of 500 and 10%. At a peak of about 2,600 the base floor is about 1,600 and
# the expiry floor about 2,100.

EXPIRING_KEY = InstrumentKey.parse("NSE_FNO:53721")
EXPIRING = INSTRUMENTS[OPTION].model_copy(
    update={"key": EXPIRING_KEY, "trading_symbol": "NIFTY-WEEKLY-22600-CE", "expiry": TODAY}
)
WITH_EXPIRING = {**INSTRUMENTS, EXPIRING_KEY: EXPIRING}
EXPIRY_GIVEBACK = {**BASE, "giveback_abs_expiry": "500", "giveback_pct_expiry": "10"}


def expiry_env(positions: list, repo: MemoryRiskRepository | None = None) -> Env:  # type: ignore[type-arg]
    return Env(
        positions=positions, limits=EXPIRY_GIVEBACK, instruments=WITH_EXPIRING.get, repo=repo
    )


def floors_at(env: Env, peak: Decimal) -> tuple[Decimal, Decimal]:
    """(base floor, expiry floor) the limits in force give at ``peak``."""
    limits = env.limits.current
    assert limits is not None
    return floor_of(limits.floor_params(), peak), floor_of(limits.floor_params(expiry=True), peak)


async def climb(env: Env, key: InstrumentKey, *prices: str) -> None:
    for price in prices:  # each within the outlier band of the last accepted price
        env.tick(price, key)
        await stepped(env, 1)


async def test_an_expiring_position_switches_the_expiry_giveback_on_and_it_is_written_at_once() -> (
    None
):
    """Spec "The expiry giveback applies once the account has expiring exposure": an open position
    in a contract expiring today. Persisted on that very pass, not at the next throttled write
    (every 10 s here), so a restart right after cannot forget it."""
    env = await expiry_env([position(EXPIRING_KEY, 65)]).ready()

    await stepped(env)

    assert env.daybook.day().expiry_exposure is True
    stored = await env.repo.load_day("main", TODAY)
    assert stored is not None and stored.expiry_exposure is True


async def test_a_position_the_master_does_not_know_leaves_the_flag_off() -> None:
    """Only the master expiry date counts. The lookup returns None until the master has loaded
    (concurrently at start-up), so a restart must not switch the stricter giveback on for a whole
    non-expiry day."""
    env = await Env(
        positions=[position(EXPIRING_KEY, 65)], limits=EXPIRY_GIVEBACK, instruments=lambda k: None
    ).ready()

    await stepped(env)

    assert env.daybook.day().expiry_exposure is False


async def test_a_flat_row_in_an_expiring_contract_does_not_switch_the_flag_on() -> None:
    env = await expiry_env([position(EXPIRING_KEY, 0)]).ready()

    await stepped(env)

    assert env.daybook.day().expiry_exposure is False


async def test_positions_that_do_not_expire_today_leave_the_base_giveback() -> None:
    env = await expiry_env([carried()]).ready()
    await stepped(env)

    await climb(env, OPTION, "110", "125", "140")

    peak = env.daybook.day().peak
    base, expiry = floors_at(env, peak)
    assert env.daybook.day().expiry_exposure is False
    assert env.daybook.day().floor == base < expiry


async def test_holding_an_expiring_contract_ratchets_the_floor_with_the_expiry_giveback() -> None:
    env = await expiry_env([position(EXPIRING_KEY, 65)]).ready()
    await stepped(env)

    await climb(env, EXPIRING_KEY, "110", "125", "140")

    peak = env.daybook.day().peak
    base, expiry = floors_at(env, peak)
    assert peak > D(2000) and base < expiry  # armed, and the expiry floor is the higher one
    assert env.daybook.day().floor == expiry


async def test_entering_an_expiring_contract_applies_the_expiry_giveback() -> None:
    """Spec "Entering an expiring contract": the gate allowed an entry in a contract expiring
    today (the day book switched the flag on), and the day's peak then reaches its arming level:
    the floor allows at most the expiry giveback."""
    env = await expiry_env([carried()]).ready()
    await stepped(env)
    assert await env.daybook.entry_allowed(1, True) is None

    await climb(env, OPTION, "110", "125", "140")

    peak = env.daybook.day().peak
    assert env.daybook.day().floor == floors_at(env, peak)[1]
    assert peak - env.daybook.day().floor == D(500)  # the larger of 500 and 10% of ~2,600


async def test_expiring_position_closed_the_expiry_giveback_still_applies() -> None:
    """Spec "Expiring position closed": the expiry giveback has applied since the morning, then
    the expiring position is closed. The flag stays on and the floor does not fall; as the day's
    peak climbs further on another position, the floor still follows the expiry giveback."""
    env = await expiry_env([position(EXPIRING_KEY, 65), carried()]).ready()
    await stepped(env)
    env.tick("100", EXPIRING_KEY)
    await climb(env, OPTION, "110", "125", "140")
    before = env.daybook.day().floor
    assert before == floors_at(env, env.daybook.day().peak)[1]

    closed = position(EXPIRING_KEY, 0)
    env.trading.positions[position_id(closed)] = closed  # the expiring position is now flat
    await climb(env, OPTION, "150")

    day = env.daybook.day()
    base, expiry = floors_at(env, day.peak)
    assert day.expiry_exposure is True
    assert day.floor == expiry > base and day.floor > before


async def test_a_restart_keeps_the_expiry_giveback() -> None:
    """Spec "Restart keeps it": the expiring position was closed before the restart, and the
    restarted engine still ratchets with the expiry giveback (the flag is read back from the
    stored day)."""
    first = await expiry_env([position(EXPIRING_KEY, 65)]).ready()
    await stepped(first)  # switched on and written; first is gone, only the repository remains

    restarted = await expiry_env([carried()], repo=first.repo).ready()
    await stepped(restarted)
    await climb(restarted, OPTION, "110", "125", "140")

    day = restarted.daybook.day()
    assert day.expiry_exposure is True
    assert day.floor == floors_at(restarted, day.peak)[1]


# ---- the floor breach ------------------------------------------------------------------------


async def test_breach_writes_the_ratchet_before_it_kills() -> None:
    """Spec "Breach": the loss side is not debounced, and the day state is persisted FIRST: the
    kill hook reads the stored row at the moment it is called."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)

    env.tick("53")  # an outlier for the peak, believed for the loss side: -3,055 against -3,000
    await stepped(env, 1)

    [kill] = env.kills
    assert kill.code == FLOOR_BREACH
    [saw] = env.kill_saw
    assert saw is not None and saw.floor == D("-3000")  # already stored when the kill was called


async def test_a_breach_is_acted_on_even_if_the_write_fails() -> None:
    """Spec "Persistence down at a breach": the kill still runs."""

    class SaveDown(MemoryRiskRepository):
        async def save_day(self, state: DayState) -> DayState:
            raise PersistenceError("db down")

    env = await Env(positions=[carried()], repo=SaveDown()).ready()
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)

    env.tick("53")
    await stepped(env, 1)

    assert [k.code for k in env.kills] == [FLOOR_BREACH]


async def test_entries_stay_refused_after_a_breach_until_the_kill_has_locked_the_day() -> None:
    """With no kill switch wired (or a slow one) nothing else refuses entries, so the breach itself
    is the standing halt."""
    env = await Env(positions=[carried()], on_kill=False).ready()
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)

    with capture_logs() as logs:
        env.tick("53")
        await stepped(env, 1)

    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == FLOOR_BREACH
    assert any("no kill switch is wired" in e["event"] for e in logs)


async def test_a_kill_hook_that_fails_is_retried_on_the_next_pass() -> None:
    env = await Env(positions=[carried()]).ready()
    calls: list[int] = []

    async def flaky(decision: Decision) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("kill switch hiccup")

    env.monitor._on_kill = flaky  # type: ignore[attr-defined]
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)
    env.tick("53")

    await stepped(env, 1)  # the hook fails
    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == FLOOR_BREACH  # logged, and the pass was not abandoned

    await stepped(env, 1)

    assert len(calls) == 2  # the next pass tried again


async def test_the_kill_comes_before_the_audit_of_the_breach() -> None:
    """A slow or failing audit must never delay a kill: the pass acts first, then records."""
    order: list[str] = []

    class Ordered(MemoryRiskRepository):
        async def append_event(self, event) -> None:  # type: ignore[no-untyped-def,override]
            if event.reason_code == FLOOR_BREACH:
                order.append("audit")
            await super().append_event(event)

    env = await Env(positions=[carried()], repo=Ordered()).ready()
    base_kill = env._kill

    async def recording(decision: Decision) -> None:
        order.append("kill")
        await base_kill(decision)

    env.monitor._on_kill = recording  # type: ignore[attr-defined]
    await stepped(env)
    env.tick("101")
    await stepped(env, 1)
    env.tick("53")

    await stepped(env, 1)

    assert order[:2] == ["kill", "audit"]


async def test_a_day_state_write_that_fails_in_any_way_does_not_stop_the_kill() -> None:
    """Not only a ``PersistenceError``: a driver bug at the write must not leave a breach
    un-acted on (and repeated on every pass)."""
    env = await Env(positions=[carried()]).ready()

    async def broken(state: DayState) -> DayState:
        raise RuntimeError("driver bug")

    await stepped(env)
    env.tick("101")
    await stepped(env, 1)
    env.repo.save_day = broken  # type: ignore[method-assign]
    env.tick("53")

    await stepped(env, 1)

    assert [k.code for k in env.kills] == [FLOOR_BREACH]


async def test_the_flatten_is_asked_for_before_the_audit_of_the_blind_decision() -> None:
    order: list[str] = []

    class Ordered(MemoryRiskRepository):
        async def append_event(self, event) -> None:  # type: ignore[no-untyped-def,override]
            if event.reason_code == MTM_BLIND:
                order.append("audit")
            await super().append_event(event)

    env = await Env(positions=[carried()], repo=Ordered()).ready()
    base = env._ensure_flat

    async def recording(reason: str) -> None:
        order.append("flatten")
        await base(reason)

    env.monitor._ensure_flat = recording  # type: ignore[attr-defined]
    env.subscribed = False
    await stepped(env)
    env.tick("101")

    await stepped(env, 61)

    assert order[:2] == ["flatten", "audit"]


async def test_a_monitor_restarted_on_a_day_locked_earlier_asks_for_the_flatten_at_once() -> None:
    """The lock is read from the stored day row, not from anything the monitor remembers, so a
    flatten that a crash cut short starts again on the new process's first pass."""
    repo = MemoryRiskRepository()
    await repo.save_day(
        DayState("main", TODAY, NOW, halt_level=HaltLevel.MTM_BLIND, halt_reason="book is blind")
    )
    env = await Env(positions=[carried()], repo=repo).ready()

    await stepped(env)

    assert env.flat_calls == ["book is blind"]


async def test_the_unprotected_positions_reach_the_checks() -> None:
    seen: list[frozenset] = []  # type: ignore[type-arg]

    class Spy:
        name = "spy"

        def continuous(self, ctx, event):  # type: ignore[no-untyped-def]
            seen.append(ctx.unprotected)
            return ALLOW

    env = await Env(positions=[carried()], checks=(Spy(),)).ready()
    env.unprotected = frozenset({position_id(carried())})

    await stepped(env)

    assert seen == [frozenset({position_id(carried())})]


# ---- the silent feed -------------------------------------------------------------------------


async def test_feed_goes_quiet_and_then_returns() -> None:
    """Specs "Feed goes quiet" and "Feed returns"."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")

    with capture_logs() as logs:
        await stepped(env, 16)  # 16 s with no tick, feed-down is 15 s, at 10:30 IST
    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == FEED_DOWN
    assert env.rows(FEED_DOWN) != []  # an audit row ...
    assert any(e["event"] == "risk decision started" and e["code"] == FEED_DOWN for e in logs)

    env.tick("101")  # ticks resume
    await stepped(env, 1)
    assert env.monitor.entry_halt() is None  # cleared by itself


async def test_the_feed_down_row_is_written_once_per_episode_not_every_pass() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)

    for _ in range(5):
        await stepped(env, 20)

    assert len(env.rows(FEED_DOWN)) == 1


async def test_silence_after_the_close_raises_nothing() -> None:
    """Spec "After the close": ten minutes without a tick at 16:00 IST. Nothing at all happens: no
    halt, no day lock, no flatten."""
    env = await Env(positions=[carried()], at=AFTER_CLOSE).ready()
    await stepped(env)

    await stepped(env, 600)

    assert env.monitor.entry_halt() is None
    assert env.flat_calls == [] and env.kills == []
    assert env.daybook.day().halt_level is HaltLevel.NONE


@pytest.mark.parametrize("when", [BEFORE_OPEN, SATURDAY], ids=["before the open", "weekend"])
async def test_silence_outside_market_hours_raises_nothing(when: datetime) -> None:
    env = await Env(positions=[carried()], at=when).ready()
    await stepped(env)

    await stepped(env, 600)

    assert env.monitor.entry_halt() is None and env.flat_calls == []
    assert env.daybook.day().halt_level is HaltLevel.NONE


async def test_nothing_subscribed_raises_nothing() -> None:
    env = await Env().ready()
    env.subscribed = False
    await stepped(env)

    await stepped(env, 600)

    assert env.monitor.entry_halt() is None


async def test_yesterdays_last_tick_does_not_halt_the_feed_at_the_open() -> None:
    """The last tick of the previous session says nothing about whether the feed is down now:
    silence is measured from no earlier than today's open."""
    env = await Env(positions=[carried()], at=AT_OPEN).ready()
    await stepped(env)
    env.monitor._last_tick_at = AT_OPEN - timedelta(hours=18)  # type: ignore[attr-defined]

    await stepped(env, 5)  # 09:15:05, five seconds into the session
    assert env.monitor.entry_halt() is None

    await stepped(env, 15)  # 09:15:20: now genuinely 20 s of silence in this session
    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == FEED_DOWN


# ---- a blind book ----------------------------------------------------------------------------


async def test_blind_too_long_locks_the_day_persisted_before_it_asks_for_the_flatten() -> None:
    """Spec "Blind too long": 60 s blind time and a position with no tick for 61 s."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")
    env.subscribed = False  # isolate this case from the feed-down halt (see the next test)

    await stepped(env, 61)

    assert env.daybook.day().halt_level is HaltLevel.MTM_BLIND
    assert env.flat_calls and MTM_BLIND in env.flat_calls[0]
    first_saw = env.flat_saw[0]
    assert first_saw is not None and first_saw.halt_level is HaltLevel.MTM_BLIND  # stored first
    assert env.kills == []  # a blind book is flattened, not killed, and no broker lock exists


@pytest.mark.parametrize(
    "checks",
    [(FeedDown(), MtmBlind(), FloorBreach()), (MtmBlind(), FeedDown(), FloorBreach())],
    ids=["feed-down first", "blind first"],
)
async def test_a_silent_feed_still_locks_and_flattens_a_blind_book(
    checks: tuple[ContinuousCheck, ...],
) -> None:
    """The case this rule exists for: the whole feed stops. ``RISK_FEED_DOWN`` fires at 15 s and
    ``RISK_MTM_BLIND`` at 61 s with the same severity, so whichever check is first must not hide
    the other: the book is locked and flattened in either order."""
    env = await Env(positions=[carried()], checks=checks).ready()
    await stepped(env)
    env.tick("101")

    await stepped(env, 61)  # subscribed, and nothing has ticked for 61 s

    assert env.daybook.day().halt_level is HaltLevel.MTM_BLIND
    assert env.flat_calls != []
    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == FEED_DOWN  # the feed-down halt still stands too


async def test_the_day_is_locked_and_announced_once_not_on_every_pass() -> None:
    env = await Env(positions=[carried()]).ready()
    env.subscribed = False
    await stepped(env)
    env.tick("101")

    with capture_logs() as logs:
        for _ in range(4):
            await stepped(env, 61)

    assert sum(e["event"].startswith("book is blind") for e in logs) == 1


async def test_the_flatten_is_asked_for_on_every_pass_while_the_day_is_locked() -> None:
    """So a flatten that failed, or was cut short by a crash, starts again."""
    env = await Env(positions=[carried()]).ready()
    env.subscribed = False
    await stepped(env)
    env.tick("101")
    await stepped(env, 61)
    asked = len(env.flat_calls)

    for _ in range(3):
        await stepped(env, 1)

    assert len(env.flat_calls) == asked + 3


async def test_a_flatten_hook_that_fails_does_not_stop_the_pass_and_is_asked_again() -> None:
    env = await Env(positions=[carried()]).ready()
    attempts: list[int] = []

    async def failing(reason: str) -> None:
        attempts.append(1)
        raise RuntimeError("could not start")

    env.monitor._ensure_flat = failing  # type: ignore[attr-defined]
    env.subscribed = False
    await stepped(env)
    env.tick("101")
    await stepped(env, 61)

    await stepped(env, 1)

    assert len(attempts) == 2 and env.monitor.unavailable(env.clock.at) is None
    assert env.monitor.entry_halt() is None  # the pass went on: it did not fail as a whole


async def test_nothing_is_asked_to_be_flattened_before_the_day_is_locked() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")

    await stepped(env, 5)

    assert env.flat_calls == []


async def test_a_position_that_was_never_priced_goes_blind_after_the_wait() -> None:
    env = await Env(positions=[carried()]).ready()
    env.subscribed = False
    await stepped(env)  # starts the wait for the first tick

    await stepped(env, 30)
    assert env.flat_calls == []
    await stepped(env, 31)

    assert env.daybook.day().halt_level is HaltLevel.MTM_BLIND and env.flat_calls != []


async def test_at_the_open_the_wait_starts_at_the_open_not_at_yesterdays_tick() -> None:
    """09:15:00 with a carried position whose last tick was yesterday: blind only after a full
    blind time of THIS session without a tick (09:16:00 is not, 09:16:30 is)."""
    env = await Env(positions=[carried()], at=AT_OPEN).ready()
    env.subscribed = False
    await stepped(env)
    env.tick("101")
    env.monitor._last_tick_at = AT_OPEN - timedelta(hours=18)  # type: ignore[attr-defined]
    env.monitor._tracker._series[OPTION].latest_at = AT_OPEN - timedelta(hours=18)  # type: ignore[attr-defined,union-attr]

    await stepped(env, 45)  # 09:15:45
    assert env.flat_calls == [] and env.daybook.day().halt_level is HaltLevel.NONE
    await stepped(env, 30)  # 09:16:15: 75 s into the session without a tick

    assert env.daybook.day().halt_level is HaltLevel.MTM_BLIND


async def test_a_position_never_priced_since_before_the_open_is_blind_only_after_the_open() -> None:
    """The engine started at 08:30 and the position has had no tick yet: 45 minutes of waiting, but
    only the time since the 09:15 open counts."""
    env = await Env(positions=[carried()], at=BEFORE_OPEN).ready()
    env.subscribed = False
    await stepped(env)
    await stepped(env, 45 * 60 + 30)  # 09:15:30, thirty seconds into the session
    assert env.flat_calls == [] and env.daybook.day().halt_level is HaltLevel.NONE

    await stepped(env, 45)  # 09:16:15

    assert env.daybook.day().halt_level is HaltLevel.MTM_BLIND


async def test_a_priced_position_is_not_blind() -> None:
    env = await Env(positions=[carried()]).ready()
    env.subscribed = False
    await stepped(env)

    for _ in range(5):
        env.tick("101")
        await stepped(env, 30)  # ticks keep arriving

    assert env.flat_calls == [] and env.daybook.day().halt_level is HaltLevel.NONE


async def test_an_unknown_mtm_from_a_missing_reference_neither_flattens_nor_locks() -> None:
    """A carried position was sold in full today and has no previous close: MTM is unknown, which
    refuses entries (the headroom rule) but is not price-blindness."""
    flat = position(OPTION, 0, "0")
    env = await Env(positions=[flat]).ready()
    env.daybook.fills_today.append(fill(OrderSide.SELL, 65, "110"))
    await stepped(env)

    await stepped(env, 120)

    assert env.monitor.mtm_day() is None
    assert env.flat_calls == [] and env.daybook.day().halt_level is HaltLevel.NONE


# ---- the day, the close and the previous close -----------------------------------------------


async def test_next_day_the_counters_reset_and_a_standing_halt_clears() -> None:
    """Spec "Next day"."""
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    for _ in range(3):
        await env.daybook.entry_allowed(1, False)
    await stepped(env, 20)  # a feed-down halt is standing
    assert env.monitor.entry_halt() is not None

    env.clock.at = NOW + timedelta(days=1)
    env.tick("101")
    await stepped(env, 0)

    assert env.daybook.day().trade_date == TODAY + timedelta(days=1)
    assert env.daybook.day().entries_today == 0 and env.monitor.entry_halt() is None


async def test_the_close_writes_tomorrows_reference_and_tomorrow_uses_it() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("104")
    env.clock.at = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)  # 15:30 IST
    env.tick("104")

    await stepped(env, 0)

    stored = await env.repo.load_day("main", TODAY)
    assert stored is not None and stored.close_marks == {mark_key(position_id(carried())): "104"}

    # The next trading day: a fresh monitor, the carried position measured from that close.
    tomorrow = Env(positions=[carried()], repo=env.repo, at=NOW + timedelta(days=1))
    await tomorrow.ready(set_limits=False)
    await tomorrow.limits.load()
    await stepped(tomorrow)
    tomorrow.tick("104")
    await stepped(tomorrow, 1)

    assert tomorrow.monitor.mtm_day() == mtm_of("104", average="104")  # zero move since the close
    day_two = [e for e in tomorrow.rows(REFERENCE_MISSING) if e.at > NOW + timedelta(hours=12)]
    assert day_two == []  # day one's row is in the shared repository; day two has a reference


async def test_the_close_marks_the_confirmed_price_not_a_spike_on_the_last_tick() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("104")
    env.clock.at = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)  # 15:30 IST
    env.tick("160")  # a spike on the very last tick: an unconfirmed outlier

    await stepped(env, 0)

    stored = await env.repo.load_day("main", TODAY)
    assert stored is not None and stored.close_marks == {mark_key(position_id(carried())): "104"}


def test_the_previous_close_is_the_previous_weekday() -> None:
    assert previous_weekday(date(2026, 11, 16)) == FRIDAY  # Monday: over the weekend
    assert previous_weekday(date(2026, 11, 17)) == date(2026, 11, 16)  # Tuesday: Monday
    assert previous_weekday(date(2026, 11, 14)) == FRIDAY  # a Saturday: Friday
    assert previous_weekday(date(2026, 11, 15)) == FRIDAY  # a Sunday: Friday


async def test_monday_measures_from_fridays_close_across_the_weekend() -> None:
    repo = MemoryRiskRepository()
    marks = {mark_key(position_id(carried())): "104"}
    await repo.save_day(DayState("main", FRIDAY, NOW, close_marks=marks))
    env = await Env(positions=[carried()], repo=repo).ready()
    await stepped(env)

    env.tick("104")
    await stepped(env, 1)

    assert env.monitor.mtm_day() == mtm_of("104", average="104")  # from Friday's 104, not 100
    assert env.rows(REFERENCE_MISSING) == []


async def test_an_older_close_is_never_used_when_the_previous_weekday_has_none() -> None:
    """The engine was down at Friday's close but ran on Thursday: Thursday's marks must not stand
    in for Friday's (a position can have moved since). The average price is the reference, and the
    gap is recorded."""
    repo = MemoryRiskRepository()
    thursday = date(2026, 11, 12)
    marks = {mark_key(position_id(carried())): "90"}
    await repo.save_day(DayState("main", thursday, NOW, close_marks=marks))
    env = await Env(positions=[carried()], repo=repo).ready()
    await stepped(env)

    env.tick("104")
    await stepped(env, 1)

    assert env.monitor.mtm_day() == mtm_of("104")  # from the average of 100, not Thursday's 90
    assert len(env.rows(REFERENCE_MISSING)) == 1


async def test_one_unusable_stored_mark_is_skipped_and_the_rest_are_used() -> None:
    repo = MemoryRiskRepository()
    good = mark_key(position_id(carried()))
    await repo.save_day(
        DayState(
            "main",
            FRIDAY,
            NOW,
            close_marks={  # type: ignore[dict-item]
                "garbage": "x",
                good: "104",
                "NSE_EQ:1333|CNC": "-5",
                "NSE_EQ:2885|CNC": None,
            },
        )  # fmt: skip
    )
    env = await Env(positions=[carried()], repo=repo).ready()

    with capture_logs() as logs:
        await stepped(env)
    env.tick("104")
    await stepped(env, 1)

    assert env.monitor.mtm_day() == mtm_of("104", average="104")  # the good mark still applied
    unusable = [e for e in logs if "unusable" in e["event"]]
    assert sorted(e["mark"] for e in unusable) == [  # each bad mark reported, none stopped the pass
        "NSE_EQ:1333|CNC",
        "NSE_EQ:2885|CNC",
        "garbage",
    ]


async def test_when_the_previous_close_cannot_be_read_the_mtm_is_unknown_for_that_pass() -> None:
    """Measuring from average prices would raise the peak and floor, which never come back down,
    so with the references unavailable the day's MTM is unknown (and entries are refused)."""

    class RefsDown(MemoryRiskRepository):
        down = False

        async def load_day(self, account_id: str, trade_date: date):  # type: ignore[no-untyped-def,override]
            if self.down and trade_date != TODAY:
                raise PersistenceError("db down")
            return await super().load_day(account_id, trade_date)

    repo = RefsDown()
    env = await Env(positions=[carried()], repo=repo).ready()
    repo.down = True

    await stepped(env)
    env.tick("120")
    await stepped(env, 1)

    assert env.monitor.mtm_day() is None and env.daybook.day().peak == D(0)
    repo.down = False
    await stepped(env, 1)
    assert env.monitor.mtm_day() is not None  # the next pass loaded them


async def test_a_missing_reference_is_recorded_once_per_day() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)
    env.tick("101")

    for _ in range(3):
        await stepped(env, 1)

    assert len(env.rows(REFERENCE_MISSING)) == 1


# ---- failing closed --------------------------------------------------------------------------


async def test_a_pass_that_fails_halts_entries_and_still_beats() -> None:
    env = await Env(positions=[carried()]).ready()
    await stepped(env)

    class Broken:
        @property
        def positions(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("book unreadable")

        orders: dict = {}  # type: ignore[type-arg]

    env.monitor.bind(Broken())  # type: ignore[arg-type]
    await stepped(env)

    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == "RISK_STATE_UNAVAILABLE"
    assert env.monitor.unavailable(env.clock.at) is None  # it is alive, and says what is wrong


# ---- the supervised loop ---------------------------------------------------------------------


async def test_run_takes_ticks_and_passes_until_stopped() -> None:
    env = await Env(positions=[carried()]).ready()
    fast = RiskMonitor(
        account_id="main", limits=env.limits, daybook=env.daybook, repository=env.repo,
        audit=env.audit, instruments=instrument_of, checks=[FeedDown()],
        subscribed=lambda: True, interval_s=0.02, persist_interval_s=10.0, now=env.clock,
    )  # fmt: skip
    fast.bind(env.trading)
    bus = TickBus()
    subscription = bus.subscribe("risk")
    stop = asyncio.Event()
    passes = asyncio.Event()
    original = fast.step

    async def counting() -> None:
        await original()
        passes.set()

    fast.step = counting  # type: ignore[method-assign]
    task = asyncio.create_task(fast.run(stop, subscription))

    await asyncio.wait_for(passes.wait(), timeout=2)
    bus.publish(Tick(OPTION, D("101"), exchange_time=NOW, received_at=env.clock.at))
    passes.clear()
    await asyncio.wait_for(passes.wait(), timeout=2)  # a pass after the tick: it was consumed
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    quote = fast.quote(OPTION)
    assert quote is not None and quote.price == D("101")


async def test_a_pending_lock_makes_the_monitor_ask_for_the_flatten_on_an_unlocked_day() -> None:
    """A kill asked for while the day could not be read is waiting to be recorded: the hook is
    asked although the loaded day is not locked yet, which is how the kill reaches the day."""
    env = await Env(positions=[carried()]).ready()
    env.monitor._lock_pending = lambda: True  # type: ignore[attr-defined]

    await stepped(env)

    assert env.flat_calls == ["day locked"] and env.daybook.day().halt_level is HaltLevel.NONE


# ---- what the continuous checks see (add-risk-guards task 4.2) --------------------------------


class _Seeing:
    """A continuous check that records the context it was given and allows."""

    name = "seeing"

    def __init__(self) -> None:
        self.seen: list[RiskContext] = []

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        self.seen.append(ctx)
        return ALLOW


async def test_the_continuous_checks_see_the_recent_entries_from_the_day_book() -> None:
    seeing = _Seeing()
    env = await Env(checks=(seeing,)).ready()
    assert await env.daybook.entry_allowed(1, False) is None
    env.clock.advance(10)

    await stepped(env)

    ctx = seeing.seen[-1]
    assert ctx.recent_entries == (NOW,)
    assert (ctx.broker_rejections, ctx.target_confirmations, ctx.ban_list) == ((), 0, None)
    assert ctx.spot_key("NIFTY") is None


# ---- the rejection breaker (add-risk-guards task 6.2) -----------------------------------------


def rejected_at(at: datetime) -> Order:
    return order(OrderSide.BUY, 65, updated_at=at, state=OrderState.REJECTED)


async def test_the_continuous_checks_see_the_broker_rejections_of_the_last_5_minutes() -> None:
    seeing = _Seeing()
    env = await Env(checks=(seeing,)).ready()
    env.rejections.record(rejected_at(NOW - timedelta(seconds=301)))  # out of the window
    env.rejections.record(rejected_at(NOW - timedelta(seconds=10)))

    await stepped(env)

    assert seeing.seen[-1].broker_rejections == (NOW - timedelta(seconds=10),)
    assert env.monitor.broker_rejections(NOW) == (NOW - timedelta(seconds=10),)  # the gate's view


def breaker_trips(logs: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    return [
        e for e in logs if e["event"] == "risk decision started" and e["code"] == REJECT_BREAKER
    ]


async def test_the_breaker_logs_one_critical_alert_per_trip_not_one_per_pass() -> None:
    """Spec "Margin rejection storm" (the monitor's half): three rejections trip the breaker; the
    critical alert and the audit row are written once while it holds, through many passes. Once
    it has cleared (spec "Breaker clears") a new storm trips it again, with a new alert."""
    env = await Env(checks=(RejectBreaker(),)).ready()  # BASE: reject_breaker_n = 3
    for seconds in (0, 60, 120):
        env.rejections.record(rejected_at(NOW + timedelta(seconds=seconds)))
    env.clock.advance(120)

    with capture_logs() as logs:
        for _ in range(5):
            await stepped(env, 1)
    halt = env.monitor.entry_halt()
    assert halt is not None and halt.code == REJECT_BREAKER  # a standing halt of new entries
    trips = breaker_trips(logs)
    assert len(trips) == 1 and trips[0]["log_level"] == "critical"
    assert len(env.rows(REJECT_BREAKER)) == 1

    env.clock.at = NOW + timedelta(minutes=5, seconds=30)  # 10:35:30: the first is 330 s old
    await stepped(env)
    assert env.monitor.entry_halt() is None  # cleared by itself

    for seconds in (0, 1, 2):
        env.rejections.record(rejected_at(env.clock.at + timedelta(seconds=seconds)))
    with capture_logs() as again:
        await stepped(env, 3)
        await stepped(env, 1)
    assert [e["log_level"] for e in breaker_trips(again)] == ["critical"]  # one for the new trip
    assert len(env.rows(REJECT_BREAKER)) == 2


async def test_a_breaker_in_shadow_mode_logs_a_warning_and_halts_nothing() -> None:
    env = await Env(
        checks=(RejectBreaker(),), limits={**BASE, MODE_PREFIX + "reject_breaker": "shadow"}
    ).ready()
    for seconds in (0, 1, 2):
        env.rejections.record(rejected_at(NOW + timedelta(seconds=seconds)))

    with capture_logs() as logs:
        await stepped(env, 3)

    assert env.monitor.entry_halt() is None  # tried out, not enforced
    assert [e["log_level"] for e in breaker_trips(logs)] == ["warning"]


# ---- the F&O ban list and the spot lookup (add-risk-guards tasks 7.2 and 7.4) -----------------


class SeeContext:
    """A continuous check that only records the contexts it is given."""

    name = "equity_floor"

    def __init__(self) -> None:
        self.seen: list[RiskContext] = []

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        self.seen.append(ctx)
        return ALLOW


async def test_the_monitor_retries_the_ban_list_read_and_fills_the_context() -> None:
    """The monitor serves today's ban list (to its own checks and to the gate through
    ``ban_list``) and the spot lookup; while the stored lists cannot be read, it retries on every
    pass, and the list is None (F&O entries refused) until a read succeeds."""
    from contracts.market import InstrumentKey
    from engine.risk.banlist import BanListService
    from engine.risk.repository import StoredBanList

    class Down(MemoryRiskRepository):
        down = True
        reads = 0

        async def load_ban_list(self, account_id: str, from_date: date) -> list[StoredBanList]:
            self.reads += 1
            if self.down:
                raise PersistenceError("database unavailable")
            return await super().load_ban_list(account_id, from_date)

    repo = Down()
    repo.ban_lists[("main", TODAY)] = StoredBanList(TODAY, frozenset({"XYZ"}), NOW)
    spy = SeeContext()
    env = await Env(repo=repo, checks=(spy,)).ready()
    nifty = InstrumentKey.parse("IDX_I:13")
    ban_lists = BanListService(
        account_id="main", repository=repo, results=MemoryRepository(), now=env.clock
    )
    monitor = RiskMonitor(
        account_id="main", limits=env.limits, daybook=env.daybook, repository=repo,
        audit=env.audit, instruments=instrument_of, checks=(spy,), subscribed=lambda: True,
        interval_s=1.0, persist_interval_s=10.0, ban_lists=ban_lists,
        spot_key={"NIFTY": nifty}.get, now=env.clock,
    )  # fmt: skip
    monitor.bind(env.trading)

    await monitor.step()
    await monitor.step()
    assert repo.reads == 2  # retried on each pass while unread
    assert spy.seen[-1].ban_list is None and monitor.ban_list(env.clock.at) is None

    repo.down = False
    await monitor.step()
    await monitor.step()
    assert repo.reads == 3  # read once, then not again
    assert spy.seen[-1].ban_list == {"XYZ"} and monitor.ban_list(env.clock.at) == {"XYZ"}
    assert spy.seen[-1].spot_key("NIFTY") == nifty and spy.seen[-1].spot_key("XYZ") is None


async def test_a_hung_ban_list_read_never_delays_the_flatten_of_a_locked_day() -> None:
    """Exits come first: on a killed day the pass asks for the flatten before it retries the
    ban-list read, so a database that hangs on that read (or fails it) cannot delay it."""
    from engine.risk.banlist import BanListService
    from engine.risk.repository import StoredBanList

    class Hung(MemoryRiskRepository):
        failing = False

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def load_ban_list(self, account_id: str, from_date: date) -> list[StoredBanList]:
            self.entered.set()
            await self.release.wait()
            if self.failing:
                raise PersistenceError("database unavailable")
            return await super().load_ban_list(account_id, from_date)

    repo = Hung()
    await repo.save_day(DayState("main", TODAY, NOW, halt_level=HaltLevel.KILL, halt_reason="k"))
    env = await Env(repo=repo).ready()

    for failing in (False, True):  # a read that hangs then answers, and one that hangs then fails
        repo.failing = failing
        repo.entered.clear()
        repo.release.clear()
        ban_lists = BanListService(
            account_id="main", repository=repo, results=MemoryRepository(), now=env.clock
        )
        monitor = RiskMonitor(
            account_id="main", limits=env.limits, daybook=env.daybook, repository=repo,
            audit=env.audit, instruments=instrument_of, checks=CHECKS, subscribed=lambda: True,
            interval_s=1.0, persist_interval_s=10.0, ensure_flat=env._ensure_flat,
            ban_lists=ban_lists, now=env.clock,
        )  # fmt: skip
        monitor.bind(env.trading)
        calls = len(env.flat_calls)
        step = asyncio.create_task(monitor.step())
        await asyncio.wait_for(repo.entered.wait(), timeout=2)  # the read is hanging now

        assert len(env.flat_calls) == calls + 1  # and the flatten was already asked for

        repo.release.set()
        await asyncio.wait_for(step, timeout=2)
        assert ban_lists.read is not failing  # a failed read leaves the lists unread


async def test_a_monitor_without_ban_lists_has_no_list_and_knows_no_spot() -> None:
    spy = SeeContext()
    env = await Env(checks=(spy,)).ready()

    await env.monitor.step()

    assert env.monitor.ban_list(env.clock.at) is None
    assert spy.seen[-1].ban_list is None and spy.seen[-1].spot_key("NIFTY") is None
