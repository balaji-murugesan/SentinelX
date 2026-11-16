"""The pre-trade rules the gate runs, in the order they are registered (add-risk-engine group 6).

The order is the tie-break: when two rules refuse an order with the same severity, the one
registered first gives the reason that is stored, so the most basic problems (no stop, no fresh
price) are named before the limits. ``add-risk-guards`` adds its rules by appending here.
"""

from engine.risk.checks.counters import Cooldown, MaxTrades
from engine.risk.checks.exposure import EntryCutoff, FloorHeadroom, OpenPositions
from engine.risk.checks.prices import PriceSanity, StaleQuote
from engine.risk.checks.stops import InitialStop, PerTradeRisk, StopDistance
from engine.risk.decision import PreTradeCheck


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
    ]
