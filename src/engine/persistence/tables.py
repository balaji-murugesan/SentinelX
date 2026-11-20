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
    # Added by migration 0004 (add-risk-engine): a stop-market's trigger, and the stop an entry
    # was sized against. Both are exchange prices on the tick grid, and NULL on older rows.
    sa.Column("trigger_price", PRICE),
    sa.Column("initial_stop", PRICE),
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

# One row per account: set when restart reconciliation finds a difference, cleared only by the
# explicit resume command (design §9). Kept after clearing (halted = false) as an audit trail.
recovery_state = sa.Table(
    "recovery_state",
    metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("halted", sa.Boolean, nullable=False),
    sa.Column("differences", sa.JSON, nullable=False),
    sa.Column("halted_at", sa.DateTime(timezone=True)),
    sa.Column("cleared_at", sa.DateTime(timezone=True)),
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


# ---- risk engine (add-risk-engine, migration 0004) -----------------------------------------

# The account's risk limits: one JSON document of flat key -> text/number/flag/None, plus a
# version that rises on every save. There is no default row: until the owner sets limits, every
# entry is rejected (risk-limits spec "Limits are stored in Postgres with no built-in defaults").
risk_limits = sa.Table(
    "risk_limits",
    metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("limits", sa.JSON, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# One row per account and IST trading day. A new date has no row, so counters, halt and peak
# start fresh: that is the "post-close reset" (design §6). The columns that may only move one way
# are written with GREATEST (see ``PostgresRiskRepository.save_day``), so a stale or out-of-order
# write can never lower them. ``floor`` is NULL until limits exist; GREATEST ignores NULLs.
# ``halt_level`` has no CHECK constraint on purpose: 20 is reserved for a level a later change adds.
risk_day_state = sa.Table(
    "risk_day_state",
    metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("trade_date", sa.Date, primary_key=True),
    sa.Column("peak", EXACT, nullable=False),
    sa.Column("floor", EXACT),
    sa.Column("halt_level", sa.Integer, nullable=False),
    sa.Column("halt_reason", sa.Text),
    sa.Column("entries_today", sa.Integer, nullable=False),
    sa.Column("loss_streak", sa.Integer, nullable=False),
    sa.Column("cooldown_until", sa.DateTime(timezone=True)),
    # SQL NULL (not JSON null) when unset, so COALESCE can keep the first value written.
    sa.Column("close_marks", sa.JSON(none_as_null=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Append-only audit of every risk decision (risk-gate spec "Every risk decision is audited").
# The migration revokes UPDATE and DELETE from the engine role, so the engine can add rows but
# never change or remove one.
risk_event = sa.Table(
    "risk_event",
    metadata,
    sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("rule", sa.Text, nullable=False),
    sa.Column("phase", sa.Text, nullable=False),
    sa.Column("decision", sa.Text, nullable=False),
    sa.Column("reason_code", sa.Text),
    sa.Column("inputs", sa.JSON, nullable=False),
    sa.Column("enforced", sa.Boolean, nullable=False),
    sa.Column("latency_us", sa.Integer, nullable=False),
    sa.Index("ix_risk_event_account_at", "account_id", "at"),
)
