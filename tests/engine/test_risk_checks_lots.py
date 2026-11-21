"""The lot caps and the expiry-day helpers (add-risk-guards task 5.1; lot-caps spec; expiry-day-
profile "A contract expires today only by its master expiry date" and "Entries in expiring
contracts use the expiry lot caps").

Scenarios: "Lots of an option order", "Unknown instrument in the book", "Pending entry reserves
its lots", "Reservation released on rejection", "Exactly at the cap", "Total cap across
underlyings", "Daily gross cap", "Exit above the cap" and "Tighter cap on expiry", plus boundaries,
carried positions, partial fills, and a ``hypothesis`` property over random order sequences.
"Count survives a restart" and "Count cannot be saved" are day-book tests (test_risk_daybook.py)
and end-to-end tests (test_risk_end_to_end.py); "Snapshot shows the count" is in
test_risk_monitor.py.
"""

from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderState, Position, ProductType
from engine.positions.book import position_id
from engine.risk.checks.lots import (
    LOTS_DAY,
    LOTS_TOTAL,
    LOTS_UNDERLYING,
    LotCaps,
    fno_lots,
    lots_of,
)
from engine.risk.checks.registry import default_checks
from engine.risk.daybook import DayBook
from engine.risk.decision import Severity
from engine.risk.expiry import (
    effective_cutoff,
    effective_lot_caps,
    effective_min_premium,
    effective_square_off,
    expires_today,
)
from engine.risk.limits import RiskLimits
from tests.engine.risk_builders import (
    LIMITS,
    NOW,
    OPTION,
    STOCK,
    TODAY,
    context,
    day_state,
    make_instrument,
    order,
    position,
)
from tests.engine.risk_fakes import MemoryRiskRepository, StubState
from tests.engine.test_risk_gate import harness

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def caps(per_underlying: int = 10, total: int = 20, day: int = 50, **more: object) -> RiskLimits:
    return replace(  # type: ignore[arg-type]
        LIMITS,
        max_open_lots_per_underlying=per_underlying,
        max_open_lots_total=total,
        max_gross_lots_per_day=day,
        **more,
    )


def contract(
    n: int, underlying: str = "NIFTY", lot: int = 75, expiry: date | None = None
) -> Instrument:
    made = make_instrument(f"NSE_FNO:{60000 + n}", underlying, lot_size=lot)
    return made if expiry is None else made.model_copy(update={"expiry": expiry})


NIFTY, NIFTY_2 = contract(1), contract(2)
BANKNIFTY, FINNIFTY = contract(3, "BANKNIFTY", 30), contract(4, "FINNIFTY", 65)
KNOWN = [NIFTY, NIFTY_2, BANKNIFTY, FINNIFTY]


def holding(instrument: Instrument, lots: int, **more: object) -> Position:
    return position(key=instrument.key, quantity=lots * instrument.lot_size, **more)


def entry(instrument: Instrument, lots: int, **more: object) -> Order:
    return order(BUY, lots * instrument.lot_size, key=instrument.key, **more)


def resting(instrument: Instrument, lots: int, order_id: str, **more: object) -> Order:
    values: dict[str, object] = {"state": OrderState.OPEN, **more}
    return entry(instrument, lots, order_id=order_id, **values)


def judge(
    new: Order,
    *,
    limits: RiskLimits | None = None,
    positions: list[Position] | None = None,
    orders: list[Order] | None = None,
    instruments: list[Instrument] | None = None,
    entry_lots_today: int = 0,
):  # type: ignore[no-untyped-def]
    ctx = context(
        limits=limits or caps(),
        day=day_state(entry_lots_today=entry_lots_today),
        positions=positions or [],
        orders=orders or [],
        extra_instruments=KNOWN if instruments is None else instruments,
    )
    return LotCaps().pre_trade(ctx, new)


# ---- counting lots ---------------------------------------------------------------------------


