"""CLI order commands: ``order place``, ``order cancel``, ``orders``, ``positions`` (task 7.3),
and the risk commands ``kill``, ``risk set-limits``, ``risk size`` and ``risk status``
(add-risk-engine task 11.2).

The CLI never talks to the broker or the database. It writes a command to the engine's Redis
stream and waits for the reply (the same path the API will use), and it shows orders, positions
and the risk figures by reading the engine's snapshot, so it never computes P&L or risk itself.

Run inside the engine container, because Redis is not published to the host:
``docker compose exec engine python -m cli order place NSE_FNO:53720 BUY 65 --product INTRADAY``
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from redis.asyncio import Redis

from contracts.commands import (
    COMMAND_STREAM,
    AnyCommand,
    CancelOrder,
    CommandResult,
    KillSwitch,
    LimitValue,
    PlaceOrder,
    ResumeRecovery,
    SetRiskLimits,
    SizeOrder,
    reply_stream,
)
from contracts.market import InstrumentKey
from contracts.orders import Order, OrderSide, OrderType, ProductType
from contracts.snapshot import PositionView, RiskStatus, Snapshot, snapshot_key

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
    initial_stop: Decimal | None = None,
) -> int:
    """``initial_stop`` is where the trade is wrong: an entry needs it (the risk engine sizes the
    risk from it and places the catastrophic stop from the fill)."""
    try:
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
            initial_stop=initial_stop,
        )
    except ValidationError as error:  # e.g. a price of zero or below: refused before sending
        out(f"Not sent: {_one_line(error)}")
        return REJECTED
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


async def resume_recovery(
    redis: Redis, *, account_id: str, out: Out, wait_s: float = REPLY_WAIT_S
) -> int:
    """Ask the engine to re-check and clear its HALTED state. Review the differences first
    (``orders`` / ``positions`` show them): this is the only way a halt is cleared."""
    command = ResumeRecovery(
        command_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        account_id=account_id,
        source="cli",
        sent_at=datetime.now(UTC),
    )
    return await _send(redis, command, out, wait_s)


async def kill(redis: Redis, *, account_id: str, out: Out, wait_s: float = REPLY_WAIT_S) -> int:
    """Stop trading for the day: the engine cancels pending entries and exits every position, and
    refuses entries until the next trading day. There is no intraday undo (the ``--yes`` flag on
    the command line is the confirmation). It works with the API and the dashboard down: this
    path only needs Redis and the engine. Telegram ``/kill`` is the other way in."""
    command = KillSwitch(
        command_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        account_id=account_id,
        source="cli",
        sent_at=datetime.now(UTC),
    )
    return await _send(redis, command, out, wait_s)


def read_limits_file(path: Path) -> dict[str, LimitValue]:
    """Read a limits file: one JSON object of flat keys, e.g. ``{"daily_max_loss": "3000"}``.

    Money and percentages are written as strings ("3000", "0.5"): a JSON number with a fraction
    is refused here, because it would be a ``float`` and a rounding error must never enter a risk
    limit. Whole numbers (counts, seconds) are written as numbers. Keys that start with an
    underscore are comments and are dropped (JSON has no comments, and the engine refuses unknown
    keys). Raises ``ValueError`` with a message for the user.
    """

    def refuse_float(text: str) -> object:
        raise ValueError(f'{text} is a fractional number: write it as a string, e.g. "{text}"')

    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=refuse_float)
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error.strerror or error}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold one JSON object of limit names and values")
    return {key: value for key, value in data.items() if not key.startswith("_")}


async def set_limits(
    redis: Redis, *, account_id: str, path: Path, out: Out, wait_s: float = REPLY_WAIT_S
) -> int:
    """Send the limits in ``path``. The engine checks every key and value and refuses an edit that
    loosens a limit while the market is open (it names the keys); the file is never half-applied."""
    try:
        limits = read_limits_file(path)
        command = SetRiskLimits(
            command_id=uuid.uuid4().hex,
            idempotency_key=uuid.uuid4().hex,
            account_id=account_id,
            source="cli",
            sent_at=datetime.now(UTC),
            limits=limits,
        )
    except (ValueError, ValidationError) as error:
        out(f"Not sent: {error}")
        return REJECTED
    return await _send(redis, command, out, wait_s)


async def size_order(
    redis: Redis,
    *,
    account_id: str,
    key: InstrumentKey,
    side: OrderSide,
    entry_price: Decimal,
    initial_stop: Decimal,
    out: Out,
    wait_s: float = REPLY_WAIT_S,
) -> int:
    """Ask the engine how many units fit the per-trade risk limit. Changes nothing."""
    try:
        command = SizeOrder(
            command_id=uuid.uuid4().hex,
            idempotency_key=uuid.uuid4().hex,
            account_id=account_id,
            source="cli",
            sent_at=datetime.now(UTC),
            key=key,
            side=side,
            entry_price=entry_price,
            initial_stop=initial_stop,
        )
    except ValidationError as error:  # e.g. a price of zero or below: refused before sending
        out(f"Not sent: {_one_line(error)}")
        return REJECTED
    return await _send(redis, command, out, wait_s)


def _one_line(error: ValidationError) -> str:
    """pydantic's own text spans several lines; the user needs the field and what is wrong with it,
    e.g. ``entry_price: Input should be greater than 0``."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )


