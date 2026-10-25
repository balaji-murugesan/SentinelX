"""JSON log output (observability spec; tasks 3.1 and 3.2)."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider

from observability.logging_setup import configure_logging


def read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_log_line_is_json_with_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    structlog.get_logger().info("hello world", answer=42)

    (record,) = read_lines(capsys)
    assert record["message"] == "hello world"
    assert record["level"] == "info"
    assert record["service"] == "engine"
    assert record["answer"] == 42
    assert "event" not in record
    parsed = datetime.fromisoformat(record["timestamp"])
    assert parsed.utcoffset() == UTC.utcoffset(None)  # timestamps are UTC


def test_one_line_per_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    log = structlog.get_logger()
    log.info("one")
    log.warning("two")
    assert [r["message"] for r in read_lines(capsys)] == ["one", "two"]


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine", "WARNING")
    log = structlog.get_logger()
    log.info("dropped")
    log.warning("kept")
    assert [r["message"] for r in read_lines(capsys)] == ["kept"]


def test_bound_account_and_order_ids_are_included(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    structlog.contextvars.bind_contextvars(account_id="1100123456", order_id="ORD-1")
    structlog.get_logger().info("placing")

    (record,) = read_lines(capsys)
    assert record["order_id"] == "ORD-1"
    assert record["account_id"] == "****3456"  # masked, see test_masking.py


def test_context_is_not_carried_over_after_clear(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    structlog.contextvars.bind_contextvars(order_id="ORD-1")
    structlog.contextvars.clear_contextvars()
    structlog.get_logger().info("later")
    assert "order_id" not in read_lines(capsys)[0]


# ---- trace correlation ---------------------------------------------------------------


def test_trace_id_added_inside_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        structlog.get_logger().info("inside")
        context = span.get_span_context()
    structlog.get_logger().info("outside")

    inside, outside = read_lines(capsys)
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in outside


# ---- masking in the pipeline ---------------------------------------------------------


def test_secret_fields_are_masked_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    structlog.get_logger().info(
        "calling broker",
        access_token="tok-SECRET-1",
        headers={"Authorization": "Bearer tok-SECRET-2"},
        client_id="1100123456",
    )
    out = capsys.readouterr().out
    assert "tok-SECRET" not in out
    assert "1100123456" not in out
    record = json.loads(out)
    assert record["access_token"] == "***"
    assert record["client_id"] == "****3456"


def test_secret_inside_message_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    structlog.get_logger().error("cannot connect to redis://u:hunter2@redis:6379")
    assert "hunter2" not in capsys.readouterr().out


def test_secret_inside_exception_text_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("engine")
    try:
        raise RuntimeError("login failed for password=hunter2")
    except RuntimeError:
        structlog.get_logger().exception("boom")

    out = capsys.readouterr().out
    assert "hunter2" not in out
    record = json.loads(out)
    assert "RuntimeError" in record["exception"]  # the traceback is still there