def test_lots_of_an_option_order() -> None:
    """Spec "Lots of an option order": lot size 75, an entry of 150 units is 2 lots."""
    assert lots_of(150, 75) == 2
    decision = judge(entry(NIFTY, 2), limits=caps(per_underlying=1))
    assert decision.code == LOTS_UNDERLYING and decision.inputs["lots"] == 2


@pytest.mark.parametrize(("quantity", "lots"), [(75, 1), (76, 2), (149, 2), (-150, 2)])
def test_a_part_lot_counts_as_a_whole_one(quantity: int, lots: int) -> None:
    assert lots_of(quantity, 75) == lots


def test_unknown_instrument_in_the_book() -> None:
    """Spec "Unknown instrument in the book": an open position whose instrument is missing from the
    master; an entry on the same underlying is refused with RISK_LOTS_UNDERLYING."""
    unknown = position(key=InstrumentKey.parse("NSE_FNO:99999"), quantity=75)

    decision = judge(entry(NIFTY, 1), positions=[unknown])

    assert decision.severity is Severity.REJECT and decision.code == LOTS_UNDERLYING
    assert decision.inputs == {"instrument": "NSE_FNO:99999"}


def test_an_unknown_pending_entry_is_refused_too() -> None:
    ghost = order(
        BUY, 75, key=InstrumentKey.parse("NSE_FNO:99999"), order_id="g", state=OrderState.OPEN
    )

    assert judge(entry(NIFTY, 1), orders=[ghost]).code == LOTS_UNDERLYING


def test_an_entry_in_an_unknown_instrument_is_refused() -> None:
    decision = judge(order(BUY, 75, key=InstrumentKey.parse("NSE_FNO:99999")))

    assert decision.code == LOTS_UNDERLYING and "instrument master" in decision.detail


# ---- per underlying --------------------------------------------------------------------------


def test_pending_entry_reserves_its_lots() -> None:
    """Spec "Pending entry reserves its lots": cap 4 and a pending 3-lot NIFTY entry."""
    pending = resting(NIFTY_2, 3, "p1")

    decision = judge(entry(NIFTY, 2), limits=caps(per_underlying=4), orders=[pending])

    assert decision.code == LOTS_UNDERLYING
    assert decision.inputs == {
        "underlying": "NIFTY", "open_or_pending": 3, "lots": 2, "limit": 4, "expiring": False,
    }  # fmt: skip


@pytest.mark.parametrize("final", [OrderState.REJECTED, OrderState.CANCELLED, OrderState.FILLED])
def test_reservation_released_on_rejection(final: OrderState) -> None:
    """Spec "Reservation released on rejection" (and on cancel; a filled order counts through its
    position instead)."""
    done = resting(NIFTY_2, 3, "p1", state=final)

    assert judge(entry(NIFTY, 2), limits=caps(per_underlying=4), orders=[done]).code is None


@pytest.mark.parametrize(("open_lots", "allowed"), [(1, True), (2, True), (3, False)])
def test_exactly_at_the_cap(open_lots: int, allowed: bool) -> None:
    """Spec "Exactly at the cap": cap 4, 2 open NIFTY lots, a 2-lot entry is not refused; one lot
    more is."""
    decision = judge(
        entry(NIFTY, 2), limits=caps(per_underlying=4), positions=[holding(NIFTY_2, open_lots)]
    )

    assert (decision.code is None) is allowed
    if not allowed:
        assert decision.code == LOTS_UNDERLYING


def test_other_underlyings_do_not_count_against_the_per_underlying_cap() -> None:
    decision = judge(
        entry(NIFTY, 2), limits=caps(per_underlying=4), positions=[holding(BANKNIFTY, 4)]
    )

    assert decision.code is None


def test_a_short_position_counts_its_size() -> None:
    short = position(key=NIFTY_2.key, quantity=-3 * NIFTY_2.lot_size)

    assert judge(entry(NIFTY, 2), limits=caps(per_underlying=4), positions=[short]).code == (
        LOTS_UNDERLYING
    )


