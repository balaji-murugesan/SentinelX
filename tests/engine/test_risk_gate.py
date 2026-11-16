"""The real gate, ``RiskEngine`` (add-risk-engine task 5.3; risk-gate spec).

Scenarios: "Live order before a live adapter", "Paper order judged by rules", "Exit after the
cutoff on a halted day", "Postgres down", "Shadow rule would reject", "Why didn't it trade",
"Rejection reason", "Most severe wins" and "A check crashes before a trade", at gate level. Plus the
fail-closed ladder for entries (unbound, no limits, unusable limits, state, day lock, standing
halt) and the guarantee that none of it refuses an exit.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from contracts.commands import LimitValue
from contracts.mode import TradingMode
from contracts.orders import Order, OrderSide
from contracts.snapshot import HaltLevel
from engine.persistence.repository import PersistenceError
from engine.positions.book import position_id
from engine.risk.audit import RiskAudit
from engine.risk.decision import (
    ALLOW,
    CHECK_ERROR,
    Decision,
    RiskContext,
    Severity,
    halt_new_entries,
    kill,
    reject,
)
from engine.risk.gate import RiskEngine
from engine.risk.limits import MODE_PREFIX, parse_limits
from engine.risk.limits_service import LimitsService
from tests.engine.risk_builders import (
    NOW,
    OPTION,
    day_state,
    instrument_of,
    order,
    position,
)
from tests.engine.risk_fakes import MemoryRiskRepository, StubState, StubTrading
from tests.engine.test_risk_limits import BASE
from tests.engine.trading_fakes import MemoryRepository

BUY, SELL = OrderSide.BUY, OrderSide.SELL


class Rule:
    """A check with a fixed decision; records the contexts and orders it was asked about."""

    def __init__(
        self,
        name: str,
        decision: Decision = ALLOW,
        *,
        applies_to_exits: bool = False,
        crash: bool = False,
    ) -> None:
        self.name = name
        self.applies_to_exits = applies_to_exits
        self._decision = decision
        self._crash = crash
        self.asked: list[tuple[RiskContext, Order]] = []

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        self.asked.append((ctx, order))
        if self._crash:
            raise ZeroDivisionError("boom")
        return self._decision


class Harness:
    def __init__(
        self,
        *,
        mode: TradingMode = TradingMode.PAPER,
        checks: list[Rule] | None = None,
        limits_values: dict[str, LimitValue] | None = BASE,
        bind: bool = True,
        positions: list[Any] | None = None,
        state: StubState | None = None,
        on_kill: Callable[[Decision], None] | None = None,
    ) -> None:
        self.repo = MemoryRiskRepository()
        self.limits = LimitsService(
            account_id="main", repository=self.repo, results=MemoryRepository(),
            figures=lambda: (Decimal(0), None), now=lambda: NOW,
        )  # fmt: skip
        self.limits_values = limits_values
        self.audit = RiskAudit(self.repo)
        self.state = state or StubState(day_state())
        self.trading = StubTrading({position_id(p): p for p in positions or []})
        self.checks = checks if checks is not None else []
        self.gate = RiskEngine(
            account_id="main", mode=mode, limits=self.limits, state=self.state, audit=self.audit,
            instruments=instrument_of, checks=self.checks, on_kill=on_kill, now=lambda: NOW,
        )  # fmt: skip
        if bind:
            self.gate.bind(self.trading)

    async def ready(self) -> "Harness":
        """Load the limits (set the first time through the command path, like production)."""
        if self.limits_values is not None:
            from contracts.commands import SetRiskLimits

            await self.limits.set_limits(
                SetRiskLimits(
                    command_id="c", idempotency_key="k", account_id="main", source="t",
                    sent_at=NOW, limits=self.limits_values,
                )
            )  # fmt: skip
        else:
            await self.limits.load()
        return self

    def rows(self, rule: str | None = None) -> list[Any]:
        return [e for e in self.repo.events if rule is None or e.rule == rule]

    def verdict(self) -> Any:
        return self.rows("gate")[-1]


async def harness(**kwargs: Any) -> Harness:
    return await Harness(**kwargs).ready()


# ---- live mode -------------------------------------------------------------------------------


@pytest.mark.parametrize("paper_order", [True, False])
async def test_live_order_before_a_live_adapter_is_refused_and_no_rule_runs(
    paper_order: bool,
) -> None:
    """Spec "Live order before a live adapter": any order in live mode, entry or exit."""
    rule = Rule("per_trade_risk")
    h = await harness(mode=TradingMode.LIVE, checks=[rule], positions=[position(quantity=65)])

    entry = await h.gate.check(order(BUY, 65, paper=paper_order))
    exit_ = await h.gate.check(order(SELL, 65, paper=paper_order))

    assert (entry or "").startswith("RISK_LIVE_NOT_ENABLED")
    assert (exit_ or "").startswith("RISK_LIVE_NOT_ENABLED")
    assert rule.asked == []
    assert h.verdict().reason_code == "RISK_LIVE_NOT_ENABLED"


async def test_a_non_paper_order_in_paper_mode_is_refused() -> None:
    h = await harness()

    reason = await h.gate.check(order(BUY, 65, paper=False))

    assert (reason or "").startswith("RISK_LIVE_NOT_ENABLED")


async def test_paper_order_is_judged_by_the_rules_not_its_mode() -> None:
    """Spec "Paper order judged by rules"."""
    allow = await harness(checks=[Rule("max_trades", ALLOW)])
    deny = await harness(checks=[Rule("max_trades", reject("RISK_MAX_TRADES"))])

    assert await allow.gate.check(order(BUY, 65)) is None
    assert (await deny.gate.check(order(BUY, 65)) or "").startswith("RISK_MAX_TRADES")


# ---- fail closed for entries -----------------------------------------------------------------


async def test_an_unbound_gate_refuses_every_order() -> None:
    h = await harness(bind=False, positions=[position(quantity=65)])

    for o in (order(BUY, 65), order(SELL, 65)):
        assert (await h.gate.check(o) or "").startswith("RISK_STATE_UNAVAILABLE")


async def test_fresh_install_rejects_entries_but_accepts_exits() -> None:
    """Spec risk-limits "Fresh install" through the gate."""
    h = await harness(limits_values=None, positions=[position(quantity=65)])

    entry = await h.gate.check(order(BUY, 65))
    exit_ = await h.gate.check(order(SELL, 65))

    assert (entry or "").startswith("RISK_LIMITS_NOT_SET")
    assert exit_ is None
    assert "skipped" in h.verdict().inputs  # the exit was not judged, and the audit says so


async def test_unusable_limits_reject_entries_with_their_own_code() -> None:
    h = Harness(limits_values=None)
    await h.repo.save_limits("main", {**BASE, "price_band_pct": "-5"}, NOW)  # type: ignore[arg-type]
    await h.limits.load()

    assert (await h.gate.check(order(BUY, 65)) or "").startswith("RISK_LIMITS_INVALID")


async def test_postgres_down_rejects_entries_with_state_unavailable() -> None:
    """Spec "Postgres down"."""
    state = StubState(day_state(), unavailable="risk state cannot be loaded")
    h = await harness(state=state, positions=[position(quantity=65)])

    entry = await h.gate.check(order(BUY, 65))
    exit_ = await h.gate.check(order(SELL, 65))

    assert (entry or "").startswith("RISK_STATE_UNAVAILABLE")
    assert "cannot be loaded" in (entry or "")
    assert exit_ is None


async def test_exit_after_the_cutoff_on_a_halted_day_is_accepted() -> None:
    """Spec "Exit after the cutoff on a halted day": the entry is locked out, the exit is not."""
    halted = day_state(halt_level=HaltLevel.KILL, halt_reason="equity floor breached")
    rule = Rule("entry_cutoff", reject("RISK_ENTRY_CUTOFF"))
    h = await harness(state=StubState(halted), checks=[rule], positions=[position(quantity=65)])

    exit_ = await h.gate.check(order(SELL, 65))
    entry = await h.gate.check(order(BUY, 65))

    assert exit_ is None
    assert (entry or "").startswith("RISK_HALTED_DAY") and "equity floor" in (entry or "")
    assert rule.asked == []  # entry rules never saw the exit, nor the locked-out entry


async def test_a_standing_halt_such_as_a_silent_feed_rejects_entries_only() -> None:
    state = StubState(day_state(), entry_halt=halt_new_entries("RISK_FEED_DOWN", "no ticks"))
    h = await harness(state=state, positions=[position(quantity=65)])

    assert (await h.gate.check(order(BUY, 65)) or "").startswith("RISK_FEED_DOWN")
    assert await h.gate.check(order(SELL, 65)) is None


# ---- the rules -------------------------------------------------------------------------------


async def test_the_most_severe_enforced_rule_decides_the_reason() -> None:
    """Spec "Most severe wins" through the gate."""
    h = await harness(
        checks=[
            Rule("per_trade_risk", reject("RISK_PER_TRADE")),
            Rule("equity_floor", kill("RISK_FLOOR_BREACH")),
        ]
    )

    reason = await h.gate.check(order(BUY, 65))

    assert (reason or "").startswith("RISK_FLOOR_BREACH")
    assert h.verdict().decision == "KILL" and h.verdict().inputs["decided_by"] == "equity_floor"


async def test_a_kill_from_a_pre_trade_rule_is_requested_as_well_as_refused() -> None:
    """The engine acts on the most severe decision wherever it was seen (task 10.3)."""
    asked: list[Decision] = []
    h = await harness(
        checks=[Rule("per_trade_risk", reject("RISK_PER_TRADE")), Rule("floor", kill("RISK_X"))],
        on_kill=asked.append,
    )

    reason = await h.gate.check(order(BUY, 65))

    assert (reason or "").startswith("RISK_X")
    assert [d.code for d in asked] == ["RISK_X"]  # once, with the decision that won


@pytest.mark.parametrize(
    "decision", [reject("RISK_A"), halt_new_entries("RISK_B"), ALLOW], ids=["reject", "halt", "ok"]
)
async def test_a_decision_below_kill_requests_no_kill(decision: Decision) -> None:
    asked: list[Decision] = []
    h = await harness(checks=[Rule("r", decision)], on_kill=asked.append)

    await h.gate.check(order(BUY, 65))

    assert asked == []


async def test_a_shadow_rule_that_would_kill_requests_no_kill() -> None:
    asked: list[Decision] = []
    h = await harness(
        checks=[Rule("per_trade_risk", kill("RISK_X"))],
        limits_values={**BASE, MODE_PREFIX + "per_trade_risk": "shadow"},
        on_kill=asked.append,
    )

    reason = await h.gate.check(order(BUY, 65))

    assert reason is None and asked == []  # logged and audited, not acted on


async def test_an_exit_rule_that_returns_kill_requests_it_too() -> None:
    asked: list[Decision] = []
    h = await harness(
        checks=[Rule("exit_rule", kill("RISK_X"), applies_to_exits=True)],
        positions=[position(quantity=65)],
        on_kill=asked.append,
    )

    await h.gate.check(order(SELL, 65))

    assert [d.code for d in asked] == ["RISK_X"]


async def test_a_kill_hook_that_raises_changes_nothing_about_the_verdict() -> None:
    def broken(decision: Decision) -> None:
        raise RuntimeError("could not schedule")

    h = await harness(checks=[Rule("floor", kill("RISK_X"))], on_kill=broken)

    reason = await h.gate.check(order(BUY, 65))

    assert (reason or "").startswith("RISK_X")  # still refused, and the gate did not fail


async def test_a_check_that_crashes_rejects_an_entry_naming_the_check() -> None:
    h = await harness(checks=[Rule("per_trade_risk", crash=True)])

    reason = await h.gate.check(order(BUY, 65))

    assert (reason or "").startswith(CHECK_ERROR) and "per_trade_risk" in (reason or "")


async def test_every_rule_sees_the_same_context_with_the_gates_view_of_the_book() -> None:
    held = position(quantity=65)
    a, b = Rule("max_trades"), Rule("cooldown")
    h = await harness(checks=[a, b], positions=[held])

    await h.gate.check(order(BUY, 65, key=OPTION, order_id="x"))

    (ctx, seen), (ctx_b, _) = a.asked[0], b.asked[0]
    assert ctx is ctx_b and ctx.limits == parse_limits(BASE) and ctx.account_id == "main"
    assert position_id(held) in ctx.positions and seen.order_id == "x" and ctx.now == NOW


async def test_shadow_rule_would_reject_the_order_proceeds_and_the_audit_says_unenforced() -> None:
    """Spec "Shadow rule would reject"."""
    values = {**BASE, MODE_PREFIX + "per_trade_risk": "shadow"}
    h = await harness(
        limits_values=values, checks=[Rule("per_trade_risk", reject("RISK_PER_TRADE"))]
    )

    assert await h.gate.check(order(BUY, 65)) is None

    [row] = h.rows("per_trade_risk")
    assert (row.decision, row.reason_code, row.enforced) == ("REJECT", "RISK_PER_TRADE", False)
    assert h.verdict().decision == "ALLOW"


async def test_why_didnt_it_trade_a_rule_row_carries_the_code_and_its_inputs() -> None:
    """Spec "Why didn't it trade"."""
    decision = reject("RISK_PER_TRADE", "risk above limit", risk="1300.00", limit="1000")
    h = await harness(checks=[Rule("per_trade_risk", decision)])

    await h.gate.check(order(BUY, 65, order_id="o-77"))

    [row] = h.rows("per_trade_risk")
    assert row.reason_code == "RISK_PER_TRADE" and row.phase == "pre_trade" and row.enforced
    assert row.inputs["risk"] == "1300.00" and row.inputs["limit"] == "1000"
    assert row.inputs["order_id"] == "o-77" and row.latency_us >= 0


async def test_rejection_reason_begins_with_its_code() -> None:
    """Spec "Rejection reason": ``OrderService`` stores this string as the order's reason."""
    h = await harness(checks=[Rule("entry_cutoff", reject("RISK_ENTRY_CUTOFF", "after 15:00"))])

    assert await h.gate.check(order(BUY, 65)) == "RISK_ENTRY_CUTOFF: after 15:00"


