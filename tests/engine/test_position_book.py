"""PositionBook (task 5.1).

Scenarios from spec positions: "Scale in then partly out", "Long 65, sell 130", "Short loses
as price rises"; properties: a split fill gives the same position and P&L, and rebuilding from
fills equals applying them one by one.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.market import InstrumentKey
from contracts.orders import CostBreakdown, Fill, OrderSide, Position, ProductType
from engine.positions.book import apply, flat, mtm, net_pnl, rebuild

T0 = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
OPTION = InstrumentKey.parse("NSE_FNO:53720")
STOCK = InstrumentKey.parse("NSE_EQ:1333")
NO_COSTS = CostBreakdown()
# Averages are unrounded Decimals; a split can differ only in the 28th significant digit.
TOLERANCE = Decimal("1e-12")

_seq = iter(range(1_000_000))


def fill(
    side: OrderSide,
    quantity: int,
    price: str | Decimal,
    key: InstrumentKey = OPTION,
    costs: CostBreakdown = NO_COSTS,
) -> Fill:
    n = next(_seq)
    return Fill(
        fill_id=f"f{n}", order_id=f"o{n}", account_id="main", key=key,
        product=ProductType.INTRADAY, side=side, quantity=quantity, price=Decimal(price),
        filled_at=T0 + timedelta(seconds=n), costs=costs, paper=True,
    )  # fmt: skip


def run(*fills: Fill) -> Position:
    position = flat("main", fills[0].key, ProductType.INTRADAY, T0)
    for f in fills:
        position, _ = apply(position, f)
    return position


BUY, SELL = OrderSide.BUY, OrderSide.SELL


def test_scale_in_then_partly_out() -> None:
    p = run(fill(BUY, 65, "100.00"), fill(BUY, 65, "110.00"))
    assert (p.quantity, p.average_price) == (130, Decimal("105.00"))
    p, realized = apply(p, fill(SELL, 65, "120.00"))
    assert realized == Decimal("975.00")  # (120 - 105) x 65
    assert (p.quantity, p.average_price, p.realized_pnl) == (
        65,
        Decimal("105.00"),
        Decimal("975.00"),
    )


def test_long_65_sell_130_flips_short() -> None:
    p = run(fill(BUY, 65, "100.00"))
    p, realized = apply(p, fill(SELL, 130, "98.00"))
    assert realized == Decimal("-130.00")  # (98 - 100) x 65 closed
    assert (p.quantity, p.average_price) == (-65, Decimal("98.00"))


def test_closing_exactly_leaves_flat_with_zero_average() -> None:
    p = run(fill(SELL, 65, "200.00"), fill(BUY, 65, "190.00"))
    assert (p.quantity, p.average_price, p.realized_pnl) == (0, Decimal("0"), Decimal("650.00"))


def test_short_loses_as_price_rises() -> None:
    p = run(fill(SELL, 65, "200.00"))
    assert mtm(p, Decimal("205.00")) == Decimal("-325.00")
    assert mtm(p, Decimal("195.00")) == Decimal("325.00")


def test_net_pnl_is_realized_plus_mtm_minus_costs() -> None:
    costs = CostBreakdown(brokerage=Decimal("20.00"), gst=Decimal("3.60"))
    p = run(fill(BUY, 130, "100.00", costs=costs), fill(SELL, 65, "104.00", costs=costs))
    assert p.realized_pnl == Decimal("260.00") and p.costs == Decimal("47.20")
    # 260 realized + (101 - 100) x 65 MTM - 47.20 costs
    assert net_pnl(p, Decimal("101.00")) == Decimal("277.80")


def test_fill_for_another_position_is_refused() -> None:
    p = run(fill(BUY, 65, "100.00"))
    with pytest.raises(ValueError, match="another position"):
        apply(p, fill(BUY, 1, "1500.00", key=STOCK))


# ---- properties -----------------------------------------------------------------------------

prices = st.integers(min_value=1, max_value=40_000).map(
    lambda ticks: Decimal(ticks) * Decimal("0.05")
)
fill_specs = st.tuples(st.sampled_from([BUY, SELL]), st.integers(1, 500), prices)


def close(a: Position, b: Position) -> bool:
    return (
        a.quantity == b.quantity
        and abs(a.average_price - b.average_price) < TOLERANCE
        and abs(a.realized_pnl - b.realized_pnl) < TOLERANCE
    )


@given(st.lists(fill_specs, min_size=1, max_size=12), st.data())
def test_splitting_a_fill_gives_the_same_position(
    specs: list[tuple[OrderSide, int, Decimal]], data: st.DataObject
) -> None:
    index = data.draw(st.integers(0, len(specs) - 1))
    side, quantity, price = specs[index]
    if quantity < 2:
        return
    first = data.draw(st.integers(1, quantity - 1))
    whole = [fill(s, q, p) for s, q, p in specs]
    split = [fill(s, q, p) for s, q, p in specs[:index]]
    split += [fill(side, first, price), fill(side, quantity - first, price)]
    split += [fill(s, q, p) for s, q, p in specs[index + 1 :]]
    assert close(run(*whole), run(*split))


@given(st.lists(st.tuples(st.sampled_from([OPTION, STOCK]), fill_specs), min_size=1, max_size=20))
def test_rebuild_equals_incremental_application(
    specs: list[tuple[InstrumentKey, tuple[OrderSide, int, Decimal]]],
) -> None:
    fills = [
        fill(s, q, p, key=k, costs=CostBreakdown(brokerage=Decimal("20"))) for k, (s, q, p) in specs
    ]
    incremental: dict[InstrumentKey, Position] = {}
    for f in fills:
        current = incremental.get(f.key) or flat("main", f.key, ProductType.INTRADAY, T0)
        incremental[f.key], _ = apply(current, f)
    rebuilt = rebuild(fills)
    assert {pid[1]: p for pid, p in rebuilt.items()} == incremental
