"""The exposure rules: how many positions, how late in the day, how close to the floor (add-risk-
engine task 6.5; risk-rules spec "Maximum open positions" and "Entry cutoff"; equity-floor spec
"Entries must leave headroom above the floor").

  * ``OpenPositions`` (``max_open_positions``) - an entry that would open a position in a new
    instrument is refused when the open positions plus the unfilled entry orders in *other*
    instruments have reached the total limit (``RISK_OPEN_POSITIONS``) or the per-underlying limit
    (``RISK_OPEN_POSITIONS_UNDERLYING``). Positions carried from earlier days count. Adding to a
    position already held (same instrument and product) is not a new position.
  * ``EntryCutoff`` (``entry_cutoff``)          - no entry at or after the cutoff time (IST)
    (``RISK_ENTRY_CUTOFF``). An entry whose contract expires today is judged against the expiry
    cutoff when ``entry_cutoff_expiry`` is set, and refused at or after it with
    ``RISK_EXPIRY_CUTOFF`` (add-risk-guards task 8.1; expiry-day-profile "Entries in expiring
    contracts use the expiry cutoff").
  * ``FloorHeadroom`` (``equity_floor``)        - the entry's worst case must leave ``MTM_day``
    above the floor: ``MTM_day - risk`` at or below the floor is refused. It uses the same
    ``next_floor`` as the breach check and the edit re-simulation, so the three cannot disagree. It
    cannot be run in shadow mode. The floor uses the expiry giveback once the account has expiring
    exposure, or when this entry's own contract expires today (expiry-day-profile "An expiring
    entry must leave headroom under the expiry floor").

Both read the expiry date from the instrument master only; a contract with no master record is
not expiring here (it is refused before the gate; see ``_expires_today``).

Trading terms: underlying - the thing an option or future is written on (NIFTY for any NIFTY
contract); unfilled entry - an order that would open or grow a position and is still resting;
expiry day - the IST date a contract expires on (from the instrument master), when its premium
moves fastest; giveback - how much of the day's peak profit may be handed back before the floor
stops the account (the expiry giveback is a smaller, stricter one).
"""

from typing import Final

from contracts.market import InstrumentKey
from contracts.orders import Order
from engine.market_data.clock import IST
from engine.positions.book import PositionId
from engine.risk.checks.common import entry_price, money, risk_per_unit
from engine.risk.decision import ALLOW, Decision, RiskContext, reject
from engine.risk.expiry import effective_cutoff, expires_today
from engine.risk.floor import next_floor

OPEN_POSITIONS: Final = "RISK_OPEN_POSITIONS"
OPEN_POSITIONS_UNDERLYING: Final = "RISK_OPEN_POSITIONS_UNDERLYING"
ENTRY_CUTOFF: Final = "RISK_ENTRY_CUTOFF"
EXPIRY_CUTOFF: Final = "RISK_EXPIRY_CUTOFF"
FLOOR_HEADROOM: Final = "RISK_FLOOR_HEADROOM"


def _exposure(ctx: RiskContext, order: Order) -> set[PositionId]:
    """The positions the account is already exposed to or about to be: every open position plus
    the (instrument, product) of every order still resting. Orders in a final state do not count.
    No need to ask whether a resting order is an entry: an exit needs an open position, and that
    is already in the set, so counting it again changes nothing."""
    exposed = {pid for pid, held in ctx.positions.items() if held.quantity != 0}
    for resting in ctx.orders.values():
        if resting.state.is_final or resting.order_id == order.order_id:
            continue
        exposed.add((resting.account_id, resting.key, resting.product))
    return exposed


