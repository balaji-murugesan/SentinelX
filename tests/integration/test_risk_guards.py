"""The add-risk-guards rules against REAL Redis and Postgres (add-risk-guards task 10.1).

What the unit and assembly tests (tests/engine) already cover is not repeated here: every rule's
scenarios, the repository contract suite (the three new day-state columns merge one-way in
Postgres, ``test_risk_repository.py``), and the assembly with in-memory stores
(``test_risk_runtime.py``, ``test_risk_end_to_end.py``). This file adds the parts that need the real
stores and processes:

  1. **Day-state columns across a kill -9.** A real engine process allows an entry (its lots, its
     time and the stored expiry flag are written), is killed without warning, a late write from the
     dead process lands with older values, and the restarted process still holds the larger ones:
     the daily gross-lot cap refuses the next entry, and its minimum gap is judged from the stored
     last entry time.
  2. **A target halt survives a restart.** The target is reached in the risk assembly on the real
     Postgres store (prices are driven, so it is deterministic), then a real engine process starts
     on that database: the day is still at TARGET_HALT, entries are refused with RISK_HALTED_DAY,
     the recovery resume command does not clear it, and neither does a kill -9 and restart.
  3. **The ban list fails closed with Postgres unreachable.** The engine (``run()``, real Redis and
     Postgres) starts while the ban-list reads go to a Postgres that is not there (a closed port,
     the same way ``test_engine_run.py`` makes Postgres unreachable; the shared test container is
     never stopped): NSE F&O entries are refused with RISK_BAN_LIST_MISSING, a ban-list command is
     refused because the store cannot be read, and once Postgres answers the monitor reads the
     list and F&O entries go through. The order service takes orders only in NSE_EQ and NSE_FNO
     today (``engine.orders.validation``), so a BSE F&O or MCX order never reaches the gate
     through it; those two are put to the real gate of the assembly directly, in the same outage:
     BSE F&O is refused for the missing list, MCX is not affected.
  4. **Each new rule refuses an entry end to end** through the real gate, order service and day
     book, with both stores in Postgres (only the broker is a fake that records what it is sent),
     and the refusal is in the Postgres audit with its code.
  5. **Exits are never blocked**, end to end on Postgres: in the same state that refused each
     entry, an exit is accepted, and no registered rule other than one that applies to exits left
     an audit row about it. The registries are iterated, so a later rule is covered. (The
     ``hypothesis`` property over every registered rule, pre-trade and continuous, is in
     ``tests/engine/test_risk_exits_never_blocked.py``: it runs the gate thousands of times, which
     needs no database.)

Needs TEST_REDIS_URL (database 15) AND TEST_DATABASE_URL (a ``*_test`` database). Skipped loudly
without them locally, never in CI. These tests reshape the shared test database: never run two
pytest processes at once.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from cli import orders as cli
from contracts.commands import COMMAND_STREAM, CommandResult, LimitValue, PlaceOrder, SetRiskLimits
from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderType, ProductType
from contracts.settings import load_settings
from contracts.snapshot import HaltLevel, Snapshot
from engine.broker.adapter import BrokerAck, BrokerRejected
from engine.config import EngineSettings
from engine.main import run
from engine.market_data.clock import ist_date, utc_now
from engine.market_data.instruments import InstrumentMaster
from engine.market_data.provider import Tick
from engine.market_data.service import MarketDataService
from engine.orders.service import OrderService
from engine.persistence import tables as t
from engine.persistence.postgres import PostgresRepository
from engine.persistence.postgres_risk import PostgresRiskRepository
from engine.persistence.repository import TradingRepository
from engine.positions.book import position_id
from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.monitor import BAN_LIST_RETRY_S
from engine.risk.repository import StoredBanList
from engine.risk.runtime import RiskRuntime
from engine.risk.state import DayState
from tests.engine.risk_builders import order, position
from tests.engine.test_order_service import command, position_feed
from tests.engine.test_risk_runtime import settings_for as runtime_settings
from tests.engine.test_risk_stops import Clock, StopAdapter
from tests.engine.test_trading_repository import prepare_database
from tests.engine.trading_fakes import MemoryRepository
from tests.integration.risk_setup import ALL_DAY, reply, send_ban_list, send_limits, with_stop
from tests.integration.test_engine_run import eventually, send, snapshot_shows
from tests.integration.test_kill_restart import (
    ACCOUNT,
    LOT,
    OPTION,
    EngineProcess,
    buy_one_lot,
    engine_env,
    wait_for_snapshot,
)

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IN_CI = os.environ.get("CI") == "true"
# A Postgres that is not there: nothing listens on port 1, so every connect fails (at once, or after
# the repository's timeout where the OS drops the attempt instead of refusing it).
UNREACHABLE = "postgresql+asyncpg://u:p@127.0.0.1:1/x_test"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (TEST_REDIS_URL and TEST_DATABASE_URL) and not IN_CI,
        reason="TEST_REDIS_URL and TEST_DATABASE_URL must both be set: needs real Redis + Postgres",
    ),
]

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
# Monday 2026-11-16, 10:30 IST: market hours, for the assembly whose clock the test drives.
NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
TODAY = date(2026, 11, 16)
TOMORROW = date(2026, 11, 17)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def database_url() -> str:
    assert TEST_DATABASE_URL, "TEST_DATABASE_URL must be set (CI provides a Postgres service)"
    return TEST_DATABASE_URL


# ---- instruments (test values; a CE option is a call) --------------------------------------------


def option(
    security_id: int,
    underlying: str,
    strike: str,
    *,
    lot: int = 65,
    expiry: date = TOMORROW,
    segment: str = "NSE_FNO",
    kind: str = "OPTIDX",
) -> Instrument:
    return Instrument(
        key=InstrumentKey.parse(f"{segment}:{security_id}"),
        trading_symbol=f"{underlying}-{strike}-CE", display_name=f"{underlying} {strike} CE",
        underlying_symbol=underlying, instrument_type=kind, lot_size=lot, tick_size=D("0.05"),
        expiry=expiry, strike=D(strike), option_type="CE", freeze_qty=1800,
    )  # fmt: skip


NIFTY_CE = option(61001, "NIFTY", "24000")
NIFTY_FAR = option(61002, "NIFTY", "25000")  # 4.2% from a NIFTY of 24,000
NIFTY_EXPIRING = option(61003, "NIFTY", "24000", expiry=TODAY)
BANKNIFTY_CE = option(61004, "BANKNIFTY", "52000", lot=30)
XYZ_CE = option(61005, "XYZ", "1000", lot=500, kind="OPTSTK")  # a stock option
SENSEX_CE = option(61006, "SENSEX", "80000", lot=20, segment="BSE_FNO")
CRUDE_FUT = Instrument(
    key=InstrumentKey.parse("MCX_COMM:62001"), trading_symbol="CRUDEOILM-DEC2026-FUT",
    display_name="CRUDEOILM DEC FUT", underlying_symbol="CRUDEOILM", instrument_type="FUTCOM",
    lot_size=10, tick_size=D("0.05"), expiry=date(2026, 12, 17), freeze_qty=None,
)  # fmt: skip
NIFTY_INDEX = Instrument(
    key=InstrumentKey.parse("IDX_I:13"), trading_symbol="NIFTY", display_name="Nifty 50",
    underlying_symbol="NIFTY", instrument_type="INDEX", lot_size=1, tick_size=D("0.05"),
)  # fmt: skip
INSTRUMENTS = [
    NIFTY_CE, NIFTY_FAR, NIFTY_EXPIRING, BANKNIFTY_CE, XYZ_CE, SENSEX_CE, CRUDE_FUT, NIFTY_INDEX,
]  # fmt: skip


def master_of(items: list[Instrument], as_of: date) -> InstrumentMaster:
    master = InstrumentMaster()
    master.replace(items, as_of)
    return master


# ---- the Postgres audit --------------------------------------------------------------------------


async def audit_rows() -> list[dict[str, Any]]:
    """Every risk record in Postgres, oldest first."""
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as conn:
            query = sa.select(t.risk_event).order_by(t.risk_event.c.event_id)
            rows = (await conn.execute(query)).mappings().all()
    finally:
        await engine.dispose()
    return [dict(r) for r in rows]


# ---- the assembly on the real stores -------------------------------------------------------------


class Broker(StopAdapter):
    """Acknowledges every order and records it (orders then rest: nothing fills), or refuses it
    while ``refuse`` is set, like a broker's margin check. No broker API is called (safety rule
    11)."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = False

    async def place(self, order: Order) -> BrokerAck:
        if self.refuse:
            raise BrokerRejected("RMS: insufficient margin")
        return await super().place(order)


