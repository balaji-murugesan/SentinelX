"""In-memory ``RiskRepository`` for tests, with the same behaviour as PostgresRiskRepository.

It uses the pure ``merge_day_state``; Postgres does the same merge in SQL. The shared contract
suite (test_risk_repository.py) runs against both, so this fake cannot quietly drift from the
real database's behaviour.
"""

from datetime import date, datetime

from contracts.commands import LimitValue
from engine.risk.repository import RiskEvent, StoredLimits
from engine.risk.state import DayState, merge_day_state


class MemoryRiskRepository:
    def __init__(self) -> None:
        self.limits: dict[str, StoredLimits] = {}
        self.days: dict[tuple[str, date], DayState] = {}
        self.events: list[RiskEvent] = []

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
