"""Where the risk engine's state is stored (add-risk-engine design §6, §9, §15).

``RiskRepository`` is a ``Protocol``, like ``TradingRepository``: a structural interface (a C#
``interface`` that a class satisfies just by having the methods, without declaring ``: IRisk...``).
``PostgresRiskRepository`` (``engine.persistence``) is the real one and tests use an in-memory
fake with the same behaviour. Every failure surfaces as ``PersistenceError`` (reused from the
trading repository), so callers have one thing to catch to fail closed.

Three kinds of data live here:
  * limits   - the owner's risk limits, one document per account, versioned;
  * day state - peak, floor, halt and counters per account and day (see ``engine.risk.state``);
  * events   - the append-only audit of risk decisions. The database role used by the engine
               can add events but never update or delete one.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from contracts.commands import LimitValue
from engine.risk.state import DayState, require_aware

# What an audit record's inputs may hold: flat, JSON-safe values. Money and percentages go in as
# text ("1300.00"), exactly as they do in ``SetRiskLimits``, so no float ever reaches an audit row.
AuditValue = str | int | bool | None


@dataclass(frozen=True)
class StoredLimits:
    limits: dict[str, LimitValue]
    version: int  # 1 on the first save, then +1 on every save
    updated_at: datetime

    def __post_init__(self) -> None:
        require_aware("updated_at", self.updated_at)


@dataclass(frozen=True)
class RiskEvent:
    """One audited risk decision (risk-gate spec "Every risk decision is audited")."""

    at: datetime
    account_id: str
    rule: str  # the rule's stable name, e.g. "per_trade_risk"; "gate" for a verdict
    phase: str  # "pre_trade", "continuous", "verdict" or "config"
    decision: str  # ALLOW, REJECT, HALT_NEW_ENTRIES, TARGET_HALT or KILL
    enforced: bool  # False for a shadow rule: logged, not acted on
    latency_us: int  # how long the check took, in microseconds
    reason_code: str | None = None  # stable code such as RISK_OPEN_POSITIONS; None for ALLOW
    # ``field(default_factory=dict)`` gives each instance its own empty dict: a mutable default
    # must never be shared (C# would write ``= new()`` in the initialiser).
    inputs: dict[str, AuditValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware("at", self.at)
        # mypy checks AuditValue only at build time. A Decimal would fail the audit write at JSON
        # serialisation (the record would be lost) and a float would be stored silently, so refuse
        # both here, where the rule that built the event can still be found. ``isinstance`` (not
        # an exact type match) so a ``StrEnum`` such as ``OrderSide`` or an ``IntEnum`` such as
        # ``HaltLevel`` is accepted: JSON stores those as their plain text or number.
        for key, value in self.inputs.items():
            if not isinstance(value, (str, int, bool, type(None))):
                raise TypeError(
                    f"audit input {key!r} must be text, a whole number, a flag or None, "
                    f"not {type(value).__name__}: money goes in as text"
                )


class RiskRepository(Protocol):
    async def load_limits(self, account_id: str) -> StoredLimits | None:
        """The stored limits, or None while the owner has not set any."""
        ...

    async def save_limits(
        self, account_id: str, limits: dict[str, LimitValue], at: datetime
    ) -> int:
        """Replace the account's limits and return the new version (1 on the first save)."""
        ...

    async def load_day(self, account_id: str, trade_date: date) -> DayState | None:
        """The day's stored state, or None: nothing has been stored for that date yet."""
        ...

    async def save_day(self, state: DayState) -> DayState:
        """Merge ``state`` into the stored day and return what is stored afterwards.

        The merge is ``engine.risk.state.merge_day_state``: peak, floor, halt level, trade count
        and cooldown end can only rise, so a delayed or repeated write is harmless. It is one
        atomic statement, so two writes cannot interleave.

        A ``PersistenceError`` does not always mean "not written": if the row is stored but the
        stored row cannot be decoded afterwards (for example a halt level this version does not
        know), the write has already taken effect. That is safe, because the writes are
        monotone and idempotent, so a retry changes nothing.
        """
        ...

    async def append_event(self, event: RiskEvent) -> None:
        """Add one audit record. There is deliberately no way to change or remove one."""
        ...