def test_resting_exits_and_protective_stops_reserve_nothing() -> None:
    """A resting order that only reduces a position (an exit, the catastrophic stop) is not a
    pending entry: it is classified against the positions like any order."""
    held = holding(NIFTY_2, 2)
    stop = order(SELL, 2 * NIFTY_2.lot_size, key=NIFTY_2.key, order_id="s1", state=OrderState.OPEN)

    assert (
        judge(entry(NIFTY, 2), limits=caps(per_underlying=4), positions=[held], orders=[stop]).code
        is None
    )


def test_a_partly_filled_entry_reserves_only_its_unfilled_part() -> None:
    """3 lots ordered, 2 filled: 2 lots are in the position and 1 is pending, 3 in all."""
    held = holding(NIFTY_2, 2)
    partial = resting(
        NIFTY_2, 3, "p1", state=OrderState.PARTIALLY_FILLED, filled_quantity=2 * NIFTY_2.lot_size
    )
    limits = caps(per_underlying=4)

    assert judge(entry(NIFTY, 1), limits=limits, positions=[held], orders=[partial]).code is None
    assert judge(entry(NIFTY, 2), limits=limits, positions=[held], orders=[partial]).code == (
        LOTS_UNDERLYING
    )


def test_a_partly_filled_exit_is_not_mistaken_for_an_entry() -> None:
    """A 3-lot exit of a 3-lot long, 2 lots filled: 1 lot is left of both. Judged on its full
    quantity it would look like a flip (an entry); on its unfilled part it is an exit."""
    held = holding(NIFTY_2, 1)
    partial = order(
        SELL, 3 * NIFTY_2.lot_size, key=NIFTY_2.key, order_id="x1",
        state=OrderState.PARTIALLY_FILLED, filled_quantity=2 * NIFTY_2.lot_size,
    )  # fmt: skip

    decision = judge(
        entry(NIFTY, 3), limits=caps(per_underlying=4), positions=[held], orders=[partial]
    )

    assert decision.code is None  # 1 open + 3 = 4: at the cap, not over


def test_the_order_being_judged_does_not_reserve_against_itself() -> None:
    mine = entry(NIFTY, 4, state=OrderState.REQUESTED)  # already in the service's order map

    assert judge(mine, limits=caps(per_underlying=4), orders=[mine]).code is None


# ---- total -----------------------------------------------------------------------------------


def test_total_cap_across_underlyings() -> None:
    """Spec "Total cap across underlyings": total 8, 5 NIFTY and 2 BANKNIFTY lots open, a 2-lot
    FINNIFTY entry is refused."""
    decision = judge(
        entry(FINNIFTY, 2),
        limits=caps(per_underlying=8, total=8),
        positions=[holding(NIFTY, 5), holding(BANKNIFTY, 2)],
    )

    assert decision.code == LOTS_TOTAL
    assert decision.inputs == {"open_or_pending": 7, "lots": 2, "limit": 8, "expiring": False}


def test_one_lot_under_the_total_is_allowed() -> None:
    decision = judge(
        entry(FINNIFTY, 1),
        limits=caps(per_underlying=8, total=8),
        positions=[holding(NIFTY, 5), holding(BANKNIFTY, 2)],
    )

    assert decision.code is None


def test_carried_positions_and_pending_entries_count_toward_the_total() -> None:
    carried = holding(BANKNIFTY, 3, product=ProductType.MARGIN)  # carried from an earlier day
    pending = resting(NIFTY, 3, "p1")

    decision = judge(
        entry(FINNIFTY, 3),
        limits=caps(per_underlying=8, total=8),
        positions=[carried],
        orders=[pending],
    )

    assert decision.code == LOTS_TOTAL and decision.inputs["open_or_pending"] == 6


# ---- F&O only --------------------------------------------------------------------------------

UNKNOWN_EQUITY = InstrumentKey.parse("NSE_EQ:99999")


