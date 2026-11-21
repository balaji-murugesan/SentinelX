"""The instrument policy (add-risk-guards task 7.4; instrument-policy spec, every entry scenario;
expiry-day-profile "Option entries in expiring contracts use the higher minimum premium").

The check is a pure function of the context, so each scenario builds one. The last section runs
it through the real gate: no part of the policy refuses an exit, a missing ban list included.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from contracts.commands import LimitValue
from contracts.market import Instrument, InstrumentKey
from contracts.orders import Order, OrderSide, OrderType
from engine.risk.banlist import BAN_LIST_SEGMENTS
from engine.risk.checks.common import LOT_CAP_EXEMPT_SEGMENTS
from engine.risk.checks.policy import (
    BAN_LIST_MISSING,
    DENIED,
    FNO_BAN,
    MIN_PREMIUM,
    NOT_ALLOWED,
    STRIKE_DISTANCE,
    UNDERLYING_UNPRICED,
    InstrumentPolicy,
)
from engine.risk.checks.registry import default_checks
from engine.risk.decision import Decision, RiskContext, Severity
from engine.risk.limits import parse_limits
from tests.engine.risk_builders import TODAY, context, day_state, order, position
from tests.engine.risk_fakes import StubState
from tests.engine.test_risk_gate import harness
from tests.engine.test_risk_limits import BASE

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
NEXT_WEEK = TODAY + timedelta(days=7)
POLICY = InstrumentPolicy()


def option(
    key: str,
    underlying: str,
    strike: str,
    *,
    expiry: date = NEXT_WEEK,
    kind: Literal["CE", "PE"] = "CE",
    lot: int = 65,
) -> Instrument:
    parsed = InstrumentKey.parse(key)
    return Instrument(
        key=parsed, trading_symbol=f"{underlying}-{strike}-{kind}",
        display_name=f"{underlying} {strike} {kind}", underlying_symbol=underlying,
        instrument_type="OPTIDX", lot_size=lot, tick_size=D("0.05"), expiry=expiry,
        strike=D(strike), option_type=kind,
    )  # fmt: skip


def plain(key: str, underlying: str, kind: str, *, expiry: date | None = None) -> Instrument:
    """A non-option row: an index, an equity or a future."""
    return Instrument(
        key=InstrumentKey.parse(key), trading_symbol=underlying, display_name=underlying,
        underlying_symbol=underlying, instrument_type=kind, lot_size=1, tick_size=D("0.05"),
        expiry=expiry,
    )  # fmt: skip


NIFTY_INDEX = plain("IDX_I:13", "NIFTY", "INDEX")
NIFTY_CALL = option("NSE_FNO:40001", "NIFTY", "24500")
NIFTY_FAR_CALL = option("NSE_FNO:40002", "NIFTY", "25000")
NIFTY_EXPIRING = option("NSE_FNO:40003", "NIFTY", "24500", expiry=TODAY)
FINNIFTY_CALL = option("NSE_FNO:40004", "FINNIFTY", "23000")
XYZ_CALL = option("NSE_FNO:40005", "XYZ", "500", lot=500)
XYZ_SHARE = plain("NSE_EQ:40006", "XYZ", "EQUITY")
SENSEX_CALL = option("BSE_FNO:40007", "SENSEX", "80000", lot=20)
SILVER = plain("MCX_COMM:40008", "SILVERM", "FUTCOM", expiry=NEXT_WEEK)
NIFTY_FUTURE = plain("NSE_FNO:40009", "NIFTY", "FUTIDX", expiry=NEXT_WEEK)
ALL = (
    NIFTY_INDEX, NIFTY_CALL, NIFTY_FAR_CALL, NIFTY_EXPIRING, FINNIFTY_CALL, XYZ_CALL, XYZ_SHARE,
    SENSEX_CALL, SILVER, NIFTY_FUTURE,
)  # fmt: skip
SPOTS = {"NIFTY": NIFTY_INDEX.key}


def ctx(
    *,
    ban_list: frozenset[str] | None = frozenset(),
    prices: dict[InstrumentKey, str] | None = None,
    age_s: float = 1,
    spots: dict[str, InstrumentKey] | None = None,
    **limits: LimitValue,
) -> RiskContext:
    """A context with every test instrument known, today's ban list loaded and empty unless
    ``ban_list`` says otherwise, and ``limits`` on top of the shared base limits."""
    base = context(
        limits=parse_limits({**BASE, **limits}), prices=prices, age_s=age_s, extra_instruments=ALL
    )
    known = SPOTS if spots is None else spots
    return replace(base, ban_list=ban_list, spot_key=known.get)


def entry(
    instrument: Instrument, *, limit: str | None = "100", quantity: int | None = None
) -> Order:
    qty = quantity if quantity is not None else instrument.lot_size
    if limit is None:
        return order(BUY, qty, key=instrument.key)  # a market order
    return order(BUY, qty, key=instrument.key, order_type=OrderType.LIMIT, limit_price=D(limit))


def code(decision: Decision) -> str | None:
    return decision.code if decision.severity > Severity.ALLOW else None


# ---- allow and deny lists --------------------------------------------------------------------


def test_not_on_the_allow_list() -> None:
    """Spec "Not on the allow list"."""
    decision = POLICY.pre_trade(
        ctx(allowed_underlyings=["NIFTY", "BANKNIFTY"]), entry(FINNIFTY_CALL)
    )

    assert code(decision) == NOT_ALLOWED
    assert decision.inputs["underlying"] == "FINNIFTY"


def test_deny_wins_over_allow() -> None:
    """Spec "Deny wins over allow"."""
    decision = POLICY.pre_trade(
        ctx(allowed_underlyings=["NIFTY"], denied_underlyings=["NIFTY"]), entry(NIFTY_CALL)
    )

    assert code(decision) == DENIED


def test_an_allowed_underlying_and_unset_lists_pass() -> None:
    assert code(POLICY.pre_trade(ctx(allowed_underlyings=["NIFTY"]), entry(NIFTY_CALL))) is None
    assert code(POLICY.pre_trade(ctx(denied_underlyings=["XYZ"]), entry(NIFTY_CALL))) is None
    assert code(POLICY.pre_trade(ctx(), entry(FINNIFTY_CALL))) is None  # unset allow list


# ---- the F&O ban list ------------------------------------------------------------------------


def test_banned_stock_option() -> None:
    """Spec "Banned stock option"."""
    decision = POLICY.pre_trade(ctx(ban_list=frozenset({"XYZ"})), entry(XYZ_CALL))

    assert code(decision) == FNO_BAN
    assert decision.inputs["underlying"] == "XYZ"


def test_a_banned_name_is_refused_in_futures_and_on_bse_too() -> None:
    assert code(POLICY.pre_trade(ctx(ban_list=frozenset({"NIFTY"})), entry(NIFTY_FUTURE))) == (
        FNO_BAN
    )
    assert code(POLICY.pre_trade(ctx(ban_list=frozenset({"SENSEX"})), entry(SENSEX_CALL))) == (
        FNO_BAN
    )


def test_equity_in_a_banned_name_is_not_refused_by_the_ban_list() -> None:
    """Spec "Equity in a banned name"."""
    assert code(POLICY.pre_trade(ctx(ban_list=frozenset({"XYZ"})), entry(XYZ_SHARE))) is None


def test_commodity_contract_with_no_ban_list_is_not_refused_for_it() -> None:
    """Spec "Commodity contract": the exchange's F&O ban list does not cover MCX."""
    decision = POLICY.pre_trade(ctx(ban_list=None), entry(SILVER))

    assert code(decision) not in {FNO_BAN, BAN_LIST_MISSING}
    assert code(decision) is None


