"""The plain records the risk repository stores: what they refuse at construction (task 2.3)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from contracts.orders import OrderSide
from contracts.snapshot import HaltLevel
from engine.risk.repository import RiskEvent, StoredLimits

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)


def event(**overrides: object) -> RiskEvent:
    values: dict[str, object] = {
        "at": NOW,
        "account_id": "main",
        "rule": "gate",
        "phase": "verdict",
        "decision": "ALLOW",
        "enforced": True,
        "latency_us": 5,
    }
    return RiskEvent(**{**values, **overrides})  # type: ignore[arg-type]


def test_audit_inputs_of_every_json_safe_kind_are_accepted() -> None:
    kept = event(inputs={"risk": "1300.00", "count": 3, "shadow": True, "note": None})

    assert kept.inputs["risk"] == "1300.00"


@pytest.mark.parametrize("bad", [Decimal("1300.00"), 1300.5, ["a"], {"a": 1}, b"x"])
def test_an_audit_input_that_is_not_flat_json_is_refused(bad: object) -> None:
    """A Decimal would lose the record at JSON time and a float would be stored silently."""
    with pytest.raises(TypeError, match="risk"):
        event(inputs={"risk": bad})


def test_a_naive_timestamp_is_refused() -> None:
    naive = datetime(2026, 11, 16, 5, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        event(at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        StoredLimits({"a": 1}, 1, naive)


def test_each_event_gets_its_own_inputs_dict() -> None:
    first, second = event(), event()

    assert first.inputs is not second.inputs


def test_enum_values_are_accepted_as_audit_inputs() -> None:
    """Rules will log ``order.side`` and halt levels as they are: JSON stores a StrEnum as its
    text and an IntEnum as its number, so an exact-type check would wrongly refuse them."""
    kept = event(inputs={"side": OrderSide.BUY, "level": HaltLevel.KILL})

    assert kept.inputs["side"] == "BUY" and kept.inputs["level"] == 30
