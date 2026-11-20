"""Decisions, severity ranking and running checks (add-risk-engine task 5.2; risk-gate spec).

Scenarios: "Most severe wins" and "A check crashes before a trade"; a continuous-check crash is
HALT_NEW_ENTRIES; a shadow rule is audited with ``enforced = False`` and treated as ALLOW.
"""

from dataclasses import replace

from contracts.orders import Order, OrderSide
from engine.risk.decision import (
    ALLOW,
    CHECK_ERROR,
    Decision,
    Phase,
    RiskContext,
    RiskTrigger,
    RuleResult,
    Severity,
    halt_new_entries,
    kill,
    most_severe,
    reject,
    run_continuous,
    run_pre_trade,
)
from engine.risk.limits import MODE_PREFIX, RuleMode
from tests.engine.risk_builders import LIMITS, context, order


class FixedCheck:
    """A pre-trade and continuous check that always returns the same decision."""

    applies_to_exits = False

    def __init__(self, name: str, decision: Decision) -> None:
        self.name = name
        self._decision = decision

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        return self._decision

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        return self._decision


class Crashing:
    name = "per_trade_risk"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        raise ZeroDivisionError("boom")

    def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
        raise ZeroDivisionError("boom")


BUY_ORDER = order(OrderSide.BUY, 65)


def test_severity_is_ranked_allow_reject_halt_target_kill() -> None:
    ranked = [
        Severity.ALLOW,
        Severity.REJECT,
        Severity.HALT_NEW_ENTRIES,
        Severity.TARGET_HALT,
        Severity.KILL,
    ]

    assert ranked == sorted(ranked)
    assert max(Severity.REJECT, Severity.KILL) is Severity.KILL


def test_a_rejection_reason_begins_with_its_code() -> None:
    assert reject("RISK_X", "too big", size="65").reason() == "RISK_X: too big"
    assert reject("RISK_X").reason() == "RISK_X"
    assert ALLOW.reason() is None
    assert reject("RISK_X", size="65").inputs == {"size": "65"}


def test_most_severe_wins() -> None:
    """Spec "Most severe wins": one check REJECTs and another says KILL."""
    checks = [
        FixedCheck("per_trade_risk", reject("RISK_PER_TRADE")),
        FixedCheck("equity_floor", kill("RISK_FLOOR_BREACH")),
        FixedCheck("max_trades", ALLOW),
    ]

    results = run_pre_trade(checks, context(), BUY_ORDER)
    winner = most_severe(results)

    assert len(results) == 3  # every check ran, so the audit shows each rule's own view
    assert winner is not None and winner.rule == "equity_floor"
    assert winner.decision.severity is Severity.KILL


def test_equal_severities_pick_the_first_registered() -> None:
    results = run_pre_trade(
        [FixedCheck("max_trades", reject("A")), FixedCheck("cooldown", reject("B"))],
        context(),
        BUY_ORDER,
    )

    winner = most_severe(results)

    assert winner is not None and winner.decision.code == "A"


def test_nothing_to_act_on_when_every_check_allows() -> None:
    results = run_pre_trade([FixedCheck("max_trades", ALLOW)], context(), BUY_ORDER)

    assert most_severe(results) is None
    assert most_severe([]) is None
    assert results[0].phase is Phase.PRE_TRADE and results[0].enforced


def test_a_check_that_crashes_before_a_trade_rejects_naming_the_check() -> None:
    """Spec "A check crashes before a trade"."""
    results = run_pre_trade([Crashing(), FixedCheck("max_trades", ALLOW)], context(), BUY_ORDER)

    crashed = results[0]
    assert crashed.decision.severity is Severity.REJECT
    assert crashed.decision.code == CHECK_ERROR
    assert "per_trade_risk" in (crashed.decision.reason() or "")
    assert "ZeroDivisionError" in (crashed.decision.reason() or "")
    assert crashed.decision.inputs == {"error": "ZeroDivisionError"}
    assert len(results) == 2  # the other check still ran
    winner = most_severe(results)
    assert winner is not None and winner.rule == "per_trade_risk"


def test_a_continuous_check_that_crashes_halts_new_entries() -> None:
    results = run_continuous([Crashing()], context(), RiskTrigger.TIMER)

    assert results[0].decision.severity is Severity.HALT_NEW_ENTRIES
    assert results[0].decision.code == CHECK_ERROR and results[0].phase is Phase.CONTINUOUS
    winner = most_severe(results)
    assert winner is not None and winner.effective is Severity.HALT_NEW_ENTRIES


def test_the_continuous_phase_passes_the_trigger_to_the_check() -> None:
    seen: list[RiskTrigger] = []

    class Spy:
        name = "stale_data"

        def continuous(self, ctx: RiskContext, event: RiskTrigger) -> Decision:
            seen.append(event)
            return halt_new_entries("RISK_FEED_DOWN")

    results = run_continuous([Spy()], context(), RiskTrigger.TICK)

    assert seen == [RiskTrigger.TICK]
    assert results[0].decision.severity is Severity.HALT_NEW_ENTRIES


def shadowed(rule: str) -> RiskContext:
    limits = replace(LIMITS, modes={rule: RuleMode.SHADOW})
    assert limits.modes == {rule: RuleMode.SHADOW} and MODE_PREFIX
    return context(limits=limits)


def test_a_shadow_rule_is_recorded_unenforced_and_treated_as_allow() -> None:
    """Spec "Shadow rule would reject"."""
    results = run_pre_trade(
        [FixedCheck("per_trade_risk", reject("RISK_PER_TRADE"))],
        shadowed("per_trade_risk"),
        BUY_ORDER,
    )

    result = results[0]
    assert result.decision.code == "RISK_PER_TRADE"  # what the rule said is kept for the audit
    assert result.enforced is False and result.effective is Severity.ALLOW
    assert most_severe(results) is None  # and it does not bind


def test_a_shadow_kill_does_not_outrank_an_enforced_reject() -> None:
    results = run_pre_trade(
        [
            FixedCheck("per_trade_risk", kill("RISK_X")),  # shadowed
            FixedCheck("max_trades", reject("RISK_MAX_TRADES")),
        ],
        shadowed("per_trade_risk"),
        BUY_ORDER,
    )

    winner = most_severe(results)

    assert winner is not None and winner.rule == "max_trades"


def test_a_crashing_shadow_rule_does_not_stop_trading() -> None:
    results = run_pre_trade([Crashing()], shadowed("per_trade_risk"), BUY_ORDER)

    assert results[0].decision.code == CHECK_ERROR and not results[0].enforced
    assert most_severe(results) is None


def test_a_rule_with_no_mode_set_is_enforced() -> None:
    results = run_pre_trade([FixedCheck("some_new_rule", reject("RISK_NEW"))], context(), BUY_ORDER)

    assert results[0].enforced and most_severe(results) is not None


def test_latency_is_recorded_in_whole_microseconds() -> None:
    result = run_pre_trade([FixedCheck("max_trades", ALLOW)], context(), BUY_ORDER)[0]

    assert isinstance(result.latency_us, int) and result.latency_us >= 0
    assert isinstance(result, RuleResult)