async def _send(redis: Redis, command: AnyCommand, out: Out, wait_s: float) -> int:
    await redis.xadd(COMMAND_STREAM, {"data": command.model_dump_json()})
    result = await _wait_for_reply(redis, command.command_id, wait_s)
    if result is None:
        if isinstance(command, KillSwitch):
            # A kill is idempotent per account and day (a second one places nothing new), so in an
            # emergency the right advice is the opposite of the one below: look, and send it again.
            out(
                f"Result unknown: no reply within {wait_s:g} s. The kill may still be processed. "
                f"Check `risk status` (halt: KILL means it was taken); the kill may be sent "
                f"again, a second one is harmless."
            )
            return UNKNOWN
        # Never resend: the engine may have processed it already (rule 4/5 spirit).
        out(
            f"Result unknown: no reply within {wait_s:g} s. The command may still be processed. "
            f"Check `orders` and `positions` before doing anything else; it was NOT resent."
        )
        return UNKNOWN
    if result.accepted:
        out(_accepted_line(result))
        return ACCEPTED
    out(f"Rejected: {result.reason}")
    return REJECTED


def _accepted_line(result: CommandResult) -> str:
    if result.sized_quantity is not None:  # the answer to `risk size`
        return f"Accepted. size: {result.sized_quantity} units, risk {result.risk_amount}"
    line = f"Accepted. order id: {result.order_id}" if result.order_id else "Accepted."
    # An accepted command can still carry a note: a second kill says the day is already stopped.
    return f"{line} ({result.reason})" if result.reason else line


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
    _show_halt(snapshot, out)
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
    _show_halt(snapshot, out)
    out(_age_line(snapshot))
    if not snapshot.positions:
        out("No positions today.")
    for view in snapshot.positions:
        out(format_position(view))
    return ACCEPTED


async def show_risk(redis: Redis, *, account_id: str, out: Out) -> int:
    """The engine's risk figures for today, read from its snapshot (the CLI computes nothing)."""
    snapshot = await read_snapshot(redis, account_id)
    if snapshot is None:
        out("No snapshot in Redis: the engine is not running or has not written one yet.")
        return UNKNOWN
    _show_halt(snapshot, out)
    out(_age_line(snapshot))
    if snapshot.risk is None:
        out("No risk figures yet: the engine has not produced any (are limits set?).")
        return ACCEPTED
    for line in format_risk(snapshot.risk):
        out(line)
    return ACCEPTED


def format_risk(r: RiskStatus) -> list[str]:
    def money(value: Decimal | None) -> str:
        return "unknown" if value is None else f"{value:.2f}"

    lines = [
        f"day P&L (net of charges) : {money(r.mtm_day)}",
        f"peak                     : {money(r.peak)}",
        f"equity floor             : {money(r.floor)}",
        f"halt                     : {r.halt_level.name}"
        + (f" ({r.halt_reason})" if r.halt_reason else ""),
        f"entries today            : {r.entries_today}",
        f"loss streak              : {r.loss_streak}",
    ]
    if r.cooldown_until is not None:
        lines.append(f"cooldown until           : {r.cooldown_until.isoformat()}")
    for ref in r.unprotected:
        lines.append(f"!! no catastrophic stop  : {ref.key} {ref.product.value}")
    return lines


def _show_halt(snapshot: Snapshot, out: Out) -> None:
    if not snapshot.halted:
        return
    out("!! HALTED: new orders are refused until the differences below are fixed and you run")
    out("!!         `python -m cli recovery resume` (cancels and fills are still processed)")
    for difference in snapshot.halt_differences:
        out(f"!!   - {difference}")


def _age_line(snapshot: Snapshot) -> str:
    age = (datetime.now(UTC) - snapshot.taken_at).total_seconds()
    return f"-- engine snapshot taken {age:.0f} s ago (account {snapshot.account_id})"


def format_order(o: Order) -> str:
    """One line per order. The type, the trigger (a resting stop has one, not a limit price) and
    the source (``cli``, or the risk engine's ``risk-stop`` / ``risk-kill`` / ``risk-squareoff``)
    are what tell a protective stop apart from an ordinary SELL."""
    price = f" @ {o.limit_price}" if o.limit_price is not None else ""
    trigger = f" trigger {o.trigger_price}" if o.trigger_price is not None else ""
    return (
        f"{o.order_id}  {o.state.value:<15} {o.side.value:<4} {o.quantity} x {o.key}"
        f" {o.order_type.value}{price}{trigger}"
        f"  {o.product.value}  filled {o.filled_quantity}  source {o.source}"
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
