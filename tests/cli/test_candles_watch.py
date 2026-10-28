"""``cli candles watch`` and its live-vs-history comparison (add-candles task 6.2).

The comparison is the spec's "Live candles agree with history" check that the owner runs in
smoke test 7.2. No network: simulated feed and history only.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cli.__main__ import candles_watch, compare_candles, main
from contracts.market import InstrumentKey
from contracts.settings import load_settings
from engine.config import EngineSettings
from engine.market_data.candles import Candle
from engine.market_data.clock import IST

NIFTY = InstrumentKey.parse("IDX_I:13")


def at(minute: int) -> datetime:
    return datetime(2026, 10, 28, 10, minute, tzinfo=IST).astimezone(UTC)


def candle(minute: int, o: str, h: str, low: str, c: str, v: int = 100) -> Candle:
    return Candle(NIFTY, 3, at(minute), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v)


def test_help_lists_candles_watch(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["candles", "watch", "--help"])
    assert info.value.code == 0
    assert "--tf" in capsys.readouterr().out


def test_comparison_passes_when_live_is_inside_history() -> None:
    history = [candle(0, "100", "105", "95", "101", 900), candle(3, "101", "103", "99", "102")]
    live = [candle(0, "100.5", "104", "96", "101", 880), candle(3, "101", "103", "99", "102")]
    lines = compare_candles(live, history)
    assert lines[0].startswith("-- comparison with history: PASS, 2 complete candles")
    assert "open 100.5 vs 100" in lines[1] and "volume 880 vs 900" in lines[1]
    assert lines[2].endswith("identical")


def test_comparison_flags_live_high_above_history_high() -> None:
    lines = compare_candles(
        [candle(0, "100", "106", "95", "101")], [candle(0, "100", "105", "95", "101")]
    )
    assert "FAIL (1)" in lines[0]
    assert lines[1].startswith("FAIL") and "live high 106 > history 105" in lines[1]


def test_comparison_flags_low_below_history_and_missing_candle() -> None:
    lines = compare_candles(
        [candle(0, "100", "105", "94", "101"), candle(3, "1", "1", "1", "1")],
        [candle(0, "100", "105", "95", "101")],
    )
    assert "FAIL (2)" in lines[0]
    assert "live low 94 < history 95" in lines[1]
    assert "no history candle" in lines[2]


async def test_watch_prints_warm_up_in_simulated_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in {
        "REDIS_URL": "redis://redis:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://u:p@postgres:5432/x",
        "INSTRUMENT_CACHE_DIR": str(tmp_path / "instruments"),
        "DHAN_INSTRUMENTS_URL": "https://files.test/instruments.csv",
        "FEED_SOURCE": "simulated",
        "SIM_INTERVAL_S": "0.01",
        "SIM_SEED": "3",
    }.items():
        monkeypatch.setenv(name, value)
    settings = load_settings(EngineSettings)
    stop = asyncio.Event()
    lines: list[str] = []

    def out(line: str) -> None:
        lines.append(line)
        if line.startswith("-- live"):
            stop.set()

    async def no_instruments(_today: object) -> None:
        return None

    code = await asyncio.wait_for(
        candles_watch(settings, NIFTY, 3, stop, out=out, load=no_instruments), timeout=10
    )
    assert code == 0
    assert lines[0] == "-- warm-up: 200 completed 3-minute candles; last 5:"
    assert all(" 3m " in line for line in lines[1:6])
    assert lines[6].startswith("-- live")
    assert lines[-1].startswith("-- comparison")  # no complete live candle yet: says so


def test_entry_point_is_the_last_statement() -> None:
    # `python -m cli` runs main() when it reaches this block; any function defined below it
    # would not exist yet (tests import the module, so they would not notice).
    import ast

    import cli.__main__ as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    last = tree.body[-1]
    assert isinstance(last, ast.If) and "__main__" in ast.unparse(last.test)
