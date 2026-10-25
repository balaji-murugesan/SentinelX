"""Api side of the diagnostic ping/pong: send a ping through Redis, wait for the engine's pong."""

import time
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from opentelemetry import propagate, trace
from redis.asyncio import Redis

from contracts.diagnostics import PING_STREAM, DiagnosticPing, DiagnosticPong, pong_key
from observability.tracing import get_tracer


class EngineNotResponding(Exception):
    """No pong arrived in time (engine down, stalled, or Redis consumer not running)."""


async def ping_engine(redis: Redis, *, timeout_seconds: float = 2.0) -> float:
    """Send a ping and return the round-trip time in milliseconds.

    The round trip is also recorded on the span as ``diagnostics.rtt_ms``.
    """
    # The span is a context manager: it starts here and ends when the `with` block exits, even
    # on an exception (like `using var activity = source.StartActivity(...)` in C#).
    with get_tracer("api.diagnostics").start_as_current_span(
        "diagnostics.ping", kind=trace.SpanKind.PRODUCER
    ) as span:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)  # fills in "traceparent" from the current span

        ping = DiagnosticPing(
            ping_id=uuid.uuid4().hex,
            sent_at=datetime.now(UTC),
            traceparent=carrier.get("traceparent"),
        )
        span.set_attribute("diagnostics.ping_id", ping.ping_id)

        started = time.perf_counter()  # monotonic clock: immune to wall-clock adjustments
        await cast("Any", redis.xadd(PING_STREAM, {"data": ping.model_dump_json()}, maxlen=100))

        key = pong_key(ping.ping_id)
        replies = await cast(
            "Any", redis.xread({key: "0-0"}, count=1, block=int(timeout_seconds * 1000))
        )
        if not replies:
            span.set_attribute("diagnostics.timed_out", True)
            raise EngineNotResponding(f"no pong within {timeout_seconds}s")

        rtt_ms = (time.perf_counter() - started) * 1000
        DiagnosticPong.model_validate_json(replies[0][1][0][1]["data"])  # validates the reply
        await cast("Any", redis.delete(key))
        span.set_attribute("diagnostics.rtt_ms", rtt_ms)
        return rtt_ms
