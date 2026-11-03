"""CLI order commands: ``order place``, ``order cancel``, ``orders``, ``positions`` (task 7.3).

The CLI never talks to the broker or the database. It writes a command to the engine's Redis
stream and waits for the reply (the same path the API will use), and it shows orders and
positions by reading the engine's snapshot, so it never computes P&L itself.

Run inside the engine container, because Redis is not published to the host:
``docker compose exec engine python -m cli order place NSE_FNO:53720 BUY 65 --product INTRADAY``
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from redis.asyncio import Redis

from contracts.commands import COMMAND_STREAM, CancelOrder, CommandResult, PlaceOrder, reply_stream
from contracts.market import InstrumentKey
from contracts.orders import Order, OrderSide, OrderType, ProductType
from contracts.snapshot import PositionView, Snapshot, snapshot_key

REPLY_WAIT_S = 10.0
Out = Callable[[str], None]

# Exit codes: 0 accepted, 1 rejected by the engine, 2 no answer (the outcome is unknown).
ACCEPTED, REJECTED, UNKNOWN = 0, 1, 2


async def place_order(
    redis: Redis,
    *,
    account_id: str,
    key: InstrumentKey,
    side: OrderSide,
    quantity: int,
    product: ProductType,
    limit_price: Decimal | None,
    out: Out,
    wait_s: float = REPLY_WAIT_S,
) -> int:
    command = PlaceOrder(
        command_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,  # fresh per invocation: a new intent each time
        account_id=account_id,
        source="cli",
        sent_at=datetime.now(UTC),
        key=key,
        side=side,
        quantity=quantity,
        product=product,
        order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
        limit_price=limit_price,
    )
    return await _send(redis, command, out, wait_s)


async def cancel_order(
    redis: Redis, *, account_id: str, order_id: str, out: Out, wait_s: float = REPLY_WAIT_S
) -> int:
    command = CancelOrder(
        command_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        account_id=account_id,
        source="cli",
        sent_at=datetime.now(UTC),
        order_id=order_id,
    )
    return await _send(redis, command, out, wait_s)


async def _send(redis: Redis, command: PlaceOrder | CancelOrder, out: Out, wait_s: float) -> int:
    await redis.xadd(COMMAND_STREAM, {"data": command.model_dump_json()})
    result = await _wait_for_reply(redis, command.command_id, wait_s)
    if result is None:
        # Never resend: the engine may have processed it already (rule 4/5 spirit).
        out(
            f"Result unknown: no reply within {wait_s:g} s. The command may still be processed. "
            f"Check `orders` and `positions` before doing anything else; it was NOT resent."
        )
        return UNKNOWN
    if result.accepted:
        out(f"Accepted. order id: {result.order_id}")
        return ACCEPTED
    out(f"Rejected: {result.reason}")
    return REJECTED


async def _wait_for_reply(redis: Redis, command_id: str, wait_s: float) -> CommandResult | None:
    """Block on the command's reply stream (XREAD from the start) for up to ``wait_s``."""
    batches = await cast(
        "Any", redis.xread({reply_stream(command_id): "0"}, count=1, block=int(wait_s * 1000))
    )
    for _stream, messages in batches or []:
        for _id, fields in messages:
            return CommandResult.model_validate_json(fields["data"])
    return None


async def read_snapshot(redis: Redis, account_id: str) -> Snapshot | None:
    raw = await redis.get(snapshot_key(account_id))
    return Snapshot.model_validate_json(raw) if raw else None


async def show_orders(redis: Redis, *, account_id: str, out: Out) -> int:
    snapshot = await read_snapshot(redis, account_id)
    if snapshot is None:
        out("No snapshot in Redis: the engine is not running or has not written one yet.")
        return UNKNOWN
    out(_age_line(snapshot))
    if not snapshot.open_orders:
        out("No open orders.")
    for order in snapshot.open_orders:
        out(format_order(order))
    return ACCEPTED


async def show_positions(redis: Redis, *, account_id: str, out: Out) -> int:
    snapshot = await read_snapshot(redis, account_id)
    if snapshot is None:
        out("No snapshot in Redis: the engine is not running or has not written one yet.")
        return UNKNOWN
    out(_age_line(snapshot))
    if not snapshot.positions:
        out("No positions today.")
    for view in snapshot.positions:
        out(format_position(view))
    return ACCEPTED


def _age_line(snapshot: Snapshot) -> str:
    age = (datetime.now(UTC) - snapshot.taken_at).total_seconds()
    return f"-- engine snapshot taken {age:.0f} s ago (account {snapshot.account_id})"


def format_order(o: Order) -> str:
    price = f" @ {o.limit_price}" if o.limit_price is not None else ""
    return (
        f"{o.order_id}  {o.state.value:<15} {o.side.value:<4} {o.quantity} x {o.key}{price}"
        f"  {o.product.value}  filled {o.filled_quantity}"
    )


def format_position(v: PositionView) -> str:
    p = v.position

    def show(value: Decimal | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    return (
        f"{p.key!s:<16} {p.product.value:<8} qty {p.quantity:>6}  avg {p.average_price:.2f}"
        f"  last {show(v.last_price)}  MTM {show(v.mtm)}  realized {p.realized_pnl:.2f}"
        f"  costs {p.costs:.2f}  net {show(v.net_pnl)}"
    )


def connect(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)


async def run_with_redis(url: str, redis: Redis | None, action: Callable[[Redis], Any]) -> int:
    """Run ``action(redis)`` with a connection that is closed afterwards (``await using``)."""
    client = redis or connect(url)
    try:
        return int(await action(client))
    finally:
        if redis is None:
            await client.aclose()