# ---- exits -----------------------------------------------------------------------------------


async def test_entry_rules_never_see_an_exit() -> None:
    entry_rules = [
        Rule(name, decision)
        for name, decision in [
            ("max_trades", reject("RISK_MAX_TRADES")),
            ("cooldown", halt_new_entries("RISK_COOLDOWN")),
            ("equity_floor", kill("RISK_FLOOR_BREACH")),
        ]
    ]
    h = await harness(checks=entry_rules, positions=[position(quantity=65)])

    assert await h.gate.check(order(SELL, 65)) is None
    assert all(rule.asked == [] for rule in entry_rules)


async def test_a_rule_that_applies_to_exits_can_still_refuse_one() -> None:
    sanity = Rule("price_sanity", reject("RISK_PRICE_SANITY"), applies_to_exits=True)
    other = Rule("max_trades", reject("RISK_MAX_TRADES"))
    h = await harness(checks=[sanity, other], positions=[position(quantity=65)])

    reason = await h.gate.check(order(SELL, 65))

    assert (reason or "").startswith("RISK_PRICE_SANITY")
    assert sanity.asked and other.asked == []


async def test_a_crashing_exit_rule_never_blocks_the_exit_but_is_audited() -> None:
    crashing = Rule("price_sanity", applies_to_exits=True, crash=True)
    h = await harness(checks=[crashing], positions=[position(quantity=65)])

    assert await h.gate.check(order(SELL, 65)) is None

    [row] = h.rows("price_sanity")
    assert row.reason_code == CHECK_ERROR and h.verdict().decision == "ALLOW"


