"""Position sizing: how many units an entry may be for the per-trade risk limit (add-risk-engine
task 6.3; risk-rules spec "Position sizing rounds down to whole lots").

Trading terms:
  * lot size      - futures and options trade in whole lots (NIFTY: 65 units per lot, say), so a
                    quantity is always a whole number of lots. Cash equity has a lot size of 1.
  * risk per unit - the distance from the entry price to the initial stop.
  * sizing        - the largest whole number of lots whose total risk does not exceed the per-trade
                    maximum. It *rounds down*: it never sizes a trade up past the limit.

``size_order`` is a pure function. ``SizingService`` answers the ``SizeOrder`` command with it: it
reads the limits and the instrument master, **changes no state** (no order, no counter, no
idempotency record) and so can be asked as often as the owner likes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final

import structlog

from contracts.commands import CommandResult, SizeOrder
from contracts.market import Instrument, InstrumentKey
from contracts.orders import OrderSide
from engine.market_data.clock import utc_now
from engine.risk.checks.common import money, risk_per_unit, stop_on_loss_side
from engine.risk.checks.stops import NO_STOP
from engine.risk.decision import Decision, reject
from engine.risk.limits import RiskLimits
from engine.risk.limits_service import LimitsService

log = structlog.get_logger("engine.risk.sizing")

SIZE_BELOW_ONE_LOT: Final = "RISK_SIZE_BELOW_ONE_LOT"
SIZE_UNMEASURABLE: Final = "RISK_SIZE_UNMEASURABLE"


@dataclass(frozen=True)
class Sized:
    quantity: int  # units: a whole number of lots
    risk_amount: Decimal  # rupees lost if the initial stop is hit at this quantity


def size_order(
    limits: RiskLimits,
    instrument: Instrument,
    side: OrderSide,
    entry_price: Decimal,
    initial_stop: Decimal,
) -> Sized | Decision:
    """The size for an entry, or a REJECT ``Decision`` saying why there is none (a stop on the
    wrong side, or even one lot risks more than the limit). The answer is *advisory*: it does not
    cap at the exchange's freeze quantity and does not check the stop's tick grid, both of which
    the order itself still has to pass (validation and ``InitialStop``).

    ``Sized | Decision`` is a union: the caller checks which one it got (C# would use a result
    type or two methods)."""
    if not stop_on_loss_side(side, entry_price, initial_stop):
        return reject(
            NO_STOP,
            f"initial stop {money(initial_stop)} is on the wrong side of the entry price "
            f"{money(entry_price)} for a {side.value}",
            stop=money(initial_stop),
            entry=money(entry_price),
        )
    per_unit = risk_per_unit(entry_price, initial_stop)  # above zero: the stop is on the loss side
    risk_of_one_lot = per_unit * instrument.lot_size
    try:
        # ``//`` on Decimals is floor division (exact, no float): whole lots in the budget.
        lots = int(limits.per_trade_max_risk // risk_of_one_lot)
    except InvalidOperation:
        # A quotient of more than 28 digits (a stop a hair from the entry price against a huge
        # budget) is not a size anyone could trade: say so instead of raising.
        return reject(
            SIZE_UNMEASURABLE,
            "the entry price and stop are too close together to size against this limit",
            entry=money(entry_price),
            stop=money(initial_stop),
        )
    if lots < 1:
        return reject(
            SIZE_BELOW_ONE_LOT,
            f"one lot of {instrument.lot_size} risks {money(risk_of_one_lot)}, more than the "
            f"per-trade maximum {money(limits.per_trade_max_risk)}",
            risk_of_one_lot=money(risk_of_one_lot),
            limit=money(limits.per_trade_max_risk),
            lot_size=instrument.lot_size,
        )
    quantity = lots * instrument.lot_size
    return Sized(quantity, per_unit * quantity)


class SizingService:
    def __init__(
        self,
        *,
        account_id: str,
        limits: LimitsService,
        instruments: Callable[[InstrumentKey], Instrument | None],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._account_id = account_id
        self._limits = limits
        self._instruments = instruments
        self._now = now

    async def size(self, command: SizeOrder) -> CommandResult:
        """Answer one sizing request. Never raises (a command that raised would be left pending
        and redelivered at every restart), never changes any state."""
        try:
            return self._size(command)
        except Exception as error:
            log.error("sizing failed", exc_info=True)
            return self._answer(command, reason=f"{SIZE_UNMEASURABLE}: {type(error).__name__}")

    def _size(self, command: SizeOrder) -> CommandResult:
        if (refusal := self._limits.reason_for()) is not None:
            return self._answer(command, reason=refusal)
        limits = self._limits.current
        instrument = self._instruments(command.key)
        if limits is None or instrument is None:
            return self._answer(command, reason=f"{command.key} is not in the instrument master")
        outcome = size_order(
            limits, instrument, command.side, command.entry_price, command.initial_stop
        )
        if isinstance(outcome, Decision):
            return self._answer(command, reason=outcome.reason())
        return self._answer(command, sized=outcome)

    def _answer(
        self, command: SizeOrder, *, reason: str | None = None, sized: Sized | None = None
    ) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=self._account_id,
            accepted=sized is not None,
            reason=reason,
            sized_quantity=None if sized is None else sized.quantity,
            risk_amount=None if sized is None else sized.risk_amount,
            decided_at=self._now(),
        )
