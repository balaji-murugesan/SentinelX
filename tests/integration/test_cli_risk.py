"""The risk CLI commands (add-risk-engine task 11.2): ``kill``, ``risk set-limits``, ``risk size``,
``risk status`` and ``order place --stop``.

The file-reading, formatting and argument tests need nothing. The round trips need a REAL Redis
(``TEST_REDIS_URL``, database 15) with the engine's command consumer running against real
``LimitsService``, ``SizingService`` and ``KillSwitch`` objects, so the whole path from the command
line to the answer is covered.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis

from cli import orders as cli
from cli.__main__ import main
from contracts.commands import COMMAND_STREAM, CommandResult, parse_command
from contracts.orders import OrderSide, OrderType, ProductType
from contracts.snapshot import HaltLevel, PositionRef, RiskStatus, Snapshot, snapshot_key
from engine.orders.commands import CommandConsumer
from engine.risk.daybook import DayBook
from engine.risk.kill import KillSwitch
from engine.risk.limits import MODE_PREFIX, REGISTRY, parse_limits
from engine.risk.limits_service import LimitsService
from engine.risk.sizing import SizingService
from tests.engine.risk_builders import NOW, OPTION, instrument_of, order
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build
from tests.engine.trading_fakes import MemoryRepository

D = Decimal
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
EXAMPLE = Path(__file__).parents[2] / "docs" / "risk-limits.example.json"

integration = pytest.mark.integration
requires_redis = pytest.mark.skipif(
    not TEST_REDIS_URL and os.environ.get("CI") != "true",
    reason="TEST_REDIS_URL not set: no real Redis to test",
)


# ---- reading a limits file -------------------------------------------------------------------


def test_the_example_limits_file_is_valid_and_its_comments_are_dropped() -> None:
    limits = cli.read_limits_file(EXAMPLE)

    assert not any(key.startswith("_") for key in limits)
    assert parse_limits(limits).daily_max_loss == D(3000)  # the engine accepts every key and value


def test_the_example_limits_file_shows_every_limit_and_says_it_is_not_a_recommendation() -> None:
    """add-risk-guards task 3.4: every limit is in the file (an optional one may be null), so the
    owner sees each new key, and the file still says its values are only examples."""
    limits = cli.read_limits_file(EXAMPLE)
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    assert set(limits) == {key for key in REGISTRY if not key.startswith(MODE_PREFIX)}
    assert "NOT A RECOMMENDATION" in raw["_note"]
    parsed = parse_limits(limits)
    assert parsed.allowed_underlyings == frozenset({"NIFTY", "BANKNIFTY"})
    assert parsed.max_gross_lots_per_day == 10


def test_a_fractional_number_in_a_limits_file_is_refused_with_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    path.write_text('{"daily_max_loss": 3000.5}', encoding="utf-8")

    with pytest.raises(ValueError, match=r'write it as a string, e\.g\. "3000\.5"'):
        cli.read_limits_file(path)


def test_whole_numbers_and_strings_pass_through_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    path.write_text('{"_c": "note", "max_trades_per_day": 5, "daily_max_loss": "3000"}')

    assert cli.read_limits_file(path) == {"max_trades_per_day": 5, "daily_max_loss": "3000"}


@pytest.mark.parametrize(
    ("content", "message"),
    [("{not json", "not valid JSON"), ("[1, 2]", "one JSON object"), ("3", "one JSON object")],
)
def test_a_limits_file_that_is_not_an_object_is_refused(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "limits.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        cli.read_limits_file(path)


def test_a_missing_limits_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        cli.read_limits_file(tmp_path / "nope.json")


class NoRedis:
    """Any use of it fails the test: these commands must refuse before sending."""

    async def xadd(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may be sent")


async def test_a_bad_limits_file_is_never_sent(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    path.write_text('{"daily_max_loss": 1.5}', encoding="utf-8")
    lines: list[str] = []

    code = await cli.set_limits(
        NoRedis(),  # type: ignore[arg-type]
        account_id="main", path=path, out=lines.append,
    )  # fmt: skip

    assert code == cli.REJECTED and lines[0].startswith("Not sent: ")


async def test_a_value_the_command_cannot_carry_is_never_sent(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    # A nested object: no LimitValue member carries one. (A list of text became a valid value
    # in add-risk-guards, for symbol sets; the engine still refuses it for a money limit.)
    path.write_text('{"daily_max_loss": {"amount": "3000"}}', encoding="utf-8")
    lines: list[str] = []

    code = await cli.set_limits(
        NoRedis(),  # type: ignore[arg-type]
        account_id="main", path=path, out=lines.append,
    )  # fmt: skip

    assert code == cli.REJECTED and lines[0].startswith("Not sent: ")


@pytest.mark.parametrize(
    ("entry", "stop", "field"),
    [("0", "90", "entry_price"), ("-5", "90", "entry_price"),
     ("100", "0", "initial_stop"), ("100", "-1", "initial_stop")],
)  # fmt: skip
async def test_size_with_a_price_of_zero_or_below_is_refused_in_one_line_and_never_sent(
    entry: str, stop: str, field: str
) -> None:
    lines: list[str] = []

    code = await cli.size_order(
        NoRedis(),  # type: ignore[arg-type]
        account_id="main", key=OPTION, side=OrderSide.BUY, entry_price=D(entry),
        initial_stop=D(stop), out=lines.append,
    )  # fmt: skip

    assert code == cli.REJECTED
    assert len(lines) == 1 and "\n" not in lines[0]  # one line, not a traceback
    assert lines[0].startswith(f"Not sent: {field}: ")


@pytest.mark.parametrize(
    ("limit", "stop", "field"),
    [("0", None, "limit_price"), ("-1", None, "limit_price"),
     ("100", "0", "initial_stop"), ("100", "-1", "initial_stop")],
)  # fmt: skip
async def test_place_with_a_price_of_zero_or_below_is_refused_in_one_line_and_never_sent(
    limit: str, stop: str | None, field: str
) -> None:
    lines: list[str] = []

    code = await cli.place_order(
        NoRedis(),  # type: ignore[arg-type]
        account_id="main", key=OPTION, side=OrderSide.BUY, quantity=65,
        product=ProductType.INTRADAY, limit_price=D(limit),
        initial_stop=None if stop is None else D(stop), out=lines.append,
    )  # fmt: skip

    assert code == cli.REJECTED
    assert len(lines) == 1 and "\n" not in lines[0]
    assert lines[0].startswith(f"Not sent: {field}: ")


class SilentRedis:
    """A Redis that stores the command and never gets a reply: the engine is not answering."""

    def __init__(self) -> None:
        self.sent = 0

    async def xadd(self, *args: object, **kwargs: object) -> None:
        self.sent += 1

    async def xread(self, *args: object, **kwargs: object) -> list[object]:
        return []


async def test_a_kill_nobody_answered_points_to_risk_status_and_says_it_may_be_sent_again() -> None:
    """A kill is idempotent per account and day, so in an emergency the owner is told to look and
    to send it again, not (like every other command) to leave it alone."""
    redis: Any = SilentRedis()  # (typed Any: the CLI functions want a real ``Redis``)
    lines: list[str] = []

    code = await cli.kill(redis, account_id="main", out=lines.append, wait_s=0.01)

    assert code == cli.UNKNOWN and redis.sent == 1  # this call itself sends once
    assert "risk status" in lines[0] and "sent again" in lines[0]
    assert "NOT resent" not in lines[0]


async def test_every_other_command_nobody_answered_still_says_it_was_not_resent() -> None:
    redis: Any = SilentRedis()  # (typed Any: the CLI functions want a real ``Redis``)
    lines: list[str] = []

    placed = await cli.place_order(
        redis, account_id="main", key=OPTION, side=OrderSide.BUY, quantity=65,
        product=ProductType.INTRADAY, limit_price=D("100"), out=lines.append, wait_s=0.01,
    )  # fmt: skip
    cancelled = await cli.cancel_order(
        redis, account_id="main", order_id="o1", out=lines.append, wait_s=0.01
    )

    assert placed == cancelled == cli.UNKNOWN
    assert len(lines) == 2 and all("NOT resent" in line for line in lines)
    assert not any("sent again" in line for line in lines)


# ---- the command line ------------------------------------------------------------------------


def test_kill_without_yes_sends_nothing_and_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["kill"]) == 1

    err = capsys.readouterr().err
    assert "--yes" in err and "no way to undo" in err


def test_help_lists_the_risk_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    top = capsys.readouterr().out
    assert "kill" in top and "risk" in top
    with pytest.raises(SystemExit):
        main(["risk", "--help"])
    text = capsys.readouterr().out
    assert "set-limits" in text and "size" in text and "status" in text
    with pytest.raises(SystemExit):
        main(["order", "place", "--help"])
    assert "--stop" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["kill", "--help"])
    assert "--yes" in capsys.readouterr().out


def test_set_limits_requires_a_file() -> None:
    with pytest.raises(SystemExit):
        main(["risk", "set-limits"])


# ---- formatting ------------------------------------------------------------------------------


def test_the_risk_figures_are_shown_with_unknowns_spelled_out() -> None:
    lines = cli.format_risk(RiskStatus(mtm_day=None, floor=None))

    assert "day P&L (net of charges) : unknown" in lines
    assert "equity floor             : unknown" in lines
    assert "halt                     : NONE" in lines


def test_an_order_line_shows_its_type_trigger_and_source() -> None:
    """A protective stop must not look like an ordinary sell: its type, trigger and source show,
    and the leading columns (id, state, side, quantity x instrument) are where they always were."""
    stop = order(
        OrderSide.SELL, 65, order_type=OrderType.STOP_MARKET, trigger_price=D("190.00"),
        source="risk-stop", order_id="o-stop",
    )  # fmt: skip
    entry = order(
        OrderSide.BUY, 65, order_type=OrderType.LIMIT, limit_price=D("200.00"), source="cli",
        order_id="o-entry",
    )  # fmt: skip
    market = order(OrderSide.SELL, 65, source="risk-kill", order_id="o-exit")

    assert cli.format_order(stop) == (
        "o-stop  Requested       SELL 65 x NSE_FNO:53720 STOP_MARKET trigger 190.00"
        "  INTRADAY  filled 0  source risk-stop"
    )
    assert cli.format_order(entry) == (
        "o-entry  Requested       BUY  65 x NSE_FNO:53720 LIMIT @ 200.00"
        "  INTRADAY  filled 0  source cli"
    )
    assert cli.format_order(market) == (
        "o-exit  Requested       SELL 65 x NSE_FNO:53720 MARKET"
        "  INTRADAY  filled 0  source risk-kill"
    )


def test_a_halt_and_an_unprotected_position_are_called_out() -> None:
    risk = RiskStatus(
        mtm_day=D("-3012.5"), floor=D("-3000"), peak=D("150"), halt_level=HaltLevel.KILL,
        halt_reason="risk: MTM_day -3012.50 is at or below the floor -3000.00", entries_today=3,
        loss_streak=2, unprotected=[PositionRef(key=OPTION, product="INTRADAY")],  # type: ignore[arg-type]
    )  # fmt: skip

    text = "\n".join(cli.format_risk(risk))

    assert "-3012.50" in text and "KILL (risk: MTM_day" in text
    assert "!! no catastrophic stop  : NSE_FNO:53720 INTRADAY" in text


def test_an_accepted_answer_shows_a_size_or_a_note() -> None:
    def answer(**fields: object) -> CommandResult:
        return CommandResult(
            command_id="c", idempotency_key="k", account_id="main", accepted=True,
            decided_at=NOW, **fields,  # type: ignore[arg-type]
        )  # fmt: skip

    assert cli._accepted_line(answer(sized_quantity=65, risk_amount=D("650"))) == (
        "Accepted. size: 65 units, risk 650"
    )
    assert cli._accepted_line(answer()) == "Accepted."
    assert cli._accepted_line(answer(order_id="o1")) == "Accepted. order id: o1"
    assert cli._accepted_line(answer(reason="already stopped today: kill")) == (
        "Accepted. (already stopped today: kill)"
    )


# ---- round trips against a real Redis --------------------------------------------------------


class NoFlatten:
    """Stands in for the flattener: the kill's own tests cover the exits."""

    def __init__(self) -> None:
        self.calls = 0

    async def flatten(self, reason: str, products: object = None, kind: str = "kill") -> None:
        self.calls += 1


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