async def test_a_flip_is_judged_as_an_entry() -> None:
    rule = Rule("max_trades", reject("RISK_MAX_TRADES"))
    h = await harness(checks=[rule], positions=[position(quantity=65)])

    assert (await h.gate.check(order(SELL, 130)) or "").startswith("RISK_MAX_TRADES")


# ---- audit -----------------------------------------------------------------------------------


async def test_every_verdict_is_audited_including_allow() -> None:
    h = await harness(checks=[Rule("max_trades", ALLOW)])

    await h.gate.check(order(BUY, 65, order_id="o-1"))

    [verdict] = h.rows("gate")
    assert (verdict.phase, verdict.decision, verdict.reason_code) == ("verdict", "ALLOW", None)
    assert verdict.inputs["intent"] == "ENTRY" and verdict.inputs["order_id"] == "o-1"
    assert verdict.inputs["instrument"] == str(OPTION) and verdict.inputs["quantity"] == 65
    assert verdict.at.tzinfo is not None and verdict.account_id == "main" and verdict.enforced
    assert h.rows("max_trades") == []  # a plain ALLOW of a rule is not a row of its own


async def test_a_refusal_is_audited_with_its_code_and_the_intent() -> None:
    h = await harness(limits_values=None)

    await h.gate.check(order(BUY, 65))

    assert (h.verdict().decision, h.verdict().reason_code) == ("REJECT", "RISK_LIMITS_NOT_SET")


