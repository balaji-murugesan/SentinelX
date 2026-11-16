"""Small builders for the risk-engine tests: instruments, positions and fills."""

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from contracts.market import Instrument, InstrumentKey
from contracts.orders import (
    CostBreakdown,
    Fill,
    Order,
    OrderSide,
    OrderType,
    Position,
    ProductType,
)
from engine.positions.book import position_id
from engine.risk.decision import Quote, RiskContext
from engine.risk.limits import RiskLimits, parse_limits
from engine.risk.state import DayState
from tests.engine.test_risk_limits import BASE

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)  # 10:30 IST, inside market hours
OPTION = InstrumentKey.parse("NSE_FNO:53720")
STOCK = InstrumentKey.parse("NSE_EQ:1333")

INSTRUMENTS: dict[InstrumentKey, Instrument] = {
    OPTION: Instrument(
        key=OPTION, trading_symbol="NIFTY-Nov2026-22600-CE", display_name="NIFTY 22600 CE",
        underlying_symbol="NIFTY", instrument_type="OPTIDX", lot_size=65,
        tick_size=Decimal("0.05"), freeze_qty=1800,
    ),
    STOCK: Instrument(
        key=STOCK, trading_symbol="HDFCBANK", display_name="HDFC Bank",
        underlying_symbol="HDFCBANK", instrument_type="EQUITY", lot_size=1,
        tick_size=Decimal("0.05"), freeze_qty=None,
    ),
}  # fmt: skip


def instrument_of(key: InstrumentKey) -> Instrument | None:
    return INSTRUMENTS.get(key)


def make_instrument(key: str, underlying: str = "NIFTY", lot_size: int = 65) -> Instrument:
    """An extra instrument for tests that need several (open-position limits)."""
    parsed = InstrumentKey.parse(key)
    return Instrument(
        key=parsed, trading_symbol=f"{underlying}-{parsed.security_id}",
        display_name=f"{underlying} {parsed.security_id}", underlying_symbol=underlying,
        instrument_type="OPTIDX", lot_size=lot_size, tick_size=Decimal("0.05"), freeze_qty=1800,
    )  # fmt: skip


def position(
    key: InstrumentKey = OPTION,
    quantity: int = 65,
    average: str = "100.00",
    product: ProductType = ProductType.INTRADAY,
    **overrides: object,
) -> Position:
    values: dict[str, object] = {
        "account_id": "main",
        "key": key,
        "product": product,
        "quantity": quantity,
        "average_price": Decimal(average),
        "updated_at": NOW,
    }
    return Position(**{**values, **overrides})  # type: ignore[arg-type]


def fill(
    side: OrderSide,
    quantity: int,
    price: str,
    *,
    key: InstrumentKey = OPTION,
    product: ProductType = ProductType.INTRADAY,
    costs: str = "0",
    breakdown: CostBreakdown | None = None,
    fill_id: str = "f",
) -> Fill:
    return Fill(
        fill_id=fill_id, order_id="o1", account_id="main", key=key, product=product, side=side,
        quantity=quantity, price=Decimal(price), filled_at=NOW,
        costs=breakdown or CostBreakdown(brokerage=Decimal(costs)), paper=True,
    )  # fmt: skip


def order(
    side: OrderSide,
    quantity: int,
    *,
    key: InstrumentKey = OPTION,
    product: ProductType = ProductType.INTRADAY,
    order_type: OrderType = OrderType.MARKET,
    **overrides: object,
) -> Order:
    values: dict[str, object] = {
        "order_id": "o1",
        "account_id": "main",
        "key": key,
        "side": side,
        "quantity": quantity,
        "product": product,
        "order_type": order_type,
        "idempotency_key": "k1",
        "source": "test",
        "paper": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Order(**{**values, **overrides})  # type: ignore[arg-type]


LIMITS: RiskLimits = parse_limits(BASE)
TODAY = date(2026, 11, 16)  # the IST date of NOW


def day_state(**overrides: object) -> DayState:
    values: dict[str, object] = {"account_id": "main", "trade_date": TODAY, "updated_at": NOW}
    return DayState(**{**values, **overrides})  # type: ignore[arg-type]


def context(
    *,
    limits: RiskLimits = LIMITS,
    day: DayState | None = None,
    mtm_day: Decimal | None = Decimal(0),
    positions: Iterable[Position] = (),
    orders: Iterable[Order] = (),
    prices: Mapping[InstrumentKey, str] | None = None,
    age_s: float = 1,
    now: datetime = NOW,
    extra_instruments: Iterable[Instrument] = (),
    feed_silent_for: timedelta | None = None,
    feed_subscribed: bool = False,
    unpriced_for: Mapping[InstrumentKey, timedelta] | None = None,
) -> RiskContext:
    """A context with sane defaults: limits set, no positions, a fresh price of 100 for each
    instrument in ``prices`` (``age_s`` seconds old)."""
    quotes = {k: Quote(Decimal(v), timedelta(seconds=age_s)) for k, v in (prices or {}).items()}
    known = {**INSTRUMENTS, **{i.key: i for i in extra_instruments}}
    return RiskContext(
        account_id="main",
        now=now,
        limits=limits,
        day=day or day_state(),
        mtm_day=mtm_day,
        positions={position_id(p): p for p in positions},
        orders={o.order_id: o for o in orders},
        instruments=known.get,
        quote=quotes.get,
        feed_silent_for=feed_silent_for,
        feed_subscribed=feed_subscribed,
        unpriced_for=unpriced_for or {},
    )
