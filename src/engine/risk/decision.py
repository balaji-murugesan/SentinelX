"""What a risk check says, how checks are run, and what they see (add-risk-engine task 5.2;
risk-gate spec "Rules are checks with ranked decisions"; design §2).

Every risk rule is a *check*: a small object with a stable ``name`` and a pre-trade method (judge
an order), a continuous method (judge the account on a tick, a timer or a fill), or both. A check
does no I/O. It reads an immutable ``RiskContext`` and returns one ``Decision``, so every rule is a
pure function that is unit-tested directly, and shadow mode is trivial: same context, same
decision, logged but not acted on.

Severity is ranked ``ALLOW < REJECT < HALT_NEW_ENTRIES < TARGET_HALT < KILL`` (PRD §7.2) and the
engine acts on the most severe decision of all enforced checks.

Python idioms here, with their C# analogues:
  * ``Protocol``  - a structural interface: a class satisfies ``PreTradeCheck`` just by having a
                    ``name`` and a matching ``pre_trade`` method, without declaring
                    ``: IPreTradeCheck``.
  * ``IntEnum``   - an ``enum`` whose members are also ints, so ``max()`` picks the more severe.
  * ``@dataclass(frozen=True)`` - an immutable ``record``.

Fail closed (spec): a check that *raises* is not skipped. Before a trade it counts as REJECT,
naming the check; on a tick or timer it counts as HALT_NEW_ENTRIES. A shadow-mode rule's crash is
shadowed like any other of its decisions: a rule being tried out must not stop trading.
"""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum
from functools import partial
from typing import Final, Protocol

import structlog

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, Position
from engine.positions.book import PositionId
from engine.risk.limits import RiskLimits, RuleMode
from engine.risk.repository import AuditValue
from engine.risk.state import DayState

log = structlog.get_logger("engine.risk.decision")

# Stable reason codes (the audit and the stored rejection reason start with one of these). A
# constant, not a literal at each use, so a typo cannot invent a new code. Rules add theirs here.
LIVE_NOT_ENABLED: Final = "RISK_LIVE_NOT_ENABLED"  # live mode before a live adapter exists
CHECK_ERROR: Final = "RISK_CHECK_ERROR"  # a check raised
HALTED_DAY: Final = (
    "RISK_HALTED_DAY"  # a kill or halt locked the day: entries refused until tomorrow
)


class Severity(IntEnum):
    """How hard a decision bites. The gap between the numbers is only for readability."""

    ALLOW = 0
    REJECT = 10  # this order is refused
    HALT_NEW_ENTRIES = 20  # no entries until the condition clears (or the day ends)
    TARGET_HALT = 30  # the daily profit target was reached (add-risk-guards)
    KILL = 40  # stop everything and flatten


class Phase(StrEnum):
    PRE_TRADE = "pre_trade"  # judging one order
    CONTINUOUS = "continuous"  # judging the account on a tick, timer or fill


class RiskTrigger(StrEnum):
    """What woke a continuous check."""

    TICK = "tick"
    TIMER = "timer"
    FILL = "fill"
    ORDER_UPDATE = "order_update"


@dataclass(frozen=True)
class Decision:
    """One check's verdict. ``inputs`` are the figures the rule used, flat and JSON-safe (money as
    text, never a float): they go to the audit row unchanged, so "why didn't it trade" can be
    answered from the log."""

    severity: Severity
    code: str | None = None  # a stable code, None for ALLOW
    detail: str = ""  # a human sentence for the rejection reason
    inputs: Mapping[str, AuditValue] = field(default_factory=dict)

    def reason(self) -> str | None:
        """The text an order is rejected with: it begins with the code (spec "Rejections carry
        reason and code")."""
        if self.code is None:
            return None
        return f"{self.code}: {self.detail}" if self.detail else self.code


ALLOW: Final = Decision(Severity.ALLOW)


def reject(code: str, detail: str = "", **inputs: AuditValue) -> Decision:
    """``**inputs`` collects named arguments into a dict (C# has no equivalent; it is like a
    params array of name/value pairs)."""
    return Decision(Severity.REJECT, code, detail, inputs)


def halt_new_entries(code: str, detail: str = "", **inputs: AuditValue) -> Decision:
    return Decision(Severity.HALT_NEW_ENTRIES, code, detail, inputs)


def kill(code: str, detail: str = "", **inputs: AuditValue) -> Decision:
    return Decision(Severity.KILL, code, detail, inputs)


@dataclass(frozen=True)
class Quote:
    """The latest accepted-or-not price of an instrument and how old it is."""

    price: Decimal
    age: timedelta


