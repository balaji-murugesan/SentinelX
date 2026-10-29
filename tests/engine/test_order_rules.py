"""Order validation, the pre-trade gate and the state machine (tasks 3.1-3.3).

Scenarios from spec orders: "Delivery on an option", "Not a whole number of lots", "At the
freeze quantity", "Unknown freeze quantity on F&O", "Instrument master unavailable",
"Selling more than held", "Live mode before the risk engine", "Gate rejection is final",
"Update after a final state" (state-machine part).
"""

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.commands import PlaceOrder
from contracts.market import Instrument, InstrumentKey
from contracts.mode import TradingMode
from contracts.orders import Order, OrderSide, OrderState, OrderType, ProductType
from engine.orders.gate import LIVE_NOT_READY, PaperOnlyGate
from engine.orders.machine import TRANSITIONS, InvalidTransition, can_transition, transition
from engine.orders.validation import Holdings, validate

NOW = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")
STOCK = InstrumentKey.parse("NSE_EQ:1333")
NIFTY = InstrumentKey.parse("IDX_I:13")


def instrument(key: InstrumentKey = OPTION, **overrides: object) -> Instrument:
    values: dict[str, object] = {
        "key": key,
        "trading_symbol": "NIFTY-Nov2026-22600-CE",
        "display_name": "NIFTY 03 NOV 22600 CALL",
        "underlying_symbol": "NIFTY",
        "instrument_type": "OPTIDX",
        "lot_size": 65,
        "tick_size": Decimal("0.05"),
        "freeze_qty": 1800,
    }
    if key == STOCK:
        values.update(
            trading_symbol="HDFCBANK", display_name="HDFC Bank", underlying_symbol="HDFCBANK",
            instrument_type="EQUITY", lot_size=1, freeze_qty=None,
        )  # fmt: skip
    return Instrument(**{**values, **overrides})  # type: ignore[arg-type]


def command(key: InstrumentKey = OPTION, **overrides: object) -> PlaceOrder:
    values: dict[str, object] = {
        "command_id": "c1",
        "idempotency_key": "k1",
        "account_id": "main",
        "source": "cli",
        "sent_at": NOW,
        "key": key,
        "side": OrderSide.BUY,
        "quantity": 65,
        "product": ProductType.INTRADAY,
        "order_type": OrderType.MARKET,
    }
    return PlaceOrder(**{**values, **overrides})  # type: ignore[arg-type]


def check(cmd: PlaceOrder, inst: Instrument | None = None, **kwargs: object) -> str | None:
    inst = inst if inst is not None or kwargs.pop("missing", False) else instrument(cmd.key)
    return validate(cmd, inst, master_loaded=bool(kwargs.pop("loaded", True)), **kwargs)  # type: ignore[arg-type]


# ---- 3.1 validation -------------------------------------------------------------------------


def test_valid_option_and_stock_orders_pass() -> None:
    assert check(command()) is None
    assert check(command(order_type=OrderType.LIMIT, limit_price=Decimal("190.05"))) is None
    assert check(command(STOCK, quantity=7, product=ProductType.CNC)) is None
    assert check(command(OPTION, product=ProductType.MARGIN, quantity=1755)) is None


def test_delivery_on_an_option() -> None:
    reason = check(command(product=ProductType.CNC))
    assert reason is not None and "CNC" in reason and "NSE_FNO" in reason


def test_margin_on_equity_is_refused() -> None:
    reason = check(command(STOCK, quantity=1, product=ProductType.MARGIN))
    assert reason is not None and "MARGIN" in reason and "NSE_EQ" in reason


def test_index_cannot_be_ordered() -> None:
    reason = validate(command(NIFTY), None, master_loaded=True)
    assert reason is not None and "index" in reason


def test_not_a_whole_number_of_lots() -> None:
    reason = check(command(quantity=100))
    assert reason is not None and "lot size 65" in reason


def test_at_the_freeze_quantity() -> None:
    assert check(command(quantity=1755)) is None  # 27 lots, below 1800
    reason = check(command(quantity=1820), instrument(freeze_qty=1820))
    assert reason is not None and "freeze quantity 1820" in reason


def test_unknown_freeze_quantity_on_fno_rejects() -> None:
    reason = check(command(), instrument(freeze_qty=None))
    assert reason is not None and "freeze quantity" in reason and "unknown" in reason


def test_unknown_freeze_quantity_on_equity_is_fine() -> None:
    assert check(command(STOCK, quantity=5000)) is None


def test_limit_price_off_the_tick_grid() -> None:
    reason = check(command(order_type=OrderType.LIMIT, limit_price=Decimal("190.03")))
    assert reason is not None and "tick size 0.05" in reason


def test_instrument_master_unavailable() -> None:
    reason = validate(command(), instrument(), master_loaded=False)
    assert reason is not None and "instrument data is unavailable" in reason


