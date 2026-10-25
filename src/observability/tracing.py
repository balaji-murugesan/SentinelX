"""OpenTelemetry tracing setup shared by engine and api.

Concepts (C# analogy: System.Diagnostics.Activity / ActivitySource):
  * a *span* is one timed operation (``Activity``); spans nest into a *trace* that can cross
    processes when the trace context (W3C ``traceparent``) is passed along;
  * a *TracerProvider* creates spans; a *span processor* hands finished spans to an
    *exporter*, here OTLP over gRPC to the OpenTelemetry Collector.

Design points:
  * The provider is kept in this module, not installed as OpenTelemetry's process-global one.
    The global can be set only once per process, which makes tests awkward; here
    ``configure_tracing()`` can be called again.
  * Exporting happens on a background thread (BatchSpanProcessor), so an unreachable
    collector never blocks request handling or the engine's event loop; it only costs a
    rate-limited warning log. Spans are masked before they leave the process.
"""

import time
from collections.abc import Sequence

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from observability.masking import mask_value

_provider: TracerProvider | None = None


def _masked(span: ReadableSpan) -> ReadableSpan:
    """Copy of ``span`` whose attributes (and event attributes) have been masked.

    A finished span is read-only, so a new ReadableSpan is built from the same fields.
    """
    events = [
        Event(
            e.name,
            mask_value(dict(e.attributes or {})),
            e.timestamp,
        )
        for e in span.events
    ]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=mask_value(dict(span.attributes or {})),
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSpanExporter(SpanExporter):
    """Wraps a real exporter: masks secrets, never raises, warns (rate-limited) on failure."""

    def __init__(self, inner: SpanExporter, *, warn_every_seconds: float = 60.0) -> None:
        self._inner = inner
        self._warn_every = warn_every_seconds
        self._last_warning: float | None = None
        self._log = structlog.get_logger("tracing")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export([_masked(s) for s in spans])
        except Exception as exc:  # exporting must never take the service down
            self._warn(f"{type(exc).__name__}")
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._warn("exporter reported failure")
        return result

    def _warn(self, reason: str) -> None:
        now = time.monotonic()
        if self._last_warning is None or now - self._last_warning >= self._warn_every:
            self._last_warning = now
            self._log.warning("trace export failed; telemetry is being dropped", reason=reason)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def configure_tracing(
    service: str,
    endpoint: str = "",
    *,
    exporter: SpanExporter | None = None,
    batch: bool = True,
    export_timeout_seconds: int = 5,
) -> TracerProvider:
    """Create this process's TracerProvider.

    ``endpoint`` is the collector's OTLP/gRPC address (for example ``http://otel-collector:4317``);
    empty means "do not export", spans are still created so logs carry trace IDs.
    ``exporter`` replaces the OTLP exporter (tests pass an in-memory one);
    ``batch=False`` exports each span immediately (tests only).
    """
    global _provider
    shutdown_tracing()

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is None and endpoint:
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=endpoint.startswith("http://"),
            timeout=export_timeout_seconds,
        )
    if exporter is not None:
        safe = SafeSpanExporter(exporter)
        provider.add_span_processor(
            BatchSpanProcessor(safe) if batch else SimpleSpanProcessor(safe)
        )
    _provider = provider
    return provider


def get_tracer(name: str) -> trace.Tracer:
    """Tracer from this module's provider (a no-op tracer before configure_tracing())."""
    provider: trace.TracerProvider = (
        _provider if _provider is not None else trace.NoOpTracerProvider()
    )
    return provider.get_tracer(name)


def get_provider() -> TracerProvider | None:
    return _provider


def shutdown_tracing() -> None:
    """Flush and stop the current provider, if any."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