def test_equity_holdings_do_not_count() -> None:
    """Spec "Equity holdings do not count": total cap 8 and a carried CNC holding of 500 shares
    (lot size 1); a 2-lot NIFTY option entry is not refused."""
    shares = position(key=STOCK, quantity=500, product=ProductType.CNC)

    decision = judge(entry(NIFTY, 2), limits=caps(per_underlying=8, total=8), positions=[shares])

    assert decision.code is None


def test_an_equity_entry_is_not_judged() -> None:
    """Spec "An equity entry is not judged": with the daily gross lots used up and the caps at 1,
    an entry of 100 shares is not refused by any lot cap."""
    shares = order(BUY, 100, key=STOCK, product=ProductType.CNC)

    decision = judge(shares, limits=caps(per_underlying=1, total=1, day=20), entry_lots_today=20)

    assert decision.code is None


def test_pending_equity_orders_reserve_nothing() -> None:
    pending = order(BUY, 500, key=STOCK, order_id="e1", state=OrderState.OPEN)

    assert (
        judge(entry(NIFTY, 2), limits=caps(per_underlying=2, total=2), orders=[pending]).code
        is None
    )


def test_an_unknown_equity_is_skipped_not_refused() -> None:
    """The segment comes from the key, so an equity the master does not know is still known to be
    an equity: neither an entry in it nor a holding of it makes the lot caps fail closed."""
    held = position(key=UNKNOWN_EQUITY, quantity=100, product=ProductType.CNC)
    pending = order(BUY, 10, key=UNKNOWN_EQUITY, order_id="e1", state=OrderState.OPEN)

    assert judge(order(BUY, 10, key=UNKNOWN_EQUITY)).code is None
    assert judge(entry(NIFTY, 1), positions=[held], orders=[pending]).code is None


def test_an_unknown_fno_instrument_is_still_refused() -> None:
    """Fail closed stays for F&O: an unknown NSE_FNO or BSE_FNO contract, ordered or held."""
    for key in ("NSE_FNO:99999", "BSE_FNO:99999"):
        unknown = InstrumentKey.parse(key)
        assert judge(order(BUY, 75, key=unknown)).code == LOTS_UNDERLYING
        held = position(key=unknown, quantity=75)
        assert judge(entry(NIFTY, 1), positions=[held]).code == LOTS_UNDERLYING


@pytest.mark.parametrize(
    ("key", "lots"), [("NSE_FNO:1", 2), ("BSE_FNO:1", 2), ("NSE_EQ:1", 0), ("BSE_EQ:1", 0)]
)
def test_only_fno_contracts_have_lots(key: str, lots: int) -> None:
    assert fno_lots(InstrumentKey.parse(key), 150, 75) == lots


async def test_an_allowed_equity_entry_leaves_the_days_gross_lots_unchanged() -> None:
    """The gate counts an allowed equity entry as an entry (entry counters and pacing) with 0 lots,
    so ``entry_lots_today`` does not move; an option entry adds its lots."""
    book = DayBook(
        account_id="main", repository=MemoryRiskRepository(), limits=lambda: None, now=lambda: NOW
    )
    assert await book.load()
    h = await harness(checks=[LotCaps()], state=_Live(book))

    equity = await h.gate.check(order(BUY, 100, key=STOCK, product=ProductType.CNC))
    assert equity is None
    assert book.day().entries_today == 1 and book.day().entry_lots_today == 0
    assert book.day().last_entry_at == NOW  # pacing still sees it

    option = await h.gate.check(order(BUY, 130, key=OPTION, order_id="o2"))  # 2 lots of 65
    assert option is None
    assert book.day().entries_today == 2 and book.day().entry_lots_today == 2


class _Live(StubState):
    """A ``StubState`` whose entry counting is the real day book's."""

    def __init__(self, book: DayBook) -> None:
        super().__init__(book.day())
        self._book = book

    def day(self):  # type: ignore[no-untyped-def]
        return self._book.day()

    async def entry_allowed(self, lots: int, expiring: bool) -> str | None:
        return await self._book.entry_allowed(lots, expiring)


# ---- daily gross -----------------------------------------------------------------------------


