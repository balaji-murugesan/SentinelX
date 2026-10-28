"""`dhan history` helper for the owner's one-off recording (add-candles task 4.1)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from cli.__main__ import FIXTURE_ROWS, summarize_history, trim_history
from engine.market_data.clock import IST

FIXTURE = Path(__file__).parents[1] / "fixtures" / "dhan" / "history_option.json"
RECORDED = datetime(2026, 10, 28, 10, 54, 2, tzinfo=IST).astimezone(UTC)


def test_summary_reads_timestamps_both_ways() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    text = "\n".join(summarize_history(payload, RECORDED))
    assert "rows: 40" in text
    assert "if UTC epoch 2026-10-26 09:15:00 IST" in text
    assert "if IST epoch 2026-10-26 03:45:00 IST" in text
    assert "last row is 2 s before now" in text
    assert "zero-volume rows: 0" in text


def test_summary_of_unexpected_shape() -> None:
    assert "unexpected shape" in summarize_history({"data": []}, RECORDED)[-1]


def test_trim_keeps_both_ends_of_each_series() -> None:
    series = list(range(100))
    payload = {name: series for name in ("open", "high", "low", "close", "volume", "timestamp")}
    trimmed = trim_history({**payload, "extra": "dropped"})
    assert "extra" not in trimmed
    assert trimmed["open"] == series[:FIXTURE_ROWS] + series[-FIXTURE_ROWS:]