@dataclass(frozen=True)
class RiskContext:
    """An immutable view of everything a check may read, built fresh for each evaluation. Checks
    never reach into the order service or the database (design §2): if a figure is not here, the
    rule cannot depend on it, which is what keeps every rule a pure function.

    ``mtm_day`` is None while the day's P&L is unknown (a held instrument has no price, or a
    reference is missing): a rule must treat that as "unknown", never as zero. ``instruments`` and
    ``quote`` are lookups (a C# ``Func<,>``): the instrument master's facts (lot size, tick size)
    and the latest price with its age.

    The last three fields feed the stale-data checks and have defaults, so a context built
    without them (a pre-trade rule's test) is simply one with a healthy feed: ``feed_silent_for``
    is the time since *any* tick arrived (None: not known), ``feed_subscribed`` says something is
    subscribed, and ``unpriced_for`` is, for each held instrument that has never been priced, how
    long the engine has been waiting for its first tick.
    """

    account_id: str
    now: datetime
    limits: RiskLimits
    day: DayState
    mtm_day: Decimal | None
    positions: Mapping[PositionId, Position]
    orders: Mapping[str, Order]
    instruments: Callable[[InstrumentKey], Instrument | None]
    quote: Callable[[InstrumentKey], Quote | None]
    feed_silent_for: timedelta | None = None
    feed_subscribed: bool = False
    unpriced_for: Mapping[InstrumentKey, timedelta] = field(default_factory=dict)


class PreTradeCheck(Protocol):
    name: str  # the rule's stable id; also its key in the limits' ``mode.<name>``
    # Does this rule judge exits too? Almost every rule is an entry rule and says False: exits are
    # never blocked by entry limits, cutoffs, caps, cooldowns or halts (spec "Exits are never
    # blocked by entry rules"). Only a rule about the order itself (price sanity) says True. There
    # is no default on purpose: every new rule has to decide.
    applies_to_exits: bool

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision: ...


class ContinuousCheck(Protocol):
    name: str

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision: ...


@dataclass(frozen=True)
class RuleResult:
    """One check's decision plus how it counted: the unit the audit records and the gate ranks."""

    rule: str
    phase: Phase
    decision: Decision
    enforced: bool  # False for a shadow rule: logged, treated as ALLOW
    latency_us: int

    @property
    def effective(self) -> Severity:
        """What the engine acts on: the decision's severity if enforced, else ALLOW."""
        return self.decision.severity if self.enforced else Severity.ALLOW


def most_severe(results: Iterable[RuleResult]) -> RuleResult | None:
    """The result the engine acts on: the highest ``effective`` severity (the first of equals, so
    the order checks are registered in is the tie-break). None when there are no results or all
    allow."""
    worst: RuleResult | None = None
    for result in results:
        if result.effective > Severity.ALLOW and (
            worst is None or result.effective > worst.effective
        ):
            worst = result
    return worst


def _run(
    check_name: str,
    phase: Phase,
    ctx: RiskContext,
    crash_severity: Severity,
    judge: Callable[[], Decision],
) -> RuleResult:
    started = time.perf_counter()
    try:
        decision = judge()
    except Exception as error:  # a crashing check must never be skipped: fail closed
        log.error("risk_check_crashed", rule=check_name, phase=phase.value, exc_info=True)
        decision = Decision(
            crash_severity,
            CHECK_ERROR,
            f"check {check_name!r} failed ({type(error).__name__})",
            {"error": type(error).__name__},
        )
    latency_us = int((time.perf_counter() - started) * 1_000_000)
    enforced = ctx.limits.mode_of(check_name) is RuleMode.ENFORCE
    return RuleResult(check_name, phase, decision, enforced, latency_us)


def run_pre_trade(
    checks: Sequence[PreTradeCheck], ctx: RiskContext, order: Order
) -> list[RuleResult]:
    """Run every check on ``order`` (all of them, even after one rejects, so the audit shows each
    rule's own view). A check that raises counts as REJECT, naming the check."""
    return [
        _run(
            check.name, Phase.PRE_TRADE, ctx, Severity.REJECT, partial(check.pre_trade, ctx, order)
        )
        for check in checks
    ]


def run_continuous(
    checks: Sequence[ContinuousCheck], ctx: RiskContext, event: RiskTrigger
) -> list[RuleResult]:
    """Run every continuous check. One that raises counts as HALT_NEW_ENTRIES: the account cannot
    be watched, so it must stop taking new risk."""
    return [
        _run(
            check.name,
            Phase.CONTINUOUS,
            ctx,
            Severity.HALT_NEW_ENTRIES,
            partial(check.continuous, ctx, event),
        )  # fmt: skip
        for check in checks
    ]