async def test_an_allow_with_a_code_is_audited_as_a_notice() -> None:
    notice = Decision(Severity.ALLOW, "RISK_PRICE_UNVERIFIED", "no fresh price")
    h = await harness(
        checks=[Rule("price_sanity", notice, applies_to_exits=True)],
        positions=[position(quantity=65)],
    )

    assert await h.gate.check(order(SELL, 65)) is None

    [row] = h.rows("price_sanity")
    assert row.decision == "ALLOW" and row.reason_code == "RISK_PRICE_UNVERIFIED"


async def test_a_failing_audit_never_blocks_an_exit_or_changes_a_verdict() -> None:
    class AuditDown(MemoryRiskRepository):
        async def append_event(self, event: Any) -> None:
            raise PersistenceError("audit down")

    h = await harness(positions=[position(quantity=65)])
    h.audit = RiskAudit(AuditDown())
    h.gate = RiskEngine(
        account_id="main", mode=TradingMode.PAPER, limits=h.limits, state=h.state,
        audit=h.audit, instruments=instrument_of, checks=[Rule("max_trades", reject("X"))],
        now=lambda: NOW,
    )  # fmt: skip
    h.gate.bind(h.trading)

    assert await h.gate.check(order(SELL, 65)) is None  # the exit
    assert (await h.gate.check(order(BUY, 65)) or "").startswith("X")  # the entry, same answer
    assert h.audit.pending >= 2  # queued for the retry loop


