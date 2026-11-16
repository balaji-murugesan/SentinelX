"""The risk pieces wired together: real gate, monitor, day book, registered rules and order service
(add-risk-engine groups 5 to 7). No stubs for the risk engine itself, only for the broker.

Scenarios: "Limit reached" (the daily maximum, end to end, with the count persisted), "Feed silent"
fail closed with the feed stopped and clearing when it returns, "Fresh install", a stalled monitor,
and exits staying available through all of it.
"""

from datetime import timedelta
from decimal import Decimal

from contracts.commands import PlaceOrder, SetRiskLimits
from contracts.market import InstrumentKey
from contracts.mode import TradingMode
from contracts.orders import OrderSide, OrderType, ProductType
from engine.market_data.provider import Tick
from engine.orders.service import OrderService
from engine.positions.book import position_id
from engine.risk.audit import RiskAudit
from engine.risk.checks.registry import default_checks
from engine.risk.checks.stale import FeedDown, FloorBreach, MtmBlind
from engine.risk.daybook import DayBook
from engine.risk.gate import RiskEngine
from engine.risk.limits_service import LimitsService
from engine.risk.monitor import RiskMonitor
from tests.engine.risk_builders import NOW, OPTION, TODAY, instrument_of, position
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build
from tests.engine.test_risk_limits import BASE
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self):  # type: ignore[no-untyped-def]
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Engine:
    """Everything except the broker, built the way ``engine.main`` will build it."""

    def __init__(
        self,
        limits: dict | None = None,  # type: ignore[type-arg]
        *,
        risk_repo: MemoryRiskRepository | None = None,
        trading_repo: MemoryRepository | None = None,
        key_prefix: str = "k",
    ) -> None:
        self.clock = Clock()
        self.risk_repo = risk_repo or MemoryRiskRepository()  # the "database": pass it to restart
        self.trading_repo = trading_repo or MemoryRepository()
        self.limit_values = limits if limits is not None else BASE
        self.limits = LimitsService(
            account_id="main", repository=self.risk_repo, results=self.trading_repo,
            figures=lambda: (self.daybook.day().peak, self.daybook.day().floor)
            if self.daybook.unavailable(self.clock.at) is None
            else (D(0), None),
            now=self.clock,
        )  # fmt: skip
        self.daybook = DayBook(
            account_id="main", repository=self.risk_repo, limits=lambda: self.limits.current,
            now=self.clock,
        )  # fmt: skip
        self.audit = RiskAudit(self.risk_repo)
        self.monitor = RiskMonitor(
            account_id="main", limits=self.limits, daybook=self.daybook,
            repository=self.risk_repo, audit=self.audit, instruments=instrument_of,
            checks=[FeedDown(), MtmBlind(), FloorBreach()], subscribed=lambda: True,
            interval_s=1.0, persist_interval_s=10.0, now=self.clock,
        )  # fmt: skip
        self.gate = RiskEngine(
            account_id="main", mode=TradingMode.PAPER, limits=self.limits, state=self.monitor,
            audit=self.audit, instruments=instrument_of, checks=default_checks(), now=self.clock,
        )  # fmt: skip
        self.service: OrderService
        self.service, self.adapter, _ = build(
            gate=self.gate, repository=self.trading_repo, now=self.clock
        )
        self.gate.bind(self.service)
        self.monitor.bind(self.service)
        self.service.add_fill_listener(self.daybook.on_fill)
        self.n = 0
        self._prefix = key_prefix  # a restarted process must not reuse its predecessor's keys

    async def start(self, *, set_limits: bool = True) -> "Engine":
        if set_limits:
            await self.limits.set_limits(
                SetRiskLimits(
                    command_id="c", idempotency_key="k", account_id="main", source="t",
                    sent_at=NOW, limits=self.limit_values,
                )
            )  # fmt: skip
        else:
            await self.limits.load()
        await self.step()  # the first pass: loads the day and builds the tracker
        return self

    async def step(self, seconds: float = 0) -> None:
        self.clock.advance(seconds)
        await self.monitor.step()

    def tick(self, price: str = "200") -> None:
        self.monitor.on_tick(
            Tick(OPTION, D(price), exchange_time=self.clock.at, received_at=self.clock.at)
        )

    async def entry(self, *, stop: str | None = "190.00", key: InstrumentKey = OPTION) -> object:
        self.n += 1
        return await self.service.place(
            PlaceOrder(
                command_id=f"c{self.n}", idempotency_key=f"{self._prefix}{self.n}",
                account_id="main",
                source="cli", sent_at=NOW, key=key, side=BUY, quantity=65,
                product=ProductType.INTRADAY, order_type=OrderType.LIMIT,
                limit_price=D("200.00"), initial_stop=None if stop is None else D(stop),
            )
        )  # fmt: skip

    async def exit_(self) -> object:
        self.n += 1
        return await self.service.place(
            PlaceOrder(
                command_id=f"c{self.n}", idempotency_key=f"{self._prefix}{self.n}",
                account_id="main",
                source="cli", sent_at=NOW, key=OPTION, side=SELL, quantity=65,
                product=ProductType.INTRADAY, order_type=OrderType.LIMIT,
                limit_price=D("199.00"),
            )
        )  # fmt: skip

    def hold(self) -> None:
        held = position(OPTION, 65, "200.00")
        self.service.positions[position_id(held)] = held


