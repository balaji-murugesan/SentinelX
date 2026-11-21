"""The risk rules the engine runs, in the order they are registered (add-risk-engine group 6, 7).

The order is the tie-break: when two rules refuse an order with the same severity, the one
registered first gives the reason that is stored, so the most basic problems (no stop, no fresh
price) are named before the limits. ``add-risk-guards`` adds its rules by appending here.
``default_checks`` are the pre-trade rules the gate runs; ``continuous_checks`` are what the
monitor runs on its timer. ``engine.risk.runtime`` builds the engine from these two lists and
nothing else, and a test pins that their names match the rule table in ``engine.risk.limits``.
"""

from engine.risk.checks.counters import Cooldown, MaxTrades
from engine.risk.checks.exposure import EntryCutoff, FloorHeadroom, OpenPositions
from engine.risk.checks.flow import EntryPacing, RejectBreaker
from engine.risk.checks.lots import LotCaps
from engine.risk.checks.prices import PriceSanity, StaleQuote
from engine.risk.checks.protection import Unprotected
from engine.risk.checks.stale import FeedDown, FloorBreach, MtmBlind
from engine.risk.checks.stops import InitialStop, PerTradeRisk, StopDistance
from engine.risk.decision import ContinuousCheck, PreTradeCheck


def default_checks() -> list[PreTradeCheck]:
    return [
        InitialStop(),
        StaleQuote(),
        PriceSanity(),
        PerTradeRisk(),
        StopDistance(),
        MaxTrades(),
        Cooldown(),
        OpenPositions(),
        EntryCutoff(),
        FloorHeadroom(),
        # add-risk-guards, appended (so the rules above keep their tie-break order):
        LotCaps(),
        EntryPacing(),
        RejectBreaker(),
    ]


def continuous_checks() -> list[ContinuousCheck]:
    """The checks the monitor runs on its timer, in the order they break a tie: the floor first
    (a KILL outranks everything anyway), then a silent feed, a blind book, an unprotected
    position and (add-risk-guards, appended) the rejection breaker."""
    return [FloorBreach(), FeedDown(), MtmBlind(), Unprotected(), RejectBreaker()]