class OpenPositions:
    name = "max_open_positions"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        mine = (order.account_id, order.key, order.product)
        held = {pid for pid, p in ctx.positions.items() if p.quantity != 0}
        if mine in held:
            return ALLOW  # adding to a position already held is not a new position
        # What is open or pending in *other* instruments: an unfilled entry in this very instrument
        # is not another position (it would be the same one).
        exposed = _exposure(ctx, order) - {mine}
        if len(exposed) >= ctx.limits.max_open_total:
            return reject(
                OPEN_POSITIONS,
                f"{len(exposed)} positions open or pending, the maximum is "
                f"{ctx.limits.max_open_total}",
                open_or_pending=len(exposed),
                limit=ctx.limits.max_open_total,
            )
        underlying = _underlying(ctx, order.key)
        if underlying is None:
            return reject(
                OPEN_POSITIONS_UNDERLYING,
                "instrument facts are unavailable, so its underlying cannot be counted",
                instrument=str(order.key),
            )
        # An exposed position whose instrument has no master record cannot be placed under any
        # underlying, so it is counted against this one: better to refuse one entry too many
        # than to let an unknown contract hide a concentration.
        same = sum(1 for pid in exposed if _underlying(ctx, pid[1]) in (underlying, None))
        if same >= ctx.limits.max_open_per_underlying:
            return reject(
                OPEN_POSITIONS_UNDERLYING,
                f"{same} positions open or pending on {underlying}, the maximum is "
                f"{ctx.limits.max_open_per_underlying}",
                underlying=underlying,
                open_or_pending=same,
                limit=ctx.limits.max_open_per_underlying,
            )
        return ALLOW


def _underlying(ctx: RiskContext, key: InstrumentKey) -> str | None:
    instrument = ctx.instruments(key)
    return None if instrument is None else instrument.underlying_symbol


def _expires_today(ctx: RiskContext, order: Order) -> bool:
    """Does the entry's contract expire today, by its master expiry date (expiry-day-profile "A
    contract expires today only by its master expiry date")? A contract with no master record
    does not: no entry for one reaches these checks in production, because the order service's
    validation refuses it before the gate, and ``RiskEngine._count_entry`` refuses it again."""
    instrument = ctx.instruments(order.key)
    return instrument is not None and expires_today(instrument, ctx.now)


class EntryCutoff:
    name = "entry_cutoff"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        local = ctx.now.astimezone(IST).time()
        expiring = _expires_today(ctx, order)
        cutoff = effective_cutoff(ctx.limits, expiring)
        if local < cutoff:
            return ALLOW
        # The expiry code whenever the expiry cutoff is the one in force for this entry. (A set
        # override is never later than the base cutoff, so it is then the binding one.)
        by_expiry = expiring and ctx.limits.entry_cutoff_expiry is not None
        return reject(
            EXPIRY_CUTOFF if by_expiry else ENTRY_CUTOFF,
            f"no new entries{' in a contract expiring today' if by_expiry else ''} at or after "
            f"{cutoff:%H:%M} IST (it is {local:%H:%M})",
            now=f"{local:%H:%M:%S}",
            cutoff=f"{cutoff:%H:%M}",
            expiring=expiring,
        )


class FloorHeadroom:
    name = "equity_floor"
    applies_to_exits = False

    def pre_trade(self, ctx: RiskContext, order: Order) -> Decision:
        if ctx.mtm_day is None:
            return reject(
                FLOOR_HEADROOM,
                "the day's P&L is unknown, so there is no way to tell how close the floor is",
                mtm_day="unknown",
            )
        # The expiry giveback applies once the account has had expiring exposure today (the
        # monitor's and the day book's one-way flag), and to an entry that would create it: the
        # entry must leave headroom under the floor it would itself switch on.
        expiry = ctx.day.expiry_exposure or _expires_today(ctx, order)
        # next_floor raises if the limits cannot make a floor: the gate then rejects the entry.
        floor = next_floor(ctx.day.floor, ctx.limits.floor_params(expiry=expiry), ctx.day.peak)
        price, stop = entry_price(order, ctx), order.initial_stop
        if price is not None and stop is not None:
            risk = risk_per_unit(price, stop) * order.quantity
        else:
            risk = ctx.limits.per_trade_max_risk  # unknown risk: assume the most it may be
        if ctx.mtm_day - risk <= floor:
            return reject(
                FLOOR_HEADROOM,
                f"MTM_day {money(ctx.mtm_day)} less this entry's risk {money(risk)} would be at "
                f"or below the floor {money(floor)}",
                mtm_day=money(ctx.mtm_day),
                risk=money(risk),
                floor=money(floor),
            )
        return ALLOW