def reason(result: object) -> str:
    return getattr(result, "reason", None) or ""


async def test_a_sound_entry_is_placed_and_counted() -> None:
    engine = await Engine().start()
    engine.tick()

    result = await engine.entry()

    assert getattr(result, "accepted", False) and len(engine.adapter.placed) == 1
    assert engine.daybook.day().entries_today == 1
    stored = await engine.risk_repo.load_day("main", TODAY)
    assert stored is not None and stored.entries_today == 1  # persisted before it was sent


async def test_limit_reached_the_next_entry_is_refused_and_exits_still_go() -> None:
    """Spec "Limit reached", end to end: the maximum is 2 and two entries were allowed."""
    engine = await Engine({**BASE, "max_trades_per_day": 2}).start()
    engine.tick()
    first, second = await engine.entry(), await engine.entry()

    third = await engine.entry()

    assert getattr(first, "accepted", False) and getattr(second, "accepted", False)
    assert not getattr(third, "accepted", True) and reason(third).startswith("RISK_MAX_TRADES")
    assert len(engine.adapter.placed) == 2
    engine.hold()
    assert getattr(await engine.exit_(), "accepted", False)  # an exit is never blocked by it


async def test_the_count_survives_a_restart() -> None:
    engine = await Engine({**BASE, "max_trades_per_day": 2}).start()
    engine.tick()
    await engine.entry()

    reborn = Engine(
        {**BASE, "max_trades_per_day": 2},
        risk_repo=engine.risk_repo,  # the same database, a new process
        trading_repo=engine.trading_repo,
        key_prefix="r",
    )
    await reborn.start(set_limits=False)
    reborn.tick()

    assert reborn.daybook.day().entries_today == 1  # not 0: one of the two is used
    assert getattr(await reborn.entry(), "accepted", False)
    assert reason(await reborn.entry()).startswith("RISK_MAX_TRADES")


async def test_feed_silent_fails_closed_then_clears_when_ticks_return() -> None:
    """Spec "Feed silent": with the feed stopped, entries are refused (RISK_FEED_DOWN); exits are
    not; when ticks resume the next pass lets entries through again."""
    engine = await Engine().start()
    engine.tick()
    engine.hold()
    assert getattr(await engine.entry(), "accepted", False)  # healthy

    await engine.step(20)  # 20 s with no tick: feed-down is 15 s
    refused = await engine.entry()
    exit_ = await engine.exit_()

    assert reason(refused).startswith("RISK_FEED_DOWN")  # the standing halt is checked first
    assert getattr(exit_, "accepted", False)  # exits continue
    engine.tick()  # the feed is back
    await engine.step(1)
    assert getattr(await engine.entry(), "accepted", False)


async def test_a_standing_feed_down_halt_is_what_the_gate_reports() -> None:
    engine = await Engine().start()
    engine.tick()
    await engine.step(20)
    engine.tick()  # a fresh quote, so only the standing halt can refuse the entry...
    # ...but the monitor has not re-evaluated yet: it still holds the halt from the last pass.

    refused = await engine.entry()

    assert reason(refused).startswith("RISK_FEED_DOWN")


async def test_fresh_install_refuses_entries_and_accepts_an_exit() -> None:
    engine = await Engine().start(set_limits=False)
    engine.hold()

    entry = await engine.entry()
    exit_ = await engine.exit_()

    assert reason(entry).startswith("RISK_LIMITS_NOT_SET")
    assert getattr(exit_, "accepted", False)


async def test_a_stalled_monitor_makes_the_gate_refuse_entries() -> None:
    engine = await Engine().start()
    engine.tick()
    assert getattr(await engine.entry(), "accepted", False)

    engine.clock.advance(10)  # no pass for 10 s: three intervals are tolerated, not ten
    engine.tick()
    refused = await engine.entry()

    assert reason(refused).startswith("RISK_STATE_UNAVAILABLE")
    assert "risk monitor has not run for" in reason(refused)


async def test_a_rejected_entry_is_not_counted() -> None:
    engine = await Engine().start()
    engine.tick()

    refused = await engine.entry(stop=None)  # no initial stop

    assert reason(refused).startswith("RISK_NO_STOP")
    assert engine.daybook.day().entries_today == 0 and engine.adapter.placed == []