def test_commodity_contract_through_every_registered_rule() -> None:
    """The same scenario through the whole registry: no rule refuses it with a ban-list code."""
    from engine.risk.decision import run_pre_trade

    results = run_pre_trade(
        default_checks(), ctx(ban_list=None, prices={SILVER.key: "100"}), entry(SILVER)
    )

    codes = {r.decision.code for r in results if r.decision.severity > Severity.ALLOW}
    assert not codes & {FNO_BAN, BAN_LIST_MISSING}


def test_the_ban_list_segments_are_exactly_nse_and_bse_fno() -> None:
    """Owner ruling 2026-11-21: only equity derivatives; not the lot caps' set (which covers
    commodity and currency too)."""
    assert {s.value for s in BAN_LIST_SEGMENTS} == {"NSE_FNO", "BSE_FNO"}
    assert BAN_LIST_SEGMENTS != LOT_CAP_EXEMPT_SEGMENTS


@pytest.mark.parametrize("instrument", [NIFTY_CALL, NIFTY_FUTURE, SENSEX_CALL])
def test_morning_without_a_list_refuses_fno_entries(instrument: Instrument) -> None:
    """Spec "Morning without a list" (and F&O entries fail closed without today's ban list)."""
    decision = POLICY.pre_trade(ctx(ban_list=None), entry(instrument))

    assert code(decision) == BAN_LIST_MISSING