@pytest.mark.parametrize(("today", "allowed"), [(17, True), (18, True), (19, False), (20, False)])
def test_daily_gross_cap(today: int, allowed: bool) -> None:
    """Spec "Daily gross cap": cap 20 and 19 lots allowed today, a 2-lot entry is refused."""
    decision = judge(entry(NIFTY, 2), limits=caps(day=20), entry_lots_today=today)

    assert (decision.code is None) is allowed
    if not allowed:
        assert decision.code == LOTS_DAY
        assert decision.inputs == {"entry_lots_today": today, "lots": 2, "limit": 20}


# ---- exits -----------------------------------------------------------------------------------


async def test_exit_above_the_cap() -> None:
    """Spec "Exit above the cap": the open lots exceed a cap tightened mid-day; an exit for part of
    the position is not refused by a lot cap and adds nothing to the day's gross lots."""
    held = holding(NIFTY, 6)
    state = StubState(day_state(entry_lots_today=6))
    h = await harness(
        checks=[LotCaps()],
        limits_values={
            **_base(), "max_open_lots_per_underlying": 4, "max_open_lots_total": 4,
            "max_gross_lots_per_day": 4,
        },
        positions=[held], state=state, instruments={i.key: i for i in KNOWN}.get,
    )  # fmt: skip

    reason = await h.gate.check(order(SELL, 2 * NIFTY.lot_size, key=NIFTY.key))

    assert reason is None
    assert state.counted == [] and h.rows("lot_caps") == []  # not judged, not counted


def test_lot_caps_are_an_entry_rule() -> None:
    assert LotCaps().applies_to_exits is False and LotCaps().name == "lot_caps"


def _base() -> dict:  # type: ignore[type-arg]
    from tests.engine.test_risk_limits import BASE

    return dict(BASE)


# ---- fail closed in shadow mode --------------------------------------------------------------


async def test_in_shadow_mode_an_unknown_instrument_entry_is_still_refused() -> None:
    """With ``lot_caps`` in shadow mode its own refusal is only logged, so an entry in an
    instrument the master does not know must still be refused by something that binds (fail
    closed). With the registered rules the code that wins is ``RISK_NO_STOP``, from
    ``initial_stop``: it is registered first and cannot check a stop without the instrument's
    facts. It always binds (it cannot be shadowed), and so does ``stale_data``, which refuses
    too (an unknown instrument has no price). Behind them the open-position rule (it cannot place
    an unknown instrument under an underlying) and the gate's own lot count would refuse as
    well."""
    unknown = InstrumentKey.parse("NSE_FNO:99999")
    state = StubState(day_state())
    h = await harness(
        checks=default_checks(), limits_values={**_base(), "mode.lot_caps": "shadow"}, state=state
    )

    reason = await h.gate.check(order(BUY, 75, key=unknown, initial_stop=D("90")))

    assert reason is not None and reason.startswith("RISK_NO_STOP")
    assert h.verdict().inputs["decided_by"] == "initial_stop"
    [shadowed] = h.rows("lot_caps")
    assert shadowed.reason_code == LOTS_UNDERLYING and shadowed.enforced is False
    enforced = {row.rule for row in h.rows() if row.enforced and row.rule != "gate"}
    assert {"initial_stop", "stale_data", "max_open_positions"} <= enforced
    assert state.counted == []


# ---- the expiry-day profile ------------------------------------------------------------------

EXPIRING = contract(10, expiry=TODAY)  # a NIFTY weekly expiring today
NEXT_WEEK = contract(11, expiry=TODAY + timedelta(days=7))
EXPIRY_CAPS = caps(
    per_underlying=4, total=8,
    max_open_lots_per_underlying_expiry=2, max_open_lots_total_expiry=3,
)  # fmt: skip