def test_instrument_not_in_master() -> None:
    reason = validate(command(), None, master_loaded=True)
    assert reason is not None and "not in the instrument master" in reason


@pytest.mark.parametrize(
    ("held", "selling", "quantity", "ok"),
    [(10, 0, 15, False), (10, 0, 10, True), (10, 4, 7, False), (10, 4, 6, True), (0, 0, 1, False)],
)
def test_selling_more_than_held(held: int, selling: int, quantity: int, ok: bool) -> None:
    cmd = command(STOCK, side=OrderSide.SELL, quantity=quantity, product=ProductType.CNC)
    reason = check(cmd, holdings=Holdings(held=held, selling=selling))
    assert (reason is None) is ok
    if not ok:
        assert reason is not None and f"{held} held" in reason and "INTRADAY" in reason


def test_intraday_short_of_equity_is_allowed() -> None:
    cmd = command(STOCK, side=OrderSide.SELL, quantity=10, product=ProductType.INTRADAY)
    assert check(cmd) is None


# ---- 3.2 gate -------------------------------------------------------------------------------


def order(state: OrderState = OrderState.REQUESTED, paper: bool = True, **changes: object) -> Order:
    values: dict[str, object] = {
        "order_id": "o1", "account_id": "main", "key": OPTION, "side": OrderSide.BUY,
        "quantity": 65, "product": ProductType.INTRADAY, "order_type": OrderType.MARKET,
        "idempotency_key": "k1", "source": "cli", "paper": paper, "state": state,
        "created_at": NOW, "updated_at": NOW,
    }  # fmt: skip
    return Order(**{**values, **changes})  # type: ignore[arg-type]


async def test_paper_orders_pass_the_stub_gate() -> None:
    assert await PaperOnlyGate(TradingMode.PAPER).check(order()) is None


async def test_live_mode_before_the_risk_engine() -> None:
    gate = PaperOnlyGate(TradingMode.LIVE)
    assert await gate.check(order(paper=False)) == LIVE_NOT_READY
    assert await gate.check(order(paper=True)) == LIVE_NOT_READY  # live mode refuses all


async def test_non_paper_order_refused_even_in_paper_mode() -> None:
    assert await PaperOnlyGate(TradingMode.PAPER).check(order(paper=False)) == LIVE_NOT_READY


def test_gate_rejection_is_final() -> None:
    rejected = transition(order(), OrderState.REJECTED, at=NOW, reason=LIVE_NOT_READY)
    assert rejected.state.is_final and rejected.reason == LIVE_NOT_READY
    with pytest.raises(InvalidTransition):
        transition(rejected, OrderState.SUBMITTED, at=NOW)  # never reaches the broker


# ---- 3.3 state machine ----------------------------------------------------------------------

S = OrderState
ALLOWED = {
    (S.REQUESTED, S.SUBMITTED), (S.REQUESTED, S.REJECTED),
    (S.SUBMITTED, S.OPEN), (S.SUBMITTED, S.REJECTED), (S.SUBMITTED, S.UNKNOWN),
    (S.UNKNOWN, S.OPEN), (S.UNKNOWN, S.REJECTED),
    (S.OPEN, S.PARTIALLY_FILLED), (S.OPEN, S.FILLED), (S.OPEN, S.CANCELLED),
    (S.PARTIALLY_FILLED, S.PARTIALLY_FILLED), (S.PARTIALLY_FILLED, S.FILLED),
    (S.PARTIALLY_FILLED, S.CANCELLED),
}  # fmt: skip


def test_table_matches_prd_section_5() -> None:
    assert {(a, b) for a, targets in TRANSITIONS.items() for b in targets} == ALLOWED


@pytest.mark.parametrize(("source", "target"), list(itertools.product(S, S)))
def test_every_pair(source: OrderState, target: OrderState) -> None:
    assert can_transition(source, target) is ((source, target) in ALLOWED)
    o = order(state=source)
    if (source, target) in ALLOWED:
        moved = transition(o, target, at=NOW + timedelta(seconds=1))
        assert moved.state is target and moved.updated_at == NOW + timedelta(seconds=1)
    else:
        with pytest.raises(InvalidTransition):
            transition(o, target, at=NOW)


def test_update_after_a_final_state_leaves_order_unchanged() -> None:
    cancelled = order(state=S.CANCELLED)
    with pytest.raises(InvalidTransition) as info:
        transition(cancelled, S.FILLED, at=NOW, filled_quantity=65)
    assert "o1" in str(info.value) and "Cancelled -> Filled" in str(info.value)
    assert cancelled.state is S.CANCELLED and cancelled.filled_quantity == 0


def test_transition_carries_changes_and_revalidates() -> None:
    opened = transition(order(state=S.SUBMITTED), S.OPEN, at=NOW, broker_order_id="paper-1")
    assert opened.broker_order_id == "paper-1"
    with pytest.raises(ValueError):
        transition(order(state=S.OPEN), S.FILLED, at=NOW, filled_quantity=-1)
