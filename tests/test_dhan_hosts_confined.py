"""Dhan hostnames appear only under src/engine/broker/ (task 6.4, CLAUDE.md import boundaries).

import-linter guards imports; this guards the other way to reach Dhan: a hard-coded URL.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = Path("engine") / "broker"
NEEDLE = "dhan.co"
SKIP_DIRS = {"node_modules", "__pycache__", "dist"}


def offenders(src: Path) -> list[str]:
    """Text files under ``src`` (outside engine/broker) that mention a Dhan host."""
    found = []
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        if not path.is_file() or SKIP_DIRS & set(relative.parts):
            continue
        if relative.is_relative_to(ALLOWED):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary files
        if NEEDLE in text.lower():
            found.append(relative.as_posix())
    return found


def test_dhan_hosts_only_in_broker_package() -> None:
    assert offenders(SRC) == []


def test_checker_catches_an_offending_file(tmp_path: Path) -> None:
    (tmp_path / "engine" / "broker").mkdir(parents=True)
    (tmp_path / "engine" / "broker" / "ok.py").write_text('URL = "https://api.dhan.co"')
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "bad.py").write_text('URL = "https://api.DHAN.co/v2"')
    assert offenders(tmp_path) == ["api/bad.py"]
