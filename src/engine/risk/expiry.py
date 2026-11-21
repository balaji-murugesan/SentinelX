"""The expiry-day profile's shared definitions (add-risk-guards task 5.1; expiry-day-profile spec;
design §6).

One place decides whether a contract expires today, and one small pure helper per override says
which value applies, so the entry cutoff, the lot caps, the instrument policy and the square-off
cannot disagree about it.

Trading terms:
  * expiry date    - the last day a futures or options contract trades; it then settles. Weekly
                     index options expire every week, and an exchange holiday moves the date. The
                     engine reads it from the instrument master and never infers it from the
                     weekday (spec "A contract expires today only by its master expiry date").
  * expiry day     - for a contract, the IST date that equals its expiry date. Premiums move
                     fastest then, so the owner may set stricter ``..._expiry`` overrides.
  * override       - an optional limit that replaces its base limit for contracts expiring today.
                     Validation already refuses an override looser than its base
                     (``limits._guard_problems``); these helpers still take the tighter of the two
                     (``min`` of times and caps, ``max`` of premiums), which is the spec's own
                     wording ("the earlier of", "the higher of") and costs nothing.
"""

from datetime import datetime, time
from decimal import Decimal

from contracts.market import Instrument
from engine.market_data.clock import ist_date
from engine.risk.limits import RiskLimits


def expires_today(instrument: Instrument, now: datetime) -> bool:
    """True when ``instrument``'s master expiry date is the IST date of ``now``. A contract with no
    expiry date (an equity, an index) never expires."""
    return instrument.expiry is not None and instrument.expiry == ist_date(now)


def treated_as_expiring(instrument: Instrument | None, now: datetime) -> bool:
    """For the square-off time choice only (``kill.SquareOff``): ``expires_today``, except that a
    contract with no master record (``None``) counts as expiring, so its intraday position is
    exited at the earlier time (expiry-day-profile "Intraday positions in expiring contracts are
    squared off earlier"). Everything else (the entry cutoff, the floor headroom, the monitor's
    expiry-exposure flag) uses ``expires_today`` on a known instrument: a contract counts as
    expiring only by its master expiry date."""
    return instrument is None or expires_today(instrument, now)


def effective_cutoff(limits: RiskLimits, expiring: bool) -> time:
    """The entry cutoff (IST) for an entry whose contract expires today (``expiring``) or not."""
    if expiring and limits.entry_cutoff_expiry is not None:
        return min(limits.entry_cutoff, limits.entry_cutoff_expiry)
    return limits.entry_cutoff


def effective_square_off(limits: RiskLimits, expiring: bool) -> time:
    """The engine's own square-off time (IST) for an intraday position in a contract expiring today
    (``expiring``) or not. The square-off treats a position with no master record as expiring."""
    if expiring and limits.square_off_time_expiry is not None:
        return min(limits.square_off_time, limits.square_off_time_expiry)
    return limits.square_off_time


def effective_lot_caps(limits: RiskLimits, expiring: bool) -> tuple[int, int]:
    """``(per_underlying, total)`` open-lot caps for an entry whose contract expires today
    (``expiring``) or not. The daily gross-lot cap has no override (expiry-day-profile "Entries in
    expiring contracts use the expiry lot caps")."""
    per_underlying, total = limits.max_open_lots_per_underlying, limits.max_open_lots_total
    if expiring:
        if limits.max_open_lots_per_underlying_expiry is not None:
            per_underlying = min(per_underlying, limits.max_open_lots_per_underlying_expiry)
        if limits.max_open_lots_total_expiry is not None:
            total = min(total, limits.max_open_lots_total_expiry)
    return per_underlying, total


def effective_min_premium(limits: RiskLimits, expiring: bool) -> Decimal | None:
    """The minimum option premium for an entry whose contract expires today (``expiring``) or not:
    on expiry, the higher of ``min_premium`` and ``min_premium_expiry``, whichever are set. None:
    no minimum applies."""
    if not expiring or limits.min_premium_expiry is None:
        return limits.min_premium
    if limits.min_premium is None:
        return limits.min_premium_expiry
    return max(limits.min_premium, limits.min_premium_expiry)