def test_tighter_cap_on_expiry() -> None:
    """Spec "Tighter cap on expiry": cap 4, expiry override 2, 1 open NIFTY lot; a 2-lot entry in
    a NIFTY contract expiring today is refused. The same entry in next week's contract is not."""
    held = holding(NIFTY, 1)
    instruments = [NIFTY, EXPIRING, NEXT_WEEK]

    expiring = judge(
        entry(EXPIRING, 2), limits=EXPIRY_CAPS, positions=[held], instruments=instruments
    )
    later = judge(
        entry(NEXT_WEEK, 2), limits=EXPIRY_CAPS, positions=[held], instruments=instruments
    )

    assert expiring.code == LOTS_UNDERLYING
    assert expiring.inputs["limit"] == 2 and expiring.inputs["expiring"] is True
    assert later.code is None


def test_the_expiry_total_cap() -> None:
    held = holding(BANKNIFTY, 2)
    instruments = [BANKNIFTY, EXPIRING, NEXT_WEEK]

    expiring = judge(
        entry(EXPIRING, 2), limits=EXPIRY_CAPS, positions=[held], instruments=instruments
    )
    later = judge(
        entry(NEXT_WEEK, 2), limits=EXPIRY_CAPS, positions=[held], instruments=instruments
    )

    assert expiring.code == LOTS_TOTAL and expiring.inputs["limit"] == 3
    assert later.code is None


def test_the_daily_gross_cap_is_the_same_on_expiry() -> None:
    decision = judge(
        entry(EXPIRING, 1), limits=replace(EXPIRY_CAPS, max_gross_lots_per_day=10),
        instruments=[EXPIRING], entry_lots_today=9,
    )  # fmt: skip

    assert decision.code is None  # 9 + 1 = 10: at the daily cap, which has no expiry override


def test_without_overrides_an_expiring_entry_uses_the_base_caps() -> None:
    decision = judge(
        entry(EXPIRING, 4), limits=caps(per_underlying=4), positions=[], instruments=[EXPIRING]
    )

    assert decision.code is None


def test_a_contract_expires_today_only_by_its_master_date() -> None:
    """Spec "Weekly option on its expiry date" and "Expiry moved by a holiday" (the helper):
    tomorrow's date does not count whatever the weekday, and no date never counts."""
    moved = contract(12, expiry=TODAY + timedelta(days=1))

    assert expires_today(EXPIRING, NOW) is True
    assert expires_today(moved, NOW) is False
    assert expires_today(NIFTY, NOW) is False  # no expiry date: an index or an equity


def test_expires_today_uses_the_ist_date() -> None:
    """18:45 UTC on the 15th is 00:15 IST on the 16th: a contract expiring on the 16th expires
    "today" by then, and not one second before IST midnight."""
    ist_midnight = NOW.replace(hour=18, minute=30) - timedelta(days=1)

    assert expires_today(EXPIRING, ist_midnight) is True
    assert expires_today(EXPIRING, ist_midnight - timedelta(seconds=1)) is False


def test_the_effective_value_helpers() -> None:
    limits = caps(
        entry_cutoff_expiry=time(14, 0), square_off_time_expiry=time(14, 30),
        max_open_lots_per_underlying_expiry=2, min_premium=D(5), min_premium_expiry=D(15),
    )  # fmt: skip

    assert effective_cutoff(limits, True) == time(14, 0)
    assert effective_cutoff(limits, False) == limits.entry_cutoff
    assert effective_square_off(limits, True) == time(14, 30)
    assert effective_square_off(limits, False) == limits.square_off_time
    assert effective_lot_caps(limits, True) == (2, 20)  # the total has no override here
    assert effective_lot_caps(limits, False) == (10, 20)
    assert effective_min_premium(limits, True) == D(15)
    assert effective_min_premium(limits, False) == D(5)


def test_unset_overrides_fall_back_to_the_base() -> None:
    assert effective_cutoff(LIMITS, True) == LIMITS.entry_cutoff
    assert effective_square_off(LIMITS, True) == LIMITS.square_off_time
    assert effective_lot_caps(LIMITS, True) == (10, 20)
    assert effective_min_premium(LIMITS, True) is None
    assert effective_min_premium(replace(LIMITS, min_premium_expiry=D(15)), True) == D(15)
    assert effective_min_premium(replace(LIMITS, min_premium_expiry=D(15)), False) is None