class Engine:
    """The engine's command side: a real consumer wired to real risk objects."""

    def __init__(self, redis: Redis) -> None:
        service, _, self.repo = build()
        self.risk_repo = MemoryRiskRepository()
        self.limits = LimitsService(
            account_id="main", repository=self.risk_repo, results=MemoryRepository(),
            figures=lambda: (D(0), None), now=lambda: NOW,
        )  # fmt: skip
        self.daybook = DayBook(
            account_id="main", repository=self.risk_repo, limits=lambda: self.limits.current,
            now=lambda: NOW,
        )  # fmt: skip
        self.flattener = NoFlatten()
        self.kill = KillSwitch(
            account_id="main", daybook=self.daybook, service=service, flattener=self.flattener,
            results=self.repo, now=lambda: NOW,
        )  # fmt: skip
        sizing = SizingService(
            account_id="main", limits=self.limits, instruments=instrument_of, now=lambda: NOW
        )
        self.consumer = CommandConsumer(
            redis, service, account_id="main", set_limits=self.limits.set_limits,
            size=sizing.size, kill=self.kill.handle,
        )  # fmt: skip
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Engine":
        assert await self.daybook.load()
        self.task = asyncio.create_task(self.consumer.run(self.stop, block_ms=50))
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.stop.set()
        assert self.task is not None
        await asyncio.gather(self.task, return_exceptions=True)


