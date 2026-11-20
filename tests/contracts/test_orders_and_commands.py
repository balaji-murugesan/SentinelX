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
    KillSwitch,
    PlaceOrder,
    ResumeRecovery,
    SetRiskLimits,
    SizeOrder,
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


def test_resume_command_round_trips_through_the_union() -> None:
    command = ResumeRecovery(
        command_id="c3", idempotency_key="k3", account_id="main", source="cli", sent_at=NOW
    )
    assert parse_command(command.model_dump_json()) == command


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


# ---- add-risk-engine task 1.1: stop orders, new commands, optional fields ---------------------


def stop_market(**overrides: object) -> PlaceOrder:
    values: dict[str, object] = {
        "order_type": OrderType.STOP_MARKET,
        "side": OrderSide.SELL,
        "trigger_price": Decimal("160.00"),
    }
    return place(**{**values, **overrides})


def test_stop_market_round_trips_with_its_trigger_and_an_entry_stop() -> None:
    command = stop_market()
    assert parse_command(command.model_dump_json()) == command
    entry = place(order_type=OrderType.LIMIT, limit_price=Decimal("200"), initial_stop="190.00")
    assert entry.initial_stop == Decimal("190.00")
    assert parse_command(entry.model_dump_json()) == entry
    stored = order(order_type=OrderType.STOP_MARKET, limit_price=None, trigger_price="160.00")
    assert Order.model_validate_json(stored.model_dump_json()) == stored


def test_stop_market_without_a_trigger_is_rejected() -> None:
    with pytest.raises(ValidationError) as info:
        stop_market(trigger_price=None)
    assert "trigger_price is required" in str(info.value) and "STOP_MARKET" in str(info.value)
    with pytest.raises(ValidationError):
        order(order_type=OrderType.STOP_MARKET, limit_price=None, trigger_price=None)


def test_limit_price_on_a_stop_market_is_rejected() -> None:
    with pytest.raises(ValidationError) as info:
        stop_market(limit_price=Decimal("159.00"))
    assert "limit_price must not be set" in str(info.value)
    with pytest.raises(ValidationError):
        order(order_type=OrderType.STOP_MARKET, limit_price=Decimal("159"), trigger_price="160")


@pytest.mark.parametrize(
    ("order_type", "limit_price"),
    [(OrderType.MARKET, None), (OrderType.LIMIT, Decimal("190"))],
)
def test_trigger_price_belongs_to_stop_market_only(
    order_type: OrderType, limit_price: Decimal | None
) -> None:
    with pytest.raises(ValidationError) as info:
        place(order_type=order_type, limit_price=limit_price, trigger_price=Decimal("160"))
    assert "trigger_price must not be set" in str(info.value)