class Assembly:
    """The risk engine as ``engine.main`` assembles it (``RiskRuntime`` plus an ``OrderService``
    built around its gate), on the REAL Postgres stores. Only the broker and the clock are fakes:
    the test drives time and prices, so each rule can be brought to its edge on purpose."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        at: datetime,
        trading: TradingRepository | None = None,
        risk: PostgresRiskRepository | None = None,
    ) -> None:
        self.clock = Clock(at)
        self.master = master_of(INSTRUMENTS, ist_date(at))
        self.trading_repo = trading or PostgresRepository(database_url())
        self.risk_repo = risk or PostgresRiskRepository(database_url())
        self.runtime = RiskRuntime(
            settings=runtime_settings(monkeypatch, tmp_path), repository=self.trading_repo,
            risk_repository=self.risk_repo, instruments=self.master.get, subscribed=lambda: True,
            spot_of=self.master.spot_of, now=self.clock,
        )  # fmt: skip
        self.broker = Broker()
        self.service = OrderService(
            account_id=ACCOUNT, paper=True, gate=self.runtime.gate, adapter=self.broker,
            repository=self.trading_repo, instruments=self.master, position_feed=position_feed(),
            tracer=TracerProvider().get_tracer("test"), ack_timeout_s=5, now=self.clock,
        )  # fmt: skip
        self.runtime.attach(self.service)
        self.stop = asyncio.Event()
        # The latest price of each instrument, ticked on every step: option premiums of 200 and a
        # NIFTY index of 24,000 (the strike-distance rule measures from it).
        self.prices: dict[InstrumentKey, str] = {i.key: "200" for i in INSTRUMENTS}
        self.prices[NIFTY_INDEX.key] = "24000"
        self.n = 0

    async def ready(
        self, limits: dict[str, LimitValue], *, bans: frozenset[str] | None = frozenset()
    ) -> "Assembly":
        """Store today's ban list (None: none stored) and the limits, start, and make the first
        monitor pass."""
        today = ist_date(self.clock.at)
        if bans is not None:
            await self.risk_repo.save_ban_list(ACCOUNT, today, bans, self.clock.at)
        result = await self.runtime.limits.set_limits(
            SetRiskLimits(
                command_id="c-limits", idempotency_key="limits", account_id=ACCOUNT, source="test",
                sent_at=self.clock.at, limits=limits,
            )
        )  # fmt: skip
        assert result.accepted, result.reason
        await self.runtime.start(self.stop)
        await self.step()
        return self

    def tick_all(self) -> None:
        for key, price in self.prices.items():
            self.runtime.monitor.on_tick(
                Tick(key, D(price), exchange_time=self.clock.at, received_at=self.clock.at)
            )

    async def step(self, seconds: float = 0) -> None:
        """Let ``seconds`` pass with a monitor pass and fresh prices on both sides of it."""
        self.clock.at += timedelta(seconds=seconds)
        self.tick_all()
        await self.runtime.monitor.step()
        self.tick_all()

    async def place(self, instrument: Instrument, side: OrderSide, quantity: int) -> CommandResult:
        self.n += 1
        return await self.service.place(
            PlaceOrder(
                command_id=f"c{self.n}", idempotency_key=f"k{self.n}", account_id=ACCOUNT,
                source="cli", sent_at=self.clock.at, key=instrument.key, side=side,
                quantity=quantity, product=ProductType.INTRADAY, order_type=OrderType.LIMIT,
                limit_price=D(self.prices[instrument.key]),
                initial_stop=D("190.00") if side is BUY else None,
            )
        )  # fmt: skip

    async def entry(self, instrument: Instrument = NIFTY_CE, lots: int = 1) -> CommandResult:
        """A one-lot limit buy at the latest price with a stop 10 below: it is acknowledged and
        rests (the fake broker never fills), so it reserves its lots as a pending entry."""
        return await self.place(instrument, BUY, lots * instrument.lot_size)

    def hold(self, instrument: Instrument, average: str = "200.00") -> None:
        held = position(instrument.key, instrument.lot_size, average)
        self.service.positions[position_id(held)] = held

    async def abandon_flatten(self) -> None:
        """End a flatten the test started: it would otherwise chase an exit that never fills."""
        kill, square_off = self.runtime.kill, self.runtime.square_off
        running = [kill._task if kill else None, square_off.running if square_off else None]
        tasks = [task for task in running if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.abandon_flatten()
        await self.risk_repo.close()
        if isinstance(self.trading_repo, PostgresRepository):
            await self.trading_repo.close()


@pytest.fixture
async def assemble(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Callable[..., Awaitable[Assembly]]]:
    """Build a ready assembly on a freshly migrated, empty test database (ALL_DAY limits plus the
    test's changes)."""
    await prepare_database(database_url())
    made: list[Assembly] = []

    async def make(
        changes: dict[str, LimitValue] | None = None, *, bans: frozenset[str] | None = frozenset()
    ) -> Assembly:
        assembly = Assembly(monkeypatch, tmp_path, at=NOW)
        made.append(assembly)
        return await assembly.ready({**ALL_DAY, **(changes or {})}, bans=bans)

    yield make
    for assembly in made:
        await assembly.close()


async def refused(assembly: Assembly, result: CommandResult, code: str, rule: str) -> None:
    """The entry was refused with ``code``, nothing reached the broker for it, and Postgres holds
    both the rule's record and the gate's verdict with that code."""
    assert not result.accepted and (result.reason or "").startswith(code), result.reason
    await assembly.runtime.audit.drain()
    rows = await audit_rows()
    assert any(r["rule"] == rule and r["reason_code"] == code and r["enforced"] for r in rows), rule
    verdicts = [r for r in rows if r["rule"] == "gate" and r["reason_code"] == code]
    assert verdicts and verdicts[-1]["inputs"]["intent"] == "ENTRY"


async def an_exit_goes(assembly: Assembly, instrument: Instrument) -> None:
    """In the state that refused the entry, an exit of a held position in ``instrument`` is accepted
    (risk-gate "Exits are never blocked"), and no registered rule that does not apply to exits
    left a record about it: the registries are iterated, so a rule added later is covered."""
    assembly.broker.refuse = False
    assembly.hold(instrument)
    result = await assembly.place(instrument, SELL, instrument.lot_size)

    assert result.accepted, result.reason
    assert result.order_id is not None
    await assembly.runtime.audit.drain()
    rows = await audit_rows()
    about_exit = [r for r in rows if r["inputs"].get("order_id") == result.order_id]
    verdict = [r for r in about_exit if r["rule"] == "gate"]
    assert [(v["inputs"]["intent"], v["decision"]) for v in verdict] == [("EXIT", "ALLOW")]
    registered = {c.name for c in default_checks()} | {c.name for c in continuous_checks()}
    applying = {c.name for c in default_checks() if c.applies_to_exits}
    assert not {r["rule"] for r in about_exit} & (registered - applying)


# ---- 4 and 5: each new rule refuses an entry end to end; an exit still goes ----------------------


async def test_lot_cap_per_underlying(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    """lot-caps: one NIFTY lot pending, a cap of 1 per underlying: a second NIFTY entry (another
    strike) is refused."""
    a = await assemble({"max_open_lots_per_underlying": 1, "max_open_lots_total": 5})
    assert (await a.entry(NIFTY_CE)).accepted
    await a.step(2)

    await refused(a, await a.entry(NIFTY_FAR), "RISK_LOTS_UNDERLYING", "lot_caps")
    await an_exit_goes(a, NIFTY_FAR)


async def test_lot_cap_total(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"max_open_lots_per_underlying": 1, "max_open_lots_total": 1})
    assert (await a.entry(NIFTY_CE)).accepted
    await a.step(2)

    await refused(a, await a.entry(BANKNIFTY_CE), "RISK_LOTS_TOTAL", "lot_caps")
    await an_exit_goes(a, BANKNIFTY_CE)


async def test_daily_gross_lot_cap_counted_in_postgres(
    assemble: Callable[..., Awaitable[Assembly]],
) -> None:
    a = await assemble({"max_gross_lots_per_day": 1})
    assert (await a.entry(NIFTY_CE)).accepted
    stored = await a.risk_repo.load_day(ACCOUNT, TODAY)
    assert stored is not None and stored.entry_lots_today == 1  # persisted before it was sent
    assert stored.last_entry_at == NOW
    await a.step(2)

    await refused(a, await a.entry(NIFTY_CE), "RISK_LOTS_DAY", "lot_caps")
    await an_exit_goes(a, NIFTY_CE)


async def test_entry_rate(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    """order-flow-guards: at most 2 entries a minute, 2 s apart: the third is refused."""
    a = await assemble({"max_entries_per_minute": 2, "min_entry_gap_s": 1})
    for _ in range(2):
        assert (await a.entry()).accepted
        await a.step(2)

    await refused(a, await a.entry(), "RISK_ENTRY_RATE", "entry_pacing")
    await an_exit_goes(a, NIFTY_CE)


async def test_entry_gap(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"min_entry_gap_s": 20})
    assert (await a.entry()).accepted
    await a.step(10)

    await refused(a, await a.entry(), "RISK_ENTRY_GAP", "entry_pacing")
    await a.step(10)
    assert (await a.entry()).accepted  # exactly the gap later
    await an_exit_goes(a, NIFTY_CE)


async def test_reject_breaker(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    """order-flow-guards "Margin rejection storm": the broker refuses two entries; with a breaker
    at 2 the next entry is refused before it reaches the broker."""
    a = await assemble({"reject_breaker_n": 2})
    a.broker.refuse = True
    for _ in range(2):
        await a.entry()
        await a.step(2)
    assert len(a.runtime.rejections.recent(a.clock.at)) == 2

    await refused(a, await a.entry(), "RISK_REJECT_BREAKER", "reject_breaker")
    await an_exit_goes(a, NIFTY_CE)


async def test_denied_underlying(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"denied_underlyings": ["NIFTY"]})

    await refused(a, await a.entry(NIFTY_CE), "RISK_DENIED", "instrument_policy")
    await an_exit_goes(a, NIFTY_CE)


async def test_not_on_the_allow_list(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"allowed_underlyings": ["BANKNIFTY"]})

    await refused(a, await a.entry(NIFTY_CE), "RISK_NOT_ALLOWED", "instrument_policy")
    await a.step(2)
    assert (await a.entry(BANKNIFTY_CE)).accepted
    await an_exit_goes(a, NIFTY_CE)


