"""The equity floor: a level of the day's P&L that only ever rises (PRD §7.1, D-026).

``MTM_day`` is the day's profit and loss (see ``engine.risk.pnl``). The floor is the level below
which the account is killed. It starts at ``-daily_max_loss`` and rises as the day's *peak*
``MTM_day`` rises, through up to four optional terms:

  * trailing-loss - for every ``trail_loss_x`` of peak, the floor rises by ``trail_loss_y``;
  * lock          - once the peak reaches ``lock_trigger``, the floor is at least ``lock_at``;
  * trail         - for every whole ``trail_step`` of peak, the floor rises by ``trail_by``;
  * giveback      - once the peak reaches ``giveback_arm``, the floor is the peak minus the
                    larger of ``giveback_abs`` and ``giveback_pct`` percent of the peak.

``floor_of`` is the floor the formulas give for a given peak. ``next_floor`` is the runtime rule:
it never returns less than the previous floor, so the floor cannot fall whatever the peak path or
configuration does. Both are pure functions over exact ``Decimal`` values (no clock, no I/O), so
the property tests can try thousands of random parameters and paths.

Trading terms:
  * peak     - the highest ``MTM_day`` reached today (it starts at 0).
  * floor    - a trigger, not a price guarantee: a gap through it fills worse than the floor.
  * giveback - how much of the day's peak profit you may hand back before stopping.

Percentages are percent numbers, like every ``..._pct`` limit: ``giveback_pct = 30`` means 30%.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# Bounds that keep the arithmetic exact. Decimal works to 28 significant digits, and a division
# whose quotient needs more than that raises ``InvalidOperation``: a step of 1e-30 against a
# peak of 100 would do it. A step below one paisa, or an amount above 1e12 rupees, is a typo.
MIN_STEP = Decimal("0.01")
MAX_AMOUNT = Decimal("1e12")


class LockMode(StrEnum):
    """Which of the lock and trail terms are on. A ``StrEnum`` is a C# ``enum`` whose members
    are also strings, so ``LockMode.LOCK == "LOCK"`` and it travels as plain text."""

    OFF = "OFF"
    LOCK = "LOCK"
    TRAIL = "TRAIL"
    LOCK_AND_TRAIL = "LOCK_AND_TRAIL"


# ``@dataclass(frozen=True)`` is a Python record: like a C# ``readonly record`` it gets a
# constructor and equality for free and cannot be changed after it is built. ``__post_init__``
# runs right after the constructor, like validation in a C# constructor body.
@dataclass(frozen=True)
class FloorParams:
    """The configured numbers behind the floor. Every term is optional: left out (None, or the
    mode that needs it switched off) it has no effect. The checks below refuse a combination
    that would divide by zero or put the floor at or above the peak that armed it, which would
    kill the account the moment the term switched on."""

    daily_max_loss: Decimal  # the floor starts at -daily_max_loss
    trail_loss_x: Decimal | None = None  # peak step ...
    trail_loss_y: Decimal | None = None  # ... and how much the floor rises per step
    lock_mode: LockMode = LockMode.OFF
    lock_trigger: Decimal | None = None  # peak at which the lock arms
    lock_at: Decimal | None = None  # the floor level the lock sets
    trail_step: Decimal | None = None  # peak step ...
    trail_by: Decimal | None = None  # ... and how much the floor rises per step
    giveback_arm: Decimal | None = None  # peak at which the giveback limit arms
    giveback_abs: Decimal | None = None  # largest giveback in rupees ...
    giveback_pct: Decimal | None = None  # ... or as a percent of the peak (30 = 30%)

    def __post_init__(self) -> None:
        # ``vars(self)`` lists every field as a name -> value dictionary (like reflection over a
        # C# record's properties), so each number is checked once, whichever field it is.
        for name, value in vars(self).items():
            if value is None or isinstance(value, LockMode):
                continue
            if not isinstance(value, Decimal):
                # An int or float would bypass the exact-arithmetic and finite checks below.
                raise TypeError(f"{name} must be a Decimal, not {type(value).__name__}")
            if not value.is_finite() or abs(value) > MAX_AMOUNT:
                raise ValueError(f"{name} must be a finite amount of at most {MAX_AMOUNT:.0E}")
        if self.daily_max_loss <= 0:
            raise ValueError("daily_max_loss must be above zero")
        self._check_trailing_loss()
        self._check_lock_and_trail()
        self._check_giveback()

    def _check_trailing_loss(self) -> None:
        if (self.trail_loss_x is None) != (self.trail_loss_y is None):
            raise ValueError("trail_loss_x and trail_loss_y go together")
        if self.trail_loss_x is None or self.trail_loss_y is None:
            return
        # y == x is allowed: the floor then trails the peak at a fixed distance below it, and
        # stays below the peak. y > x would rise faster than the peak: breach on arming.
        if self.trail_loss_x < MIN_STEP or not 0 < self.trail_loss_y <= self.trail_loss_x:
            raise ValueError(
                "trail_loss_x must be at least 0.01, and trail_loss_y above zero and at most "
                "trail_loss_x: a floor that rises faster than the peak would be in breach the "
                "moment the term switched on"
            )

    def _check_lock_and_trail(self) -> None:
        if self.lock_mode in (LockMode.LOCK, LockMode.LOCK_AND_TRAIL):
            if self.lock_trigger is None or self.lock_at is None:
                raise ValueError(f"lock_mode {self.lock_mode} needs lock_trigger and lock_at")
            if self.lock_trigger <= 0 or self.lock_at >= self.lock_trigger:
                raise ValueError("lock_at must be below lock_trigger, which must be above zero")
        if self.lock_mode in (LockMode.TRAIL, LockMode.LOCK_AND_TRAIL):
            if self.trail_step is None or self.trail_by is None:
                raise ValueError(f"lock_mode {self.lock_mode} needs trail_step and trail_by")
            if self.trail_step < MIN_STEP or not 0 < self.trail_by < self.trail_step:
                raise ValueError(
                    "trail_step must be at least 0.01, and trail_by above zero and below it"
                )

    def _check_giveback(self) -> None:
        if self.giveback_arm is None:
            if self.giveback_abs is not None or self.giveback_pct is not None:
                raise ValueError("giveback_abs and giveback_pct need giveback_arm")
            return
        if self.giveback_arm <= 0:
            raise ValueError("giveback_arm must be above zero")
        if self.giveback_abs is None and self.giveback_pct is None:
            raise ValueError("giveback_arm needs giveback_abs or giveback_pct")
        if self.giveback_abs is not None and self.giveback_abs < 0:
            raise ValueError("giveback_abs must not be negative")
        if self.giveback_pct is None and (self.giveback_abs is None or self.giveback_abs <= 0):
            # A zero giveback puts the floor exactly at the peak: breach the moment it is armed.
            raise ValueError("giveback_abs must be above zero when there is no giveback_pct")
        if self.giveback_pct is not None and not 0 < self.giveback_pct < 100:
            raise ValueError("giveback_pct must be above 0 and below 100 (30 means 30%)")


def floor_of(params: FloorParams, peak: Decimal) -> Decimal:
    """The floor the formulas give when the day's peak ``MTM_day`` is ``peak``.

    The floor is the highest of its starting value and every configured term that is active at
    this peak. Steps are counted in whole units with ``peak // step``, floor division. On a
    ``Decimal`` that operator truncates toward zero (like C# integer ``/``), which equals
    rounding down here only because ``peak`` is never negative. A term with no whole step yet
    adds nothing, so the trail term never lifts the floor to zero before the peak has reached
    its first step.
    """
    if not peak.is_finite() or not 0 <= peak <= MAX_AMOUNT:
        raise ValueError(f"peak must be a finite number from 0 to {MAX_AMOUNT:.0E}, got {peak}")
    candidates = [-params.daily_max_loss]

    if params.trail_loss_x is not None and params.trail_loss_y is not None and peak > 0:
        steps = peak // params.trail_loss_x
        candidates.append(-params.daily_max_loss + steps * params.trail_loss_y)

    locked = (
        params.lock_mode in (LockMode.LOCK, LockMode.LOCK_AND_TRAIL)
        and params.lock_trigger is not None
        and params.lock_at is not None
        and peak >= params.lock_trigger
    )
    if locked:
        assert params.lock_at is not None  # narrowed by `locked`, for mypy
        candidates.append(params.lock_at)

    if params.trail_step is not None and params.trail_by is not None:
        if params.lock_mode is LockMode.TRAIL:
            steps = peak // params.trail_step
            if steps >= 1:
                candidates.append(steps * params.trail_by)
        elif params.lock_mode is LockMode.LOCK_AND_TRAIL and locked:
            assert params.lock_at is not None and params.lock_trigger is not None
            steps = (peak - params.lock_trigger) // params.trail_step
            candidates.append(params.lock_at + steps * params.trail_by)

    if params.giveback_arm is not None and peak >= params.giveback_arm:
        giveback = max(
            params.giveback_abs if params.giveback_abs is not None else Decimal(0),
            params.giveback_pct * peak / 100 if params.giveback_pct is not None else Decimal(0),
        )
        candidates.append(peak - giveback)

    return max(candidates)


def next_floor(previous: Decimal | None, params: FloorParams, peak: Decimal) -> Decimal:
    """The floor to use now: never below ``previous`` (None: no floor yet today).

    This is the ratchet. Even if a limits change or a bad input made ``floor_of`` smaller, the
    floor the engine acts on cannot fall (D-026; safety rule 15).
    """
    current = floor_of(params, peak)
    return current if previous is None else max(previous, current)
