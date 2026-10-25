"""api -> Redis -> engine -> Redis -> api round trip against a REAL Redis (task 3.5).

Needs a Redis; point TEST_REDIS_URL at one (CI provides a service container, locally:
``docker run --rm -p 127.0.0.1:6380:6379 redis:7-alpine`` and
``TEST_REDIS_URL=redis://127.0.0.1:6380/15``). Without it these tests are skipped, loudly.

Both "services" run in this one process, but the only thing connecting their traces is the
``traceparent`` string carried inside the Redis message, so the cross-service propagation is
genuinely exercised.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis

from api.app import create_app
from api.config import ApiSettings
from api.diagnostics import EngineNotResponding, ping_engine
from contracts.diagnostics import PING_STREAM, DiagnosticPing, pong_key
from contracts.settings import load_settings
from engine.diagnostics import answer_pings
from observability.tracing import configure_tracing

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    # Skip locally when there is no Redis; never in CI (GitHub sets CI=true), where a missing
    # TEST_REDIS_URL must fail loudly instead of silently not testing.
    pytest.mark.skipif(
        not TEST_REDIS_URL and os.environ.get("CI") != "true",
        reason="TEST_REDIS_URL not set: no real Redis to test",
    ),
]


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    # flushdb() below wipes the whole database, so insist on a clearly-marked test database.
    assert TEST_REDIS_URL.rstrip("/").endswith("/15"), "TEST_REDIS_URL must use database 15"
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_tracing("roundtrip-test", exporter=exporter, batch=False)
    return exporter


async def finished_spans(spans: InMemorySpanExporter, *names: str) -> dict[str, ReadableSpan]:
    """Finished spans by name, waiting briefly for the named ones.

    The api receives the pong slightly before the engine's span closes, so an immediate
    lookup would race.
    """
    for _ in range(50):
        found = {s.name: s for s in spans.get_finished_spans()}
        if all(name in found for name in names):
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(f"spans {names} never finished; saw {sorted(found)}")


@pytest.fixture
async def engine_task(redis: Redis) -> AsyncIterator[None]:
    """The engine's ping responder, running as a background task (like the engine's loop)."""
    stop = asyncio.Event()
    task = asyncio.create_task(answer_pings(redis, stop, block_ms=100))
    await asyncio.sleep(0.05)  # let it record the stream position before the first ping
    yield
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_round_trip_returns_rtt(redis: Redis, engine_task: None) -> None:
    rtt_ms = await ping_engine(redis, timeout_seconds=3)
    assert 0 < rtt_ms < 3000


async def test_one_trace_spans_api_and_engine(
    redis: Redis, spans: InMemorySpanExporter, engine_task: None
) -> None:
    rtt_ms = await ping_engine(redis, timeout_seconds=3)

    by_name = await finished_spans(spans, "diagnostics.ping", "diagnostics.answer_ping")
    api_span = by_name["diagnostics.ping"]
    engine_span = by_name["diagnostics.answer_ping"]

    # Same trace, and the engine span is a child of the api span: context crossed Redis.
    assert engine_span.context.trace_id == api_span.context.trace_id
    assert engine_span.parent is not None
    assert engine_span.parent.span_id == api_span.context.span_id
    # The engine ran in a separate asyncio task, with no ambient span of its own.
    assert api_span.attributes is not None
    assert api_span.attributes["diagnostics.rtt_ms"] == pytest.approx(rtt_ms)


async def test_probe_endpoint_returns_rtt_and_traces_the_request(
    redis: Redis, spans: InMemorySpanExporter, engine_task: None
) -> None:
    settings = load_settings_for_api()
    app = create_app(settings, redis_client=redis)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/diagnostics/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rtt_ms"] > 0

    await finished_spans(spans, "diagnostics.ping", "diagnostics.answer_ping")
    finished = spans.get_finished_spans()
    trace_ids = {s.context.trace_id for s in finished}
    names = {s.name for s in finished}
    assert {"diagnostics.ping", "diagnostics.answer_ping"} <= names
    assert any("POST" in s.name for s in finished)  # the FastAPI request span
    assert len(trace_ids) == 1, "request, api probe and engine answer share one trace"


async def test_no_engine_means_timeout_not_a_hang(
    redis: Redis, spans: InMemorySpanExporter
) -> None:
    with pytest.raises(EngineNotResponding):
        await ping_engine(redis, timeout_seconds=0.3)
    (api_span,) = spans.get_finished_spans()
    assert api_span.attributes is not None
    assert api_span.attributes["diagnostics.timed_out"] is True
    assert "diagnostics.rtt_ms" not in api_span.attributes


async def test_probe_endpoint_returns_504_without_engine(redis: Redis) -> None:
    app = create_app(load_settings_for_api(), redis_client=redis)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/diagnostics/ping")
    assert response.status_code == 504


async def test_pings_sent_while_engine_was_down_are_not_replayed(redis: Redis) -> None:
    """A stale ping from before the engine started must not be answered."""
    stale = DiagnosticPing(ping_id="stale", sent_at=datetime.now(UTC))
    await redis.xadd(PING_STREAM, {"data": stale.model_dump_json()})

    stop = asyncio.Event()
    task = asyncio.create_task(answer_pings(redis, stop, block_ms=100))
    await asyncio.sleep(0.05)
    try:
        assert await ping_engine(redis, timeout_seconds=3) > 0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert await redis.exists(pong_key("stale")) == 0


async def test_malformed_ping_does_not_kill_the_responder(redis: Redis, engine_task: None) -> None:
    await redis.xadd(PING_STREAM, {"data": "not even json"})
    await redis.xadd(PING_STREAM, {"other": "field"})
    assert await ping_engine(redis, timeout_seconds=3) > 0


def load_settings_for_api() -> ApiSettings:
    assert TEST_REDIS_URL, "TEST_REDIS_URL must be set (CI provides a Redis service)"
    os.environ["REDIS_URL"] = TEST_REDIS_URL
    try:
        return load_settings(ApiSettings)
    finally:
        del os.environ["REDIS_URL"]