async def test_banned_underlying(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble(bans=frozenset({"XYZ"}))

    await refused(a, await a.entry(XYZ_CE), "RISK_FNO_BAN", "instrument_policy")
    await a.step(2)
    assert (await a.entry(NIFTY_CE)).accepted  # not banned
    await an_exit_goes(a, XYZ_CE)  # instrument-policy "Exiting a banned position"


async def test_no_ban_list_for_today(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble(bans=None)

    await refused(a, await a.entry(NIFTY_CE), "RISK_BAN_LIST_MISSING", "instrument_policy")
    await an_exit_goes(a, NIFTY_CE)


async def test_minimum_premium(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"min_premium": "250"})  # the premium is 200

    await refused(a, await a.entry(NIFTY_CE), "RISK_MIN_PREMIUM", "instrument_policy")
    await an_exit_goes(a, NIFTY_CE)


async def test_strike_distance(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    """instrument-policy "Far out of the money": 3% from a NIFTY of 24,000; the 25,000 call is
    refused, the 24,000 call is not."""
    a = await assemble({"max_strike_distance_pct": "3"})

    await refused(a, await a.entry(NIFTY_FAR), "RISK_STRIKE_DISTANCE", "instrument_policy")
    assert (await a.entry(NIFTY_CE)).accepted
    await an_exit_goes(a, NIFTY_FAR)


async def test_underlying_unpriced(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    a = await assemble({"max_strike_distance_pct": "3"})
    del a.prices[NIFTY_INDEX.key]
    await a.step(120)  # the index's last price is now older than stale_s (60)

    await refused(a, await a.entry(NIFTY_CE), "RISK_UNDERLYING_UNPRICED", "instrument_policy")
    await an_exit_goes(a, NIFTY_CE)


async def test_expiry_cutoff(assemble: Callable[..., Awaitable[Assembly]]) -> None:
    """expiry-day-profile: at 10:30 IST, after a 10:00 expiry cutoff, an entry in a contract that
    expires today is refused; one expiring tomorrow is not."""
    a = await assemble({"entry_cutoff_expiry": "10:00", "square_off_time_expiry": "10:15"})

    await refused(a, await a.entry(NIFTY_EXPIRING), "RISK_EXPIRY_CUTOFF", "entry_cutoff")
    assert (await a.entry(NIFTY_CE)).accepted
    await an_exit_goes(a, NIFTY_EXPIRING)


async def test_profit_target_halts_then_entries_meet_the_locked_day(
    assemble: Callable[..., Awaitable[Assembly]],
) -> None:
    """profit-target: MTM_day above a target of 500 on two passes locks the day at TARGET_HALT
    (stored in Postgres with RISK_TARGET_HALT, audited), the book is flattened through the gate
    (the flatten's exit is an exit, and it goes), and the next entry meets RISK_HALTED_DAY."""
    a = await assemble({"daily_profit_target": "500", "target_confirm_ticks": 2})
    a.hold(NIFTY_CE)  # 65 bought at 200
    a.prices[NIFTY_CE.key] = "210"  # 650 up, less the exit's charges: above the target
    for _ in range(2):
        await a.step(1)

    stored = await a.risk_repo.load_day(ACCOUNT, TODAY)
    assert stored is not None and stored.halt_level is HaltLevel.TARGET_HALT
    assert (stored.halt_reason or "").startswith("RISK_TARGET_HALT")
    await a.runtime.audit.drain()
    assert any(
        r["rule"] == "profit_target" and r["reason_code"] == "RISK_TARGET_HALT"
        for r in await audit_rows()
    )
    await until(
        lambda: any(o.source == "risk-kill" and o.side is SELL for o in a.broker.placed)
    )  # the target's flatten: an exit, accepted by the gate on a locked day
    result = await a.entry(BANKNIFTY_CE)
    assert not result.accepted and (result.reason or "").startswith("RISK_HALTED_DAY")


async def until(condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


# ---- 1: the day-state columns across a kill -9 ------------------------------------------------


async def test_the_new_day_state_columns_survive_kill_minus_9_and_merge_one_way(
    redis: Redis, tmp_path: Path
) -> None:
    """A real engine process: earlier today the account had expiring exposure (the stored flag is
    on), an entry of one lot is allowed (lots and time written before it is sent), the process is
    killed, and a late write from it lands with older values (no lots, no time, flag off). The
    stored row keeps the larger values, and the restarted process holds them: the next entry is
    refused by the daily gross-lot cap of 1, its minimum gap was judged from the stored time, and
    the flag is still on."""
    url = database_url()
    await prepare_database(url)
    repo = PostgresRiskRepository(url)
    today = ist_date(utc_now())
    await repo.save_day(DayState(ACCOUNT, today, utc_now(), expiry_exposure=True))
    engine = EngineProcess(engine_env(tmp_path), tmp_path / "engine.log")
    try:
        engine.start()
        await wait_for_snapshot(redis, engine)
        limits = {**ALL_DAY, "max_gross_lots_per_day": 1, "min_entry_gap_s": 3600}
        assert (await send_limits(redis, limits)).accepted
        assert (await send_ban_list(redis)).accepted
        await buy_one_lot(redis, engine)
        before = await repo.load_day(ACCOUNT, today)
        assert before is not None and before.last_entry_at is not None
        assert (before.entry_lots_today, before.entries_today, before.expiry_exposure) == (
            1, 1, True,
        )  # fmt: skip

        engine.kill()
        late = await repo.save_day(DayState(ACCOUNT, today, utc_now()))  # the dead process's
        assert (late.entry_lots_today, late.last_entry_at, late.expiry_exposure) == (
            1, before.last_entry_at, True,
        )  # fmt: skip

        restarted_at = datetime.now(UTC)
        engine.start()
        await wait_for_snapshot(redis, engine, newer_than=restarted_at)
        await eventually(
            lambda: snapshot_shows(
                redis, lambda s: s.risk is not None and s.risk.entry_lots_today == 1
            ),
            seconds=30,
        )
        refusal = ""
        for attempt in range(60):  # the gate answers once the monitor has had its first pass
            engine.assert_running()
            entry = await send(
                redis,
                with_stop(command(f"after-{attempt}", instrument=OPTION, quantity=LOT, limit="95")),
                wait_s=10,
            )
            assert not entry.accepted, "an entry over the stored daily gross-lot cap was accepted"
            refusal = entry.reason or ""
            if refusal.startswith("RISK_LOTS_DAY"):
                break
            await asyncio.sleep(0.5)
        assert refusal.startswith("RISK_LOTS_DAY"), refusal

        after = await repo.load_day(ACCOUNT, today)
        assert after is not None
        assert (after.entry_lots_today, after.last_entry_at, after.expiry_exposure) == (
            1, before.last_entry_at, True,
        )  # fmt: skip
        gaps = [
            r for r in await audit_rows()
            if r["rule"] == "entry_pacing" and r["reason_code"] == "RISK_ENTRY_GAP"
            and r["at"] >= restarted_at
        ]  # fmt: skip
        assert gaps, "the restarted gate did not judge the gap from the stored last entry time"
    except BaseException:
        print(f"--- engine log (last 3000 chars) ---\n{engine.log()[-3000:]}")
        raise
    finally:
        engine.kill_if_running()
        await repo.close()


# ---- 2: a target halt survives a restart ------------------------------------------------------


async def refused_on_the_locked_day(redis: Redis, engine: EngineProcess, prefix: str) -> None:
    """Entries are never accepted, and once the engine is ready the refusal is RISK_HALTED_DAY."""
    refusal = ""
    for attempt in range(60):
        engine.assert_running()
        entry = await send(
            redis,
            with_stop(command(f"{prefix}-{attempt}", instrument=OPTION, quantity=LOT, limit="95")),
            wait_s=10,
        )
        assert not entry.accepted, "an entry was accepted on a target-halted day"
        refusal = entry.reason or ""
        if refusal.startswith("RISK_HALTED_DAY"):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"never refused with RISK_HALTED_DAY: {refusal}")


def target_halted(snapshot: Snapshot) -> bool:
    return snapshot.risk is not None and snapshot.risk.halt_level is HaltLevel.TARGET_HALT


async def test_a_target_halt_survives_a_restart_the_resume_command_and_a_kill_minus_9(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = database_url()
    await prepare_database(url)
    # First life: the target is reached with driven prices, today (by the real clock), on the real
    # Postgres risk store. Its trading state is in memory, so the next life's recovery finds a
    # clean book and nothing but the day's lock can refuse its entries.
    first = Assembly(monkeypatch, tmp_path, at=utc_now(), trading=MemoryRepository())
    try:
        await first.ready({**ALL_DAY, "daily_profit_target": "500", "target_confirm_ticks": 2})
        first.hold(NIFTY_CE)
        first.prices[NIFTY_CE.key] = "210"
        for _ in range(2):
            await first.step(1)
        today = ist_date(first.clock.at)
        stored = await first.risk_repo.load_day(ACCOUNT, today)
        assert stored is not None and stored.halt_level is HaltLevel.TARGET_HALT
    finally:
        await first.close()  # the process ends mid-flatten

    engine = EngineProcess(engine_env(tmp_path), tmp_path / "engine.log")
    repo = PostgresRiskRepository(url)
    try:
        engine.start()
        await wait_for_snapshot(redis, engine)
        await eventually(lambda: snapshot_shows(redis, target_halted), seconds=30)
        await refused_on_the_locked_day(redis, engine, "restart")

        lines: list[str] = []
        code = await cli.resume_recovery(redis, account_id=ACCOUNT, out=lines.append, wait_s=30)
        # The engine answered (accepted or refused, either is fine): the resume path really ran.
        assert code in (cli.ACCEPTED, cli.REJECTED), (code, lines)
        await refused_on_the_locked_day(redis, engine, "after-resume")
        snapshot = await cli.read_snapshot(redis, ACCOUNT)
        assert snapshot is not None and target_halted(snapshot), lines

        engine.kill()
        restarted_at = datetime.now(UTC)
        engine.start()
        await wait_for_snapshot(redis, engine, newer_than=restarted_at)
        await eventually(lambda: snapshot_shows(redis, target_halted), seconds=30)
        await refused_on_the_locked_day(redis, engine, "after-kill")
        stored = await repo.load_day(ACCOUNT, today)
        assert stored is not None and stored.halt_level is HaltLevel.TARGET_HALT
        assert (stored.halt_reason or "").startswith("RISK_TARGET_HALT")
    except BaseException:
        print(f"--- engine log (last 3000 chars) ---\n{engine.log()[-3000:]}")
        raise
    finally:
        engine.kill_if_running()
        await repo.close()


# ---- 3: the ban list fails closed with Postgres unreachable -----------------------------------


class BanListsUnreachable(PostgresRiskRepository):
    """The real Postgres risk store, except that while ``down`` its ban-list reads and writes go to
    a Postgres that is not there, so the driver fails to connect exactly as it does when the
    database is lost. Everything else (limits, the day's row, the audit) stays on the real store,
    so the only thing that can refuse an F&O entry is the missing list."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.down = True
        self._nowhere = PostgresRiskRepository(UNREACHABLE, timeout_s=2)

    async def load_ban_list(self, account_id: str, from_date: date) -> list[StoredBanList]:
        store = self._nowhere if self.down else super()
        return await store.load_ban_list(account_id, from_date)

    async def save_ban_list(
        self, account_id: str, trade_date: date, symbols: frozenset[str], at: datetime
    ) -> StoredBanList:
        store = self._nowhere if self.down else super()
        return await store.save_ban_list(account_id, trade_date, symbols, at)

    async def close(self) -> None:
        await self._nowhere.close()
        await super().close()


class RunningEngine:
    """``engine.main.run`` as a background task (real Redis, Postgres from DATABASE_URL, the paper
    broker and the simulated feed), with an instrument master holding one NSE F&O option, priced by
    the feed."""

    ITEMS = [NIFTY_CE]

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, redis: Redis, risk: Any
    ) -> None:
        for name, value in {
            "REDIS_URL": TEST_REDIS_URL or "", "DATABASE_URL": database_url(),
            "ENGINE_HEARTBEAT_PATH": str(tmp_path / "hb"), "FEED_SOURCE": "simulated",
            "SNAPSHOT_INTERVAL_S": "0.05", "SIM_INTERVAL_S": "0.05",
            "FEED_INSTRUMENTS": ",".join(str(i.key) for i in self.ITEMS),
        }.items():  # fmt: skip
            monkeypatch.setenv(name, value)
        settings = load_settings(EngineSettings)

        async def no_instruments(_today: date) -> None:
            return None  # no instrument download in this test

        market_data = MarketDataService(settings, load=no_instruments)
        market_data.master = master_of(self.ITEMS, ist_date(utc_now()))
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(
            run(
                settings, redis=redis, stop=self.stop, market_data=market_data,
                risk_repository=risk,
            )
        )  # fmt: skip

    async def shutdown(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=10)


async def attempt_entries(
    redis: Redis, instrument: Instrument, prefix: str, *, until_reason: str | None = None
) -> tuple[CommandResult, list[str]]:
    """Send a resting one-lot limit buy (95, stop 85: the simulated price starts at 100) with a new
    key each time, until one is accepted or, with ``until_reason``, refused for that reason.
    Returns the last answer and every refusal reason seen on the way (early ones are the engine
    starting)."""
    reasons: list[str] = []
    for attempt in range(150):
        order = with_stop(
            command(
                f"{prefix}-{attempt}", instrument=instrument.key, quantity=instrument.lot_size,
                limit="95",
            ),
            "85",
        )  # fmt: skip
        await redis.xadd(COMMAND_STREAM, {"data": order.model_dump_json()})
        result = await reply(redis, order.command_id)
        if result.accepted:
            return result, reasons
        reasons.append(result.reason or "")
        if until_reason is not None and reasons[-1].startswith(until_reason):
            return result, reasons
        await asyncio.sleep(0.1)
    raise AssertionError(f"{prefix}: no final answer; last refusals {reasons[-3:]}")


async def test_the_ban_list_fails_closed_while_postgres_is_unreachable_and_recovers(
    redis: Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = database_url()
    await prepare_database(url)
    seed = PostgresRiskRepository(url)
    try:  # today's list IS stored (no bans): it just cannot be read yet
        await seed.save_ban_list(ACCOUNT, ist_date(utc_now()), frozenset(), utc_now())
    finally:
        await seed.close()
    risk = BanListsUnreachable(url)
    engine = RunningEngine(monkeypatch, tmp_path, redis, risk)
    try:
        assert (await send_limits(redis)).accepted
        result, _ = await attempt_entries(
            redis, NIFTY_CE, "nse", until_reason="RISK_BAN_LIST_MISSING"
        )
        assert not result.accepted, "an F&O entry was accepted with no ban list read"
        await eventually(lambda: snapshot_shows(redis, lambda s: s.risk is not None), seconds=10)
        snapshot = await cli.read_snapshot(redis, ACCOUNT)
        assert snapshot is not None and snapshot.risk is not None
        assert not snapshot.risk.ban_list_loaded
        # Loading a list cannot help while the store cannot be read: refused, nothing remembered.
        loaded = await send_ban_list(redis, key="ban-while-down")
        assert not loaded.accepted and (loaded.reason or "").startswith("RISK_STATE_UNAVAILABLE")

        risk.down = False  # Postgres answers again: the monitor's next due retry reads the list
        await eventually(  # at most BAN_LIST_RETRY_S after the last retry, plus slack
            lambda: snapshot_shows(redis, lambda s: s.risk is not None and s.risk.ban_list_loaded),
            seconds=BAN_LIST_RETRY_S + 15,
        )
        result, reasons = await attempt_entries(redis, NIFTY_CE, "nse-back")
        assert result.accepted
        assert not [r for r in reasons if r.startswith("RISK_BAN_LIST_MISSING")]
    finally:
        await engine.shutdown()
        await risk.close()


async def test_with_the_ban_lists_unreachable_bse_fno_fails_closed_and_mcx_is_not_affected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same outage, for the segments the order service does not take orders in yet: each order
    is put to the assembly's real gate (real day book and monitor, both stores in Postgres). A BSE
    F&O entry is refused for the missing list like an NSE one; an MCX entry is not (the exchange's
    F&O ban list does not cover commodities); once the store answers, the monitor's next due retry
    (rate-limited to one per ``BAN_LIST_RETRY_S``) reads the list and the BSE entry is allowed."""
    url = database_url()
    await prepare_database(url)
    seed = PostgresRiskRepository(url)
    try:
        await seed.save_ban_list(ACCOUNT, TODAY, frozenset(), NOW)  # stored, not yet readable
    finally:
        await seed.close()
    risk = BanListsUnreachable(url)
    a = Assembly(monkeypatch, tmp_path, at=NOW, risk=risk)
    try:
        await a.ready(ALL_DAY, bans=None)

        def entry(instrument: Instrument, n: int) -> Order:
            return order(
                BUY, instrument.lot_size, key=instrument.key, order_type=OrderType.LIMIT,
                limit_price=D("200.00"), initial_stop=D("190.00"), order_id=f"g{n}",
                idempotency_key=f"g{n}", created_at=a.clock.at, updated_at=a.clock.at,
            )  # fmt: skip

        assert not a.runtime.ban_lists.read
        for n, instrument in enumerate((SENSEX_CE, NIFTY_CE)):
            reason = await a.runtime.gate.check(entry(instrument, n))
            assert (reason or "").startswith("RISK_BAN_LIST_MISSING"), (instrument.key, reason)
        assert await a.runtime.gate.check(entry(CRUDE_FUT, 2)) is None  # allowed

        risk.down = False
        await a.step(2)
        assert not a.runtime.ban_lists.read  # the retry is rate-limited: not due yet
        await a.step(BAN_LIST_RETRY_S)  # due now: the monitor's retry reads the list
        assert a.runtime.ban_lists.read
        assert await a.runtime.gate.check(entry(SENSEX_CE, 3)) is None
    finally:
        await a.close()
