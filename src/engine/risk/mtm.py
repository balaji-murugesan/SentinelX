"""Prices and the day's MTM as the risk engine sees them (add-risk-engine design §7, §10).

``MtmTracker`` is fed every tick and answers two different questions, deliberately differently:

  * the LOSS side ("what is my P&L if I stop now?") uses the *latest* price, even an unconfirmed
    outlier. It is not debounced (PRD §7.1): a real crash must trip the floor at once, so a
    suspicious low tick is believed.
  * the PEAK ("how high did the day get?") uses only *accepted* prices that are still fresh. A
    one-tick spike must never raise the peak, because the equity floor trails the peak upwards:
    a bad high tick would lift the floor and could kill a healthy account on the way back down.

A tick that jumps more than ``outlier_pct`` percent from the last accepted price is an *outlier*.
It is not accepted until the next tick of that instrument stays within the same percentage of it
(a genuine gap: the market really moved). If the next tick is back near the old level instead,
the spike is dropped. Percentages are percent numbers, so ``Decimal("20")`` means 20%.

A tick whose price is not a finite number above zero (the feed can produce NaN or infinity from
a damaged packet) is *rejected*: it changes nothing, not even the time of the last tick, so
garbage can neither move a price nor make a silent feed look alive.

Where to change the two thresholds: ``outlier_pct`` and ``stale_after`` are the ``outlier_pct``
and ``stale_s`` risk limits; the monitor (task 7.1) passes them in from the stored limits.

No I/O, no clock of its own: every method that needs the time takes ``now``.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from contracts.market import Instrument, InstrumentKey
from contracts.orders import Fill, Position
from engine.market_data.provider import Tick
from engine.positions.book import PositionId
from engine.risk.floor import MAX_AMOUNT
from engine.risk.pnl import DayPnl, day_pnl


class TickKind(StrEnum):
    """What a tick turned out to be (a ``StrEnum``: a C# enum whose members are also strings)."""

    ACCEPTED = "ACCEPTED"  # an ordinary tick, within the band of the last accepted price
    OUTLIER = "OUTLIER"  # a jump beyond the band, awaiting confirmation (audit RISK_OUTLIER_TICK)
    CONFIRMED = "CONFIRMED"  # the next tick agreed with a pending outlier: a genuine gap
    REJECTED = "REJECTED"  # not a finite price above zero: ignored entirely (the caller logs it)


@dataclass
class _Series:
    """What is known about one instrument. Mutable on purpose: it changes on every tick (a
    plain class would do the same; a dataclass just writes the constructor)."""

    latest: Decimal
    latest_at: datetime
    accepted: Decimal
    accepted_at: datetime
    pending: Decimal | None = None  # an outlier awaiting confirmation


@dataclass(frozen=True)
class MtmReading:
    mtm_day: Decimal  # the day's P&L net of charges, valued at the latest prices
    peak: Decimal  # the day's highest MTM_day from fresh accepted prices; starts at 0
    reference_missing: tuple[PositionId, ...] = ()


class MtmTracker:
    # ``*,`` makes every parameter after it keyword-only, like C# named arguments.
    def __init__(self, *, outlier_pct: Decimal, stale_after: timedelta) -> None:
        if not outlier_pct.is_finite() or outlier_pct <= 0:
            raise ValueError("outlier_pct must be above zero")
        if stale_after <= timedelta(0):
            # Zero or negative would make every price "stale" and freeze the peak for good.
            raise ValueError("stale_after must be above zero")
        self._band = outlier_pct / 100
        self._stale_after = stale_after
        self._series: dict[InstrumentKey, _Series] = {}
        self._peak = Decimal(0)

    # ---- prices ---------------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> TickKind:
        price, at = tick.ltp, tick.received_at
        if not price.is_finite() or price <= 0:
            return TickKind.REJECTED  # garbage: leave every price and time untouched
        series = self._series.get(tick.key)
        if series is None:
            self._series[tick.key] = _Series(price, at, price, at)  # nothing to compare with yet
            return TickKind.ACCEPTED
        series.latest, series.latest_at = price, at

        if series.pending is not None:
            pending, series.pending = series.pending, None
            if self._within_band(price, pending):
                series.accepted, series.accepted_at = price, at  # the gap was real
                return TickKind.CONFIRMED
            # Not confirmed: the spike is dropped, and this tick is judged on its own below.

        if self._within_band(price, series.accepted):
            series.accepted, series.accepted_at = price, at
            return TickKind.ACCEPTED
        series.pending = price
        return TickKind.OUTLIER

    def _within_band(self, price: Decimal, reference: Decimal) -> bool:
        return abs(price - reference) <= self._band * reference

    def latest_price(self, key: InstrumentKey) -> Decimal | None:
        series = self._series.get(key)
        return None if series is None else series.latest

    def accepted_price(self, key: InstrumentKey) -> Decimal | None:
        series = self._series.get(key)
        return None if series is None else series.accepted

    def age(self, key: InstrumentKey, now: datetime) -> timedelta | None:
        """How long ago the latest tick for ``key`` arrived (None: no tick yet)."""
        series = self._series.get(key)
        return None if series is None else now - series.latest_at

    # ---- the day's P&L and peak -----------------------------------------------------------

    # A read-only property: the peak cannot be assigned, only raised (``raise_peak``) or reset
    # for a new day (``new_day``), because the equity floor trails it and a ratchet that can be
    # lowered is not a ratchet (safety rule 15).
    @property
    def peak(self) -> Decimal:
        return self._peak

    def raise_peak(self, value: Decimal) -> None:
        """Take ``value`` if it is higher than the peak. A lower, non-finite or absurdly large
        value (above ``MAX_AMOUNT``, 1e12 rupees: a corrupt price, not a profit) is ignored, so a
        restart can pass what it stored and recomputed: the peak becomes the larger. Without the
        upper bound one bad tick would set a peak that ``floor_of`` refuses, blinding the floor
        check for the rest of the day."""
        if value.is_finite() and self._peak < value <= MAX_AMOUNT:
            self._peak = value

    def new_day(self) -> None:
        """Start a trading day: the peak returns to 0. Prices are kept (they are still current)."""
        self._peak = Decimal(0)

    def evaluate(
        self,
        positions: Iterable[Position],
        fills_today: Iterable[Fill],
        references: Mapping[PositionId, Decimal],
        instruments: Callable[[InstrumentKey], Instrument | None],
        now: datetime,
    ) -> MtmReading | None:
        """The day's MTM at the latest prices, and the peak updated from fresh accepted ones.

        Returns None when MTM is unknown (see ``day_pnl``): the caller treats that as "blind",
        never as zero. The peak is updated only when EVERY open position has an accepted price
        that is fresh; otherwise it stays where it was.
        """
        held = list(positions)
        fills = list(fills_today)  # read twice below: once per valuation
        latest = {k: s.latest for k, s in self._series.items()}
        valued = day_pnl(held, fills, latest, references, instruments)
        if valued is None:
            return None
        accepted = self._fresh_accepted(now)
        for_peak: DayPnl | None = day_pnl(held, fills, accepted, references, instruments)
        if for_peak is not None:
            self.raise_peak(for_peak.net)
        return MtmReading(valued.net, self._peak, valued.reference_missing)

    def _fresh_accepted(self, now: datetime) -> dict[InstrumentKey, Decimal]:
        return {
            key: series.accepted
            for key, series in self._series.items()
            if now - series.accepted_at <= self._stale_after
        }