@pytest.mark.parametrize("field", ["trigger_price", "initial_stop"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_stop_prices_must_be_positive(field: str, value: str) -> None:
    overrides: dict[str, object] = {field: Decimal(value)}
    if field == "trigger_price":
        overrides["order_type"] = OrderType.STOP_MARKET
    with pytest.raises(ValidationError):
        place(**overrides)


@pytest.mark.parametrize("field", ["trigger_price", "initial_stop"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_the_order_model_guards_stop_prices_too(field: str, value: str) -> None:
    """The stored Order has its own guard, separate from the command's."""
    overrides: dict[str, object] = {field: Decimal(value)}
    if field == "trigger_price":
        overrides.update(order_type=OrderType.STOP_MARKET, limit_price=None)
    with pytest.raises(ValidationError):
        order(**overrides)


def test_an_order_with_an_initial_stop_round_trips() -> None:
    entry = order(initial_stop="190.00")
    again = Order.model_validate_json(entry.model_dump_json())
    assert again == entry and again.initial_stop == Decimal("190.00")


def test_an_order_from_before_add_risk_engine_still_parses() -> None:
    old_order = (
        '{"schema_version":1,"order_id":"o1","account_id":"main",'
        '"key":{"schema_version":1,"segment":"NSE_FNO","security_id":"53720"},'
        '"side":"BUY","quantity":65,"product":"INTRADAY","order_type":"LIMIT",'
        '"limit_price":"190.00","idempotency_key":"k1","source":"cli","state":"Open",'
        '"filled_quantity":0,"average_fill_price":null,"broker_order_id":"paper-1",'
        '"paper":true,"reason":null,"created_at":"2026-10-29T04:00:00Z",'
        '"updated_at":"2026-10-29T04:00:00Z"}'
    )
    parsed = Order.model_validate_json(old_order)
    assert parsed.trigger_price is None and parsed.initial_stop is None
    assert parsed.limit_price == Decimal("190.00") and parsed.state is OrderState.OPEN


def test_a_payload_from_before_add_risk_engine_still_parses() -> None:
    """The new fields are optional, so S2-era JSON (no stop fields, no sizing fields) parses."""
    old_command = (
        '{"schema_version":1,"command_id":"c1","idempotency_key":"k1","account_id":"main",'
        '"source":"cli","sent_at":"2026-10-29T04:00:00Z","command":"place_order",'
        '"key":{"schema_version":1,"segment":"NSE_FNO","security_id":"53720"},"side":"BUY",'
        '"quantity":65,"product":"INTRADAY","order_type":"MARKET","limit_price":null}'
    )
    parsed = parse_command(old_command)
    assert isinstance(parsed, PlaceOrder)
    assert parsed.trigger_price is None and parsed.initial_stop is None

    old_result = (
        '{"schema_version":1,"command_id":"c1","idempotency_key":"k1","account_id":"main",'
        '"accepted":true,"order_id":"o1","reason":null,"decided_at":"2026-10-29T04:00:00Z"}'
    )
    result = CommandResult.model_validate_json(old_result)
    assert result.sized_quantity is None and result.risk_amount is None


def test_kill_switch_command_round_trips_through_the_union() -> None:
    command = KillSwitch(
        command_id="c4", idempotency_key="k4", account_id="main", source="cli", sent_at=NOW
    )
    parsed = parse_command(command.model_dump_json())
    assert parsed == command and json.loads(command.model_dump_json())["command"] == "kill_switch"


def test_set_risk_limits_round_trips_and_keeps_money_as_text() -> None:
    command = SetRiskLimits(
        command_id="c5", idempotency_key="k5", account_id="main", source="cli", sent_at=NOW,
        limits={"per_trade_max_risk": "1000.00", "max_trades_per_day": 10, "lock_at": None,
                "mode.per_trade_risk": "shadow", "trail_enabled": True},
    )  # fmt: skip
    parsed = parse_command(command.model_dump_json())
    assert parsed == command
    assert isinstance(parsed, SetRiskLimits)
    assert parsed.limits["per_trade_max_risk"] == "1000.00"  # text: never through a float


def test_a_risk_limit_given_as_a_float_is_refused() -> None:
    """A JSON number with a fraction would be a float, and money must never be one."""
    raw = (
        '{"command":"set_risk_limits","command_id":"c5","idempotency_key":"k5",'
        '"account_id":"main","source":"cli","sent_at":"2026-10-29T04:00:00Z",'
        '"limits":{"per_trade_max_risk":1000.5}}'
    )
    with pytest.raises(ValidationError):
        parse_command(raw)


def size_order(**overrides: object) -> SizeOrder:
    values: dict[str, object] = {
        "command_id": "c6",
        "idempotency_key": "k6",
        "account_id": "main",
        "source": "cli",
        "sent_at": NOW,
        "key": OPTION,
        "side": OrderSide.BUY,
        "entry_price": Decimal("200.00"),
        "initial_stop": Decimal("190.00"),
    }
    return SizeOrder(**{**values, **overrides})  # type: ignore[arg-type]


def test_size_order_round_trips() -> None:
    command = size_order()
    assert parse_command(command.model_dump_json()) == command


@pytest.mark.parametrize("field", ["entry_price", "initial_stop"])
def test_size_order_needs_positive_prices(field: str) -> None:
    with pytest.raises(ValidationError):
        size_order(**{field: Decimal("0")})


def test_the_sizing_answer_round_trips() -> None:
    result = CommandResult(
        command_id="c6", idempotency_key="k6", account_id="main", accepted=True,
        sized_quantity=65, risk_amount=Decimal("650.00"), decided_at=NOW,
    )  # fmt: skip
    again = CommandResult.model_validate_json(result.model_dump_json())
    assert again == result and again.risk_amount == Decimal("650.00")
