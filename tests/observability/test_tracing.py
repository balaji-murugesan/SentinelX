"""Tracing setup (observability spec; tasks 3.3 and 3.4)."""

import json
import socket
import time
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs

from observability.logging_setup import configure_logging
from observability.tracing import (
    SafeSpanExporter,
    configure_tracing,
    get_tracer,
    shutdown_tracing,
)


@pytest.fixture
def memory() -> Any:
    """In-memory exporter wired into a fresh provider; exports immediately (no batching)."""
    exporter = InMemorySpanExporter()
    configure_tracing("engine", exporter=exporter, batch=False)
    yield exporter
    shutdown_tracing()


def test_spans_are_exported_with_service_name(memory: InMemorySpanExporter) -> None:
    with get_tracer("t").start_as_current_span("work"):
        pass
    (span,) = memory.get_finished_spans()
    assert span.name == "work"
    assert span.resource.attributes["service.name"] == "engine"


def test_trace_id_in_logs_matches_the_span(
    memory: InMemorySpanExporter, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("engine")
    with get_tracer("t").start_as_current_span("work") as span:
        structlog.get_logger().info("inside")
        expected = format(span.get_span_context().trace_id, "032x")
    assert json.loads(capsys.readouterr().out)["trace_id"] == expected


# ---- spans never carry secrets -------------------------------------------------------


def test_span_attributes_are_masked(memory: InMemorySpanExporter) -> None:
    with get_tracer("t").start_as_current_span("call-broker") as span:
        span.set_attribute("access_token", "tok-SECRET-1")
        span.set_attribute("client_id", "1100123456")
        span.set_attribute("http.url", "redis://u:hunter2@redis:6379")
        span.set_attribute("order_id", "ORD-1")
        span.set_attribute("qty", 75)
        span.add_event("retry", {"password": "hunter2", "attempt": 2})

    (span_out,) = memory.get_finished_spans()
    attrs = dict(span_out.attributes or {})
    assert attrs["access_token"] == "***"
    assert attrs["client_id"] == "****3456"
    assert "hunter2" not in str(attrs["http.url"])
    assert attrs["order_id"] == "ORD-1"
    assert attrs["qty"] == 75
    (event,) = span_out.events
    assert dict(event.attributes or {}) == {"password": "***", "attempt": 2}
    everything = repr(span_out.attributes) + repr(span_out.events)
    assert "tok-SECRET" not in everything
    assert "1100123456" not in everything
    assert "hunter2" not in everything


def test_masking_keeps_trace_identity(memory: InMemorySpanExporter) -> None:
    tracer = get_tracer("t")
    with tracer.start_as_current_span("parent") as parent:
        with tracer.start_as_current_span("child"):
            pass
    child, parent_out = memory.get_finished_spans()
    assert child.context.trace_id == parent.get_span_context().trace_id
    assert child.parent is not None
    assert child.parent.span_id == parent_out.context.span_id


# ---- exporter safety (task 3.4) ------------------------------------------------------


class ExplodingExporter(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        raise ConnectionError("collector down")


class FailingExporter(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        return SpanExportResult.FAILURE


@pytest.mark.parametrize("inner", [ExplodingExporter(), FailingExporter()])
def test_failed_export_warns_and_does_not_raise(inner: SpanExporter) -> None:
    provider = configure_tracing("engine", exporter=inner, batch=False)
    with capture_logs() as logs:
        with get_tracer("t").start_as_current_span("work"):
            pass  # the span ends here and is exported synchronously
    shutdown_tracing()
    assert provider is not None
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert len(warnings) == 1
    assert "export failed" in warnings[0]["event"]


def test_warnings_are_rate_limited() -> None:
    safe = SafeSpanExporter(FailingExporter(), warn_every_seconds=3600)
    with capture_logs() as logs:
        for _ in range(5):
            safe.export([])
    assert len([log for log in logs if log["log_level"] == "warning"]) == 1


def closed_port() -> int:
    """A localhost port with nothing listening."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_unreachable_collector_does_not_block_and_warns() -> None:
    endpoint = f"http://127.0.0.1:{closed_port()}"

    started = time.monotonic()
    configure_tracing("engine", endpoint, export_timeout_seconds=1)
    with capture_logs() as logs:
        with get_tracer("t").start_as_current_span("work"):
            pass
        elapsed_in_app = time.monotonic() - started
        # The export itself fails on the background thread; wait for it, then check the log.
        from observability.tracing import get_provider

        provider = get_provider()
        assert provider is not None
        provider.force_flush(timeout_millis=10_000)
    shutdown_tracing()

    assert elapsed_in_app < 1.0, "creating spans must not wait for the collector"
    assert any(log["log_level"] == "warning" and "export failed" in log["event"] for log in logs), (
        logs
    )


def test_no_endpoint_means_no_export_but_spans_still_exist() -> None:
    configure_tracing("engine", "")
    with get_tracer("t").start_as_current_span("work") as span:
        assert span.get_span_context().is_valid
    shutdown_tracing()


def test_get_tracer_before_configuration_is_a_noop() -> None:
    shutdown_tracing()
    with get_tracer("t").start_as_current_span("work") as span:
        assert not span.get_span_context().is_valid