# ---- property: open plus pending lots never exceed a cap -------------------------------------

PER_UNDERLYING, TOTAL, DAILY = 4, 6, 12
PROPERTY_LIMITS = caps(per_underlying=PER_UNDERLYING, total=TOTAL, day=DAILY)
UNIVERSE = [NIFTY, NIFTY_2, BANKNIFTY, FINNIFTY, contract(5, "BANKNIFTY", 30)]

steps = st.lists(
    st.one_of(
        st.tuples(st.just("place"), st.integers(0, len(UNIVERSE) - 1), st.integers(1, 3)),
        st.tuples(st.just("fill"), st.integers(0, 50), st.booleans()),  # True: one lot only
        st.tuples(st.just("reject"), st.integers(0, 50), st.just(False)),
        st.tuples(st.just("cancel"), st.integers(0, 50), st.just(False)),
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_open_plus_pending_lots_never_exceed_a_cap(script: list[tuple[str, int, object]]) -> None:
    """Design §10: over random place, fill, reject and cancel sequences (partial fills included),
    the lots open plus pending never exceed the per-underlying or the total cap, and the gross
    lots allowed never exceed the daily cap. The invariant is counted here independently of the
    rule's own arithmetic."""
    lot_size = {i.key: i.lot_size for i in UNIVERSE}
    underlying_of = {i.key: i.underlying_symbol for i in UNIVERSE}
    positions: dict[InstrumentKey, int] = {}
    orders: dict[str, Order] = {}
    gross = 0

    for n, (action, pick, flag) in enumerate(script):
        live = [o for o in orders.values() if not o.state.is_final]
        if action == "place":
            instrument, lots = UNIVERSE[pick], int(flag)  # type: ignore[call-overload]
            new = entry(instrument, lots, order_id=f"o{n}", idempotency_key=f"k{n}")
            ctx = context(
                limits=PROPERTY_LIMITS, day=day_state(entry_lots_today=gross),
                positions=[position(key=k, quantity=q) for k, q in positions.items() if q],
                orders=orders.values(), extra_instruments=UNIVERSE,
            )  # fmt: skip
            if LotCaps().pre_trade(ctx, new).severity is Severity.ALLOW:
                orders[new.order_id] = new.model_copy(update={"state": OrderState.OPEN})
                gross += lots
        elif live:
            target = live[pick % len(live)]
            unfilled = target.quantity - target.filled_quantity
            if action == "fill":
                qty = lot_size[target.key] if flag else unfilled
                filled = target.filled_quantity + qty
                state = (
                    OrderState.FILLED if filled == target.quantity else OrderState.PARTIALLY_FILLED
                )
                orders[target.order_id] = target.model_copy(
                    update={"filled_quantity": filled, "state": state}
                )
                positions[target.key] = positions.get(target.key, 0) + qty
            else:
                final = OrderState.REJECTED if action == "reject" else OrderState.CANCELLED
                orders[target.order_id] = target.model_copy(update={"state": final})

        per: dict[str, int] = {}
        for key, quantity in positions.items():
            per[underlying_of[key]] = per.get(underlying_of[key], 0) + quantity // lot_size[key]
        for o in orders.values():
            if not o.state.is_final:
                pending = (o.quantity - o.filled_quantity) // lot_size[o.key]
                per[underlying_of[o.key]] = per.get(underlying_of[o.key], 0) + pending
        assert all(lots <= PER_UNDERLYING for lots in per.values()), per
        assert sum(per.values()) <= TOTAL, per
        assert gross <= DAILY


def test_lot_caps_are_registered() -> None:
    assert any(isinstance(c, LotCaps) for c in default_checks())


def test_positions_are_keyed_like_the_service() -> None:
    """The builders key positions the way ``OrderService`` does (a guard for the tests above)."""
    held = holding(NIFTY, 1)
    assert position_id(held) == ("main", NIFTY.key, ProductType.INTRADAY)