def test_an_empty_list_means_no_bans() -> None:
    assert code(POLICY.pre_trade(ctx(ban_list=frozenset()), entry(XYZ_CALL))) is None


def test_without_a_list_cash_equity_and_indexless_entries_pass() -> None:
    assert code(POLICY.pre_trade(ctx(ban_list=None), entry(XYZ_SHARE))) is None


# ---- minimum premium -------------------------------------------------------------------------


def test_premium_too_low() -> None:
    """Spec "Premium too low"."""
    decision = POLICY.pre_trade(ctx(min_premium="5"), entry(NIFTY_CALL, limit="3.50"))

    assert code(decision) == MIN_PREMIUM
    assert decision.inputs == {"price": "3.50", "minimum": "5", "expiring": False}


def test_premium_at_the_minimum_is_allowed_and_a_paisa_below_is_not() -> None:
    limits = ctx(min_premium="5")

    assert code(POLICY.pre_trade(limits, entry(NIFTY_CALL, limit="5.00"))) is None
    assert code(POLICY.pre_trade(limits, entry(NIFTY_CALL, limit="4.99"))) == MIN_PREMIUM


def test_a_market_order_is_judged_at_the_latest_price() -> None:
    cheap = ctx(min_premium="5", prices={NIFTY_CALL.key: "4.95"})
    fine = ctx(min_premium="5", prices={NIFTY_CALL.key: "5.05"})

    assert code(POLICY.pre_trade(cheap, entry(NIFTY_CALL, limit=None))) == MIN_PREMIUM
    assert code(POLICY.pre_trade(fine, entry(NIFTY_CALL, limit=None))) is None


def test_a_market_order_with_no_price_cannot_be_checked_and_is_refused() -> None:
    assert code(POLICY.pre_trade(ctx(min_premium="5"), entry(NIFTY_CALL, limit=None))) == (
        MIN_PREMIUM
    )


def test_the_minimum_premium_applies_to_options_only() -> None:
    limits = ctx(min_premium="5")

    assert code(POLICY.pre_trade(limits, entry(NIFTY_FUTURE, limit="3"))) is None
    assert code(POLICY.pre_trade(limits, entry(XYZ_SHARE, limit="3"))) is None


def test_cheap_expiring_option() -> None:
    """Expiry-day-profile "Cheap expiring option": the higher of the two minimums applies."""
    limits = ctx(min_premium="5", min_premium_expiry="15")

    decision = POLICY.pre_trade(limits, entry(NIFTY_EXPIRING, limit="9"))

    assert code(decision) == MIN_PREMIUM
    assert decision.inputs == {"price": "9", "minimum": "15", "expiring": True}
    # Next week's contract keeps the base minimum.
    assert code(POLICY.pre_trade(limits, entry(NIFTY_CALL, limit="9"))) is None


def test_an_expiry_minimum_alone_applies_only_on_expiry() -> None:
    limits = ctx(min_premium_expiry="15")

    assert code(POLICY.pre_trade(limits, entry(NIFTY_EXPIRING, limit="9"))) == MIN_PREMIUM
    assert code(POLICY.pre_trade(limits, entry(NIFTY_CALL, limit="9"))) is None


# ---- strike distance -------------------------------------------------------------------------


