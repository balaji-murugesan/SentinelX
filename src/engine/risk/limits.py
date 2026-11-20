"""The owner's risk limits: what they are, how they are checked, which way is "tighter"
(add-risk-engine design §9; risk-limits spec; PRD §7.2, D-026).

Limits are configuration stored in Postgres. The engine has **no built-in defaults** (spec "Limits
are stored in Postgres with no built-in defaults"): every number below is set by the owner, and
until they are, entries are refused (task 4.3).

Four pieces live here:

  * ``REGISTRY``   - one ``LimitSpec`` per limit: its unit and its *tightening direction*. This is
                     the single table that makes the tighten-only rule mechanical (a wrong
                     direction here is a hole in the rule, so each row is tested both ways).
  * ``RiskLimits`` - the typed, validated document that the risk checks read, for example
                     ``limits.per_trade_max_risk`` is a ``Decimal`` and ``limits.entry_cutoff`` a
                     ``time``. ``parse_limits`` builds one from the flat command/storage form and
                     refuses anything out of range or inconsistent, naming every problem at once.
  * ``loosened_keys`` - which keys of an edit moved *against* their direction.
  * ``validate_edit`` - the whole decision on an edit: valid, and (in market hours) tighten-only.

Trading terms:
  * per-trade max risk - the most rupees one trade may lose if its initial stop is hit.
  * catastrophic SL    - the far-away stop kept at the broker in case the engine is down.
  * price band         - how far (in %) an order price may sit from the last traded price.
  * entry cutoff / square-off - the time after which no entry is allowed / intraday positions are
                     closed by the engine itself (both are wall-clock times in IST).
  * shadow mode        - a rule runs and is logged but its verdict is ignored, to try a new rule
                     safely before it is enforced.

Units. Money (INR) and percentages (PCT) are exact ``Decimal`` values and travel as text, never as
floats. **Every ``..._pct`` limit is a percent number**: ``giveback_pct = 30`` means 30%. Nothing
here converts it; the code that uses a percentage (``floor_of``, ``MtmTracker``) divides by 100
itself, so a percent number is passed through unchanged and cannot be converted twice.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from contracts.commands import LimitValue
from engine.market_data.clock import is_market_hours
from engine.risk.floor import MAX_AMOUNT, FloorParams, LockMode, floor_of, next_floor
from engine.risk.state import require_aware

# Reason codes for a refused edit (risk-limits spec). Exported as constants so a typo in a caller
# cannot invent a new code.
TIGHTEN_ONLY: Final = "TIGHTEN_ONLY"
LIMITS_INVALID: Final = "RISK_LIMITS_INVALID"
LIMITS_NOT_SET: Final = "RISK_LIMITS_NOT_SET"
STATE_UNAVAILABLE: Final = "RISK_STATE_UNAVAILABLE"  # state, limits or persistence missing

MODE_PREFIX: Final = "mode."  # "mode.per_trade_risk" sets the mode of the rule "per_trade_risk"
MAX_DECIMAL_PLACES: Final = 6  # far finer than a paisa; keeps a limit's text short and exact


class Unit(StrEnum):
    """What a limit's number means; each unit has its own valid range (see ``_parse_value``).
    ``StrEnum`` is a C# ``enum`` whose members are also strings. INR_PER_LOT, LOTS and BOOL (the
    other units in the risk-limits spec) join when a limit needs them (``add-risk-guards``)."""

    INR = "INR"  # rupees, exact Decimal
    PCT = "PCT"  # a percent number, 0 < x <= 100
    COUNT = "COUNT"  # a whole number >= 1
    SECONDS = "SECONDS"  # a whole number of seconds >= 1
    TIME_OF_DAY = "TIME_OF_DAY"  # "HH:MM" in IST
    ENUM = "ENUM"  # one of a fixed set of names


class Direction(StrEnum):
    UP = "up"  # a larger value is tighter
    DOWN = "down"  # a smaller value is tighter
    ENUM_ORDER = "enum_order"  # tighter = later in the spec's declared ranking


class RuleMode(StrEnum):
    SHADOW = "shadow"  # the rule's verdict is logged but not acted on
    ENFORCE = "enforce"  # the rule's verdict binds. This is what a rule runs as if none is set


@dataclass(frozen=True)
class LimitSpec:
    key: str
    unit: Unit
    direction: Direction
    # An optional limit may be left out (None): its floor term is off. Turning it on (None ->
    # value) tightens, turning it off (value -> None) loosens: the spec's ``enable_only``
    # direction, applied on top of the value direction.
    optional: bool = False
    # Part of the equity floor. Its range is policed by ``FloorParams`` (one source for those
    # rules), so the unit-level "must be above zero" check is skipped for it (``lock_at`` may even
    # be negative), and an edit to it is re-simulated against today's floor (``validate_edit``).
    floor_term: bool = False
    enum: type[StrEnum] | None = None  # ENUM only: the allowed names
    order: tuple[str, ...] = ()  # ENUM_ORDER only: loosest first. A name outside it is unranked
    # This limit is one *leg* of a term that the named limit arms (the giveback term takes the
    # larger of its two legs). Adding a leg to a term that is already on can only widen what it
    # gives back, so it loosens; dropping one narrows it, so it tightens. That is the opposite of
    # ``optional``'s "turning it on tightens", which holds for a whole term, not for one leg.
    leg_of: str | None = None


# The rules that can have a mode, mapped to "may this rule run in shadow?". The names are the
# stable rule ids used in audit records (``RiskEvent.rule``). The equity floor, the stale-data
# guard and the initial-stop rule are the ones that must always bind (risk-gate spec "Shadow and
# enforce modes per rule"; initial_stop is kept binding too, because without a stop no per-trade
# risk can be measured). Kill switch and the fail-closed checks have no mode at all.
RULES: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "per_trade_risk": True,
        "stop_distance": True,  # initial stop inside the catastrophic distance
        "max_trades": True,
        "cooldown": True,
        "max_open_positions": True,
        "entry_cutoff": True,
        "price_sanity": True,
        "initial_stop": False,
        "equity_floor": False,
        "stale_data": False,
    }
)

# Directions follow design §9. A move in the stated direction is a tightening. To change one,
# change the row here: the per-row test then shows what the new direction accepts and refuses.
_ROWS: tuple[LimitSpec, ...] = (
    # Rupees at risk: a smaller amount is tighter.
    LimitSpec("per_trade_max_risk", Unit.INR, Direction.DOWN),
    LimitSpec("daily_max_loss", Unit.INR, Direction.DOWN, floor_term=True),
    # Percentages. A narrower band or a closer backstop is tighter; a wider exit buffer makes
    # the kill switch's exits more aggressive, which is tighter.
    LimitSpec("catastrophic_sl_pct", Unit.PCT, Direction.DOWN),
    LimitSpec("price_band_pct", Unit.PCT, Direction.DOWN),
    LimitSpec("outlier_pct", Unit.PCT, Direction.DOWN),
    LimitSpec("exit_buffer_pct", Unit.PCT, Direction.UP),
    # Counts: fewer allowed is tighter, except more chase steps (a better chance to get flat).
    LimitSpec("max_trades_per_day", Unit.COUNT, Direction.DOWN),
    LimitSpec("loss_streak_limit", Unit.COUNT, Direction.DOWN),
    LimitSpec("max_open_total", Unit.COUNT, Direction.DOWN),
    LimitSpec("max_open_per_underlying", Unit.COUNT, Direction.DOWN),
    LimitSpec("max_chase_steps", Unit.COUNT, Direction.UP),
    # Seconds: a longer cooldown is tighter; shorter staleness and chase timings are tighter.
    LimitSpec("cooldown_seconds", Unit.SECONDS, Direction.UP),
    LimitSpec("stale_s", Unit.SECONDS, Direction.DOWN),
    LimitSpec("feed_down_s", Unit.SECONDS, Direction.DOWN),
    LimitSpec("mtm_blind_s", Unit.SECONDS, Direction.DOWN),
    LimitSpec("chase_interval_s", Unit.SECONDS, Direction.DOWN),
    LimitSpec("exit_escalate_s", Unit.SECONDS, Direction.DOWN),
    # Times of day: earlier is tighter.
    LimitSpec("entry_cutoff", Unit.TIME_OF_DAY, Direction.DOWN),
    LimitSpec("square_off_time", Unit.TIME_OF_DAY, Direction.DOWN),
    # The equity floor's optional terms (PRD §7.1). Each pair raises the floor, and a larger
    # rise or an earlier arming is tighter. ``lock_mode`` picks which of lock and trail are on.
    LimitSpec("trail_loss_x", Unit.INR, Direction.DOWN, optional=True, floor_term=True),
    LimitSpec("trail_loss_y", Unit.INR, Direction.UP, optional=True, floor_term=True),
    LimitSpec("lock_trigger", Unit.INR, Direction.DOWN, optional=True, floor_term=True),
    LimitSpec("lock_at", Unit.INR, Direction.UP, optional=True, floor_term=True),
    LimitSpec("trail_step", Unit.INR, Direction.DOWN, optional=True, floor_term=True),
    LimitSpec("trail_by", Unit.INR, Direction.UP, optional=True, floor_term=True),
    LimitSpec("giveback_arm", Unit.INR, Direction.DOWN, optional=True, floor_term=True),
    LimitSpec(
        "giveback_abs",
        Unit.INR,
        Direction.DOWN,
        optional=True,
        floor_term=True,
        leg_of="giveback_arm",
    ),  # fmt: skip
    LimitSpec(
        "giveback_pct",
        Unit.PCT,
        Direction.DOWN,
        optional=True,
        floor_term=True,
        leg_of="giveback_arm",
    ),  # fmt: skip
    LimitSpec(
        "lock_mode",
        Unit.ENUM,
        Direction.ENUM_ORDER,
        floor_term=True,
        enum=LockMode,
        order=(LockMode.OFF, LockMode.LOCK, LockMode.LOCK_AND_TRAIL),
    ),  # fmt: skip
    # One mode per rule, shadow -> enforce only.
    *(
        LimitSpec(
            MODE_PREFIX + rule,
            Unit.ENUM,
            Direction.ENUM_ORDER,
            enum=RuleMode,
            order=(RuleMode.SHADOW, RuleMode.ENFORCE),
        )
        for rule in RULES
    ),  # fmt: skip
)

# ``MappingProxyType`` is a read-only view of a dict (C# ``IReadOnlyDictionary``): the registry
# cannot be changed by accident at runtime.
REGISTRY: Final[Mapping[str, LimitSpec]] = MappingProxyType({row.key: row for row in _ROWS})


# ``kw_only=True`` makes every field a named argument, so building one of these 30-odd fields
# can never mix two of them up. ``frozen=True`` is a C# ``record`` with init-only properties.
@dataclass(frozen=True, kw_only=True)
class RiskLimits:
    """A validated set of limits. Build it with ``parse_limits``, which checks ranges and
    consistency; constructing it directly skips those checks (tests do, on purpose)."""

    per_trade_max_risk: Decimal
    daily_max_loss: Decimal  # the equity floor starts at -daily_max_loss (PRD §7.1)
    catastrophic_sl_pct: Decimal
    price_band_pct: Decimal
    outlier_pct: Decimal
    exit_buffer_pct: Decimal
    max_trades_per_day: int
    loss_streak_limit: int
    max_open_total: int
    max_open_per_underlying: int
    max_chase_steps: int
    cooldown_seconds: int
    stale_s: int
    feed_down_s: int
    mtm_blind_s: int
    chase_interval_s: int
    exit_escalate_s: int
    entry_cutoff: time  # IST wall-clock time
    square_off_time: time  # IST wall-clock time
    trail_loss_x: Decimal | None = None
    trail_loss_y: Decimal | None = None
    lock_trigger: Decimal | None = None
    lock_at: Decimal | None = None
    trail_step: Decimal | None = None
    trail_by: Decimal | None = None
    giveback_arm: Decimal | None = None
    giveback_abs: Decimal | None = None
    giveback_pct: Decimal | None = None  # a percent number: 30 means 30%
    lock_mode: LockMode = LockMode.OFF
    # Explicitly set modes only. A rule not listed runs as ENFORCE. ``field(default_factory=dict)``
    # gives each instance its own empty dict (a shared mutable default would be a bug; C# would
    # write ``= new()``).
    modes: dict[str, RuleMode] = field(default_factory=dict)

    def mode_of(self, rule: str) -> RuleMode:
        return self.modes.get(rule, RuleMode.ENFORCE)

    def floor_params(self) -> FloorParams:
        """The equity floor's settings. ``FloorParams`` refuses a combination that would breach
        the moment it armed, with ``ValueError`` or ``TypeError``: callers treat either as "these
        limits are unusable" and fail closed (reject entries), never as "no floor"."""
        return FloorParams(
            daily_max_loss=self.daily_max_loss,
            trail_loss_x=self.trail_loss_x,
            trail_loss_y=self.trail_loss_y,
            lock_mode=self.lock_mode,
            lock_trigger=self.lock_trigger,
            lock_at=self.lock_at,
            trail_step=self.trail_step,
            trail_by=self.trail_by,
            giveback_arm=self.giveback_arm,
            giveback_abs=self.giveback_abs,
            giveback_pct=self.giveback_pct,
        )

    def to_values(self) -> dict[str, LimitValue]:
        """The flat form that is stored and sent: text for money, percentages and times, plain
        numbers for counts. ``parse_limits(limits.to_values()) == limits`` always holds."""
        out: dict[str, LimitValue] = {}
        for key in REGISTRY:
            if key.startswith(MODE_PREFIX):
                continue
            out[key] = _dump(getattr(self, key))
        for rule, mode in self.modes.items():
            out[MODE_PREFIX + rule] = mode.value
        return out


