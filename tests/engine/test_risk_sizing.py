"""Position sizing (add-risk-engine task 6.3; risk-rules spec "Position sizing rounds down to
whole lots").

Scenarios: "Round down" and "Less than a lot", plus a ``hypothesis`` property: the sized risk never
exceeds the budget, and one more lot always would.
"""

from dataclasses import replace
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from contracts.commands import SetRiskLimits, SizeOrder
from contracts.market import InstrumentKey
from contracts.orders import OrderSide
from engine.risk.checks.stops import NO_STOP
from engine.risk.decision import Decision, Severity
from engine.risk.limits_service import LimitsService
from engine.risk.sizing import (
    SIZE_BELOW_ONE_LOT,
    SIZE_UNMEASURABLE,
    Sized,
    SizingService,
    size_order,
)
from tests.engine.risk_builders import LIMITS, NOW, OPTION, STOCK, instrument_of
from tests.engine.test_risk_limits import BASE
from tests.engine.test_risk_limits_service import service as limits_service

D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
LOT_65 = instrument_of(OPTION)  # lot size 65
SHARE = instrument_of(STOCK)  # lot size 1


def sized(outcome: Sized | Decision) -> Sized:
    assert isinstance(outcome, Sized), outcome
    return outcome


def test_round_down() -> None:
    """Spec "Round down": limit 1,000, lot 65, risk per unit 10.00: 1 lot (65 units, risk 650.00),
    because 2 lots would risk 1,300.00."""
    assert LOT_65 is not None
    result = sized(size_order(LIMITS, LOT_65, BUY, D("200.00"), D("190.00")))

    assert result.quantity == 65 and result.risk_amount == D("650.00")


def test_less_than_a_lot() -> None:
    """Spec "Less than a lot": the risk of one lot already exceeds the limit."""
    assert LOT_65 is not None
    outcome = size_order(LIMITS, LOT_65, BUY, D("200.00"), D("180.00"))  # 20.00 x 65 = 1,300.00

    assert isinstance(outcome, Decision)
    assert outcome.severity is Severity.REJECT and outcome.code == SIZE_BELOW_ONE_LOT
    assert outcome.inputs["risk_of_one_lot"] == "1300.00" and outcome.inputs["limit"] == "1000"


def test_cash_equity_sizes_in_whole_shares() -> None:
    assert SHARE is not None
    result = sized(size_order(LIMITS, SHARE, BUY, D("200.00"), D("192.50")))  # 7.50 per share

    assert result.quantity == 133 and result.risk_amount == D("997.50")  # 1000 // 7.5 = 133


def test_a_whole_number_of_several_lots() -> None:
    assert LOT_65 is not None
    result = sized(size_order(LIMITS, LOT_65, BUY, D("200.00"), D("199.00")))  # 1.00 per unit

    assert result.quantity == 15 * 65 and result.risk_amount == D("975.00")  # 1000 // 65 = 15 lots


def test_the_budget_exactly_fills_a_whole_number_of_lots() -> None:
    assert LOT_65 is not None
    limits = replace(LIMITS, per_trade_max_risk=D("1300.00"))

    result = sized(size_order(limits, LOT_65, BUY, D("200.00"), D("190.00")))

    assert result.quantity == 2 * 65 and result.risk_amount == D("1300.00")  # at, not over


def test_a_short_entry_is_sized_from_a_stop_above() -> None:
    assert LOT_65 is not None
    result = sized(size_order(LIMITS, LOT_65, SELL, D("200.00"), D("210.00")))

    assert result.quantity == 65


def test_a_stop_on_the_wrong_side_gets_no_size() -> None:
    assert LOT_65 is not None
    for side, stop in ((BUY, "205.00"), (BUY, "200.00"), (SELL, "190.00"), (SELL, "200.00")):
        outcome = size_order(LIMITS, LOT_65, side, D("200.00"), D(stop))

        assert isinstance(outcome, Decision) and outcome.code == NO_STOP


