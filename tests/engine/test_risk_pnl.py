"""The day's net P&L (add-risk-engine task 3.2; equity-floor spec).

Scenarios: "Loss net of estimated exit charge", "Charges paid today", "Carried position gaps
down" and "Reference missing". A property test checks the cash-flow formula against the position
book: for a position opened today, ``day_gross`` equals realized plus unrealized P&L.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contracts.orders import CostBreakdown, Fill, OrderSide, Position, ProductType
from engine.broker.costs import CostCategory, fill_costs
from engine.positions.book import apply, flat, mtm, position_id
from engine.risk.pnl import day_pnl, estimate_exit_cost
from tests.engine.risk_builders import (
    INSTRUMENTS,
    NOW,
    OPTION,
    STOCK,
    fill,
    instrument_of,
    position,
)

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
CNC = ProductType.CNC


def carried_long() -> Position:
    """100 shares held under CNC since an earlier day, bought at 480.00."""
    return position(STOCK, 100, "480.00", CNC)


def test_loss_net_of_estimated_exit_charge() -> None:
    held = carried_long()
    previous_close = {position_id(held): D("500.00")}

    result = day_pnl([held], [], {STOCK: D("495.00")}, previous_close, instrument_of)

    assert result is not None
    assert result.gross == D("-500.00")  # down 5.00 a share from the previous close
    assert result.costs_paid == D(0)
    # Closing 100 delivery shares at 495.00: STT 49.50 + exchange 1.47 + SEBI 0.05 + GST 0.27.
    assert result.estimated_exit_costs == D("51.29")
    assert result.net == D("-551.29")
    expected = fill_costs(CostCategory.EQUITY_DELIVERY, SELL, 100, D("495.00")).total
    assert result.estimated_exit_costs == expected  # the same cost model as the paper broker


def test_charges_paid_today() -> None:
    fills = [
        fill(BUY, 65, "100.00", costs="30", fill_id="f1"),
        fill(SELL, 65, "103.00", costs="30", fill_id="f2"),  # 195 gross profit on 65 units
    ]
    closed = position(OPTION, 0, "0")

    result = day_pnl([closed], fills, {}, {}, instrument_of)  # flat: no price needed

    assert result is not None
    assert result.gross == D("195.00") and result.costs_paid == D("60")
    assert result.estimated_exit_costs == D(0)
    assert result.net == D("135.00")


def test_a_carried_position_is_measured_from_the_previous_close_not_from_entry() -> None:
    held = carried_long()  # bought at 480.00, but it closed yesterday at 500.00

    result = day_pnl(
        [held], [], {STOCK: D("495.00")}, {position_id(held): D("500.00")}, instrument_of
    )

    assert result is not None and result.gross == D("-500.00")  # -5.00 x 100, whatever the entry
    assert result.reference_missing == ()


def test_reference_missing_falls_back_to_the_average_price_and_says_so() -> None:
    held = carried_long()

    result = day_pnl([held], [], {STOCK: D("495.00")}, {}, instrument_of)

    assert result is not None
    assert result.gross == D("1500.00")  # (495 - 480) x 100: measured from entry instead
    assert result.reference_missing == (position_id(held),)


def test_a_carried_position_partly_sold_today_counts_each_part_from_its_own_basis() -> None:
    held = position(STOCK, 60, "480.00", CNC)  # 100 carried, 40 sold today, 60 left
    sold = fill(SELL, 40, "505.00", key=STOCK, product=CNC)

    result = day_pnl(
        [held], [sold], {STOCK: D("495.00")}, {position_id(held): D("500.00")}, instrument_of
    )

    # 40 sold at 505 against a 500 close: +200. 60 left at 495 against a 500 close: -300.
    assert result is not None and result.gross == D("-100.00")


def test_a_short_position_gains_when_the_price_falls() -> None:
    short = position(OPTION, -65, "200.00")
    sold = fill(SELL, 65, "200.00")

    result = day_pnl([short], [sold], {OPTION: D("190.00")}, {}, instrument_of)

    assert result is not None and result.gross == D("650.00")  # (200 - 190) x 65


def test_a_position_flipped_through_zero_today() -> None:
    now_short = position(OPTION, -65, "98.00")  # long 65 at 100, then sold 130 at 98
    fills = [fill(BUY, 65, "100.00", fill_id="a"), fill(SELL, 130, "98.00", fill_id="b")]

    result = day_pnl([now_short], fills, {OPTION: D("97.00")}, {}, instrument_of)

    # -130 realized on the 65 closed, then 65 short from 98 to 97 gains +65.
    assert result is not None and result.gross == D("-65.00")


def test_an_open_position_without_a_price_makes_the_mtm_unknown() -> None:
    assert day_pnl([carried_long()], [], {}, {}, instrument_of) is None  # never a guessed zero


def test_an_open_position_whose_instrument_is_unknown_makes_the_mtm_unknown() -> None:
    held = carried_long()
    assert (
        day_pnl([held], [], {STOCK: D("495")}, {position_id(held): D("500")}, lambda _k: None)
        is None
    )


def test_a_flat_position_needs_no_price() -> None:
    assert day_pnl([position(OPTION, 0, "0")], [], {}, {}, instrument_of) is not None


def test_no_positions_and_no_fills_is_a_zero_day() -> None:
    result = day_pnl([], [], {}, {}, instrument_of)

    assert result is not None and result.net == D(0)


def test_a_fill_for_a_position_that_is_not_listed_is_an_error() -> None:
    with pytest.raises(ValueError, match="belongs to no listed position"):
        day_pnl([], [fill(BUY, 65, "100.00")], {}, {}, instrument_of)


def test_the_estimated_exit_cost_is_zero_when_flat_and_uses_the_closing_side() -> None:
    assert estimate_exit_cost(position(OPTION, 0, "0"), D("100"), INSTRUMENTS[OPTION]) == D(0)
    long = estimate_exit_cost(position(OPTION, 65, "100"), D("100"), INSTRUMENTS[OPTION])
    short = estimate_exit_cost(position(OPTION, -65, "100"), D("100"), INSTRUMENTS[OPTION])
    # Selling to close a long pays STT; buying to close a short pays stamp duty instead.
    assert long == fill_costs(CostCategory.OPTION, SELL, 65, D("100")).total
    assert short == fill_costs(CostCategory.OPTION, BUY, 65, D("100")).total
    assert long != short


# ---- property: opened today, the cash-flow formula equals realized + unrealized --------------

trades = st.lists(
    st.tuples(
        st.sampled_from([BUY, SELL]),
        st.integers(min_value=1, max_value=200),
        st.decimals(min_value=D("1"), max_value=D("500"), places=2, allow_nan=False),
    ),
    min_size=1,
    max_size=8,
)


@given(trades, st.decimals(min_value=D("1"), max_value=D("500"), places=2, allow_nan=False))
def test_for_a_position_opened_today_gross_equals_realized_plus_unrealized(
    sequence: list[tuple[OrderSide, int, Decimal]], last: Decimal
) -> None:
    book = flat("main", OPTION, ProductType.INTRADAY, NOW)
    fills: list[Fill] = []
    for index, (side, quantity, price) in enumerate(sequence):
        one = fill(side, quantity, str(price), fill_id=f"f{index}")
        book, _ = apply(book, one)
        fills.append(one)

    result = day_pnl([book], fills, {OPTION: last}, {}, instrument_of)

    assert result is not None
    expected = book.realized_pnl + mtm(book, last)
    assert abs(result.gross - expected) < D("1e-9")
    assert result.reference_missing == ()  # nothing was carried, so no reference is needed


# ---- review round 1 ---------------------------------------------------------------------------


def test_a_carried_position_fully_closed_today_without_a_reference_is_unknown() -> None:
    """A flat row keeps no average price: using 0 would count the whole sale as profit."""
    closed = position(STOCK, 0, "0", CNC)  # 100 carried, all sold today at 495.00
    sold = fill(SELL, 100, "495.00", key=STOCK, product=CNC)

    assert day_pnl([closed], [sold], {}, {}, instrument_of) is None


def test_the_same_closed_carried_position_with_a_reference_has_a_real_number() -> None:
    closed = position(STOCK, 0, "0", CNC)
    sold = fill(SELL, 100, "495.00", key=STOCK, product=CNC)

    result = day_pnl([closed], [sold], {}, {position_id(closed): D("500.00")}, instrument_of)

    assert result is not None
    assert result.gross == D("-500.00")  # 100 x 495 proceeds, less 100 x 500 opening value
    assert result.reference_missing == ()


def test_a_carried_position_flipped_today_without_a_reference_is_reported() -> None:
    flipped = position(STOCK, -50, "495.00", ProductType.INTRADAY)
    sold = fill(SELL, 150, "495.00", key=STOCK, product=ProductType.INTRADAY)

    # 100 were held at the start of the day under the same id, now short 50.
    result = day_pnl([flipped], [sold], {STOCK: D("495.00")}, {}, instrument_of)

    assert result is not None
    assert result.reference_missing == (position_id(flipped),)


def test_two_positions_with_the_same_id_are_an_error() -> None:
    with pytest.raises(ValueError, match="share the id"):
        day_pnl([position(OPTION, 0, "0"), position(OPTION, 0, "0")], [], {}, {}, instrument_of)


def test_costs_paid_adds_every_component_of_every_fill() -> None:
    full = CostBreakdown(
        brokerage=D("20"), stt=D("13"), exchange=D("4.32"), sebi=D("0.07"),
        stamp_duty=D("1.50"), gst=D("4.38"),
    )  # fmt: skip
    closed = position(OPTION, 0, "0")

    result = day_pnl(
        [closed],
        [
            fill(BUY, 65, "100.00", breakdown=full, fill_id="a"),
            fill(SELL, 65, "100.00", breakdown=full, fill_id="b"),
        ],
        {},
        {},
        instrument_of,
    )

    assert result is not None
    assert full.total == D("43.27")
    assert result.costs_paid == D("86.54")  # both fills, every component