@integration
@requires_redis
async def test_set_limits_then_size_then_kill_round_trip(redis: Redis, tmp_path: Path) -> None:
    async with Engine(redis) as engine:
        lines: list[str] = []

        sent = await cli.set_limits(redis, account_id="main", path=EXAMPLE, out=lines.append)
        assert sent == cli.ACCEPTED and lines == ["Accepted."]
        assert engine.limits.current is not None
        assert engine.limits.current.daily_max_loss == D(3000)

        # Loosening a limit while the market is open (NOW is 10:30 IST) is refused, by name.
        looser = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        looser["daily_max_loss"] = "5000"
        path = tmp_path / "looser.json"
        path.write_text(json.dumps(looser), encoding="utf-8")
        lines.clear()
        refused = await cli.set_limits(redis, account_id="main", path=path, out=lines.append)
        assert refused == cli.REJECTED and "TIGHTEN_ONLY" in lines[0]
        assert "daily_max_loss" in lines[0]
        assert engine.limits.current.daily_max_loss == D(3000)  # nothing was half-applied

        lines.clear()
        sized = await cli.size_order(
            redis, account_id="main", key=OPTION, side=OrderSide.BUY, entry_price=D("100"),
            initial_stop=D("90"), out=lines.append,
        )  # fmt: skip
        assert sized == cli.ACCEPTED and lines[0].startswith("Accepted. size: 65 units, risk ")

        lines.clear()
        assert await cli.kill(redis, account_id="main", out=lines.append) == cli.ACCEPTED
        assert lines == ["Accepted."]
        assert engine.daybook.day().halt_level is HaltLevel.KILL

        lines.clear()  # a second kill: accepted, and it says nothing new was started
        assert await cli.kill(redis, account_id="main", out=lines.append) == cli.ACCEPTED
        assert lines[0].startswith("Accepted. (already stopped today: ")


