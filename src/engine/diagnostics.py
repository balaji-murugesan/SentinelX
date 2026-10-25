"""Engine side of the diagnostic ping/pong. Answers pings only: no orders, no state, no broker."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from opentelemetry import propagate, trace
from pydantic import ValidationError
from redis.asyncio import Redis

from contracts.diagnostics import (
    PING_STREAM,
    PONG_TTL_SECONDS,
    DiagnosticPing,
    DiagnosticPong,
    pong_key,
)
from observability.tracing import get_tracer

log = structlog.get_logger("engine.diagnostics")


async def _newest_id(redis: Redis) -> str:
    """Id of the newest ping already in the stream, so only later pings are answered.

    XREAD's "$" means "from now" but is re-evaluated on every call, so pings arriving between
    two reads would be lost. Remembering a concrete id avoids that gap.
    """
    newest = await cast("Any", redis.xrevrange(PING_STREAM, count=1))
    return str(newest[0][0]) if newest else "0-0"


async def _answer(redis: Redis, fields: dict[str, str]) -> None:
    try:
        ping = DiagnosticPing.model_validate_json(fields["data"])
    except (KeyError, ValidationError):
        log.warning("ignoring malformed diagnostic ping")
        return

    # Continue the sender's trace. Extracting from the message (not from asyncio's ambient
    # context) is what makes this a real cross-process hop.
    parent = propagate.extract({"traceparent": ping.traceparent}) if ping.traceparent else None
    with get_tracer("engine.diagnostics").start_as_current_span(
        "diagnostics.answer_ping", context=parent, kind=trace.SpanKind.CONSUMER
    ) as span:
        span.set_attribute("diagnostics.ping_id", ping.ping_id)
        pong = DiagnosticPong(ping_id=ping.ping_id, received_at=datetime.now(UTC))
        key = pong_key(ping.ping_id)
        await cast("Any", redis.xadd(key, {"data": pong.model_dump_json()}))
        await cast("Any", redis.expire(key, PONG_TTL_SECONDS))


async def answer_pings(redis: Redis, stop: asyncio.Event, *, block_ms: int = 500) -> None:
    """Read pings until ``stop`` is set, replying to each on its own pong stream."""
    last_id = await _newest_id(redis)
    while not stop.is_set():
        batches = await cast("Any", redis.xread({PING_STREAM: last_id}, count=10, block=block_ms))
        for _stream, messages in batches or []:
            for message_id, fields in messages:
                last_id = message_id
                await _answer(redis, fields)
