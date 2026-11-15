"""The snapshot's risk block (add-risk-engine task 1.2).

The snapshot is what the CLI now, and the dashboard later, read: every figure is computed by the
engine. The risk block is optional, so a snapshot written before add-risk-engine still parses.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.market import InstrumentKey
from contracts.orders import ProductType
from contracts.snapshot import HaltLevel, PositionRef, RiskStatus, Snapshot

NOW = datetime(2026, 11, 16, 5, 30, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")


def snapshot(**overrides: object) -> Snapshot:
    values: dict[str, object] = {
        "account_id": "main",
        "taken_at": NOW,
        "open_orders": [],
        "positions": [],
    }
    return Snapshot(**{**values, **overrides})  # type: ignore[arg-type]


def test_a_snapshot_from_before_the_risk_engine_still_parses() -> None:
    old = (
        '{"schema_version":1,"account_id":"main","taken_at":"2026-11-16T05:30:00Z",'
        '"open_orders":[],"positions":[],"halted":false,"halt_differences":[]}'
    )
    parsed = Snapshot.model_validate_json(old)
    assert parsed.risk is None and parsed.halted is False


def test_the_risk_block_round_trips() -> None:
    risk = RiskStatus(
        mtm_day=Decimal("-510.00"),
        floor=Decimal("-3000"),
        peak=Decimal("1200.50"),
        halt_level=HaltLevel.KILL,
        halt_reason="equity floor breached",
        entries_today=4,
        loss_streak=2,
        cooldown_until=NOW,
        unprotected=[PositionRef(key=OPTION, product=ProductType.CNC)],
    )
    built = snapshot(risk=risk)
    again = Snapshot.model_validate_json(built.model_dump_json())
    assert again == built
    assert again.risk is not None
    assert again.risk.mtm_day == Decimal("-510.00")  # exact decimals, never a float
    assert again.risk.unprotected[0].product is ProductType.CNC


def test_unknown_figures_stay_none_never_a_guessed_zero() -> None:
    risk = RiskStatus()
    assert risk.mtm_day is None and risk.floor is None
    assert risk.halt_level is HaltLevel.NONE and risk.peak == Decimal("0")
    assert risk.unprotected == [] and risk.cooldown_until is None


def test_a_snapshot_from_before_the_risk_engine_with_orders_and_positions_still_parses() -> None:
    old = (
        '{"schema_version":1,"account_id":"main","taken_at":"2026-11-16T05:30:00Z",'
        '"open_orders":[{"schema_version":1,"order_id":"o1","account_id":"main",'
        '"key":{"schema_version":1,"segment":"NSE_FNO","security_id":"53720"},"side":"BUY",'
        '"quantity":65,"product":"INTRADAY","order_type":"LIMIT","limit_price":"190.00",'
        '"idempotency_key":"k1","source":"cli","state":"Open","filled_quantity":0,'
        '"average_fill_price":null,"broker_order_id":"paper-1","paper":true,"reason":null,'
        '"created_at":"2026-11-16T05:00:00Z","updated_at":"2026-11-16T05:00:00Z"}],'
        '"positions":[{"position":{"schema_version":1,"account_id":"main",'
        '"key":{"schema_version":1,"segment":"NSE_FNO","security_id":"53720"},'
        '"product":"INTRADAY","quantity":65,"average_price":"200.00","realized_pnl":"0",'
        '"costs":"30.00","updated_at":"2026-11-16T05:10:00Z"},'
        '"last_price":"201.00","mtm":"65.00","net_pnl":"35.00"}],'
        '"halted":false,"halt_differences":[]}'
    )
    parsed = Snapshot.model_validate_json(old)
    assert parsed.risk is None
    assert parsed.open_orders[0].initial_stop is None
    assert parsed.positions[0].net_pnl == Decimal("35.00")


def test_halt_level_numbers_are_a_stored_contract() -> None:
    """The values are persisted with GREATEST (task 2.3), so they must not change. 20 stays free
    for add-risk-guards' profit-target halt, ranked between MTM_BLIND and KILL."""
    numbers = {level.name: int(level) for level in HaltLevel}
    assert numbers == {"NONE": 0, "MTM_BLIND": 10, "KILL": 30}
    assert 10 < 20 < 30
    for level, number in ((HaltLevel.NONE, 0), (HaltLevel.MTM_BLIND, 10), (HaltLevel.KILL, 30)):
        assert RiskStatus(halt_level=level).model_dump(mode="json")["halt_level"] == number


def test_halt_level_ranks_by_severity_and_refuses_unknown_numbers() -> None:
    assert max(HaltLevel.NONE, HaltLevel.KILL, HaltLevel.MTM_BLIND) is HaltLevel.KILL
    for unknown in (7, 20, 2):  # 20 is reserved, not yet a level
        with pytest.raises(ValidationError):
            RiskStatus(halt_level=unknown)  # type: ignore[arg-type]


def test_unknown_risk_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        RiskStatus(floor_is_editable=True)  # type: ignore[call-arg]
