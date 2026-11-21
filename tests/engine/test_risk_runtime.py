"""The risk engine as ``engine.main`` assembles it (add-risk-engine task 11.1).

``RiskRuntime`` is built here exactly as the engine builds it, with only the broker faked, and the
tests go through the real gate, order service, day book, monitor and stop manager: the assembly
is what is under test, since each piece has its own tests.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from contracts.commands import CommandResult, KillSwitch, PlaceOrder, SetRiskLimits
from contracts.orders import Order, OrderSide, OrderType, ProductType
from contracts.settings import load_settings
from contracts.snapshot import HaltLevel, Snapshot
from engine.config import EngineSettings
from engine.market_data.provider import Tick
from engine.orders.commands import CommandConsumer
from engine.orders.intent import PROTECTIVE_STOP_SOURCE, RESERVED_SOURCES
from engine.orders.snapshot import SnapshotWriter
from engine.persistence.repository import PersistenceError
from engine.positions.book import position_id
from engine.risk import runtime as runtime_module
from engine.risk.checks.protection import UNPROTECTED
from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.decision import Decision, RiskContext, kill
from engine.risk.kill import OWN_SOURCES
from engine.risk.limits import RULES, TIGHTEN_ONLY
from engine.risk.runtime import RiskRuntime
from tests.engine.risk_builders import NOW, OPTION, TODAY, fill, instrument_of, position
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build
from tests.engine.test_risk_checks_registry import NOT_BUILT_YET
from tests.engine.test_risk_limits import BASE, edit
from tests.engine.test_risk_stops import Clock, StopAdapter
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
BUY = OrderSide.BUY


def settings_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EngineSettings:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/x")
    monkeypatch.setenv("ENGINE_HEARTBEAT_PATH", str(tmp_path / "hb"))
    monkeypatch.setenv("FEED_SOURCE", "simulated")
    return load_settings(EngineSettings)


class Env:
    """The runtime plus an order service built around its gate, like ``engine.main``."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        risk_repo: MemoryRiskRepository | None = None,
        at: datetime = NOW,
    ) -> None:
        self.clock = Clock(at)
        self.settings = settings_for(monkeypatch, tmp_path)
        self.trading_repo = MemoryRepository()
        self.risk_repo = risk_repo or MemoryRiskRepository()
        self.runtime = RiskRuntime(
            settings=self.settings, repository=self.trading_repo, risk_repository=self.risk_repo,
            instruments=instrument_of, subscribed=lambda: True, now=self.clock,
        )  # fmt: skip
        self.adapter = StopAdapter()
        self.service, _, _ = build(
            gate=self.runtime.gate,
            adapter=self.adapter,
            repository=self.trading_repo,
            now=self.clock,
        )
        self.runtime.attach(self.service)
        self.stop = asyncio.Event()
        self.n = 0

    async def send_limits(self, limits: dict[str, Any], key: str = "k") -> CommandResult:
        return await self.runtime.limits.set_limits(
            SetRiskLimits(
                command_id="c", idempotency_key=key, account_id="main", source="t", sent_at=NOW,
                limits=limits,
            )
        )  # fmt: skip

    async def set_limits(self, limits: dict[str, Any] | None = None) -> None:
        result = await self.send_limits(limits or BASE)
        assert result.accepted, result.reason

    async def ready(self, limits: dict[str, Any] | None = None) -> "Env":
        await self.set_limits(limits)
        await self.runtime.start(self.stop)
        await self.runtime.monitor.step()  # the first pass: the gate reads its heartbeat
        self.tick()
        return self

    def tick(self, price: str = "200") -> None:
        self.runtime.monitor.on_tick(
            Tick(OPTION, D(price), exchange_time=self.clock.at, received_at=self.clock.at)
        )

    async def abandon_flatten(self) -> None:
        """End a flatten the test started: it would otherwise chase an exit that never fills."""
        kill, square_off = self.runtime.kill, self.runtime.square_off
        running = [kill._task if kill else None, square_off.running if square_off else None]
        tasks = [task for task in running if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def entry(self, *, stop: str | None = "190.00") -> CommandResult:
        self.n += 1
        return await self.service.place(
            PlaceOrder(
                command_id=f"c{self.n}", idempotency_key=f"k{self.n}", account_id="main",
                source="cli", sent_at=NOW, key=OPTION, side=BUY, quantity=65,
                product=ProductType.INTRADAY, order_type=OrderType.LIMIT,
                limit_price=D("200.00"), initial_stop=None if stop is None else D(stop),
            )
        )  # fmt: skip


@pytest.fixture
def env_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Env]:
    def make(**kwargs: Any) -> Env:
        return Env(monkeypatch, tmp_path, **kwargs)

    return make


