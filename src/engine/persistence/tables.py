"""Trading tables (add-order-position-core design §1, §3), as SQLAlchemy Core ``Table``s.

Core tables (not ORM classes) keep rows as plain data: the engine converts them to and from
the frozen contract models itself, so nothing is lazily loaded or implicitly flushed.

Money columns:
  * exchange prices (limit, fill) are ``numeric(14, 2)``: they are on the tick grid;
  * computed values (average price, realized P&L, costs) are unconstrained ``NUMERIC``, which
    stores the exact Python Decimal. A weighted average such as 100.0333... must not be
    rounded, or positions rebuilt from fills would differ from stored ones by a paisa.
Every table carries ``account_id`` (D-020); times are ``timestamptz`` (stored UTC).
"""

import sqlalchemy as sa

metadata = sa.MetaData()

PRICE = sa.Numeric(14, 2)
EXACT = sa.Numeric()  # unconstrained: any precision and scale

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("segment", sa.Text, nullable=False),
    sa.Column("security_id", sa.Text, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("order_type", sa.Text, nullable=False),
    sa.Column("limit_price", PRICE),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("filled_quantity", sa.Integer, nullable=False),
    sa.Column("average_fill_price", EXACT),
    sa.Column("broker_order_id", sa.Text),
    sa.Column("paper", sa.Boolean, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_orders_account_state", "account_id", "state"),
)

# One row per state change: the audit trail (spec "Audit trail").
order_events = sa.Table(
    "order_events",
    metadata,
    sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), nullable=False),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_order_events_order", "order_id"),
)

fills = sa.Table(
    "fills",
    metadata,
    sa.Column("fill_id", sa.Text, primary_key=True),
    sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), nullable=False),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("segment", sa.Text, nullable=False),
    sa.Column("security_id", sa.Text, nullable=False),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("price", PRICE, nullable=False),
    sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("brokerage", EXACT, nullable=False),
    sa.Column("stt", EXACT, nullable=False),
    sa.Column("exchange", EXACT, nullable=False),
    sa.Column("sebi", EXACT, nullable=False),
    sa.Column("stamp_duty", EXACT, nullable=False),
    sa.Column("gst", EXACT, nullable=False),
    sa.Column("paper", sa.Boolean, nullable=False),
    # Order of application: positions are rebuilt by replaying fills in this order.
    sa.Column("seq", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
    sa.Index("ix_fills_account_filled_at", "account_id", "filled_at"),
)

positions = sa.Table(
    "positions",
    metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("segment", sa.Text, primary_key=True),
    sa.Column("security_id", sa.Text, primary_key=True),
    sa.Column("product", sa.Text, primary_key=True),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("average_price", EXACT, nullable=False),
    sa.Column("realized_pnl", EXACT, nullable=False),
    sa.Column("costs", EXACT, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Every processed command's result, keyed for idempotency (spec "Idempotency key on every
# command"); kept after restart so a repeated key still returns the first answer.
command_results = sa.Table(
    "command_results",
    metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("idempotency_key", sa.Text, primary_key=True),
    sa.Column("command_id", sa.Text, nullable=False),
    sa.Column("accepted", sa.Boolean, nullable=False),
    sa.Column("order_id", sa.Text),
    sa.Column("reason", sa.Text),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
)