async def test_a_malformed_audit_record_never_blocks_an_exit_or_the_verdict_row() -> None:
    """A rule's notice with a Decimal input fails ``RiskEvent``'s own check when it is built. The
    exit still goes through and the verdict row is still written; only the bad row is lost."""
    bad = Decision(Severity.ALLOW, "RISK_PRICE_UNVERIFIED", "x", {"price": Decimal("1.5")})  # type: ignore[dict-item]
    h = await harness(
        checks=[Rule("price_sanity", bad, applies_to_exits=True)],
        positions=[position(quantity=65)],
    )

    assert await h.gate.check(order(SELL, 65)) is None

    assert h.rows("price_sanity") == []  # the malformed row was not stored
    assert h.verdict().decision == "ALLOW"  # but the verdict was


# ---- the gate failing internally -------------------------------------------------------------


class BrokenState(StubState):
    """A risk state whose day figures cannot be read (a bug, say)."""

    def day(self) -> Any:
        raise RuntimeError("state bug")


async def test_a_gate_that_fails_internally_refuses_an_entry_and_lets_an_exit_through() -> None:
    h = await harness(state=BrokenState(day_state()), positions=[position(quantity=65)])

    entry = await h.gate.check(order(BUY, 65))
    exit_ = await h.gate.check(order(SELL, 65))

    assert (entry or "").startswith("RISK_STATE_UNAVAILABLE") and "RuntimeError" in (entry or "")
    assert exit_ is None
    assert h.verdict().inputs["gate_error"] == "RuntimeError"  # and the audit says why


