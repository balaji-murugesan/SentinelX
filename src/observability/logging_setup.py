"""Structured JSON logging for every service.

structlog builds each log record as a dict and passes it through a list of *processors*
(plain functions), then renders it. That is similar to ASP.NET Core's logging scopes plus
a formatter, except each step is an explicit, testable function.

Output: one JSON object per line on stdout, with ``timestamp`` (UTC ISO-8601), ``level``,
``service``, ``message``, plus ``trace_id``/``span_id`` when a trace span is active, plus
anything bound with ``structlog.contextvars.bind_contextvars(account_id=..., order_id=...)``.
"""

import logging

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor, WrappedLogger

from observability.masking import mask_value


def add_service(service: str) -> Processor:
    """Build a processor that stamps every record with the service name."""

    def processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        return event_dict

    return processor


def add_trace_context(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Add the active span's ``trace_id`` / ``span_id`` (hex), if there is one."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def mask_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask secrets and account IDs in the whole record, including nested values and the message.

    Runs after every other processor (and after exceptions are rendered to text) so nothing
    downstream can reintroduce a raw value.
    """
    return {key: mask_value(value, key) for key, value in event_dict.items()}


def build_processors(service: str) -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,  # values bound with bind_contextvars
        structlog.processors.add_log_level,  # -> "level"
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # -> "timestamp", always UTC
        add_service(service),
        add_trace_context,
        structlog.processors.format_exc_info,  # exception -> text, so masking can see it
        structlog.processors.EventRenamer("message"),  # structlog's "event" -> "message"
        mask_sensitive,
        structlog.processors.JSONRenderer(),
    ]


def configure_logging(service: str, level: str = "INFO") -> None:
    """Install the JSON pipeline for this process. Call once at startup."""
    structlog.configure(
        processors=build_processors(service),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),  # writes to stdout
        cache_logger_on_first_use=False,
    )