def test_far_out_of_the_money() -> None:
    """Spec "Far out of the money": 3%, NIFTY at 24,000, a 25,000 call (4.17% away)."""
    decision = POLICY.pre_trade(
        ctx(max_strike_distance_pct="3", prices={NIFTY_INDEX.key: "24000"}),
        entry(NIFTY_FAR_CALL),
    )

    assert code(decision) == STRIKE_DISTANCE
    assert decision.inputs == {"strike": "25000", "spot": "24000", "limit_pct": "3"}


def test_a_strike_within_the_distance_and_exactly_at_it_is_allowed() -> None:
    # 24,500 is 2.08% from 24,000; 24,720 is exactly 3%; 24,720.05 is just over.
    near = ctx(max_strike_distance_pct="3", prices={NIFTY_INDEX.key: "24000"})
    at_limit = option("NSE_FNO:40010", "NIFTY", "24720")
    over = option("NSE_FNO:40011", "NIFTY", "24720.05")
    known = replace(near, instruments={**{i.key: i for i in ALL}, at_limit.key: at_limit,
                                       over.key: over}.get)  # fmt: skip

    assert code(POLICY.pre_trade(known, entry(NIFTY_CALL))) is None
    assert code(POLICY.pre_trade(known, entry(at_limit))) is None
    assert code(POLICY.pre_trade(known, entry(over))) == STRIKE_DISTANCE


def test_a_put_below_the_spot_is_measured_the_same_way() -> None:
    put = option("NSE_FNO:40012", "NIFTY", "23000", kind="PE")  # 4.17% below 24,000
    c = ctx(max_strike_distance_pct="3", prices={NIFTY_INDEX.key: "24000"})
    known = replace(c, instruments={**{i.key: i for i in ALL}, put.key: put}.get)

    assert code(POLICY.pre_trade(known, entry(put))) == STRIKE_DISTANCE


def test_underlying_not_priced() -> None:
    """Spec "Underlying not priced": the NIFTY index has no price at all."""
    decision = POLICY.pre_trade(ctx(max_strike_distance_pct="3"), entry(NIFTY_CALL))

    assert code(decision) == UNDERLYING_UNPRICED


def test_a_stale_spot_price_is_not_fresh_and_exactly_stale_s_is() -> None:
    """``stale_s`` is 5 in the base limits: a price exactly 5 s old is still fresh, 5.001 s is
    not (the same edge as ``StaleQuote``)."""
    prices = {NIFTY_INDEX.key: "24000"}

    fresh = ctx(max_strike_distance_pct="3", prices=prices, age_s=5)
    stale = ctx(max_strike_distance_pct="3", prices=prices, age_s=5.001)

    assert code(POLICY.pre_trade(fresh, entry(NIFTY_CALL))) is None
    assert code(POLICY.pre_trade(stale, entry(NIFTY_CALL))) == UNDERLYING_UNPRICED


def test_an_underlying_with_no_spot_instrument_is_unpriced() -> None:
    """FINNIFTY's index row is not known (not in the recorded master), so it fails closed."""
    decision = POLICY.pre_trade(
        ctx(max_strike_distance_pct="3", prices={NIFTY_INDEX.key: "24000"}), entry(FINNIFTY_CALL)
    )

    assert code(decision) == UNDERLYING_UNPRICED


def test_strike_distance_applies_to_options_only_and_not_when_unset() -> None:
    assert code(POLICY.pre_trade(ctx(max_strike_distance_pct="3"), entry(NIFTY_FUTURE))) is None
    assert code(POLICY.pre_trade(ctx(), entry(NIFTY_FAR_CALL))) is None  # unset: not judged


# ---- the order of the steps and an unknown instrument -----------------------------------------