def test_a_gate_cannot_be_built_without_its_rules() -> None:
    """A gate with no rules would allow every entry, so ``checks`` has no default."""
    import pytest

    h = Harness()
    with pytest.raises(TypeError, match="checks"):
        RiskEngine(  # type: ignore[call-arg]
            account_id="main", mode=TradingMode.PAPER, limits=h.limits, state=h.state,
            audit=h.audit, instruments=instrument_of, now=lambda: NOW,
        )  # fmt: skip


async def test_an_unbound_gate_says_it_is_not_connected() -> None:
    h = await harness(bind=False)

    reason = await h.gate.check(order(BUY, 65))

    assert "not connected to trading state" in (reason or "")


# ---- review round 1 --------------------------------------------------------------------------


async def test_any_persisted_halt_refuses_entries_not_only_a_kill() -> None:
    """The MTM-blind halt (flatten, no entries for the rest of the day) locks the day too."""
    blind = day_state(halt_level=HaltLevel.MTM_BLIND, halt_reason="prices unknown too long")
    h = await harness(state=StubState(blind), positions=[position(quantity=65)])

    entry = await h.gate.check(order(BUY, 65))
    exit_ = await h.gate.check(order(SELL, 65))

    assert (entry or "").startswith("RISK_HALTED_DAY") and "prices unknown" in (entry or "")
    assert exit_ is None


async def test_an_exit_that_could_not_be_judged_says_why_in_the_audit() -> None:
    state = StubState(day_state(), unavailable="risk state cannot be loaded")
    h = await harness(state=state, positions=[position(quantity=65)])

    assert await h.gate.check(order(SELL, 65)) is None

    assert h.verdict().inputs["skipped"] == "risk state cannot be loaded"


async def test_an_order_refused_before_it_was_classified_is_audited_as_unclassified() -> None:
    live = await harness(mode=TradingMode.LIVE, positions=[position(quantity=65)])
    unbound = await harness(bind=False)

    await live.gate.check(order(SELL, 65))
    await unbound.gate.check(order(SELL, 65))

    assert live.verdict().inputs["intent"] == "UNCLASSIFIED"
    assert unbound.verdict().inputs["intent"] == "UNCLASSIFIED"


async def test_a_standing_halt_is_audited_with_its_own_severity() -> None:
    halt = halt_new_entries("RISK_FEED_DOWN", "no ticks for 12 s")
    h = await harness(state=StubState(day_state(), entry_halt=halt))

    await h.gate.check(order(BUY, 65))

    [row] = h.rows("standing_halt")
    assert (row.decision, row.reason_code, row.enforced) == (
        "HALT_NEW_ENTRIES",
        "RISK_FEED_DOWN",
        True,
    )
    assert h.verdict().decision == "HALT_NEW_ENTRIES"
    assert h.verdict().inputs["decided_by"] == "standing_halt"


# ---- counting the allowed entry (task 6.4) ---------------------------------------------------


async def test_an_allowed_entry_is_counted_once_and_a_refused_one_or_an_exit_is_not() -> None:
    state = StubState(day_state())
    h = await harness(
        state=state, checks=[Rule("max_trades", ALLOW)], positions=[position(quantity=65)]
    )

    assert await h.gate.check(order(BUY, 65)) is None
    assert state.entries_counted == 1  # an allowed entry
    assert await h.gate.check(order(SELL, 65)) is None
    assert state.entries_counted == 1  # an exit is not an entry

    refused = await harness(
        state=StubState(day_state()), checks=[Rule("max_trades", reject("RISK_MAX_TRADES"))]
    )
    assert await refused.gate.check(order(BUY, 65)) is not None
    assert refused.state.entries_counted == 0  # type: ignore[attr-defined]  # a refused entry


async def test_an_entry_whose_count_cannot_be_saved_is_refused_after_the_rules_allowed_it() -> None:
    state = StubState(day_state())
    state.count_fails = "the entry count could not be saved: db down"
    h = await harness(state=state, checks=[Rule("max_trades", ALLOW)])

    reason = await h.gate.check(order(BUY, 65))

    assert (reason or "").startswith("RISK_STATE_UNAVAILABLE") and "count" in (reason or "")
    assert h.verdict().reason_code == "RISK_STATE_UNAVAILABLE"
