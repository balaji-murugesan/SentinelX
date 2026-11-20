"""The real gate inside the real ``OrderService`` (add-risk-engine task 5.3): a gate refusal is
final, its stored reason starts with the code, nothing reaches the broker, and an exit passes a
halted day. The gate's own rules are tested in ``test_risk_gate``; this is the seam.
"""

from contracts.mode import TradingMode
from contracts.orders import OrderSide, OrderState
from contracts.snapshot import HaltLevel
from engine.positions.book import position_id
from engine.risk.decision import reject
from tests.engine.risk_builders import day_state, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_order_service import build, command
from tests.engine.test_risk_gate import Rule, harness


async def test_live_mode_through_the_real_gate_is_final_and_never_reaches_the_broker() -> None:
    """Specs "Live order before a live adapter" and "Rejection reason"."""
    h = await harness(mode=TradingMode.LIVE, bind=False)
    service, adapter, repo = build(mode=TradingMode.LIVE, gate=h.gate)
    h.gate.bind(service)

    result = await service.place(command())

    assert not result.accepted and (result.reason or "").startswith("RISK_LIVE_NOT_ENABLED")
    assert adapter.placed == []  # zero broker calls
    stored = repo.orders[result.order_id or ""]
    assert stored.state is OrderState.REJECTED and stored.reason == result.reason
    assert h.verdict().reason_code == "RISK_LIVE_NOT_ENABLED"


async def test_a_rule_rejection_is_stored_with_its_code_and_never_reaches_the_broker() -> None:
    h = await harness(
        bind=False, checks=[Rule("entry_cutoff", reject("RISK_ENTRY_CUTOFF", "after 15:00"))]
    )
    service, adapter, repo = build(gate=h.gate)
    h.gate.bind(service)

    result = await service.place(command())

    assert not result.accepted
    stored = repo.orders[result.order_id or ""]
    assert stored.state is OrderState.REJECTED
    assert (stored.reason or "").startswith("RISK_ENTRY_CUTOFF")
    assert adapter.placed == []


async def test_a_paper_order_that_the_rules_allow_reaches_the_broker() -> None:
    h = await harness(bind=False, checks=[Rule("max_trades")])
    service, adapter, repo = build(gate=h.gate)
    h.gate.bind(service)

    result = await service.place(command())

    assert result.accepted and len(adapter.placed) == 1
    assert repo.orders[result.order_id or ""].state is OrderState.OPEN
    assert h.verdict().decision == "ALLOW"


async def test_the_gate_reads_the_services_own_positions_to_tell_an_exit_from_an_entry() -> None:
    """A halted day locks entries out; the exit that only reduces the position still goes."""
    halted = StubState(day_state(halt_level=HaltLevel.KILL, halt_reason="kill"))
    h = await harness(
        bind=False, state=halted, checks=[Rule("max_trades", reject("RISK_MAX_TRADES"))]
    )
    service, adapter, _ = build(gate=h.gate)
    h.gate.bind(service)
    held = position(quantity=65)
    service.positions[position_id(held)] = held

    entry = await service.place(command("entry", side=OrderSide.BUY, quantity=65))
    exit_ = await service.place(command("exit", side=OrderSide.SELL, quantity=65))

    assert not entry.accepted and (entry.reason or "").startswith("RISK_HALTED_DAY")
    assert exit_.accepted and len(adapter.placed) == 1
    assert adapter.placed[0].side is OrderSide.SELL