# ---- the rule table --------------------------------------------------------------------------


def test_the_engines_rules_are_exactly_the_ones_in_the_rule_table() -> None:
    """The names the engine runs and the names a limits file may give a mode to must agree: a
    rule with no row could never be shadowed, and a row with no rule would be a silent no-op.
    The catastrophic-stop check has no mode on purpose (a missing stop always binds)."""
    names = {c.name for c in default_checks()} | {c.name for c in continuous_checks()}

    assert NOT_BUILT_YET <= set(RULES)
    assert names - {"catastrophic_stop"} == set(RULES) - NOT_BUILT_YET


def test_the_engine_registers_every_check_once() -> None:
    """No check is listed twice. (Two continuous checks, a silent feed and a blind book, share the
    one rule name ``stale_data`` on purpose: one rule, one mode, two things it can notice.)"""
    from collections import Counter

    assert max(Counter(type(c) for c in default_checks()).values()) == 1
    assert Counter(c.name for c in continuous_checks()) == {
        "equity_floor": 1, "stale_data": 2, "catastrophic_stop": 1,
    }  # fmt: skip
    assert max(Counter(type(c) for c in continuous_checks()).values()) == 1


# ---- the assembly ----------------------------------------------------------------------------


async def test_the_gate_and_monitor_are_bound_and_both_fill_listeners_registered(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()

    assert env.runtime.kill is not None and env.runtime.stops is not None
    listeners = env.service._fill_listeners  # type: ignore[attr-defined]
    assert env.runtime.daybook.on_fill in listeners and env.runtime.stops.on_fill in listeners
    # Bound: an unbound gate refuses every order, an unbound monitor says it is not connected.
    assert env.runtime.monitor.unavailable(env.clock.at) != (
        "the risk monitor is not connected to trading state"
    )


async def test_before_it_has_started_the_engine_refuses_entries_and_accepts_exits(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held

    entry = await env.entry()
    exit_ = await env.service.place(
        PlaceOrder(
            command_id="x", idempotency_key="x", account_id="main", source="cli", sent_at=NOW,
            key=OPTION, side=OrderSide.SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.LIMIT, limit_price=D("199.00"),
        )
    )  # fmt: skip

    assert not entry.accepted and "RISK_" in (entry.reason or "")  # fail closed
    assert exit_.accepted  # and never refused: the engine can always reduce risk


async def test_limit_reached_end_to_end(env_factory: Callable[..., Env]) -> None:
    """Spec "Limit reached": the daily maximum, end to end through the real gate and day book."""
    env = await env_factory().ready({**BASE, "max_trades_per_day": 1})

    first = await env.entry()
    second = await env.entry()

    assert first.accepted
    assert not second.accepted and (second.reason or "").startswith("RISK_MAX_TRADES")
    assert env.runtime.daybook.day().entries_today == 1


async def test_a_fresh_install_with_no_limits_rejects_every_entry_and_accepts_an_exit(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()
    await env.runtime.start(env.stop)  # nothing stored: the limits are NOT_SET
    await env.runtime.monitor.step()
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held

    entry = await env.entry()
    exit_ = await env.service.place(
        PlaceOrder(
            command_id="x", idempotency_key="x", account_id="main", source="cli", sent_at=NOW,
            key=OPTION, side=OrderSide.SELL, quantity=65, product=ProductType.INTRADAY,
            order_type=OrderType.LIMIT, limit_price=D("199.00"),
        )
    )  # fmt: skip

    assert not entry.accepted and "RISK_LIMITS_NOT_SET" in (entry.reason or "")
    assert exit_.accepted


async def test_an_open_position_without_a_stop_halts_entries_end_to_end(
    env_factory: Callable[..., Env],
) -> None:
    """The broker rejects the stop: the position is unprotected, so the monitor halts new entries
    (through ``continuous_checks()`` and ``stops.unprotected``), and exits stay open."""
    env = await env_factory().ready()
    env.adapter.refuse_stops = True
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held
    assert env.runtime.stops is not None

    await env.runtime.stops.sweep()
    await env.runtime.stops.process_queue()  # the stop is refused: critical alert, backoff
    await env.runtime.monitor.step()  # the next pass sees the unprotected position
    entry = await env.entry()

    assert not entry.accepted and (entry.reason or "").startswith(UNPROTECTED)
    env.adapter.refuse_stops = False
    env.clock.at = env.clock.at.replace(second=env.clock.at.second + 5)
    await env.runtime.stops.process_queue()  # the retry is due and the stop is taken
    await env.runtime.monitor.step()
    assert env.runtime.stops.unprotected() == frozenset()
    assert (await env.entry()).accepted  # the halt cleared by itself


# ---- starting and stopping -------------------------------------------------------------------


class SlowToStart(MemoryRiskRepository):
    """Limits cannot be read until ``up`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.up = False

    async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
        if not self.up:
            raise PersistenceError("database down")
        return await super().load_limits(account_id)


async def test_start_retries_until_the_database_answers_and_the_monitor_waits_for_it(
    env_factory: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "RETRY_S", 0.01)
    repo = SlowToStart()
    env = env_factory(risk_repo=repo)
    starting = asyncio.create_task(env.runtime.start(env.stop))
    watching = asyncio.create_task(env.runtime.run_monitor(env.stop, _NoTicks()))  # type: ignore[arg-type]
    await asyncio.sleep(0.1)

    assert not starting.done() and not watching.done()  # still waiting for the database
    entry = await env.entry()
    assert not entry.accepted  # and the gate refuses entries meanwhile

    repo.up = True
    await asyncio.wait_for(starting, timeout=2)
    env.stop.set()
    await asyncio.wait_for(watching, timeout=2)  # the monitor loop ends with the engine
    assert env.runtime.daybook.day().trade_date == TODAY


async def test_the_monitor_makes_no_pass_until_start_has_loaded_and_seeded_the_day(
    env_factory: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_monitor`` waits for ``start``. A pass before that would stamp the heartbeat (a monitor
    that looks alive) over a day book that is not loaded and not seeded, and judge an account that
    carries a position and a day's history against nothing. So while ``start`` waits for the
    database no pass runs, and the first pass after it sees the loaded, seeded day book."""
    monkeypatch.setattr(runtime_module, "RETRY_S", 0.01)
    monkeypatch.setenv("RISK_MONITOR_INTERVAL_S", "0.05")  # passes come fast once it may run
    repo = SlowToStart()
    env = env_factory(risk_repo=repo)
    carried = position(OPTION, 65, "100.00", realized_pnl=D("0"))
    bought = fill(BUY, 65, "100", costs="10", fill_id="in").model_copy(
        update={"filled_at": NOW - timedelta(days=1)}
    )
    env.service.positions[position_id(carried)] = carried
    env.trading_repo.positions[(carried.account_id, str(carried.key), carried.product.value)] = (
        carried
    )
    env.trading_repo.fills.append(bought)
    monitor = env.runtime.monitor
    first_pass: list[tuple[str | None, dict[Any, Decimal]]] = []
    real_step = monitor.step

    async def spy() -> None:
        """What the day book looked like when each pass began."""
        daybook = env.runtime.daybook
        first_pass.append((daybook.unavailable(env.clock.at), dict(daybook._trade_net)))
        await real_step()

    monitor.step = spy  # type: ignore[method-assign]
    starting = asyncio.create_task(env.runtime.start(env.stop))
    watching = asyncio.create_task(env.runtime.run_monitor(env.stop, _NoTicks()))  # type: ignore[arg-type]
    await asyncio.sleep(0.3)  # many monitor intervals, and many start attempts

    assert not starting.done()
    assert first_pass == []  # no pass has begun
    assert monitor.unavailable(env.clock.at) == "the risk monitor has not run yet"  # no heartbeat
    assert monitor.mtm_day() is None  # and nothing was evaluated

    repo.up = True
    await asyncio.wait_for(starting, timeout=2)
    await until(lambda: len(first_pass) >= 1)
    env.stop.set()
    await asyncio.wait_for(watching, timeout=2)

    reason, trade_net = first_pass[0]
    assert reason is None  # the day was loaded when the first pass began
    assert trade_net == {position_id(carried): D("-10")}  # and seeded: the carried trade's charges


class _NoTicks:
    """An empty tick subscription, enough for a monitor loop that is stopped at once."""

    def __aiter__(self) -> "_NoTicks":
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(3600)

    def get_nowait(self) -> None:
        return None


async def test_start_gives_up_promptly_when_the_engine_is_stopped_mid_attempt(
    env_factory: Callable[..., Env],
) -> None:
    """A database that does not answer takes its timeout to fail; the engine must not wait that
    long to shut down."""

    class Hangs(MemoryRiskRepository):
        async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
            await asyncio.sleep(3600)

    env = env_factory(risk_repo=Hangs())
    starting = asyncio.create_task(env.runtime.start(env.stop))
    await asyncio.sleep(0.05)

    env.stop.set()

    await asyncio.wait_for(starting, timeout=1)


async def test_the_tasks_end_with_the_engine_even_if_start_never_succeeded(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()
    env.stop.set()

    await asyncio.wait_for(env.runtime.run_stops(env.stop), timeout=1)
    await asyncio.wait_for(env.runtime.run_square_off(env.stop), timeout=1)
    await asyncio.wait_for(env.runtime.run_monitor(env.stop, _NoTicks()), timeout=1)  # type: ignore[arg-type]


async def test_starting_on_a_locked_day_says_so_loudly_and_keeps_the_lock(
    env_factory: Callable[..., Env],
) -> None:
    from engine.risk.state import DayState

    repo = MemoryRiskRepository()
    await repo.save_day(
        DayState("main", TODAY, NOW, halt_level=HaltLevel.KILL, halt_reason="risk: floor breached")
    )
    env = env_factory(risk_repo=repo)

    with capture_logs() as logs:
        await env.runtime.start(env.stop)

    errors = [e["event"] for e in logs if e["log_level"] == "error"]
    assert any("DAY LOCKED (KILL)" in e and "NEW ENTRIES REFUSED" in e for e in errors)
    assert any("floor breached" in e for e in errors)
    assert env.runtime.daybook.day().halt_level is HaltLevel.KILL


# ---- the snapshot's risk block ---------------------------------------------------------------


async def test_the_snapshot_carries_the_risk_block_and_it_round_trips(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()
    assert env.runtime.status() is None  # nothing before the day is loaded
    await env.ready()
    assert (await env.entry()).accepted
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held
    writer = SnapshotWriter(
        None,  # type: ignore[arg-type]
        env.service,
        _Subscription(),  # type: ignore[arg-type]
        account_id="main",
        interval_s=1,
        now=env.clock,
        risk=env.runtime.status,
    )

    snapshot = writer.build()

    assert snapshot.risk is not None
    assert snapshot.risk.entries_today == 1 and snapshot.risk.halt_level is HaltLevel.NONE
    assert [(r.key, r.product) for r in snapshot.risk.unprotected] == [
        (OPTION, ProductType.INTRADAY)
    ]  # the carried position has no stop yet
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


class _Subscription:
    def get_nowait(self) -> None:
        return None


# ---- the command consumer's kill hook --------------------------------------------------------


def kill_command() -> KillSwitch:
    return KillSwitch(
        command_id="k1", idempotency_key="kill-1", account_id="main", source="cli",
        sent_at=datetime(2026, 11, 16, 5, 0, tzinfo=UTC),
    )  # fmt: skip


async def test_a_kill_command_reaches_the_kill_switch(env_factory: Callable[..., Env]) -> None:
    env = await env_factory().ready()
    assert env.runtime.kill is not None
    consumer = CommandConsumer(
        None,  # type: ignore[arg-type]
        env.service,
        account_id="main",
        kill=env.runtime.kill.handle,
    )

    result = await consumer._process(kill_command())  # type: ignore[attr-defined]

    assert result.accepted
    assert env.runtime.daybook.day().halt_level is HaltLevel.KILL


async def test_a_kill_command_with_no_kill_switch_wired_is_answered_not_dropped(
    env_factory: Callable[..., Env],
) -> None:
    env = env_factory()
    consumer = CommandConsumer(None, env.service, account_id="main")  # type: ignore[arg-type]

    result = await consumer._process(kill_command())  # type: ignore[attr-defined]

    assert not result.accepted and "not supported" in (result.reason or "")


async def test_a_monitor_that_crashes_is_restarted_and_entries_wait_for_its_next_pass(
    env_factory: Callable[..., Env],
) -> None:
    """Deferred from task 7.1: the supervisor (``engine.main._keep_alive``) restarts a monitor
    that crashed, and while it is down its heartbeat goes stale, so the gate refuses entries
    (never exits) until the restarted run's first pass."""
    from datetime import timedelta

    from engine.main import _keep_alive

    env = await env_factory().ready()
    assert (await env.entry()).accepted  # healthy
    runs: list[int] = []
    restarted = asyncio.Event()

    async def make() -> None:
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("monitor bug")  # the first run crashes
        await restarted.wait()
        await env.runtime.monitor.step()  # the restarted run's first pass

    task = asyncio.create_task(_keep_alive("risk monitor", env.stop, make, restart_s=0.01))
    for _ in range(200):  # the crash, and the restart waiting on the event
        if len(runs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(runs) == 2

    env.clock.at += timedelta(seconds=5)  # more than three monitor intervals with no pass
    env.tick()
    stale = await env.entry()
    assert not stale.accepted and "RISK_STATE_UNAVAILABLE" in (stale.reason or "")

    restarted.set()
    await asyncio.wait_for(task, timeout=2)  # the restarted run completes its pass
    env.tick()

    assert (await env.entry()).accepted  # the heartbeat is fresh again


@pytest.mark.parametrize("source", ["risk-stop", "risk-kill", "risk-squareoff"])
async def test_a_client_cannot_claim_one_of_the_risk_engines_own_sources(
    env_factory: Callable[..., Env], source: str
) -> None:
    """The engine decides what an order is by its source (the protective stop is exempt from the
    holdings check, the flattener adopts its own exits), so no command may claim one."""
    env = env_factory()
    consumer = CommandConsumer(None, env.service, account_id="main")  # type: ignore[arg-type]
    place = PlaceOrder(
        command_id="c1", idempotency_key="k1", account_id="main", source=source, sent_at=NOW,
        key=OPTION, side=BUY, quantity=65, product=ProductType.INTRADAY,
        order_type=OrderType.LIMIT, limit_price=D("200.00"), initial_stop=D("190.00"),
    )  # fmt: skip

    result = await consumer._process(place)  # type: ignore[attr-defined]

    assert not result.accepted and "reserved for the risk engine" in (result.reason or "")
    assert env.service.orders == {}  # nothing was created


async def test_a_kill_taken_before_the_day_could_be_read_reaches_the_day_through_the_runtime(
    env_factory: Callable[..., Env],
) -> None:
    """The assembled engine, not hand-wired parts: the database was down when the kill came, so the
    kill is pending; once the runtime has loaded the day, the monitor's next pass records it."""
    env = env_factory()
    await env.set_limits()
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held
    assert env.runtime.kill is not None
    await env.runtime.kill.trigger("floor breached", "risk")  # the day is not loaded yet
    assert env.runtime.kill.pending

    await env.runtime.start(env.stop)  # the database answers: the day loads, unlocked
    assert env.runtime.daybook.day().halt_level is HaltLevel.NONE
    await env.runtime.monitor.step()

    day = env.runtime.daybook.day()
    assert day.halt_level is HaltLevel.KILL and "floor breached" in (day.halt_reason or "")
    assert not env.runtime.kill.pending


# ---- what the runtime hands to the pieces (review of group 11) --------------------------------


async def until(condition: Callable[[], bool], *, seconds: float = 3.0) -> None:
    """Wait for ``condition`` (polled), for a task that is running in the background."""
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the condition was not reached in time")


def test_the_gate_and_the_monitor_are_built_with_the_registered_checks(
    env_factory: Callable[..., Env],
) -> None:
    """The rule-table test above checks the registry; this one checks that the runtime hands that
    registry to the gate and to the monitor (a runtime that dropped the last rule would pass the
    registry test)."""
    env = env_factory()

    assert [type(c) for c in env.runtime.gate._checks] == [type(c) for c in default_checks()]
    assert [type(c) for c in env.runtime.monitor._checks] == [type(c) for c in continuous_checks()]


def test_every_source_the_risk_engine_stamps_on_its_own_orders_is_reserved() -> None:
    """A client may not claim a source the engine acts on: the kill switch adopts its own exits by
    source and the protective stop is exempt from the holdings check by source. A new engine source
    added to one list and not the other would be claimable by a client."""
    assert OWN_SOURCES | {PROTECTIVE_STOP_SOURCE} == RESERVED_SOURCES


async def test_a_limits_edit_is_refused_while_the_day_is_not_loaded(
    env_factory: Callable[..., Env],
) -> None:
    """The tighten-only floor replay needs today's real peak and floor. Before the day has loaded
    (the command consumer starts at the same moment as the risk runtime) a market-hours edit is
    refused, not judged against a made-up peak of zero; and the refusal is not remembered."""
    env = env_factory()
    await env.set_limits()  # the first set needs no figures

    early = await env.send_limits(edit(per_trade_max_risk="500"), key="tighter")
    await env.runtime.start(env.stop)
    later = await env.send_limits(edit(per_trade_max_risk="500"), key="tighter")

    assert not early.accepted and (early.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    assert later.accepted  # the same key goes through once the day is there


async def test_a_limits_edit_is_judged_against_the_days_real_peak_and_floor(
    env_factory: Callable[..., Env],
) -> None:
    """Spec "A limits edit may not lower the floor": the lock term is removed after it has raised
    the floor. Judged against a peak of zero (a zero stub in place of the day book's figures) the
    edit would pass."""
    env = await env_factory().ready()  # BASE: a lock at 1000 puts the floor at 200 above a peak
    env.runtime.daybook.ratchet(D(1500), D(200))  # of 1500, which the day has reached

    result = await env.send_limits(
        edit(lock_mode="OFF", lock_trigger=None, lock_at=None), key="drop-lock"
    )

    assert not result.accepted and (result.reason or "").startswith(TIGHTEN_ONLY)
    assert "lower today's floor" in (result.reason or "")


async def test_a_floor_breach_through_the_assembly_locks_the_day_and_sends_the_exit(
    env_factory: Callable[..., Env],
) -> None:
    """Spec "Floor breach kills without any client", through the real assembly: the checks the
    runtime gives the monitor find the breach, its hook reaches the kill switch, the day locks,
    entries are refused and the exit goes out."""
    env = env_factory()
    held = position(OPTION, 65, "200.00", realized_pnl=D("0"))
    env.service.positions[position_id(held)] = held
    env.trading_repo.positions[(held.account_id, str(held.key), held.product.value)] = held
    await env.ready()  # the price is 200: nothing lost yet
    env.clock.at += timedelta(seconds=1)
    env.tick("100")  # 65 x (100 - 200) = -6,500 against a floor of -3,000

    await env.runtime.monitor.step()

    assert env.runtime.daybook.day().halt_level is HaltLevel.KILL
    refused = await env.entry()
    assert not refused.accepted and (refused.reason or "").startswith("RISK_HALTED_DAY")
    await until(
        lambda: any(
            o.source == "risk-kill" and o.side is OrderSide.SELL for o in env.adapter.placed
        )
    )
    await env.abandon_flatten()


async def test_the_start_seeds_the_day_book_with_the_whole_fill_history(
    env_factory: Callable[..., Env],
) -> None:
    """Bought yesterday with 10 of charges and carried; today sold for +13.00 gross with 10 more
    of charges: the whole trade lost 7.00, so it is a loss. Judged on today's part alone it would
    be a win (+3.00) and reset the streak, so the history must be seeded whole, old fills too."""
    env = env_factory()
    carried = position(OPTION, 65, "100.00", realized_pnl=D("0"))
    bought = fill(BUY, 65, "100", costs="10", fill_id="in").model_copy(
        update={"filled_at": NOW - timedelta(days=1)}
    )
    env.trading_repo.positions[(carried.account_id, str(carried.key), carried.product.value)] = (
        carried
    )
    env.trading_repo.fills.append(bought)
    await env.ready()

    sold = fill(OrderSide.SELL, 65, "100.20", costs="10", fill_id="out")
    await env.runtime.daybook.on_fill(sold, position(OPTION, 0, "100.00", realized_pnl=D("13")))

    assert env.runtime.daybook.day().loss_streak == 1


class _KillRule:
    """A pre-trade rule that always says KILL. No real pre-trade rule does today (the floor is a
    continuous check), so this stands in for one to see what the assembly does with the verdict."""

    name = "test_kill_rule"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        return kill("RISK_TEST_KILL", "a rule that stops the day")


async def test_a_kill_from_a_pre_trade_rule_locks_the_day_through_the_assembly(
    env_factory: Callable[..., Env],
) -> None:
    """The runtime gives the gate ``on_kill=self._request_kill``: without it a KILL from a rule only
    refuses the one order and the day stays open (the gate's own test uses a fake hook, so it
    cannot see the runtime forget to pass the real one)."""
    env = await env_factory().ready()
    gate = env.runtime.gate
    gate._checks = (*gate._checks, _KillRule())

    refused = await env.entry()

    assert not refused.accepted and (refused.reason or "").startswith("RISK_TEST_KILL")
    await until(lambda: env.runtime.daybook.day().halt_level is HaltLevel.KILL, seconds=1.0)
    assert "RISK_TEST_KILL" in (env.runtime.daybook.day().halt_reason or "")
    await env.abandon_flatten()


async def test_the_risk_block_carries_every_figure_the_engine_holds(
    env_factory: Callable[..., Env],
) -> None:
    """Every field of the snapshot's risk block, each with a value of its own, so that a field that
    is dropped (it would show its default) or two that are swapped (the floor and the peak) fail."""
    from engine.risk.state import DayState

    repo = MemoryRiskRepository()
    cooldown = NOW + timedelta(minutes=15)
    stored = DayState(
        "main", TODAY, NOW, peak=D("120"), floor=D("-2000"), entries_today=3, loss_streak=2,
        cooldown_until=cooldown,
    )  # fmt: skip
    await repo.save_day(stored)
    env = env_factory(risk_repo=repo)
    held = position(OPTION, 65, "200.00", realized_pnl=D("0"))
    env.service.positions[position_id(held)] = held
    env.trading_repo.positions[(held.account_id, str(held.key), held.product.value)] = held
    await env.ready()  # the price is 200: the held position has lost only its exit charges
    await env.runtime.monitor.step()  # a pass that sees that price

    risk = env.runtime.status()

    assert risk is not None
    assert (risk.peak, risk.floor) == (D("120"), D("-2000"))
    assert (risk.entries_today, risk.loss_streak, risk.cooldown_until) == (3, 2, cooldown)
    assert risk.halt_level is HaltLevel.NONE and risk.halt_reason is None
    assert risk.mtm_day is not None and risk.mtm_day == env.runtime.monitor.mtm_day()
    assert D("-2000") < risk.mtm_day < 0  # the engine's own figure, apart from the peak and floor


# ---- the supervised tasks do their work ------------------------------------------------------


class Feed:
    """A tick subscription the test feeds (the shape ``RiskMonitor.run`` reads)."""

    def __init__(self) -> None:
        self._ticks: asyncio.Queue[Tick] = asyncio.Queue()

    def __aiter__(self) -> "Feed":
        return self

    async def __anext__(self) -> Tick:
        return await self._ticks.get()

    def get_nowait(self) -> Tick | None:
        try:
            return self._ticks.get_nowait()
        except asyncio.QueueEmpty:
            return None


async def test_the_monitor_task_runs_the_passes_that_stamp_the_heartbeat(
    env_factory: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_monitor`` waits for ``start`` and then runs a pass every interval: with the clock moved
    on, the heartbeat is stale (entries are refused) until the running task has made a pass."""
    monkeypatch.setenv("RISK_MONITOR_INTERVAL_S", "0.05")
    env = await env_factory().ready()
    env.clock.at += timedelta(seconds=5)  # three intervals are tolerated: five seconds is not
    env.tick()
    assert env.runtime.monitor.unavailable(env.clock.at) is not None
    task = asyncio.create_task(env.runtime.run_monitor(env.stop, Feed()))  # type: ignore[arg-type]

    await until(lambda: env.runtime.monitor.unavailable(env.clock.at) is None)

    env.stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_the_stop_task_places_the_catastrophic_stop_of_an_open_position(
    env_factory: Callable[..., Env],
) -> None:
    env = await env_factory().ready()
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held
    task = asyncio.create_task(env.runtime.run_stops(env.stop))

    await until(lambda: any(o.order_type is OrderType.STOP_MARKET for o in env.adapter.placed))

    env.stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_the_square_off_task_exits_the_intraday_book_at_the_configured_time(
    env_factory: Callable[..., Env],
) -> None:
    at = datetime(2026, 11, 16, 9, 45, tzinfo=UTC)  # 15:15 IST, the square-off time in BASE
    env = await env_factory(at=at).ready()
    held = position(OPTION, 65, "200.00")
    env.service.positions[position_id(held)] = held
    task = asyncio.create_task(env.runtime.run_square_off(env.stop))

    await until(
        lambda: any(
            o.source == "risk-squareoff" and o.side is OrderSide.SELL for o in env.adapter.placed
        )
    )

    env.stop.set()
    await asyncio.wait_for(task, timeout=2)
    await env.abandon_flatten()