@given(
    budget=st.decimals(min_value=1, max_value=1_000_000, places=2),
    per_unit=st.decimals(min_value="0.05", max_value=500, places=2),
    lot=st.sampled_from([1, 25, 50, 65, 75, 100]),
    side=st.sampled_from([BUY, SELL]),
)
def test_sized_risk_never_exceeds_the_budget_and_one_more_lot_always_would(
    budget: Decimal, per_unit: Decimal, lot: int, side: OrderSide
) -> None:
    base = instrument_of(OPTION)
    assert base is not None
    instrument = base.model_copy(update={"lot_size": lot})
    limits = replace(LIMITS, per_trade_max_risk=budget)
    entry = D("1000.00")
    stop = entry - per_unit if side is BUY else entry + per_unit

    outcome = size_order(limits, instrument, side, entry, stop)

    risk_of_one_lot = per_unit * lot
    if isinstance(outcome, Decision):
        assert outcome.code == SIZE_BELOW_ONE_LOT
        assert risk_of_one_lot > budget  # not even one lot fits
    else:
        assert outcome.quantity % lot == 0 and outcome.quantity >= lot  # whole lots
        assert outcome.risk_amount <= budget  # never over the budget
        assert outcome.risk_amount == per_unit * outcome.quantity
        assert outcome.risk_amount + risk_of_one_lot > budget  # and one more lot always would


# ---- the command -----------------------------------------------------------------------------


def command(entry: str = "200.00", stop: str = "190.00", key: InstrumentKey = OPTION) -> SizeOrder:
    return SizeOrder(
        command_id="c1", idempotency_key="k1", account_id="main", source="cli", sent_at=NOW,
        key=key, side=BUY, entry_price=D(entry), initial_stop=D(stop),
    )  # fmt: skip


async def sizing(with_limits: bool = True) -> tuple[SizingService, LimitsService]:
    limits, *_ = limits_service()
    if with_limits:
        await limits.set_limits(
            SetRiskLimits(
                command_id="s", idempotency_key="s", account_id="main", source="t",
                sent_at=NOW, limits=BASE,
            )
        )  # fmt: skip
    else:
        await limits.load()
    return SizingService(account_id="main", limits=limits, instruments=instrument_of), limits


async def test_the_command_answers_with_the_quantity_and_the_risk() -> None:
    service, _ = await sizing()

    result = await service.size(command())

    assert result.accepted and result.sized_quantity == 65 and result.risk_amount == D("650.00")
    assert result.command_id == "c1" and result.reason is None


async def test_the_command_answers_a_rejection_with_its_code() -> None:
    service, _ = await sizing()

    result = await service.size(command(stop="180.00"))

    assert not result.accepted and (result.reason or "").startswith(SIZE_BELOW_ONE_LOT)
    assert result.sized_quantity is None and result.risk_amount is None


async def test_without_limits_there_is_nothing_to_size_against() -> None:
    service, _ = await sizing(with_limits=False)

    result = await service.size(command())

    assert not result.accepted and (result.reason or "").startswith("RISK_LIMITS_NOT_SET")


async def test_an_unknown_instrument_is_refused() -> None:
    service, _ = await sizing()

    result = await service.size(command(key=InstrumentKey.parse("NSE_FNO:99999")))

    assert not result.accepted and "not in the instrument master" in (result.reason or "")


async def test_sizing_changes_no_state() -> None:
    """Asking again and again writes no audit row, stores no command result and touches no limit."""
    limits, risk, results, _ = limits_service()
    await limits.set_limits(
        SetRiskLimits(
            command_id="s", idempotency_key="s", account_id="main", source="t", sent_at=NOW,
            limits=BASE,
        )
    )  # fmt: skip
    service = SizingService(account_id="main", limits=limits, instruments=instrument_of)
    before = (len(risk.events), dict(results.results), limits.current)

    for _ in range(3):
        await service.size(command())

    assert (len(risk.events), dict(results.results), limits.current) == before