class InvalidLimits(ValueError):
    """The limits cannot be used. ``problems`` names every reason, so the owner fixes the whole
    edit in one go (the edit is applied completely or not at all)."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


_HH_MM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")  # compiled once, like a static C# Regex


def _dump(value: object) -> LimitValue:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")  # "f" never uses an exponent: Decimal("1E+3") -> "1000"
    if isinstance(value, time):
        return f"{value:%H:%M}"
    return str(value)  # a StrEnum member: its text


def _as_decimal(key: str, raw: LimitValue) -> Decimal:
    # bool is a subclass of int in Python (True == 1), so it has to be refused by name.
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError(f"{key} must be an amount, as text or a whole number")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{key} is not a number: {raw!r}") from None
    # Decimal("NaN") and Decimal("Infinity") parse fine, and would pass every "<" check below.
    if not value.is_finite() or abs(value) > MAX_AMOUNT:
        raise ValueError(f"{key} must be a finite amount of at most {MAX_AMOUNT:.0E}")
    # "1E-999999999" is finite, positive and tiny, so it passes every range check, and writing it
    # back out in full (``format(value, 'f')``) would build a billion-character string.
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -MAX_DECIMAL_PLACES:
        raise ValueError(f"{key} may have at most {MAX_DECIMAL_PLACES} decimal places")
    return value


type Parsed = Decimal | int | time | StrEnum | None


def _parse_value(spec: LimitSpec, raw: LimitValue) -> Parsed:
    """Check one value against its unit's valid range and convert it. None means "not set",
    which is allowed only for an optional limit or an enum (which then has its neutral value)."""
    key = spec.key
    if raw is None:
        if spec.optional or spec.unit is Unit.ENUM:
            return None
        raise ValueError(f"{key} is required")
    match spec.unit:
        case Unit.INR:
            amount = _as_decimal(key, raw)
            if not spec.floor_term and amount <= 0:
                raise ValueError(f"{key} must be above zero")
            return amount
        case Unit.PCT:
            pct = _as_decimal(key, raw)
            if not 0 < pct <= 100:
                raise ValueError(f"{key} must be above 0 and at most 100 (30 means 30%)")
            return pct
        case Unit.COUNT | Unit.SECONDS:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValueError(f"{key} must be a whole number of at least 1")
            return raw
        case Unit.TIME_OF_DAY:
            found = _HH_MM.fullmatch(raw) if isinstance(raw, str) else None
            if found is None:
                raise ValueError(f"{key} must be a time of day as HH:MM, for example 15:00")
            return time(int(found[1]), int(found[2]))
        case Unit.ENUM:
            assert spec.enum is not None  # every ENUM row names its enum
            names = ", ".join(member.value for member in spec.enum)
            if not isinstance(raw, str):  # a number or a flag is never a name
                raise ValueError(f"{key} must be one of {names}")
            try:
                return spec.enum(raw)
            except ValueError:
                raise ValueError(f"{key} must be one of {names}") from None


def _consistency_problems(limits: RiskLimits) -> list[str]:
    """Rules about several limits together (risk-limits spec "Inconsistent limit sets")."""
    problems: list[str] = []
    if limits.entry_cutoff >= limits.square_off_time:
        problems.append("entry_cutoff must be earlier than square_off_time")
    if not limits.stale_s < limits.feed_down_s < limits.mtm_blind_s:
        problems.append("stale_s, feed_down_s and mtm_blind_s must be in increasing order")
    if limits.exit_buffer_pct > limits.price_band_pct:
        problems.append(
            "exit_buffer_pct must not exceed price_band_pct: the kill switch's own exits "
            "would fail price sanity"
        )
    for rule, mode in limits.modes.items():
        if mode is RuleMode.SHADOW and not RULES[rule]:
            problems.append(f"{MODE_PREFIX}{rule} cannot be shadow: that rule must always bind")
    return problems


def parse_limits(raw: Mapping[str, LimitValue]) -> RiskLimits:
    """Validate the flat form and build ``RiskLimits``, or raise ``InvalidLimits`` naming every
    problem. Used for an edit and for every document loaded from storage, so a bad document
    fails closed instead of reaching a rule.

    A key that is not in ``REGISTRY`` is refused as an unknown limit. That is also how the
    engine-owned state (the floor, the day's peak, halt state, counters) is kept out of reach:
    none of them is a limit.
    """
    problems = [f"unknown limit {key!r}" for key in raw if key not in REGISTRY]
    values: dict[str, object] = {}
    modes: dict[str, RuleMode] = {}
    for key, spec in REGISTRY.items():
        try:
            parsed = _parse_value(spec, raw.get(key))
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if parsed is None:
            continue
        if key.startswith(MODE_PREFIX):
            assert isinstance(parsed, RuleMode)  # a mode row parses to its own enum
            modes[key.removeprefix(MODE_PREFIX)] = parsed
        else:
            values[key] = parsed
    if problems:
        raise InvalidLimits(problems)

    # ``**values`` unpacks the dict into named arguments (C# has no equivalent; it is like
    # calling the constructor with every property by name). mypy cannot check a dict of mixed
    # values against the typed fields, so that is the one place it is told to look away: the
    # registry test asserts that every field has exactly one row.
    limits = RiskLimits(modes=modes, **values)  # type: ignore[arg-type]
    problems = _consistency_problems(limits)
    try:
        limits.floor_params()
    except (ValueError, TypeError) as exc:
        problems.append(f"equity floor settings: {exc}")
    if problems:
        raise InvalidLimits(problems)
    return limits


def value_of(limits: RiskLimits, key: str) -> Parsed:
    """The current value of a registry key (a mode key gives that rule's mode)."""
    if key.startswith(MODE_PREFIX):
        return limits.mode_of(key.removeprefix(MODE_PREFIX))
    return getattr(limits, key)  # type: ignore[no-any-return]


def trail_move_tightens(old: LockMode, new: LockMode) -> bool:
    """Is a change of ``lock_mode`` that involves TRAIL a tightening? Called only when ``old`` or
    ``new`` is TRAIL, because the ranking OFF < LOCK < LOCK_AND_TRAIL does not place it.

    Why it needs a rule of its own: the floor is the highest of its active terms, so adding a
    term can only raise it. But TRAIL and LOCK_AND_TRAIL count their steps from different
    starting points (the first from a peak of 0, the second from ``lock_trigger``), so for some
    peaks one gives a lower floor than the other. Design §9 leaves TRAIL "guarded by the floor
    re-simulation", yet that only looks at *today's* peak: a later, higher peak could still meet
    a floor that is lower than before.

    So only OFF -> TRAIL counts: it adds a term and can only raise the floor. Every other move
    involving TRAIL (to or from LOCK, LOCK_AND_TRAIL, or TRAIL -> OFF) is refused during market
    hours and waits for the close. This is stricter than design §9, which left those moves to the
    re-simulation alone; the cost is only that the owner makes them after 15:30.
    """
    return old is LockMode.OFF and new is LockMode.TRAIL


def _is_tightening(spec: LimitSpec, old: Parsed, new: Parsed) -> bool:
    """Does changing ``old`` to ``new`` (different values) move in the limit's tightening
    direction? A value that is not comparable counts as not tightening."""
    if old is None or new is None:  # only an optional limit can be None
        return old is None  # on (None -> value) tightens; off (value -> None) loosens
    if spec.direction is Direction.ENUM_ORDER:
        assert isinstance(old, StrEnum) and isinstance(new, StrEnum)
        if old in spec.order and new in spec.order:
            return spec.order.index(new) > spec.order.index(old)
        return trail_move_tightens(LockMode(old), LockMode(new))  # lock_mode is the only one
    # Both values share the limit's unit, so they compare: Decimal with Decimal, int with int,
    # time with time. mypy cannot see that through the union.
    if spec.direction is Direction.UP:
        return new > old  # type: ignore[operator]
    return new < old  # type: ignore[operator]


def loosened_keys(old: RiskLimits, new: RiskLimits) -> list[str]:
    """The keys whose change from ``old`` to ``new`` is not a tightening (spec "Tighten-only
    during market hours"). Empty means every change tightens. A key that did not change is not
    listed."""
    out: list[str] = []
    for key, spec in REGISTRY.items():
        before, after = value_of(old, key), value_of(new, key)
        if before == after:
            continue
        if spec.leg_of is not None and (before is None) != (after is None):
            # One leg added or dropped. Dropping tightens. Adding loosens, unless the whole term
            # is being switched on in this very edit (its arming limit was off before).
            if before is None and value_of(old, spec.leg_of) is not None:
                out.append(key)
        elif not _is_tightening(spec, before, after):
            out.append(key)
    return out


class EditRejected(Exception):
    """An edit that must not be applied. ``code`` is the stable reason (``TIGHTEN_ONLY`` or
    ``RISK_LIMITS_INVALID``); ``problems`` names every offending limit."""

    def __init__(self, code: str, problems: Sequence[str]) -> None:
        self.code = code
        self.problems = tuple(problems)
        super().__init__(f"{code}: " + "; ".join(self.problems))


# The limits that make up the equity floor: an edit to any of them is re-simulated.
_FLOOR_KEYS: Final = tuple(key for key, spec in REGISTRY.items() if spec.floor_term)


def _floor_problems(
    stored: RiskLimits, new: RiskLimits, peak: Decimal, floor: Decimal | None
) -> list[str]:
    """Spec "A limits edit may not lower the floor": replay today's peak through the new floor
    terms. The floor the engine acts on today is ``next_floor`` (never below its ratchet), so the
    new terms must give at least that. This is the same ``floor_of`` the breach check and the
    entry headroom check use, so the three cannot disagree."""
    if all(value_of(stored, key) == value_of(new, key) for key in _FLOOR_KEYS):
        return []  # no floor term changed, so there is nothing to replay
    try:
        current = next_floor(floor, stored.floor_params(), peak)
        proposed = floor_of(new.floor_params(), peak)
    except (ValueError, TypeError, ArithmeticError) as exc:
        # Cannot replay (a bad peak, say): refuse, never wave it through (fail closed).
        return [f"the floor cannot be re-simulated, so floor terms stay as they are: {exc}"]
    if proposed < current:
        return [f"the floor terms would lower today's floor from {current} to {proposed}"]
    return []


def validate_edit(
    stored: RiskLimits | None,
    raw: Mapping[str, LimitValue],
    *,
    now: datetime,
    peak: Decimal,
    floor: Decimal | None,
) -> RiskLimits:
    """Decide an edit: return the new limits, or raise ``EditRejected``.

    ``stored`` is what is in force (None: nothing set yet), ``raw`` the whole proposed set in the
    flat form, ``now`` an aware instant from the injected clock, and ``peak`` and ``floor``
    today's figures, used only to re-simulate the floor. The edit is judged as a whole: nothing
    is half applied, and every problem is reported at once.

      1. It must be valid and consistent (``parse_limits``), at any hour.
      2. The first set is accepted at any hour: there is nothing to loosen.
      3. During market hours (``engine.market_data.clock.is_market_hours``: weekdays 09:15 to
         15:30 IST) every changed limit must move in its tightening direction, and the floor
         re-simulated with the new terms and today's peak must not be lower than today's floor.
         Outside those hours any valid edit is accepted.

    Rule modes need no code of their own: shadow -> enforce is a tightening in the registry, and
    a rule that must always bind is refused shadow by ``parse_limits``.
    """
    require_aware("now", now)
    try:
        new = parse_limits(raw)
    except InvalidLimits as exc:
        raise EditRejected(LIMITS_INVALID, exc.problems) from None
    if stored is None or not is_market_hours(now):
        return new
    problems = [
        f"{key}: {_dump(value_of(stored, key))} -> {_dump(value_of(new, key))} is not a tightening"
        for key in loosened_keys(stored, new)
    ]
    problems += _floor_problems(stored, new, peak, floor)
    if problems:
        raise EditRejected(TIGHTEN_ONLY, problems)
    return new
