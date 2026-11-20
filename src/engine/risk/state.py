"""The risk engine's persisted day state, and the rule for merging a write into it (design §6).

One ``DayState`` per account and IST trading day holds what the risk engine must remember
across a crash: the day's peak and equity floor, the halt level, and the counters. A new date
simply has no row, so everything starts fresh: that is the "post-close reset".

Some of it may only move one way (spec equity-floor "Floor and peak are persisted before the
engine acts"; D-026): a stale or out-of-order write must never lower the floor, the peak, a halt,
the day's trade count, the cooldown end, the day's entry lots or last entry time, or switch the
expiry flag off. ``merge_day_state`` is that rule as a pure function.
Postgres applies the same rule in SQL (``GREATEST`` and friends), and one contract suite runs
against both, so the two cannot drift. After a restart, "resume with ``max(stored, recomputed)``"
is exactly ``merge_day_state(stored, recomputed)``.

``@dataclass(frozen=True)`` is a Python record: like a C# ``record`` with init-only properties,
it gets equality and a constructor for free and cannot be changed after it is built.
``__post_init__`` runs right after the constructor (like a C# constructor body) and is where the
checks below refuse a value that must never be stored.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from contracts.snapshot import HaltLevel


def require_aware(name: str, value: datetime | None) -> None:
    """Refuse a naive datetime (CLAUDE.md: datetimes are timezone-aware, stored as UTC)."""
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class DayState:
    account_id: str
    trade_date: date  # the IST calendar date
    updated_at: datetime
    # Highest MTM_day today, from fresh non-outlier prices. It starts at 0 each day.
    peak: Decimal = Decimal("0")
    floor: Decimal | None = None  # None until limits exist; then only ever rises (PRD §7.1)
    halt_level: HaltLevel = HaltLevel.NONE
    halt_reason: str | None = None
    entries_today: int = 0  # entries the gate allowed today, whether or not the broker took them
    loss_streak: int = 0  # consecutive losing trades; the one field a win may lower
    cooldown_until: datetime | None = None
    # Last price per open position at the close, "SEGMENT:ID|PRODUCT" -> price as text. The next
    # trading day measures carried positions from it (spec equity-floor "previous close").
    close_marks: dict[str, str] | None = None
    # Added by add-risk-guards. All three only move one way within a day.
    # Lots the gate allowed for entries today (filled or not): the daily gross-lot cap counts
    # these. One lot is the exchange's minimum contract size, e.g. 65 units of a NIFTY option.
    entry_lots_today: int = 0
    last_entry_at: datetime | None = None  # when the gate last allowed an entry (minimum gap)
    # True once the account has held, or been allowed to enter, a contract expiring today: from
    # then on the day's floor uses the tighter expiry-day giveback. It never switches back off.
    expiry_exposure: bool = False

    def __post_init__(self) -> None:
        # Postgres GREATEST treats NaN as larger than every number, so a non-finite peak or
        # floor would be stored for the whole day and could never be lowered. Python's max()
        # does not behave that way, so the two stores would also disagree. Refuse it here.
        for name, value in (("peak", self.peak), ("floor", self.floor)):
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be a finite number, got {value}")
        require_aware("updated_at", self.updated_at)
        require_aware("cooldown_until", self.cooldown_until)
        require_aware("last_entry_at", self.last_entry_at)


def merge_day_state(stored: DayState | None, new: DayState) -> DayState:
    """The state after writing ``new`` over ``stored`` (None: nothing stored yet, so ``new``).

    Only ever-rising fields keep the larger value (``expiry_exposure`` is a flag, so "larger" is
    OR: once on, it stays on); ``loss_streak`` takes the new value, because a
    winning trade legitimately resets it; ``close_marks`` keeps the first marks recorded, so a
    restart after the close cannot replace them; ``updated_at`` is always the new write's.

    The halt reason belongs to the level that set it: a write at a strictly higher level brings
    its own reason (even none: a wrong label is worse than no label), a write at the same level
    may add or replace the reason but never wipe it, and a stale write at a lower level changes
    nothing. Merging two different days is a bug, so it raises rather than carry yesterday's
    halt into today (or lose it).
    """
    if stored is None:
        return new
    if (stored.account_id, stored.trade_date) != (new.account_id, new.trade_date):
        raise ValueError(
            f"cannot merge {new.account_id} {new.trade_date} into "
            f"{stored.account_id} {stored.trade_date}"
        )
    if new.halt_level > stored.halt_level:
        reason = new.halt_reason
    elif new.halt_level == stored.halt_level:
        reason = new.halt_reason if new.halt_reason is not None else stored.halt_reason
    else:
        reason = stored.halt_reason
    return DayState(
        account_id=stored.account_id,
        trade_date=stored.trade_date,
        updated_at=new.updated_at,
        peak=max(stored.peak, new.peak),
        floor=_highest(stored.floor, new.floor),
        halt_level=max(stored.halt_level, new.halt_level),
        halt_reason=reason,
        entries_today=max(stored.entries_today, new.entries_today),
        loss_streak=new.loss_streak,
        cooldown_until=_highest(stored.cooldown_until, new.cooldown_until),
        close_marks=stored.close_marks if stored.close_marks is not None else new.close_marks,
        entry_lots_today=max(stored.entry_lots_today, new.entry_lots_today),
        last_entry_at=_highest(stored.last_entry_at, new.last_entry_at),
        expiry_exposure=stored.expiry_exposure or new.expiry_exposure,
    )


# ``[T: (Decimal, datetime)]`` is a type parameter restricted to exactly those two types: the
# function works on Decimals or on datetimes, never a mix. C# ``where T : ...`` cannot say "one
# of these two types"; the closest is two overloads.
def _highest[T: (Decimal, datetime)](a: T | None, b: T | None) -> T | None:
    """The larger of two optional values, ignoring None (like SQL ``GREATEST``)."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
