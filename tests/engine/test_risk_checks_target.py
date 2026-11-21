"""The profit target (add-risk-guards task 9.1; profit-target spec "The profit target is optional
and off unless set" and "The target is reached only after consecutive confirmations").

``next_confirmations`` is the monitor's counting rule and ``ProfitTarget`` the continuous check
that reads its count. Scenarios, with the spec's own figures: "No target set", "Confirmed target",
"A dip resets the count" and "Unknown MTM resets the count". The monitor's wiring (the count in
its context, the halt and the flatten) is in test_risk_monitor.py and test_risk_runtime.py.
"""

from dataclasses import replace
from decimal import Decimal

from engine.risk.checks.registry import continuous_checks, default_checks
from engine.risk.checks.target import TARGET_HALT, ProfitTarget, next_confirmations
from engine.risk.decision import ALLOW, Decision, RiskTrigger, Severity
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import LIMITS, context

D = Decimal
TARGET = D("4000")
WITH_TARGET: RiskLimits = replace(LIMITS, daily_profit_target=TARGET, target_confirm_ticks=2)


def passes(*mtms: Decimal | None, target: Decimal | None = TARGET) -> list[int]:
    """The count after each pass that sees these MTM_day values, from zero."""
    counts: list[int] = []
    count = 0
    for mtm in mtms:
        count = next_confirmations(count, mtm, target)
        counts.append(count)
    return counts


def judge(confirmations: int, limits: RiskLimits = WITH_TARGET, mtm: str = "4010") -> Decision:
    ctx = replace(context(limits=limits, mtm_day=D(mtm)), target_confirmations=confirmations)
    return ProfitTarget().continuous(ctx, RiskTrigger.TIMER)


# ---- the counting rule -------------------------------------------------------------------------


def test_confirmed_target_two_passes_at_or_above_reach_it() -> None:
    """Spec "Confirmed target": ₹4,000 with a count of 2; passes at ₹4,050 and ₹4,010."""
    assert passes(D("4050"), D("4010")) == [1, 2]
    assert judge(2).severity is Severity.TARGET_HALT


def test_a_pass_exactly_at_the_target_counts() -> None:
    assert passes(D("4000"), D("4000")) == [1, 2]
    assert passes(D("3999.99")) == [0]  # one paisa below does not


def test_a_dip_resets_the_count() -> None:
    """Spec "A dip resets the count": ₹4,050, then ₹3,990, then ₹4,020 is not yet reached."""
    counts = passes(D("4050"), D("3990"), D("4020"))

    assert counts == [1, 0, 1]
    assert judge(counts[-1]) == ALLOW


def test_unknown_mtm_resets_the_count() -> None:
    """Spec "Unknown MTM resets the count": one pass at or above, then MTM_day unknown."""
    assert passes(D("4050"), None) == [1, 0]
    assert passes(D("4050"), None, D("4050")) == [1, 0, 1]  # counting starts again from zero


def test_no_target_set_never_counts() -> None:
    """Spec "No target set": MTM_day at any value counts nothing."""
    assert passes(D("4050"), D("1000000"), target=None) == [0, 0]
    assert next_confirmations(5, D("1000000"), None) == 0  # and a target removed resets it


# ---- the check ---------------------------------------------------------------------------------


def test_below_the_confirmation_count_the_check_allows() -> None:
    assert judge(0) == ALLOW
    assert judge(1) == ALLOW


def test_at_the_confirmation_count_it_halts_with_the_target_code() -> None:
    decision = judge(2)

    assert (decision.severity, decision.code) == (Severity.TARGET_HALT, TARGET_HALT)
    assert (decision.reason() or "").startswith("RISK_TARGET_HALT")
    assert decision.inputs == {
        "mtm_day": "4010", "target": "4000", "confirmations": 2, "needed": 2,
    }  # fmt: skip
    assert judge(3).severity is Severity.TARGET_HALT  # it holds while the count stays reached


def test_with_no_target_set_the_check_never_fires() -> None:
    """Spec "No target set": whatever the count, no profit-target halt occurs."""
    assert LIMITS.daily_profit_target is None
    assert judge(10_000, limits=LIMITS, mtm="1000000") == ALLOW


def test_the_target_ranks_above_every_entry_halt_and_below_a_kill() -> None:
    assert Severity.HALT_NEW_ENTRIES < Severity.TARGET_HALT < Severity.KILL


def test_the_target_is_a_continuous_check_only() -> None:
    """The gate never runs it (an order cannot reach the target): only the monitor does."""
    assert "profit_target" in {c.name for c in continuous_checks()}
    assert "profit_target" not in {c.name for c in default_checks()}
    assert not hasattr(ProfitTarget(), "pre_trade")