def test_the_first_refusal_wins_in_the_documented_order() -> None:
    """deny -> allow -> ban list -> minimum premium -> strike distance."""
    every = {
        "denied_underlyings": ["XYZ"], "allowed_underlyings": ["NIFTY"], "min_premium": "5",
        "max_strike_distance_pct": "3",
    }  # fmt: skip

    assert code(POLICY.pre_trade(ctx(ban_list=None, **every), entry(XYZ_CALL, limit="1"))) == (
        DENIED
    )
    del every["denied_underlyings"]
    assert code(POLICY.pre_trade(ctx(ban_list=None, **every), entry(XYZ_CALL, limit="1"))) == (
        NOT_ALLOWED
    )
    assert code(POLICY.pre_trade(ctx(ban_list=None, **every), entry(NIFTY_CALL, limit="1"))) == (
        BAN_LIST_MISSING
    )
    assert code(POLICY.pre_trade(ctx(**every), entry(NIFTY_CALL, limit="1"))) == MIN_PREMIUM
    assert code(POLICY.pre_trade(ctx(**every), entry(NIFTY_FAR_CALL))) == UNDERLYING_UNPRICED


@pytest.mark.parametrize(
    ("key", "limits", "ban_list", "expected"),
    [
        ("NSE_FNO:99999", {"denied_underlyings": ["XYZ"]}, frozenset(), DENIED),
        ("NSE_FNO:99999", {"allowed_underlyings": ["NIFTY"]}, frozenset(), NOT_ALLOWED),
        ("NSE_FNO:99999", {}, None, BAN_LIST_MISSING),
        ("NSE_FNO:99999", {}, frozenset(), FNO_BAN),
        # Outside the ban-list segments, so the later steps are reached:
        ("MCX_COMM:99999", {"min_premium_expiry": "15"}, None, MIN_PREMIUM),
        ("MCX_COMM:99999", {"max_strike_distance_pct": "3"}, None, STRIKE_DISTANCE),
    ],
)
def test_an_unknown_instrument_fails_closed_at_each_step_that_needs_it(
    key: str, limits: dict[str, LimitValue], ban_list: frozenset[str] | None, expected: str
) -> None:
    unknown = order(BUY, 65, key=InstrumentKey.parse(key))

    assert code(POLICY.pre_trade(ctx(ban_list=ban_list, **limits), unknown)) == expected


def test_an_unknown_instrument_with_nothing_to_check_passes_the_policy() -> None:
    """Nothing set and not a ban-list segment: the policy has nothing to judge (the lot caps and
    validation refuse an unknown contract on their own)."""
    unknown = order(BUY, 1, key=InstrumentKey.parse("MCX_COMM:99999"))

    assert code(POLICY.pre_trade(ctx(ban_list=None), unknown)) is None


# ---- exits -----------------------------------------------------------------------------------


def test_the_policy_is_an_entry_rule_and_registered_last() -> None:
    assert POLICY.applies_to_exits is False
    assert [c.name for c in default_checks()][-1] == "instrument_policy"


@pytest.mark.parametrize("bans", [None, frozenset({"XYZ"})], ids=["no list", "banned"])
async def test_exiting_a_banned_position_is_never_refused(bans: frozenset[str] | None) -> None:
    """Spec "Exiting a banned position" (and "Instrument policy never applies to exits"), through
    the real gate with every part of the policy set to refuse: the deny list, an allow list that
    leaves it out, the ban list (holding it, or missing), the minimum premium and the strike
    distance with no spot price."""
    state = StubState(day_state())
    state.bans = bans
    held = position(XYZ_CALL.key, 500, "1.00")
    instruments = {i.key: i for i in ALL}.get
    every = {
        **BASE, "denied_underlyings": ["XYZ"], "allowed_underlyings": ["NIFTY"],
        "min_premium": "50", "min_premium_expiry": "60", "max_strike_distance_pct": "1",
    }  # fmt: skip
    h = await harness(
        checks=[InstrumentPolicy()], limits_values=every, positions=[held], state=state,
        instruments=instruments,
    )  # fmt: skip

    reason = await h.gate.check(
        order(SELL, 500, key=XYZ_CALL.key, order_type=OrderType.LIMIT, limit_price=D("1.00"))
    )
    entry_reason = await h.gate.check(
        order(BUY, 500, key=XYZ_CALL.key, order_type=OrderType.LIMIT, limit_price=D("1.00"),
              order_id="o2", idempotency_key="k2")
    )  # fmt: skip

    assert reason is None  # the exit goes through
    assert (entry_reason or "").startswith(DENIED)  # while an entry in the same name is refused
