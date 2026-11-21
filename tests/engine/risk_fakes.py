"""In-memory ``RiskRepository`` for tests, with the same behaviour as PostgresRiskRepository.

It uses the pure ``merge_day_state``; Postgres does the same merge in SQL. The shared contract
suite (test_risk_repository.py) runs against both, so this fake cannot quietly drift from the
real database's behaviour.
"""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from contracts.commands import LimitValue
from contracts.market import InstrumentKey
from contracts.orders import Order, Position
from engine.positions.book import PositionId
from engine.risk.decision import Decision, Quote
from engine.risk.repository import RiskEvent, StoredBanList, StoredLimits
from engine.risk.state import DayState, merge_day_state


class MemoryRiskRepository:
    def __init__(self) -> None:
        self.limits: dict[str, StoredLimits] = {}
        self.days: dict[tuple[str, date], DayState] = {}
        self.events: list[RiskEvent] = []
        self.ban_lists: dict[tuple[str, date], StoredBanList] = {}

    async def load_limits(self, account_id: str) -> StoredLimits | None:
        return self.limits.get(account_id)

    async def save_limits(
        self, account_id: str, limits: dict[str, LimitValue], at: datetime
    ) -> int:
        stored = self.limits.get(account_id)
        version = 1 if stored is None else stored.version + 1
        self.limits[account_id] = StoredLimits(dict(limits), version, at)
        return version

    async def load_day(self, account_id: str, trade_date: date) -> DayState | None:
        return self.days.get((account_id, trade_date))

    async def save_day(self, state: DayState) -> DayState:
        key = (state.account_id, state.trade_date)
        merged = merge_day_state(self.days.get(key), state)
        self.days[key] = merged
        return merged

    async def append_event(self, event: RiskEvent) -> None:
        self.events.append(event)

    async def load_ban_list(self, account_id: str, from_date: date) -> list[StoredBanList]:
        return sorted(
            (
                stored
                for (account, trade_date), stored in self.ban_lists.items()
                if account == account_id and trade_date >= from_date
            ),
            key=lambda stored: stored.trade_date,
        )

    async def save_ban_list(
        self, account_id: str, trade_date: date, symbols: frozenset[str], at: datetime
    ) -> StoredBanList:
        stored = StoredBanList(trade_date, frozenset(symbols), at)
        self.ban_lists[(account_id, trade_date)] = stored
        return stored


class StubState:
    """A ``RiskState`` whose answers a test sets directly (the real one is the day book and
    monitor of groups 6 and 7)."""

    def __init__(
        self,
        day: DayState,
        *,
        mtm_day: Decimal | None = Decimal(0),
        unavailable: str | None = None,
        entry_halt: Decision | None = None,
        quotes: Mapping[InstrumentKey, Quote] | None = None,
    ) -> None:
        self._day = day
        self._mtm_day = mtm_day
        self.unavailable_reason = unavailable
        self.halt = entry_halt
        self.quotes = dict(quotes or {})
        self.entries_counted = 0  # how many times the gate asked to count an allowed entry
        self.counted: list[tuple[int, bool]] = []  # the (lots, expiring) of each counted entry
        self.count_fails: str | None = None  # a reason: make the count fail like a database error
        self.recent: tuple[datetime, ...] = ()  # what ``recent_entries`` returns
        self.rejections: tuple[datetime, ...] = ()  # what ``broker_rejections`` returns
        # What ``ban_list`` returns. A stub starts with today's list loaded and empty (no bans),
        # so a test about another rule is not refused for a missing list; set None to test that.
        self.bans: frozenset[str] | None = frozenset()

    def unavailable(self, now: datetime) -> str | None:
        return self.unavailable_reason

    def day(self) -> DayState:
        return self._day

    def mtm_day(self) -> Decimal | None:
        return self._mtm_day

    def quote(self, key: InstrumentKey) -> Quote | None:
        return self.quotes.get(key)

    def entry_halt(self) -> Decision | None:
        return self.halt

    def recent_entries(self, now: datetime) -> tuple[datetime, ...]:
        return self.recent

    def broker_rejections(self, now: datetime) -> tuple[datetime, ...]:
        return self.rejections

    def ban_list(self, now: datetime) -> frozenset[str] | None:
        return self.bans

    async def entry_allowed(self, lots: int, expiring: bool) -> str | None:
        if self.count_fails is not None:
            return self.count_fails
        self.entries_counted += 1
        self.counted.append((lots, expiring))
        return None


class StubTrading:
    """A ``TradingView``: the two maps the gate reads, set by the test."""

    def __init__(
        self,
        positions: Mapping[PositionId, Position] | None = None,
        orders: Mapping[str, Order] | None = None,
    ) -> None:
        self.positions: Mapping[PositionId, Position] = positions or {}
        self.orders: Mapping[str, Order] = orders or {}
