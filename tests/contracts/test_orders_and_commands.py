"""Order and command contracts (add-order-position-core task 1.1).

Covers JSON round trips, the discriminated command union, and the orders spec scenario "Limit
price on a market order" at model level.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.commands import (
    CancelOrder,
    CommandResult,
    PlaceOrder,
    parse_command,
    reply_stream,
)
from contracts.market import InstrumentKey
from contracts.orders import (
    CostBreakdown,
    Fill,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    ProductType,
)

NOW = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")


def place(**overrides: object) -> PlaceOrder:
    values: dict[str, object] = {
        "command_id": "c1",
        "idempotency_key": "k1",
        "account_id": "main",
        "source": "cli",
        "sent_at": NOW,
        "key": OPTION,
        "side": OrderSide.BUY,
        "quantity": 65,
        "product": ProductType.INTRADAY,
        "order_type": OrderType.MARKET,
    }
    return PlaceOrder(**{**values, **overrides})  # type: ignore[arg-type]


def order(**overrides: object) -> Order:
    values: dict[str, object] = {
        "order_id": "o1",
        "account_id": "main",
        "key": OPTION,
        "side": OrderSide.BUY,
        "quantity": 65,
        "product": ProductType.INTRADAY,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("190.00"),
        "idempotency_key": "k1",
        "source": "cli",
        "paper": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Order(**{**values, **overrides})  # type: ignore[arg-type]


def test_place_command_round_trips_through_the_union() -> None:
    command = place(order_type=OrderType.LIMIT, limit_price=Decimal("190.05"))
    parsed = parse_command(command.model_dump_json())
    assert parsed == command
    assert isinstance(parsed, PlaceOrder) and parsed.limit_price == Decimal("190.05")
    assert json.loads(command.model_dump_json())["command"] == "place_order"


def test_cancel_command_round_trips_through_the_union() -> None:
    command = CancelOrder(
        command_id="c2", idempotency_key="k2", account_id="main", source="cli", sent_at=NOW,
        order_id="o1",
    )  # fmt: skip
    assert parse_command(command.model_dump_json()) == command


@pytest.mark.parametrize(
    ("order_type", "limit_price", "field"),
    [(OrderType.MARKET, Decimal("190"), "MARKET"), (OrderType.LIMIT, None, "LIMIT")],
)
def test_limit_price_on_a_market_order(
    order_type: OrderType, limit_price: Decimal | None, field: str
) -> None:
    with pytest.raises(ValidationError) as info:
        place(order_type=order_type, limit_price=limit_price)
    assert "limit_price" in str(info.value) and field in str(info.value)
    with pytest.raises(ValidationError):
        order(order_type=order_type, limit_price=limit_price)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"command": "launch_missiles"}',
        b'{"command": "place_order"}',
        place().model_dump_json().replace('"quantity":65', '"quantity":0').encode(),
        place().model_dump_json().replace("+00:00", "").replace("Z", "").encode(),  # naive time
    ],
)
def test_malformed_commands_rejected(raw: bytes) -> None:
    with pytest.raises(ValidationError):
        parse_command(raw)


def test_models_are_frozen() -> None:
    with pytest.raises(ValidationError):
        order().state = OrderState.FILLED  # type: ignore[misc]


def test_order_fill_position_result_round_trip() -> None:
    o = order(state=OrderState.OPEN, broker_order_id="paper-1")
    assert Order.model_validate_json(o.model_dump_json()) == o
    costs = CostBreakdown(brokerage=Decimal("20"), stt=Decimal("13"), gst=Decimal("4.42"))
    fill = Fill(
        fill_id="f1", order_id="o1", account_id="main", key=OPTION, product=ProductType.MARGIN,
        side=OrderSide.SELL, quantity=65, price=Decimal("200.00"), filled_at=NOW, costs=costs,
        paper=True,
    )  # fmt: skip
    assert Fill.model_validate_json(fill.model_dump_json()) == fill
    assert costs.total == Decimal("37.42")
    pos = Position(
        account_id="main", key=OPTION, product=ProductType.MARGIN, quantity=-65,
        average_price=Decimal("200.00"), updated_at=NOW,
    )  # fmt: skip
    assert Position.model_validate_json(pos.model_dump_json()) == pos
    result = CommandResult(
        command_id="c1", idempotency_key="k1", account_id="main", accepted=False,
        reason="paper only", decided_at=NOW,
    )  # fmt: skip
    assert CommandResult.model_validate_json(result.model_dump_json()) == result


def test_final_states_and_reply_stream() -> None:
    assert {s for s in OrderState if s.is_final} == {
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
    }
    assert reply_stream("c1") == "sentinelx:reply:c1"