@integration
@requires_redis
async def test_size_with_no_limits_set_is_rejected_with_the_reason(redis: Redis) -> None:
    async with Engine(redis):
        lines: list[str] = []

        code = await cli.size_order(
            redis, account_id="main", key=OPTION, side=OrderSide.BUY, entry_price=D("100"),
            initial_stop=D("90"), out=lines.append,
        )  # fmt: skip

    assert code == cli.REJECTED and lines[0].startswith("Rejected: ")


@integration
@requires_redis
async def test_kill_with_no_engine_answering_says_the_result_is_unknown(redis: Redis) -> None:
    lines: list[str] = []

    code = await cli.kill(redis, account_id="main", out=lines.append, wait_s=0.3)

    assert code == cli.UNKNOWN and "risk status" in lines[0] and "sent again" in lines[0]
    assert "NOT resent" not in lines[0]  # a kill is safe to repeat: this is the one exception
    assert await redis.xlen(COMMAND_STREAM) == 1  # but this call still sends it only once


@integration
@requires_redis
async def test_order_place_carries_the_initial_stop(redis: Redis) -> None:
    from contracts.commands import PlaceOrder
    from contracts.orders import ProductType

    await cli.place_order(
        redis, account_id="main", key=OPTION, side=OrderSide.BUY, quantity=65,
        product=ProductType.INTRADAY, limit_price=D("100"), out=lambda _: None, wait_s=0.2,
        initial_stop=D("90"),
    )  # fmt: skip

    [(_, fields)] = await redis.xrange(COMMAND_STREAM)
    command = parse_command(fields["data"])
    assert isinstance(command, PlaceOrder) and command.initial_stop == D("90")


@integration
@requires_redis
async def test_risk_status_reads_the_engines_snapshot(redis: Redis) -> None:
    lines: list[str] = []
    assert await cli.show_risk(redis, account_id="main", out=lines.append) == cli.UNKNOWN
    assert "No snapshot" in lines[0]

    snapshot = Snapshot(
        account_id="main", taken_at=datetime(2026, 11, 16, 5, 0, tzinfo=UTC), open_orders=[],
        positions=[], risk=None,
    )  # fmt: skip
    await redis.set(snapshot_key("main"), snapshot.model_dump_json())
    lines.clear()
    assert await cli.show_risk(redis, account_id="main", out=lines.append) == cli.ACCEPTED
    assert any("No risk figures yet" in line for line in lines)

    with_risk = snapshot.model_copy(
        update={"risk": RiskStatus(mtm_day=D("125.5"), floor=D("-3000"))}
    )
    await redis.set(snapshot_key("main"), with_risk.model_dump_json())
    lines.clear()
    assert await cli.show_risk(redis, account_id="main", out=lines.append) == cli.ACCEPTED
    assert "day P&L (net of charges) : 125.50" in lines
