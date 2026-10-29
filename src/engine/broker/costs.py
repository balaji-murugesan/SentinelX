"""Trading costs charged on each fill (paper-broker spec "Trading costs on every fill").

ONE rate table, the place to change when a rate changes (design §7). Rates are fractions of
turnover (price x quantity; for options, the premium). Each component is rounded to the
paisa, half up, as on a contract note.

Trading terms:
  * brokerage  - the broker's fee per executed order.
  * STT        - securities transaction tax, levied by the government.
  * exchange   - NSE's transaction charge.
  * SEBI fee   - the regulator's turnover fee (Rs 10 per crore = 0.0001%).
  * stamp duty - a state tax, on buys only.
  * GST        - 18% on brokerage + exchange charge + SEBI fee (not on STT or stamp duty).

Owner check against Dhan's brokerage calculator (task 4.4), 2026-10-28: OPTION corrected
(STT sell 0.15%, exchange 0.03553%; 65 x 200 sell = 48.56, buy = 29.45). EQUITY_INTRADAY
brokerage, STT, SEBI, stamp and GST match. Dhan's displayed exchange line (0.00307%) is
Rs 0.10 per Rs 1L above our 0.00297%, but its own GST base implies 0.00297%; reproduced at
Rs 1L and Rs 10L, so treated as a Dhan calculator inconsistency and the rate is kept.
FUTURE and EQUITY_DELIVERY rates are NOT verified.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from contracts.market import ExchangeSegment
from contracts.orders import CostBreakdown, OrderSide, ProductType

PAISA = Decimal("0.01")


class CostCategory(StrEnum):
    OPTION = "option"
    FUTURE = "future"
    EQUITY_INTRADAY = "equity_intraday"
    EQUITY_DELIVERY = "equity_delivery"


@dataclass(frozen=True)
class Rates:
    brokerage_flat: Decimal  # rupees per executed order
    brokerage_pct_cap: Decimal | None  # brokerage is the lower of flat and this x turnover
    stt_buy: Decimal
    stt_sell: Decimal
    exchange: Decimal
    sebi: Decimal
    stamp_buy: Decimal
    gst: Decimal = Decimal("0.18")


def pct(text: str) -> Decimal:
    """'0.03503' percent -> 0.0003503 as a fraction."""
    return Decimal(text) / 100


SEBI = pct("0.0001")
RATES: dict[CostCategory, Rates] = {
    CostCategory.OPTION: Rates(
        brokerage_flat=Decimal("20"), brokerage_pct_cap=None,
        stt_buy=Decimal("0"), stt_sell=pct("0.15"),
        exchange=pct("0.03553"), sebi=SEBI, stamp_buy=pct("0.003"),
    ),
    CostCategory.FUTURE: Rates(
        brokerage_flat=Decimal("20"), brokerage_pct_cap=pct("0.03"),
        stt_buy=Decimal("0"), stt_sell=pct("0.02"),
        exchange=pct("0.00173"), sebi=SEBI, stamp_buy=pct("0.002"),
    ),
    CostCategory.EQUITY_INTRADAY: Rates(
        brokerage_flat=Decimal("20"), brokerage_pct_cap=pct("0.03"),
        stt_buy=Decimal("0"), stt_sell=pct("0.025"),
        exchange=pct("0.00297"), sebi=SEBI, stamp_buy=pct("0.003"),
    ),
    CostCategory.EQUITY_DELIVERY: Rates(
        brokerage_flat=Decimal("0"), brokerage_pct_cap=None,
        stt_buy=pct("0.1"), stt_sell=pct("0.1"),
        exchange=pct("0.00297"), sebi=SEBI, stamp_buy=pct("0.015"),
    ),
}  # fmt: skip


def category(segment: ExchangeSegment, instrument_type: str, product: ProductType) -> CostCategory:
    if segment is ExchangeSegment.NSE_FNO:
        return CostCategory.OPTION if instrument_type.startswith("OPT") else CostCategory.FUTURE
    if product is ProductType.CNC:
        return CostCategory.EQUITY_DELIVERY
    return CostCategory.EQUITY_INTRADAY


def fill_costs(
    cost_category: CostCategory, side: OrderSide, quantity: int, price: Decimal
) -> CostBreakdown:
    """Costs of one fill of ``quantity`` units at ``price``."""
    r = RATES[cost_category]
    turnover = price * quantity
    brokerage = r.brokerage_flat
    if r.brokerage_pct_cap is not None:
        brokerage = min(brokerage, turnover * r.brokerage_pct_cap)
    stt = turnover * (r.stt_buy if side is OrderSide.BUY else r.stt_sell)
    exchange = turnover * r.exchange
    sebi = turnover * r.sebi
    stamp = turnover * r.stamp_buy if side is OrderSide.BUY else Decimal("0")
    brokerage, exchange, sebi = _paisa(brokerage), _paisa(exchange), _paisa(sebi)
    return CostBreakdown(
        brokerage=brokerage,
        stt=_paisa(stt),
        exchange=exchange,
        sebi=sebi,
        stamp_duty=_paisa(stamp),
        gst=_paisa((brokerage + exchange + sebi) * r.gst),
    )


def _paisa(amount: Decimal) -> Decimal:
    return amount.quantize(PAISA, rounding=ROUND_HALF_UP)
