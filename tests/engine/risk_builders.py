"""Small builders for the risk-engine tests: instruments, positions and fills."""

from datetime import UTC, datetime
from decimal import Decimal

from contracts.market import Instrument, InstrumentKey
from contracts.orders import CostBreakdown, Fill, OrderSide, Position, ProductType

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
