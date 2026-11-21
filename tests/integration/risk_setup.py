"""Shared setup for the integration tests that run the whole engine with the real risk gate.

Every order now passes the risk engine, so a test that places one needs limits, an initial stop
on an entry, and a moment for the monitor to start. The limits here are *test values*: nothing
about them is a recommendation. They are chosen so a rule can never end a test by accident:

  * the entry cut-off and square-off are at the very end of the day, so the time the test runs
    does not decide whether an entry is allowed;
  * the loss limit is huge and every optional floor term is off, so the simulated random walk can
    never breach the floor and trigger a kill;
  * the price band is wide, so a far resting limit order still passes the price check;
  * the lot caps, entry pacing and rejection breaker (add-risk-guards) are far above anything a
    test does, and every optional add-risk-guards limit is unset.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis

from contracts.commands import (
    COMMAND_STREAM,
    CommandResult,
    KillSwitch,
    LimitValue,
    PlaceOrder,
    SetRiskLimits,
    reply_stream,
)

ALL_DAY: dict[str, LimitValue] = {
    "per_trade_max_risk": "100000", "daily_max_loss": "10000000",
    "catastrophic_sl_pct": "40", "price_band_pct": "100", "outlier_pct": "100",
    "exit_buffer_pct": "2",
    "max_trades_per_day": 50, "loss_streak_limit": 50, "max_open_total": 20,
    "max_open_per_underlying": 20, "max_chase_steps": 5,
    "cooldown_seconds": 900, "stale_s": 60, "feed_down_s": 120, "mtm_blind_s": 600,
    "chase_interval_s": 2, "exit_escalate_s": 30,
    "entry_cutoff": "23:58", "square_off_time": "23:59",
    "lock_mode": "OFF", "trail_loss_x": None, "trail_loss_y": None, "lock_trigger": None,
    "lock_at": None, "trail_step": None, "trail_by": None, "giveback_arm": None,
    "giveback_abs": None, "giveback_pct": None,
    "max_open_lots_per_underlying": 100, "max_open_lots_total": 100,
    "max_gross_lots_per_day": 1000, "max_entries_per_minute": 100, "reject_breaker_n": 100,
    "min_entry_gap_s": 1,
}  # fmt: skip


async def reply(redis: Redis, command_id: str, wait_s: float = 30) -> CommandResult:
    """Wait for one command's reply. Generous: the engine reads commands only after recovery's
    first attempt, and a cold start (or a slow Postgres connect) takes a few seconds."""
    for _ in range(int(wait_s / 0.02)):
        entries = await redis.xrange(reply_stream(command_id))
        if entries:
            return CommandResult.model_validate_json(entries[0][1]["data"])
        await asyncio.sleep(0.02)
    raise AssertionError(f"no reply for {command_id}")


async def send_limits(
    redis: Redis, limits: dict[str, LimitValue] | None = None, key: str = "limits-1"
) -> CommandResult:
    """Send a ``SetRiskLimits`` command through the stream, like ``cli risk set-limits``."""
    command = SetRiskLimits(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", source="test",
        sent_at=datetime.now(UTC), limits=limits or ALL_DAY,
    )  # fmt: skip
    await redis.xadd(COMMAND_STREAM, {"data": command.model_dump_json()})
    return await reply(redis, command.command_id)


def with_stop(command: PlaceOrder, stop: str = "90") -> PlaceOrder:
    """The same command with an initial stop: an entry is refused without one."""
    return command.model_copy(update={"initial_stop": Decimal(stop)})


async def place_when_ready(
    redis: Redis, make: Callable[[str], PlaceOrder], *, prefix: str = "e", tries: int = 100
) -> tuple[str, CommandResult]:
    """Place an order, retrying with a NEW idempotency key while the engine is not ready.

    Right after start the gate refuses entries (the monitor has not run yet, no price has arrived
    yet), and a refused key is final (its result is stored), so each retry needs a new key.
    Returns the key that was accepted and its result.
    """
    last: CommandResult | None = None
    for attempt in range(tries):
        key = f"{prefix}-{attempt}"
        last = await _send(redis, make(key))
        if last.accepted:
            return key, last
        await asyncio.sleep(0.1)
    raise AssertionError(f"the order was never accepted; last answer: {last and last.reason}")


async def _send(redis: Redis, command: PlaceOrder) -> CommandResult:
    await redis.xadd(COMMAND_STREAM, {"data": command.model_dump_json()})
    return await reply(redis, command.command_id)


async def send_kill(redis: Redis, key: str = "kill-1", wait_s: float = 30) -> CommandResult:
    """Send the ``KillSwitch`` command through the stream, like ``cli kill --yes``."""
    command = KillSwitch(
        command_id=f"c-{key}", idempotency_key=key, account_id="main", source="test",
        sent_at=datetime.now(UTC),
    )  # fmt: skip
    await redis.xadd(COMMAND_STREAM, {"data": command.model_dump_json()})
    return await reply(redis, command.command_id, wait_s)
