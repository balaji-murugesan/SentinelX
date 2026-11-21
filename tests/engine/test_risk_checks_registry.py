"""The registered pre-trade rules (add-risk-engine group 6): each is named in the limits' rule
table, exactly one applies to exits, and the order is the tie-break."""

from engine.risk.checks.registry import default_checks
from engine.risk.limits import RULES


def test_every_registered_rule_is_a_rule_the_limits_know_by_name() -> None:
    names = [check.name for check in default_checks()]

    assert len(names) == len(set(names))  # one check per name
    assert set(names) <= set(RULES)  # so each has a mode and a shadow rule


# add-risk-guards added these rule names to the limits (task 3.1) before their checks exist
# (groups 5-9). Each group that registers a check removes its name here, so a rule left without a
# check, or a check whose name is still listed, fails the test below.
NOT_BUILT_YET = {"reject_breaker", "profit_target", "instrument_policy"}


def test_every_rule_in_the_limits_table_is_registered() -> None:
    assert NOT_BUILT_YET <= set(RULES)
    assert {check.name for check in default_checks()} == set(RULES) - NOT_BUILT_YET


def test_only_price_sanity_judges_exits() -> None:
    assert [c.name for c in default_checks() if c.applies_to_exits] == ["price_sanity"]


def test_the_most_basic_problems_are_named_first() -> None:
    names = [check.name for check in default_checks()]

    assert names.index("initial_stop") < names.index("per_trade_risk")
    assert names.index("stale_data") < names.index("price_sanity")
    # add-risk-guards appends its rules after the floor headroom, the last add-risk-engine rule,
    # so the earlier rules keep their tie-break order.
    assert names.index("equity_floor") < names.index("lot_caps") < names.index("entry_pacing")
