"""The F&O ban-list service (add-risk-guards task 7.2; instrument-policy spec, every ban-list
scenario: loading ahead, past date, repeated key, tighten-only during market hours, an empty list,
the audit record, and the stored lists unreadable at start or during a command)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from contracts.commands import SetBanList
from engine.persistence.repository import PersistenceError
from engine.risk.banlist import BAN_LIST_INVALID, BanListService
from engine.risk.limits import STATE_UNAVAILABLE, TIGHTEN_ONLY
from engine.risk.repository import StoredBanList
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.trading_fakes import MemoryRepository

# Monday 2026-11-16 (IST = UTC + 5:30).
MID_DAY = datetime(2026, 11, 16, 5, 30, tzinfo=UTC)  # 11:00 IST, market hours
EVENING = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)  # 20:00 IST
BEFORE_OPEN = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 08:30 IST
TODAY = date(2026, 11, 16)
TOMORROW = date(2026, 11, 17)
YESTERDAY = date(2026, 11, 15)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class FlakyRepository(MemoryRiskRepository):
    """The in-memory store, with switches that make the ban-list calls fail like a database."""

    def __init__(self) -> None:
        super().__init__()
        self.read_down = False
        self.write_down = False
        self.fail_after_write = False  # the save commits, then the call still fails (a timeout)

    async def load_ban_list(self, account_id: str, from_date: date) -> list[StoredBanList]:
        if self.read_down:
            raise PersistenceError("database unavailable")
        return await super().load_ban_list(account_id, from_date)

    async def save_ban_list(
        self, account_id: str, trade_date: date, symbols: frozenset[str], at: datetime
    ) -> StoredBanList:
        if self.write_down:
            raise PersistenceError("database unavailable")
        stored = await super().save_ban_list(account_id, trade_date, symbols, at)
        if self.fail_after_write:
            raise PersistenceError("timeout after commit")
        return stored


class Env:
    def __init__(self, at: datetime = MID_DAY, repo: FlakyRepository | None = None) -> None:
        self.clock = Clock(at)
        self.repo = repo or FlakyRepository()
        self.results = MemoryRepository()
        self.service = BanListService(
            account_id="main", repository=self.repo, results=self.results, now=self.clock
        )
        self.n = 0

    def store(self, day: date, *symbols: str) -> None:
        self.repo.ban_lists[("main", day)] = StoredBanList(day, frozenset(symbols), self.clock.at)

    async def send(self, day: date, *symbols: str, key: str | None = None) -> tuple[bool, str]:
        self.n += 1
        result = await self.service.set_ban_list(
            SetBanList(
                command_id=f"c{self.n}", idempotency_key=key or f"k{self.n}", account_id="main",
                source="cli", sent_at=self.clock.at, trade_date=day, symbols=list(symbols),
            )
        )  # fmt: skip
        return result.accepted, result.reason or ""

    def stored(self, day: date) -> frozenset[str] | None:
        found = self.repo.ban_lists.get(("main", day))
        return None if found is None else found.symbols

    def audit(self) -> list[dict[str, object]]:
        return [
            {"decision": e.decision, "code": e.reason_code, **e.inputs}
            for e in self.repo.events
            if e.rule == "ban_list"
        ]


# ---- loading ---------------------------------------------------------------------------------


async def test_nothing_is_known_until_the_stored_lists_are_read() -> None:
    env = Env()
    env.store(TODAY, "XYZ")

    assert not env.service.read and env.service.for_now(env.clock.at) is None
    assert await env.service.load()
    assert env.service.read and env.service.for_now(env.clock.at) == {"XYZ"}


async def test_storage_down_at_start_leaves_the_lists_unread_until_a_retry_succeeds() -> None:
    """Spec "Storage down at start": no list (so F&O entries are refused) until the lists are
    read; the read is retried (by the monitor) and then succeeds."""
    env = Env()
    env.store(TODAY, "XYZ")
    env.repo.read_down = True

    assert not await env.service.load()
    assert not env.service.read and env.service.for_now(env.clock.at) is None

    env.repo.read_down = False
    assert await env.service.load()
    assert env.service.for_now(env.clock.at) == {"XYZ"}


async def test_only_today_and_later_lists_are_read() -> None:
    env = Env()
    env.store(YESTERDAY, "OLD")
    env.store(TODAY, "XYZ")
    env.store(TOMORROW, "ABC")

    await env.service.load()

    assert env.service.for_date(YESTERDAY) is None
    assert env.service.for_date(TODAY) == {"XYZ"}
    assert env.service.for_date(TOMORROW) == {"ABC"}


# ---- the command -----------------------------------------------------------------------------


async def test_loading_tomorrows_list_in_the_evening() -> None:
    """Spec "Loading tomorrow's list in the evening": stored, applies from that date, and
    survives a restart."""
    env = Env(at=EVENING)
    await env.service.load()

    accepted, _ = await env.send(TOMORROW, "xyz", "ABC")

    assert accepted
    assert env.stored(TOMORROW) == {"XYZ", "ABC"}  # upper-cased
    assert env.service.for_now(env.clock.at) is None  # not today's
    env.clock.at = EVENING + timedelta(hours=13)  # 09:00 IST the next day
    assert env.service.for_now(env.clock.at) == {"XYZ", "ABC"}
    restarted = BanListService(
        account_id="main", repository=env.repo, results=env.results, now=env.clock
    )
    assert await restarted.load() and restarted.for_now(env.clock.at) == {"XYZ", "ABC"}


async def test_a_past_date_is_refused_and_nothing_is_stored() -> None:
    """Spec "Past date"."""
    env = Env()

    accepted, reason = await env.send(YESTERDAY, "XYZ")

    assert not accepted and reason.startswith(BAN_LIST_INVALID)
    assert env.stored(YESTERDAY) is None


async def test_a_repeated_key_returns_the_first_result_and_changes_nothing() -> None:
    """Spec "Repeated command"."""
    env = Env(at=EVENING)

    first = await env.send(TOMORROW, "ABC", key="same")
    second = await env.send(TOMORROW, "XYZ", key="same")

    assert first == second == (True, "")
    assert env.stored(TOMORROW) == {"ABC"}
    assert len(env.audit()) == 1  # the repeat was not judged again


async def test_a_repeated_refused_key_is_refused_the_same_way() -> None:
    env = Env()

    first = await env.send(YESTERDAY, "ABC", key="same")
    second = await env.send(TODAY, "ABC", key="same")

    assert first == second and not first[0]
    assert env.stored(TODAY) is None


async def test_removing_a_symbol_mid_day_is_refused() -> None:
    """Spec "Removing a symbol mid-day"."""
    env = Env(at=MID_DAY)
    env.store(TODAY, "ABC", "XYZ")
    await env.service.load()

    accepted, reason = await env.send(TODAY, "ABC")

    assert not accepted and reason.startswith(TIGHTEN_ONLY) and "XYZ" in reason
    assert env.stored(TODAY) == {"ABC", "XYZ"}
    assert env.service.for_now(env.clock.at) == {"ABC", "XYZ"}


async def test_adding_a_symbol_mid_day_is_accepted() -> None:
    """Spec "Adding a symbol mid-day"."""
    env = Env(at=MID_DAY)
    env.store(TODAY, "ABC")
    await env.service.load()

    accepted, _ = await env.send(TODAY, "ABC", "XYZ")

    assert accepted
    assert env.stored(TODAY) == {"ABC", "XYZ"}
    assert env.service.for_now(env.clock.at) == {"ABC", "XYZ"}


async def test_the_first_list_of_the_day_mid_day_is_accepted() -> None:
    env = Env(at=MID_DAY)

    assert (await env.send(TODAY, "ABC"))[0]
    assert env.service.for_now(env.clock.at) == {"ABC"}


async def test_outside_market_hours_a_list_replaces_todays() -> None:
    env = Env(at=BEFORE_OPEN)
    env.store(TODAY, "ABC", "XYZ")

    accepted, _ = await env.send(TODAY, "ABC")

    assert accepted and env.stored(TODAY) == {"ABC"}


async def test_a_list_for_a_later_day_may_shrink_during_market_hours() -> None:
    env = Env(at=MID_DAY)
    env.store(TOMORROW, "ABC", "XYZ")

    assert (await env.send(TOMORROW, "ABC"))[0]
    assert env.stored(TOMORROW) == {"ABC"}


async def test_an_empty_list_is_accepted_as_no_bans() -> None:
    env = Env(at=BEFORE_OPEN)

    accepted, _ = await env.send(TODAY)

    assert accepted
    assert env.service.for_now(env.clock.at) == frozenset()  # loaded, and empty: not None


async def test_a_blank_symbol_is_refused() -> None:
    env = Env(at=BEFORE_OPEN)

    accepted, reason = await env.send(TODAY, "ABC", "  ")

    assert not accepted and reason.startswith(BAN_LIST_INVALID)
    assert env.stored(TODAY) is None


async def test_a_command_for_another_account_is_refused() -> None:
    env = Env(at=BEFORE_OPEN)
    result = await env.service.set_ban_list(
        SetBanList(
            command_id="c", idempotency_key="k", account_id="other", source="cli",
            sent_at=BEFORE_OPEN, trade_date=TODAY, symbols=["ABC"],
        )
    )  # fmt: skip

    assert not result.accepted and (result.reason or "").startswith(BAN_LIST_INVALID)
    assert env.stored(TODAY) is None
    [row] = env.audit()  # refused, and recorded like every other refused load
    assert (row["decision"], row["code"], row["outcome"]) == (
        "REJECT",
        BAN_LIST_INVALID,
        "rejected",
    )
    assert "'other'" in str(row["problem"]) and row["symbols"] == "ABC"


# ---- the audit record ------------------------------------------------------------------------


async def test_every_accepted_or_refused_load_appends_a_record() -> None:
    env = Env(at=MID_DAY)
    env.store(TODAY, "ABC")

    await env.send(TODAY, "ABC", "XYZ")  # accepted
    await env.send(TODAY, "XYZ")  # refused: drops ABC
    await env.send(YESTERDAY, "ABC")  # refused: past date

    rows = env.audit()
    assert [(r["decision"], r["code"], r["outcome"]) for r in rows] == [
        ("ALLOW", None, "accepted"),
        ("REJECT", TIGHTEN_ONLY, "rejected"),
        ("REJECT", BAN_LIST_INVALID, "rejected"),
    ]
    assert rows[0]["trade_date"] == "2026-11-16" and rows[0]["symbols"] == "ABC,XYZ"
    assert rows[1]["symbols"] == "XYZ" and rows[2]["trade_date"] == "2026-11-15"


# ---- storage failures during a command -------------------------------------------------------


async def test_a_command_with_the_store_unreadable_is_refused_and_not_remembered() -> None:
    env = Env(at=MID_DAY)
    env.repo.read_down = True

    accepted, reason = await env.send(TODAY, "ABC", key="k")
    assert not accepted and reason.startswith(STATE_UNAVAILABLE)

    env.repo.read_down = False
    assert (await env.send(TODAY, "ABC", key="k"))[0]  # the same key succeeds once it is back


async def test_a_failed_save_is_refused_and_the_list_unchanged() -> None:
    env = Env(at=MID_DAY)
    env.store(TODAY, "ABC")
    await env.service.load()
    env.repo.write_down = True

    accepted, reason = await env.send(TODAY, "ABC", "XYZ")

    assert not accepted and reason.startswith(STATE_UNAVAILABLE)
    assert env.service.for_now(env.clock.at) == {"ABC"}


async def test_a_save_that_committed_before_failing_is_read_back_and_accepted() -> None:
    env = Env(at=MID_DAY)
    env.repo.fail_after_write = True

    accepted, _ = await env.send(TODAY, "ABC")

    assert accepted and env.service.for_now(env.clock.at) == {"ABC"}


async def test_a_failed_save_that_cannot_be_read_back_forgets_the_lists() -> None:
    """Fail closed: the store may now hold more bans than memory, so memory is dropped (F&O
    entries refused) until a read succeeds."""

    class DownAfterWrite(FlakyRepository):
        async def save_ban_list(
            self, account_id: str, trade_date: date, symbols: frozenset[str], at: datetime
        ) -> StoredBanList:
            self.read_down = True
            raise PersistenceError("connection lost")

    env = Env(at=MID_DAY, repo=DownAfterWrite())
    env.store(TODAY, "ABC")
    await env.service.load()

    accepted, _ = await env.send(TODAY, "ABC", "XYZ")

    assert not accepted
    assert not env.service.read and env.service.for_now(env.clock.at) is None


@pytest.mark.parametrize("at", [MID_DAY, BEFORE_OPEN])
async def test_an_audit_write_that_fails_does_not_change_the_outcome(at: datetime) -> None:
    class NoAudit(FlakyRepository):
        async def append_event(self, event: object) -> None:
            raise PersistenceError("audit down")

    env = Env(at=at, repo=NoAudit())

    assert (await env.send(TODAY, "ABC"))[0]
    assert env.stored(TODAY) == {"ABC"}