async def test_a_repeated_key_is_answered_afresh_from_the_limits_then_in_force() -> None:
    """Order-commands "Size command repeated": a size command changes nothing, so its answer is not
    remembered by idempotency key (unlike every other command). The same key after the per-trade
    maximum was tightened is judged against the tightened limit, and nothing is stored."""
    limits, risk, results, _ = limits_service()
    await limits.set_limits(
        SetRiskLimits(
            command_id="s", idempotency_key="s", account_id="main", source="t", sent_at=NOW,
            limits=BASE,
        )
    )  # fmt: skip
    service = SizingService(account_id="main", limits=limits, instruments=instrument_of)

    first = await service.size(command())  # key "k1": one lot of 65 risks 650.00, limit 1,000

    assert first.accepted and first.sized_quantity == 65
    tightened = await limits.set_limits(
        SetRiskLimits(
            command_id="t", idempotency_key="t", account_id="main", source="t", sent_at=NOW,
            limits={**BASE, "per_trade_max_risk": "500"},
        )
    )  # fmt: skip
    assert tightened.accepted, tightened.reason
    before = (len(risk.events), dict(results.results))

    second = await service.size(command())  # the same key "k1", now one lot (650.00) exceeds 500

    assert not second.accepted and (second.reason or "").startswith(SIZE_BELOW_ONE_LOT)
    assert "500" in (second.reason or "") and second.sized_quantity is None
    assert (len(risk.events), dict(results.results)) == before  # no audit row, no stored result
    assert ("main", "k1") not in results.results


async def test_a_size_that_cannot_be_measured_is_refused_not_raised() -> None:
    """A stop a hair from the entry against a huge budget makes a quotient of more than 28 digits,
    which Decimal cannot hold. The answer is a refusal: a command that raised would be left
    pending and redelivered at every restart."""
    assert SHARE is not None
    huge = replace(LIMITS, per_trade_max_risk=D("1000000000000"))
    stop = D("200") - D("1E-17")

    outcome = size_order(huge, SHARE, BUY, D("200"), stop)

    assert isinstance(outcome, Decision) and outcome.code == SIZE_UNMEASURABLE
    limits, *_ = limits_service()
    await limits.set_limits(
        SetRiskLimits(
            command_id="s", idempotency_key="s", account_id="main", source="t", sent_at=NOW,
            limits={**BASE, "per_trade_max_risk": "1000000000000"},
        )
    )  # fmt: skip
    service = SizingService(account_id="main", limits=limits, instruments=instrument_of)

    reply = await service.size(command(entry="200", stop="199.99999999999999999", key=STOCK))

    assert not reply.accepted and (reply.reason or "").startswith(SIZE_UNMEASURABLE)


async def test_the_consumer_routes_size_order_to_the_sizing_service() -> None:
    from typing import cast

    from redis.asyncio import Redis

    from engine.orders.commands import CommandConsumer
    from tests.engine.test_order_service import build

    service, _ = await sizing()
    order_service, adapter, repo = build()
    consumer = CommandConsumer(
        cast(Redis, None), order_service, account_id="main", size=service.size
    )

    result = await consumer._process(command())

    assert result.accepted and result.sized_quantity == 65
    assert adapter.placed == [] and repo.orders == {}  # sizing touched no order path


async def test_a_failure_anywhere_in_sizing_is_answered_not_raised() -> None:
    """The service never raises: a command that raised would be left pending and redelivered at
    every restart."""
    limits, *_ = limits_service()
    await limits.set_limits(
        SetRiskLimits(
            command_id="s", idempotency_key="s", account_id="main", source="t", sent_at=NOW,
            limits=BASE,
        )
    )  # fmt: skip

    def broken(key: InstrumentKey) -> None:
        raise RuntimeError("instrument master exploded")

    service = SizingService(account_id="main", limits=limits, instruments=broken)  # type: ignore[arg-type]

    reply = await service.size(command())

    assert not reply.accepted and (reply.reason or "").startswith(SIZE_UNMEASURABLE)
